import sqlite3
import os
import logging
import asyncio
import threading
import time
from contextlib import contextmanager
from typing import Generator

import tempfile
from pathlib import Path

# database.py는 가장 먼저 로드되므로 services.logger 순환 방지용 fallback 사용
try:
    from services.logger import setup_logger
    logger = setup_logger(__name__)
except Exception:
    logger = logging.getLogger(__name__)

def get_db_path():
    """Get the database path in local app data."""
    app_data = os.getenv('LOCALAPPDATA')
    if not app_data:
        app_data = os.path.expanduser('~')
    
    db_dir = os.path.join(app_data, 'welfare-scraper')
    os.makedirs(db_dir, exist_ok=True)
    return os.path.join(db_dir, 'welfare.db')

DB_FILE = get_db_path()

class DatabaseMigrationError(RuntimeError):
    """Startup must stop when an existing database could not be preserved."""


def _backup_legacy_database(source_path, candidate_path, *, timeout=30.0):
    """Read committed WAL data through SQLite and produce a standalone snapshot."""
    deadline = time.monotonic() + timeout

    def progress(status, remaining, total):
        if time.monotonic() >= deadline:
            raise TimeoutError('Database migration backup timed out')

    # Never create, edit, or checkpoint the user's legacy database explicitly.
    source = sqlite3.connect(Path(source_path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        candidate = sqlite3.connect(candidate_path)
        try:
            source.backup(candidate, pages=256, progress=progress, sleep=0.05)
            candidate.execute('PRAGMA journal_mode=DELETE')
            check = candidate.execute('PRAGMA integrity_check').fetchone()
            if not check or check[0] != 'ok':
                raise sqlite3.DatabaseError('Database migration snapshot failed integrity check')
        finally:
            candidate.close()
    finally:
        source.close()
    with open(candidate_path, 'rb+') as persisted:
        os.fsync(persisted.fileno())


def _publish_migrated_database(candidate_path, target_path):
    """Publish atomically without replacing a database another process created."""
    if os.name == 'nt':
        os.rename(candidate_path, target_path)  # Windows refuses an existing target.
    else:
        os.link(candidate_path, target_path)  # POSIX rename would overwrite it.


def migrate_db():
    """Preserve legacy data before initialization; failure leaves no empty target."""
    if os.path.exists(DB_FILE):
        return

    # 1. Determine the installation directory robustly
    import sys
    if getattr(sys, 'frozen', False):
        # If wrapped by PyInstaller (or similar), use sys.executable dir
        base_dir = os.path.dirname(sys.executable)
    else:
        # Otherwise use the script directory
        base_dir = os.path.dirname(os.path.abspath(__file__))
        
    local_db = os.path.join(base_dir, 'welfare.db')
    
    if os.path.exists(local_db):
        candidate_path = None
        try:
            fd, candidate_path = tempfile.mkstemp(
                prefix='welfare_migration_', suffix='.db', dir=os.path.dirname(DB_FILE))
            os.close(fd)
            _backup_legacy_database(local_db, candidate_path)
            try:
                _publish_migrated_database(candidate_path, DB_FILE)
            except FileExistsError:
                # A concurrent startup completed first. Never overwrite its database.
                return
            logger.info('기존 데이터베이스를 안전하게 이전했습니다. 원본은 보존했습니다.')
            return
        except Exception as e:
            message = ('기존 데이터베이스를 옮기지 못해 시작을 중단했습니다. 원본은 보존했습니다. '
                       '다른 복지F5 실행을 종료하고 저장 공간과 폴더 권한을 확인한 뒤 다시 실행해주세요.')
            logger.error('%s (%s)', message, type(e).__name__)
            # Raised at module import, before init_db() can create an empty target.
            raise DatabaseMigrationError(message) from e
        finally:
            if candidate_path:
                for suffix in ('', '-wal', '-shm', '-journal'):
                    try:
                        os.remove(candidate_path + suffix)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        logger.warning('이전 중 생성한 임시파일을 정리하지 못했습니다. 원본은 보존했습니다.')

    # 2. Check VirtualStore (Common issue with Program Files writes)
    # %LOCALAPPDATA%\VirtualStore\Program Files\SearchMaster... (Path varies)
    # We can't easily guess the full path, but if the user ran as non-admin, 
    # it might be in VirtualStore. For now, we rely on local_db check.

migrate_db()

# ── 스레드 로컬 연결 재사용 ───────────────────────────────────────────────────
# 기존: 쿼리마다 sqlite3.connect()/close() → 대시보드 1회 로드에 수십 번 연결 생성.
# 변경: 스레드별 단일 연결을 재사용 (FastAPI 스레드풀 + asyncio.to_thread 안전).
#   - 쿼리는 스레드 전용 연결을 사용하고, 배타적 정리 시에만 다른 스레드에서 닫는다.
#   - busy_timeout: 동시 writer 발생 시 즉시 'database is locked' 대신 대기.
_local = threading.local()
# 열린 모든 연결 레지스트리: DB 복원 시 일괄 종료(파일 덮어쓰기 충돌 방지)용
_all_conns: "set[sqlite3.Connection]" = set()
_conns_lock = threading.Lock()
_access_condition = threading.Condition()
_active_accesses = 0
_exclusive_owner = None


class DatabaseMaintenanceError(RuntimeError):
    """복원/연결 정리 동안에는 새로운 DB 작업을 받지 않는다."""


def has_active_accesses():
    with _access_condition:
        return bool(_active_accesses or _exclusive_owner is not None)


@contextmanager
def _database_access():
    global _active_accesses
    with _access_condition:
        depth = getattr(_local, 'access_depth', 0)
        if not depth and _exclusive_owner not in (None, threading.get_ident()):
            raise DatabaseMaintenanceError('데이터베이스 복원 중입니다. 잠시 후 다시 시도해주세요.')
        _active_accesses += 1
        _local.access_depth = depth + 1
    try:
        yield
    finally:
        with _access_condition:
            _local.access_depth -= 1
            _active_accesses -= 1
            _access_condition.notify_all()


@contextmanager
def exclusive_database(timeout: float = 5.0):
    """새 접근을 거부하고 기존 작업을 배출한다. 대기는 반드시 worker에서 실행.

    소유 스레드는 백업/초기화 및 중첩 연결 정리가 가능하다.
    일반 get_connection 블록 안에서 배타적 모드로 승격하는 것은 금지한다.
    """
    global _exclusive_owner
    owner = threading.get_ident()
    with _access_condition:
        nested = _exclusive_owner == owner
        if not nested:
            if _exclusive_owner is not None or getattr(_local, 'access_depth', 0):
                raise DatabaseMaintenanceError('다른 DB 작업이 진행 중입니다. 잠시 후 다시 시도해주세요.')
            _exclusive_owner = owner
            deadline = time.monotonic() + timeout
            while _active_accesses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _exclusive_owner = None
                    _access_condition.notify_all()
                    raise DatabaseMaintenanceError('DB 작업이 끝나지 않아 복원을 중단했습니다. 잠시 후 다시 시도해주세요.')
                _access_condition.wait(remaining)
    try:
        yield
    finally:
        if not nested:
            with _access_condition:
                _exclusive_owner = None
                _access_condition.notify_all()

def _new_connection() -> sqlite3.Connection:
    # 연결의 쿼리 소유권은 thread-local로 유지. close는 접근 배출 뒤에만 허용.
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # 연결 단위로 1회만 적용되는 PRAGMA를 연결 생성 시점에 설정
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA synchronous=NORMAL;')
    conn.execute('PRAGMA busy_timeout=5000;')  # 동시 writer 최대 5초 대기
    conn.execute('PRAGMA foreign_keys=ON;')
    with _conns_lock:
        _all_conns.add(conn)
    return conn

class ConnectionCloseError(RuntimeError):
    """DB 파일 교체를 안전하게 진행할 수 없는 열린 연결이 남아 있음."""


def close_all_connections(strict: bool = False) -> None:
    try:
        with exclusive_database(timeout=0):
            _close_idle_connections(strict)
    except DatabaseMaintenanceError:
        if strict:
            raise
        logger.info('진행 중 DB 작업이 있어 연결 정리를 건너뜁니다.')


def _close_idle_connections(strict: bool) -> None:
    """모든 스레드의 열린 연결을 닫는다(DB 복원 직전 호출).

    닫힌 연결을 참조하는 스레드 로컬은 다음 _thread_connection() 호출 시
    살아있음 검사(SELECT 1)에서 실패하여 자동으로 재생성된다.
    """
    with _conns_lock:
        conns = list(_all_conns)
        _all_conns.clear()
    failed = []
    for c in conns:
        try:
            c.close()
        except Exception:
            failed.append(c)
    # 정리 실패 연결은 추적을 잃지 않고 복원을 중단한다.
    with _conns_lock:
        _all_conns.update(failed)
    _local.conn = None
    if strict and failed:
        raise ConnectionCloseError("사용 중인 DB 연결을 닫지 못해 복원을 중단했습니다. 앱을 재시작한 후 다시 시도해주세요.")

def _thread_connection() -> sqlite3.Connection:
    """현재 스레드 전용 연결을 반환(없으면 생성). 끊긴 연결은 자동 재생성."""
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        try:
            conn.execute('SELECT 1')  # 살아있는지 확인
            return conn
        except sqlite3.Error:
            with _conns_lock:
                _all_conns.discard(conn)
            try:
                conn.close()
            except Exception:
                pass
            _local.conn = None
    conn = _new_connection()
    _local.conn = conn
    return conn

@contextmanager
def get_connection() -> Generator[sqlite3.Connection, None, None]:
    """스레드 로컬 연결을 반환하는 컨텍스트 매니저.

    연결은 닫지 않고 스레드 내에서 재사용한다. 예외 발생 시 열린 트랜잭션을
    롤백하여 다음 사용자가 깨끗한 상태로 시작하도록 보장한다.
    """
    with _database_access():
        conn = _thread_connection()
        try:
            yield conn
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

async def run_db(func, *args, **kwargs):
    """동기 DB 함수를 스레드풀에서 실행하는 async 헬퍼.

    async 핸들러/코루틴에서 history_manager 등 동기 DB 호출을 감싸
    이벤트 루프 블로킹을 방지한다. 예) await database.run_db(history_manager.save_data, items)
    """
    def run():
        with _database_access():
            return func(*args, **kwargs)
    return await asyncio.to_thread(run)

def backup_to(dest_path: str) -> None:
    """SQLite 온라인 백업 API로 WAL 상태와 무관하게 일관된 백업 생성.

    shutil.copy2는 -wal 파일의 미병합 데이터를 놓칠 수 있어, 연결을 재사용하는
    구조에서는 백업이 최신 데이터를 누락할 위험이 있다. .backup()은 핫 백업을 보장.
    """
    with _database_access():
        _backup_to(dest_path)


def _backup_to(dest_path: str) -> None:
    src = sqlite3.connect(DB_FILE)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

def init_db() -> None:
    """Initialize the database schema."""
    with get_connection() as conn:
        # Enable WAL mode for better concurrency and performance
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('PRAGMA synchronous=NORMAL;')  # WAL + NORMAL: 성능 향상, 내구성 유지
        cursor = conn.cursor()
        
        # 1. Sites Table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            base_url TEXT,
            selectors JSON,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # 2. Welfare Items Table (Main Data)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS welfare_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_name TEXT,
            title TEXT NOT NULL,
            link TEXT NOT NULL UNIQUE,
            date TEXT,
            category TEXT,
            target TEXT,
            residence TEXT,
            start_age_months INTEGER,
            end_age_months INTEGER,
            age_detail TEXT,
            target_income TEXT,
            support_detail TEXT,
            eligibility_detail TEXT,
            contact TEXT,
            event_date TEXT,
            deadline TEXT,
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # 3. Keywords Table (Positive)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL UNIQUE
        )
        ''')

        # 4. Excluded Keywords Table (Negative)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS excluded_keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL UNIQUE
        )
        ''')
        
        # 5. Schema Migration (Add matched_keyword if missing)
        try:
            cursor.execute("ALTER TABLE welfare_items ADD COLUMN matched_keyword TEXT")
            logger.info("Added 'matched_keyword' column to welfare_items")
        except Exception:
            # Column likely exists
            pass
        
        # 6. Performance Indexes
        try:
            # Create indexes if they don't exist
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_scraped_at ON welfare_items (scraped_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_deadline ON welfare_items (deadline)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_category ON welfare_items (category)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_name ON welfare_items (site_name)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_target ON welfare_items (target)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_date ON welfare_items (date)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_scraped ON welfare_items (site_name, scraped_at)")
            logger.info("Checked/Created DB Indexes")
        except Exception as e:
            logger.warning(f"Index creation failed (ignorable): {e}")

        # 7. Change Detection Columns
        try:
            cursor.execute("ALTER TABLE welfare_items ADD COLUMN last_updated TIMESTAMP")
            cursor.execute("ALTER TABLE welfare_items ADD COLUMN change_log TEXT")
            logger.info("Added 'last_updated' and 'change_log' columns")
        except Exception:
            pass

        # 8. Notion-aligned AI data column, and Content Hash for deduplication
        for col_sql in [
            "ALTER TABLE welfare_items ADD COLUMN ai_data TEXT",
            "ALTER TABLE welfare_items ADD COLUMN target_characteristics TEXT",
            "ALTER TABLE welfare_items ADD COLUMN life_cycle TEXT",
            "ALTER TABLE welfare_items ADD COLUMN content_hash TEXT",
        ]:
            try:
                cursor.execute(col_sql)
            except Exception:
                pass

        # 8-a2. content_hash 인덱스 — (버그 2026-07-20) 예전엔 6번 블록(컬럼 추가보다 먼저)에
        # 있어서, 컬럼이 아직 없는 완전 신규 DB에서는 이 인덱스 하나만 조용히 생성 실패했다
        # (재실행 시엔 컬럼이 이미 있어 자연히 해소되지만, 첫 실행 로그에 불필요한 경고가 남았다).
        # 컬럼 추가 확정 이후로 옮겨 신규 DB에서도 바로 성공하게 한다.
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_content_hash ON welfare_items (content_hash)")
        except Exception as e:
            logger.warning(f"idx_content_hash 생성 실패 (ignorable): {e}")

        # 8-b. Incremental Scraping: last_scraped_at for sites table
        try:
            cursor.execute("ALTER TABLE sites ADD COLUMN last_scraped_at TIMESTAMP")
            logger.info("Added 'last_scraped_at' column to sites")
        except Exception:
            pass  # 이미 존재하는 경우 무시

        # 8-c. max_pages per site (how many pages to paginate)
        try:
            cursor.execute("ALTER TABLE sites ADD COLUMN max_pages INTEGER DEFAULT 3")
            logger.info("Added 'max_pages' column to sites")
        except Exception:
            pass  # 이미 존재하는 경우 무시

        columns = {row[1] for row in cursor.execute('PRAGMA table_info(sites)')}
        if 'registration_status' not in columns:
            cursor.execute("ALTER TABLE sites ADD COLUMN registration_status TEXT DEFAULT ''")

        # 8-d. AI call tracking: 실제 classify_post_async 호출 시각 (cache hit 구분용)
        try:
            cursor.execute("ALTER TABLE welfare_items ADD COLUMN ai_call_at TIMESTAMP")
            logger.info("Added 'ai_call_at' column to welfare_items")
        except Exception:
            pass  # 이미 존재하는 경우 무시

        # 8-e. 행사 종료일: event_date(시작일)와 구분되는 별도 종료일
        try:
            cursor.execute("ALTER TABLE welfare_items ADD COLUMN event_end_date TEXT")
            logger.info("Added 'event_end_date' column to welfare_items")
        except Exception:
            pass

        # 8-f. 아카이브 플래그: 만료된 항목을 완전 삭제 없이 숨김 처리
        try:
            cursor.execute("ALTER TABLE welfare_items ADD COLUMN is_archived INTEGER DEFAULT 0")
            logger.info("Added 'is_archived' column to welfare_items")
        except Exception:
            pass

        # 8-g. 페이지 최종수정일: 공공기관 사이트에서 추출한 '최종수정일' 값
        try:
            cursor.execute("ALTER TABLE welfare_items ADD COLUMN page_modified_date TEXT")
            logger.info("Added 'page_modified_date' column to welfare_items")
        except Exception:
            pass

        # 8-g2. 의미 검색/중복제거용 임베딩 벡터 (float32 BLOB)
        try:
            cursor.execute("ALTER TABLE welfare_items ADD COLUMN embedding BLOB")
            logger.info("Added 'embedding' column to welfare_items")
        except Exception:
            pass

        # 8-g2b. 검색 벡터 생성 기준. 서로 다른 입력 레시피의 벡터가 한 순위표에
        # 섞이지 않게 하며, 기준이 바뀌면 안전하게 재생성 대상으로 잡는다.
        try:
            cursor.execute("ALTER TABLE welfare_items ADD COLUMN embedding_recipe TEXT")
            logger.info("Added 'embedding_recipe' column to welfare_items")
        except Exception:
            pass

        # 8-g3. Notion 업로드 추적: 업로드된 페이지 ID (재업로드 방지)
        try:
            cursor.execute("ALTER TABLE welfare_items ADD COLUMN notion_page_id TEXT")
            logger.info("Added 'notion_page_id' column to welfare_items")
        except Exception:
            pass

        # 8-g4. Notion 후보 숨김: 사용자가 후보 목록에서 제외한 항목 (다시 안 뜸)
        try:
            cursor.execute("ALTER TABLE welfare_items ADD COLUMN notion_dismissed INTEGER DEFAULT 0")
            logger.info("Added 'notion_dismissed' column to welfare_items")
        except Exception:
            pass

        # 8-h. 변경 내역 스냅샷 테이블: 항목 업데이트 시 이전 상태 보존
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS welfare_item_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_link TEXT NOT NULL,
            snapshot_data TEXT NOT NULL,
            change_summary TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_snapshot_link ON welfare_item_snapshots (item_link)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_snapshot_created ON welfare_item_snapshots (created_at)")
        except Exception:
            pass

        # 8-i. 사이트 스크래핑 실행 기록 테이블: 성공/실패 이력 추적
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS scrape_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER,
            site_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'success',
            items_found INTEGER DEFAULT 0,
            items_saved INTEGER DEFAULT 0,
            error_message TEXT,
            duration_sec REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (site_id) REFERENCES sites(id)
        )
        ''')
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_scrape_runs_site ON scrape_runs (site_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_scrape_runs_created ON scrape_runs (created_at)")
        except Exception:
            pass

        # 8-j. 정책 watch 전용 테이블: 정해진 정책 페이지의 변경점 집중 감지(저비용, AI 불필요)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS policy_watch (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            scope TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            last_hash TEXT,
            last_text TEXT,
            prev_text TEXT,
            last_checked TIMESTAMP,
            last_changed_at TIMESTAMP,
            last_error TEXT,
            change_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        if 'last_error' not in {row[1] for row in cursor.execute('PRAGMA table_info(policy_watch)')}:
            cursor.execute('ALTER TABLE policy_watch ADD COLUMN last_error TEXT')
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_policy_watch_enabled ON policy_watch (enabled)")
        except Exception:
            pass

        # 8-k. 정책 최신화: 연결된 Notion 정책 보관함 페이지의 로컬 미러 (매칭·변경감지 중심축)
        #  - source_urls(관련홈페이지1·2·3)를 출처로 삼아 Stage1 해시비교 → Stage2 AI재추출·비교
        #  - notion_fields: Notion 현재 필드값 전체(JSON), pending_diff: Stage2 필드 diff(JSON)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS notion_policies (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            db_key          TEXT NOT NULL,
            page_id         TEXT NOT NULL UNIQUE,
            title           TEXT,
            notion_fields   TEXT,
            source_urls     TEXT,
            source_hashes   TEXT,
            source_texts    TEXT,
            title_embedding BLOB,
            change_status   TEXT DEFAULT 'none',
            pending_diff    TEXT,
            dead_links      TEXT,
            last_synced_at  TIMESTAMP,
            last_checked_at TIMESTAMP,
            last_changed_at TIMESTAMP,
            audited_at      TIMESTAMP,
            last_extracted_fields TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_np_dbkey ON notion_policies (db_key)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_np_status ON notion_policies (change_status)")
        except Exception:
            pass
        # 8-k2. 기존 설치(테이블 이미 생성됨)용 마이그레이션: 최초 감사(베이스라인 vs Notion 현재값) 추적 컬럼
        try:
            cursor.execute("ALTER TABLE notion_policies ADD COLUMN audited_at TIMESTAMP")
            logger.info("Added 'audited_at' column to notion_policies")
        except Exception:
            pass
        # 8-k3. 정기 점검용 AI 추출 스냅샷: 다음 변경 시 Notion과 비교하는 대신
        #       'AI(이전 원문) vs AI(새 원문)'으로 비교해 사람-AI 표현차 노이즈를 제거(P2)
        try:
            cursor.execute("ALTER TABLE notion_policies ADD COLUMN last_extracted_fields TEXT")
            logger.info("Added 'last_extracted_fields' column to notion_policies")
        except Exception:
            pass

        # 8-l. 정책 DB 스키마 캐시 — Notion 데이터소스의 select/multi_select 옵션 목록을
        #      1회 조회해 저장(2026-07-13, 하드코딩 제거). 생애주기·대상특성 등 "유효 태그"
        #      판정을 코드에 박은 고정값이 아니라 사용자의 실제 Notion 스키마에서 읽어와,
        #      다른 지자체가 다른 옵션 구성의 DB를 써도 코드 수정 없이 그대로 동작한다.
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS policy_db_schema (
            db_key      TEXT PRIMARY KEY,
            schema_json TEXT,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        # 8-l2. 정책 보관함별 연결 확인·동기화 상태. 설정에서 연결을 잠시 해제해도
        # 사용자가 직접 정리하기 전까지 마지막 정상 상태와 오류 원인을 보존한다.
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS policy_db_status (
            db_key              TEXT PRIMARY KEY,
            title               TEXT,
            last_verified_at    TIMESTAMP,
            verification_ok     INTEGER,
            last_sync_at        TIMESTAMP,
            last_sync_count     INTEGER DEFAULT 0,
            last_error          TEXT,
            updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        # 8-m. AI 분석 백로그 재처리 (2026-08-06): 하루 자체 상한(150건) 초과로 AI 분석 없이
        #      저장된 항목이, 링크가 이미 DB에 있다는 이유만으로 다음 수집에서 "이미 수집된
        #      항목"으로 걸러져 영구히 재분석되지 않던 문제(실측: 운영 DB 191건)를 고치기 위한
        #      백로그 재처리용 컬럼. ai_last_attempt_at은 실패(429 등)도 기록해 같은 항목이
        #      매 실행마다 큐 맨 앞을 계속 차지하는 편향을 막는다(ai_call_at은 성공 시에만
        #      찍혀 이 용도로 못 씀 — GPT Codex 리뷰로 발견).
        try:
            cursor.execute("ALTER TABLE welfare_items ADD COLUMN ai_skip_reason TEXT")
            logger.info("Added 'ai_skip_reason' column to welfare_items")
        except Exception:
            pass

        try:
            cursor.execute("ALTER TABLE welfare_items ADD COLUMN ai_last_attempt_at TIMESTAMP")
            logger.info("Added 'ai_last_attempt_at' column to welfare_items")
        except Exception:
            pass

        # 9. FTS5 Virtual Table for Fast Search
        try:
            cursor.execute('''
            CREATE VIRTUAL TABLE IF NOT EXISTS welfare_items_fts USING fts5(
                title, support_detail, target, category, matched_keyword, site_name,
                content='welfare_items',
                content_rowid='id'
            )
            ''')
            
            # Triggers to keep FTS index synced
            cursor.execute('''
            CREATE TRIGGER IF NOT EXISTS welfare_items_ai AFTER INSERT ON welfare_items BEGIN
                INSERT INTO welfare_items_fts(rowid, title, support_detail, target, category, matched_keyword, site_name)
                VALUES (new.id, new.title, new.support_detail, new.target, new.category, new.matched_keyword, new.site_name);
            END;
            ''')
            cursor.execute('''
            CREATE TRIGGER IF NOT EXISTS welfare_items_ad AFTER DELETE ON welfare_items BEGIN
                INSERT INTO welfare_items_fts(welfare_items_fts, rowid, title, support_detail, target, category, matched_keyword, site_name)
                VALUES('delete', old.id, old.title, old.support_detail, old.target, old.category, old.matched_keyword, old.site_name);
            END;
            ''')
            cursor.execute('''
            CREATE TRIGGER IF NOT EXISTS welfare_items_au AFTER UPDATE ON welfare_items BEGIN
                INSERT INTO welfare_items_fts(welfare_items_fts, rowid, title, support_detail, target, category, matched_keyword, site_name)
                VALUES('delete', old.id, old.title, old.support_detail, old.target, old.category, old.matched_keyword, old.site_name);
                INSERT INTO welfare_items_fts(rowid, title, support_detail, target, category, matched_keyword, site_name)
                VALUES (new.id, new.title, new.support_detail, new.target, new.category, new.matched_keyword, new.site_name);
            END;
            ''')
            
            # Rebuild FTS table if empty but items exist
            cursor.execute("SELECT COUNT(*) as c FROM welfare_items_fts")
            if cursor.fetchone()['c'] == 0:
                cursor.execute("SELECT COUNT(*) as c FROM welfare_items")
                if cursor.fetchone()['c'] > 0:
                    cursor.execute("INSERT INTO welfare_items_fts(welfare_items_fts) VALUES('rebuild')")
                    logger.info("Rebuilt FTS5 index from existing data")
                    
            logger.info("Initialized FTS5 search indexes")
        except Exception as e:
            logger.warning(f"FTS5 initialization failed (ignorable on old SQLite): {e}")

        # Additive schema only. Capturing user settings into a protected profile
        # is explicit and is not performed during startup or DB restore.
        from services.news_profile_store import ensure_schema as ensure_news_profile_schema
        ensure_news_profile_schema(conn)
        from services.news_analysis_store import ensure_schema as ensure_news_analysis_schema
        ensure_news_analysis_schema(conn)
        from services.news_result_import import ensure_schema as ensure_news_import_schema
        ensure_news_import_schema(conn)
        from services.news_official_results import ensure_schema as ensure_news_official_schema
        ensure_news_official_schema(conn)
        # Derive sources for saved custom conditions without altering historical runs.
        from services.news_auto_sources import upgrade_saved_sources
        upgrade_saved_sources(conn)
        conn.commit()

# Initialize on import (CREATE IF NOT EXISTS is idempotent)
init_db()

def execute_query(sql: str, params: tuple = (), fetch_one: bool = False, *, raise_on_error: bool = False):
    """SELECT 쿼리 실행 헬퍼. 결과를 dict 또는 dict 리스트로 반환.

    오래된 조회 화면은 일시적인 DB 오류 때 빈 결과로 계속 동작해야 하므로 기본 동작은
    유지한다. 저장 성공 여부가 중요한 API/작업은 ``raise_on_error=True``를 사용해 오류를
    호출자까지 전달해야 한다.
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            if fetch_one:
                row = cursor.fetchone()
                return dict(row) if row else None
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"DB query error: {e}")
        if raise_on_error:
            raise
        return None if fetch_one else []

def execute_write(sql: str, params: tuple = (), *, raise_on_error: bool = False):
    """INSERT/UPDATE/DELETE 실행 헬퍼. lastrowid 반환.

    ``raise_on_error=True``이면 저장 실패를 숨기지 않는다. 사용자에게 성공 응답을 보내는
    중요한 경로에서는 반드시 이 모드를 사용하거나 명시적 트랜잭션을 사용한다.
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            conn.commit()
            return cursor.lastrowid
    except Exception as e:
        logger.error(f"DB write error: {e}")
        if raise_on_error:
            raise
        return None

def auto_backup_db() -> bool:
    """Automatically creates a backup of the DB file. Keeps up to 5 latest backups."""
    backup_dir = os.path.join(os.path.dirname(DB_FILE), 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    
    import datetime
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"welfare_daily_backup_{timestamp}.db")
    
    try:
        backup_to(backup_path)  # 온라인 백업 API: WAL 미병합 데이터까지 포함
        logger.info(f"Auto-backup created: {backup_path}")
        
        # Cleanup old backups
        backups = [os.path.join(backup_dir, f) for f in os.listdir(backup_dir) if f.startswith("welfare_daily_backup_")]
        backups.sort(key=os.path.getmtime, reverse=True)
        
        if len(backups) > 5:
            for old_backup in backups[5:]:
                os.remove(old_backup)
                logger.info(f"Removed old backup: {old_backup}")
        return True
    except Exception as e:
        logger.error(f"Auto-backup failed: {e}")
        return False
