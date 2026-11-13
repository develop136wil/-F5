import json
import tempfile
import threading
import os
import sys
from typing import List, Dict, Any
import database

try:
    from dotenv import load_dotenv
except ImportError:
    # If python-dotenv not installed, provide a no-op fallback
    def load_dotenv(*args, **kwargs):
        pass

try:
    from services.logger import setup_logger
    logger = setup_logger(__name__)
except Exception:
    import logging
    logger = logging.getLogger(__name__)

SITES_FILE = 'sites.json'

def get_env_file_path() -> str:
    """Get path to .env file (checks current dir, then parent dir)."""
    # Current directory
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(env_path):
        return env_path
        
    # Executable directory (for PyInstaller)
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        exe_env = os.path.join(exe_dir, '.env')
        if os.path.exists(exe_env):
            return exe_env
            
    # Parent directory
    parent_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    if os.path.exists(parent_env):
        return parent_env
    return env_path  # Default to current dir

# ==================== SITE MANAGEMENT ====================
REQUIRED_SITE_FIELDS = ('name', 'url')

def validate_site(site: Dict[str, Any]):
    """사이트 설정 dict의 유효성을 검사. (ok: bool, reason: str) 튜플 반환.

    저장 전 잘못된 항목을 걸러내기 위한 순수 함수(부수효과 없음 → 단위 테스트 용이).
    """
    if not isinstance(site, dict):
        return False, "site는 dict여야 합니다"
    for field in REQUIRED_SITE_FIELDS:
        if not str(site.get(field, '') or '').strip():
            return False, f"필수 필드 누락: {field}"
    url = str(site['url']).strip()
    if not (url.startswith('http://') or url.startswith('https://')):
        return False, f"url은 http(s)로 시작해야 합니다: {url[:60]}"
    selectors = site.get('selectors')
    if selectors is not None and not isinstance(selectors, dict):
        return False, "selectors는 객체(dict)여야 합니다"
    max_pages = site.get('max_pages', 3)
    try:
        if int(max_pages) < 1:
            return False, "max_pages는 1 이상이어야 합니다"
    except (ValueError, TypeError):
        return False, "max_pages는 정수여야 합니다"
    return True, ""


def load_sites() -> List[Dict[str, Any]]:
    """Load sites configuration from DB."""
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sites ORDER BY id")
            rows = cursor.fetchall()
            
            sites = []
            for row in rows:
                site = dict(row)
                if site.get('selectors'):
                    try:
                        site['selectors'] = json.loads(site['selectors'])
                    except json.JSONDecodeError:
                        site['selectors'] = {}
                sites.append(site)
                
            return sites
    except Exception as e:
        logger.error(f"DB Load Sites Error: {e}")
        raise

def save_sites(sites: List[Dict[str, Any]]) -> None:
    """
    Save sites to DB.
    """
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()

            skipped = 0
            for site in sites:
                ok, reason = validate_site(site)
                if not ok:
                    raise ValueError(reason)
                try:
                    selectors_json = json.dumps(site.get('selectors', {}), ensure_ascii=False)
                    cursor.execute('''
                    INSERT INTO sites (name, url, base_url, selectors, max_pages, registration_status)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        name=excluded.name,
                        base_url=excluded.base_url,
                        selectors=excluded.selectors,
                        max_pages=excluded.max_pages,
                        registration_status=COALESCE(excluded.registration_status, sites.registration_status)
                    ''', (site['name'], site['url'], site.get('base_url', ''), selectors_json, site.get('max_pages', 3), site.get('registration_status')))
                except Exception as e:
                    logger.warning(f"Error saving site {site.get('name')}: {e}")
                    raise

            from services.news_default_condition import synchronize_sites
            synchronize_sites(conn)
            conn.commit()
            if skipped:
                logger.info(f"사이트 저장 완료 — {skipped}개 유효성 검사 실패로 건너뜀")

    except Exception as e:
        logger.error(f"DB Save Sites Error: {e}")
        raise

def delete_site(site_id: int) -> bool:
    """Delete a site by its ID.

    scrape_runs.site_id가 sites.id를 FK로 참조(CASCADE 없음)하므로, foreign_keys=ON
    상태에서 site만 지우면 FK 제약 위반이 난다. 자식 레코드(scrape_runs)를 먼저 삭제한다.
    """
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM scrape_runs WHERE site_id = ?", (site_id,))
            cursor.execute("DELETE FROM sites WHERE id = ?", (site_id,))
            deleted = cursor.rowcount > 0
            from services.news_default_condition import synchronize_sites
            synchronize_sites(conn)
            conn.commit()
            return deleted
    except Exception as e:
        logger.error(f"DB Delete Site Error: {e}")
        raise

# ==================== CONFIG MANAGEMENT (Replacing ENV) ====================
def get_app_data_dir() -> str:
    """Get the application data directory in user home."""
    app_data = os.getenv('LOCALAPPDATA')
    if not app_data:
        app_data = os.path.expanduser('~')
    
    config_dir = os.path.join(app_data, 'welfare-scraper')
    os.makedirs(config_dir, exist_ok=True)
    return config_dir

def safe_config_int(key: str, default: int, minimum: int, maximum: int) -> int:
    """기존 설정 파일에 손상된 값이 있어도 조회/작업 전체를 중단하지 않는다."""
    try:
        value = int(get_env_value(key))
        return value if minimum <= value <= maximum else default
    except (ValueError, TypeError):
        return default


def get_config_path() -> str:
    """Get path to config.json."""
    return os.path.join(get_app_data_dir(), 'config.json')

def _read_config_object(path):
    with open(path, 'r', encoding='utf-8') as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError('설정 파일은 JSON 객체여야 합니다.')
    return data


def load_config_file(*, strict=False) -> Dict[str, str]:
    """Load configuration from JSON file."""
    config_path = get_config_path()
    if not os.path.exists(config_path) and not os.path.exists(config_path + '.bak'):
        return {}
    
    try:
        return _read_config_object(config_path)
    except Exception as e:
        logger.error(f"Failed to load config file: {e}")
        try:
            recovered = _read_config_object(config_path + '.bak')
            logger.warning('설정 파일을 읽지 못해 마지막 정상 백업을 사용합니다.')
            return recovered
        except (OSError, ValueError, TypeError):
            if strict:
                raise ValueError('설정과 백업을 읽을 수 없습니다. 기존 파일 보호를 위해 저장하지 않았습니다.') from e
            return {}

_CONFIG_LOCK = threading.RLock()


def save_config_file(config: Dict[str, str]) -> None:
    """설정을 원자적으로 저장하고 직전 정상본을 ``config.json.bak``에 보존한다.

    실패를 삼키지 않고 호출자에게 전달하므로 API가 거짓 성공을 응답하지 않는다.
    """
    config_path = get_config_path()
    config_dir = os.path.dirname(config_path)
    backup_path = config_path + '.bak'
    tmp_path = None
    backup_tmp = None
    try:
        with _CONFIG_LOCK:
            if not isinstance(config, dict):
                raise ValueError('설정은 JSON 객체여야 합니다.')
            fd, tmp_path = tempfile.mkstemp(prefix='config_', suffix='.json.tmp', dir=config_dir)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())

            if os.path.exists(config_path):
                try:
                    _read_config_object(config_path)
                    backup_source = config_path
                except (OSError, ValueError, TypeError):
                    # 손상 파일은 보존하고, 마지막으로 검증된 백업은 덮지 않는다.
                    _read_config_object(backup_path)
                    import shutil
                    quarantine_fd, quarantine_path = tempfile.mkstemp(
                        prefix='config_corrupt_', suffix='.json', dir=config_dir)
                    os.close(quarantine_fd)
                    shutil.copy2(config_path, quarantine_path)
                    backup_source = backup_path
                backup_fd, backup_tmp = tempfile.mkstemp(
                    prefix='config_backup_', suffix='.tmp', dir=config_dir)
                os.close(backup_fd)
                import shutil
                shutil.copy2(backup_source, backup_tmp)
                os.replace(backup_tmp, backup_path)
                backup_tmp = None

            os.replace(tmp_path, config_path)
            tmp_path = None
    except Exception as e:
        logger.error(f"Failed to save config file: {e}")
        raise
    finally:
        for path in (tmp_path, backup_tmp):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

def load_environment() -> None:
    """
    Load environment variables from .env (dev) AND config.json (prod).
    config.json takes precedence for user settings.
    """
    # 1. Load .env (Development defaults)
    env_path = get_env_file_path()
    if os.path.exists(env_path):
        load_dotenv(env_path)
    
    # 2. Load config.json (User overrides)
    config = load_config_file()
    for key, value in config.items():
        if value:
            # (2026-08-05) 환경변수는 항상 문자열이라 dict/list 값을 str()로 바로 박으면
            # {"a": 1} → "{'a': 1}"(파이썬 repr, 작은따옴표) 같은 깨진 유사-JSON이 된다.
            # NOTION_POLICY_PROP_MAP처럼 사용자가 config.json에 중첩 객체를 직접 써 넣는
            # 경우를 대비해, dict/list는 json.dumps로 유효한 JSON 문자열로 보존한다.
            # (API로 저장되는 경우처럼 이미 문자열이면 그대로 둔다 — 이중 인코딩 방지)
            os.environ[key] = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)

def get_env_value(key: str) -> str:
    """Get config value (checks environ which includes config.json)."""
    # Ensure loaded
    if key not in os.environ:
        load_environment()
    return os.getenv(key, '')

def set_env_value(key: str, value: str) -> None:
    """Save config value to user-writable config.json."""
    set_env_values({key: value})


def set_env_values(values: Dict[str, str]) -> None:
    """여러 설정을 한 파일 교체로 저장하고 성공한 뒤에만 프로세스 환경을 갱신한다."""
    with _CONFIG_LOCK:
        config = load_config_file(strict=True)
        config.update(values)
        save_config_file(config)
        for key, value in values.items():
            os.environ[key] = value

# ==================== JSON HELPER ====================
def parse_json_safe(value, default=None):
    """JSON 문자열을 안전하게 파싱. 실패 시 default 반환."""
    if not value:
        return default if default is not None else {}
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else {}

# ==================== CONFIG VALIDATION ====================
def validate_config() -> None:
    """환경변수/설정값 유효성 검사. 문제 있으면 경고 로깅."""
    key = os.getenv('GOOGLE_API_KEY', '')
    if key:
        logger.info("GOOGLE_API_KEY가 설정되어 있습니다.")
    elif not key:
        logger.info("GOOGLE_API_KEY 미설정 — AI 분류 기능이 비활성화됩니다.")

    webhook = os.getenv('DISCORD_WEBHOOK_URL', '')
    if webhook and 'discord.com/api/webhooks/' not in webhook:
        logger.warning("DISCORD_WEBHOOK_URL 형식이 올바르지 않습니다.")

    gchat = os.getenv('GOOGLE_CHAT_WEBHOOK_URL', '')
    if gchat and 'chat.googleapis.com' not in gchat:
        logger.warning("GOOGLE_CHAT_WEBHOOK_URL 형식이 올바르지 않을 수 있습니다 (일반적으로 chat.googleapis.com 포함).")

# ==================== DATE NORMALIZATION ====================
def normalize_date(date_str: str) -> str:
    """
    Normalizes a date string to YYYY-MM-DD format.
    Returns None if parsing fails or if it's 'Sangsi' (Always/Ongoing).
    한국어 날짜 표현 (MM월 DD일, ~마감, 오는 N월 등) 포함 처리.
    """
    if not date_str:
        return None

    date_str = date_str.strip()

    # 진행 중·상시 키워드 → None (마감 없음)
    if any(k in date_str for k in ['상시', '계속', '연중', '모집 중', '접수 중', '수시', '당일']):
        return None

    import re
    from datetime import datetime as _dt

    # 1. YYYY.MM.DD / YYYY-MM-DD / YYYY/MM/DD
    match = re.search(r'(\d{4})[\.\-/](\d{1,2})[\.\-/](\d{1,2})', date_str)
    if match:
        y, m, d = match.groups()
        try:
            _dt(int(y), int(m), int(d))
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        except ValueError:
            pass

    # 2. YYYY년 MM월 DD일
    match = re.search(r'(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일', date_str)
    if match:
        y, m, d = match.groups()
        try:
            _dt(int(y), int(m), int(d))
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        except ValueError:
            pass

    # 3. ~YYYY.MM.DD / ~YYYY-MM-DD (마감 표기: ~2024.12.31)
    match = re.search(r'~\s*(\d{4})[\.\-/](\d{1,2})[\.\-/](\d{1,2})', date_str)
    if match:
        y, m, d = match.groups()
        try:
            _dt(int(y), int(m), int(d))
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        except ValueError:
            pass

    # 4. MM월 DD일 (연도 없음 → 현재 연도 우선, 과거면 내년)
    match = re.search(r'(\d{1,2})월\s*(\d{1,2})일', date_str)
    if match:
        m, d = match.groups()
        now = _dt.now()
        try:
            candidate = _dt(now.year, int(m), int(d))
            # 이미 지난 날짜면 내년으로
            if candidate.date() < now.date():
                candidate = _dt(now.year + 1, int(m), int(d))
            return candidate.strftime('%Y-%m-%d')
        except ValueError:
            pass

    # 5. YY.MM.DD (2자리 연도 → 20xx, 미래 1년 초과 시 19xx)
    match = re.search(r'(\d{2})[\.\-/](\d{1,2})[\.\-/](\d{1,2})', date_str)
    if match:
        y, m, d = match.groups()
        try:
            year = 2000 + int(y)
            candidate = _dt(year, int(m), int(d))
            # 현재보다 1년 넘게 미래이면 19xx로 간주
            if candidate.year > _dt.now().year + 1:
                year = 1900 + int(y)
                _dt(year, int(m), int(d))  # validate
            return f"{year:04d}-{int(m):02d}-{int(d):02d}"
        except ValueError:
            pass

    return None
