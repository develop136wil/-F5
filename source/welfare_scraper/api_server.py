import sys

# Local document extraction must never initialize the operating database, server,
# configuration, schedules or log redirection in a child process.
if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == '--local-document-worker':
    from services.local_document_worker import main as _local_document_main
    raise SystemExit(_local_document_main(sys.argv[2:]))

import asyncio
import datetime
import json
import re
import database
import socket
import ipaddress
import sqlite3
import sys
import time
import uuid
CONFIG_REVISION = uuid.uuid4().hex
from contextlib import asynccontextmanager
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, unquote

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, StrictBool
import os
from services.secret_redaction import redact_secrets
from services.temp_files import create_temp_file, delete_temp_file, cleanup_stale_temp_files

# ============================================================
# CRITICAL: Redirect stderr → log file early (PyInstaller)
# Without this, crashes before logger setup are invisible.
# ============================================================
class _RotatingStderr:
    """sys.stderr 대체용 회전 가능한 스트림.

    (2026-07-27, GPT Codex 리뷰로 발견) logging의 RotatingFileHandler는 logger.*() 호출에만
    적용되고, 여기서 다루는 sys.stderr는 print()/traceback이 write()로 직접 쓰기 때문에
    logging 회전과 무관하다. 실제로 이모지→cp949 인코딩 버그 때 로깅 예외 트레이스백이
    sys.stderr로 반복 출력돼 11MB·7천여 건까지 무한정 쌓인 전례가 있다(그 근본 원인은
    이미 고쳤지만, 앞으로 다른 예외가 반복돼도 다시 무한정 커지지 않도록 자체 회전을 둔다).
    앱 시작 시 1회 자르는 방식은 실행 중 폭주를 못 막으므로, 쓰기마다 크기를 확인해
    임계치 초과 시 그 자리에서 회전한다(sidecar_stderr.log → .1 → .2 → .3, 최대 backup_count개).
    """
    def __init__(self, path: str, max_bytes: int = 5 * 1024 * 1024, backup_count: int = 3):
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.encoding = 'utf-8'
        self._file = open(path, 'a', encoding='utf-8')
        try:
            self._size = self._file.tell()
        except OSError:
            self._size = 0

    def write(self, data: str) -> int:
        try:
            data = redact_secrets(data)
            if self._size >= self.max_bytes:
                self._rotate()
            n = self._file.write(data)
            self._size += len(data.encode('utf-8', errors='replace'))
            return n
        except Exception:
            return 0  # stderr 자체가 죽는 것보다 조용히 무시하는 편이 안전(2차 크래시 방지)

    def flush(self) -> None:
        try:
            self._file.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        return False

    def _rotate(self) -> None:
        try:
            self._file.close()
        except Exception:
            pass
        for i in range(self.backup_count - 1, 0, -1):
            src, dst = f"{self.path}.{i}", f"{self.path}.{i + 1}"
            if os.path.exists(dst):
                try:
                    os.remove(dst)
                except OSError:
                    pass
            if os.path.exists(src):
                try:
                    os.rename(src, dst)
                except OSError:
                    pass
        if os.path.exists(self.path):
            try:
                os.rename(self.path, f"{self.path}.1")
            except OSError:
                pass
        self._file = open(self.path, 'a', encoding='utf-8')
        self._size = 0

    def close(self) -> None:
        try:
            self._file.close()
        except Exception:
            pass


def _setup_pyinstaller_env():
    """Configure environment variables and redirects for PyInstaller."""
    import sys

    # 1. Fix Playwright browser path (prevent looking in _MEI temp dir)
    app_data = os.getenv('LOCALAPPDATA') or os.path.expanduser('~')
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = os.path.join(app_data, 'ms-playwright')

    # 2. Redirect stderr (회전 가능한 스트림 — 위 _RotatingStderr 참조)
    if getattr(sys, 'frozen', False):
        try:
            import atexit
            log_dir = os.path.join(app_data, 'welfare-scraper', 'logs')
            os.makedirs(log_dir, exist_ok=True)
            stderr_log = os.path.join(log_dir, 'sidecar_stderr.log')
            _stderr_file = _RotatingStderr(stderr_log, max_bytes=5 * 1024 * 1024, backup_count=3)
            atexit.register(_stderr_file.close)
            sys.stderr = _stderr_file
        except Exception:
            pass  # Fail silently — we tried our best

_setup_pyinstaller_env()


def bind_server_socket(port: int):
    """Reserve our listener once. A conflict never authorizes killing its owner."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(('127.0.0.1', port))
        listener.listen(128)
        listener.setblocking(False)
        return listener
    except BaseException:
        listener.close()
        raise

import utils
import history_manager
import site_analyzer
import content_filter
import notifier
from services import policy_watch
from services import app_lifecycle
# Lazy load ScraperService to avoid heavy imports at startup
from services.logger import setup_logger

# Force inclusion of python-multipart for PyInstaller
try:
    import multipart
    import python_multipart
except ImportError:
    pass

# Setup Logger
logger = setup_logger("APIServer")

# Load Environment
utils.load_environment()
utils.validate_config()

# Global State
SCRAPER_STATUS = {
    "is_running": False,
    "status_code": "idle",   # "idle" | "running" | "stopped" | "completed" | "error"
    "message": "대기 중",
    "progress": 0,
    "total": 0,
    "last_run": None,
    "stopped": False,
    "ai_savings": None,   # 마지막 실행의 AI 호출 절감 통계
    "save_result": None,  # 실제 DB 저장 요청/성공/중복 제외 건수
}

BATCH_STATUS = {
    "is_running": False,
    "progress": 0,
    "total": 0,
    "current_url": "",
    "success_count": 0,
    "fail_count": 0,
    "failed_urls": [],   # 실패한 URL 목록 (재시도용)
    "cancelled": False,
    "message": ""
}

# 복지로 영유아 일괄 수집 상태
BOKJIRO_STATUS = {
    "is_running": False, "message": "", "collected": 0,
    "saved": 0, "skipped": 0, "total": 0,
}

# Cancel events
STOP_EVENT = asyncio.Event()
BATCH_CANCEL_EVENT = asyncio.Event()

# Lock: start_scrape의 check-and-set 보호 (동시 요청 Race Condition 방지)
_scraper_lock = asyncio.Lock()
# Lock: batch_add_sites의 check-and-set 보호
_batch_lock = asyncio.Lock()
# Notion 등록과 HTML 저장이 포스터 확인용 브라우저를 동시에 띄우지 않게 직렬화한다.
_news_media_lock = asyncio.Lock()

# ── 인앱 스케줄링 (APScheduler) ───────────────────────────────────────────────
# 예약 실행도 분당 요청 조절·제공사 한도 응답·증분 수집 안전장치를 따른다.
# 별도 일일 횟수 제한은 없으며, 유료 프로젝트의 예약 AI 호출에는 비용이 발생할 수 있다.
scheduler = None
_SCHEDULE_JOB_ID = "scheduled_scrape"
_SCHEDULE_CONFIG_KEY = "SCHEDULE_CONFIG"
_SCHEDULE_DEFER_TASK = None
_SCHEDULE_DEFERRED_AT = None

def _default_schedule():
    return {"enabled": False, "mode": "daily", "time": "09:00", "interval_hours": 6, "headless": True}

def get_schedule_config() -> Dict[str, Any]:
    """config.json에 저장된 스케줄 설정을 반환(없으면 기본값)."""
    import json as _json
    raw = utils.get_env_value(_SCHEDULE_CONFIG_KEY)
    cfg = _default_schedule()
    if raw:
        try:
            cfg.update({k: v for k, v in _json.loads(raw).items() if k in cfg})
        except (ValueError, TypeError):
            pass
    return cfg

def save_schedule_config(cfg: Dict[str, Any]) -> None:
    import json as _json
    utils.set_env_value(_SCHEDULE_CONFIG_KEY, _json.dumps(cfg, ensure_ascii=False))

def _queue_deferred_scrape(reason: str) -> None:
    """겹친 예약 실행을 버리지 않고 한 건만 보류한다."""
    global _SCHEDULE_DEFER_TASK, _SCHEDULE_DEFERRED_AT
    if app_lifecycle.shutdown_requested:
        return
    if _SCHEDULE_DEFER_TASK and not _SCHEDULE_DEFER_TASK.done():
        return
    _SCHEDULE_DEFERRED_AT = datetime.datetime.now().isoformat()
    logger.info(f"[Scheduler] {reason} 예약 수집 1건을 보류합니다.")
    _SCHEDULE_DEFER_TASK = asyncio.create_task(_run_deferred_scrape())


async def _run_deferred_scrape() -> None:
    """다른 작업 종료를 기다렸다가 보류된 예약을 한 번만 실행한다."""
    global _SCHEDULE_DEFERRED_AT
    max_wait = max(60, int(os.getenv("SCHEDULE_DEFER_MAX_SEC", "3600")))
    deadline = time.monotonic() + max_wait
    try:
        while time.monotonic() < deadline:
            if app_lifecycle.shutdown_requested:
                return
            if not get_schedule_config().get("enabled"):
                logger.info("[Scheduler] 스케줄이 꺼져 보류된 예약을 취소합니다.")
                return
            await asyncio.sleep(5)
            started = await _scheduled_scrape_job(allow_defer=False)
            if started:
                return
        logger.warning(f"[Scheduler] {max_wait}초 동안 실행 기회를 얻지 못해 보류 예약을 종료합니다.")
    finally:
        _SCHEDULE_DEFERRED_AT = None


async def _scheduled_scrape_job(allow_defer: bool = True) -> bool:
    """스케줄러가 호출하는 스크래핑 트리거 (수동 시작과 동일한 가드 적용)."""
    from services import policy_sync, ai_coordinator
    if app_lifecycle.shutdown_requested:
        return False
    from services.news_profiles_api import start_default_condition
    try:
        if await start_default_condition(headless=get_schedule_config().get('headless', True), wait=True) is not None:
            return True
    except HTTPException as error:
        logger.warning('예약 수집 준비 확인 필요: %s', error.detail)
        return False
    async with _scraper_lock:
        busy = policy_sync.maintenance_busy_message()
        if busy:
            if allow_defer:
                _queue_deferred_scrape(busy)
            return False
        if SCRAPER_STATUS["is_running"]:
            if allow_defer:
                _queue_deferred_scrape("이미 정보 수집이 진행 중입니다.")
            return False
        if not await ai_coordinator.try_start_job("scrape", "예약 정보 수집"):
            if allow_defer:
                _queue_deferred_scrape(ai_coordinator.busy_message() or "다른 AI 작업이 진행 중입니다.")
            return False
        STOP_EVENT.clear()
        SCRAPER_STATUS["is_running"] = True
        SCRAPER_STATUS["status_code"] = "running"
        SCRAPER_STATUS["stopped"] = False
        SCRAPER_STATUS["message"] = "예약 실행 시작..."
        SCRAPER_STATUS["progress"] = 0
        SCRAPER_STATUS["save_result"] = None
    cfg = get_schedule_config()
    logger.info("⏰ [Scheduler] 예약된 스크래핑을 시작합니다.")
    await run_scraper_task(cfg.get("headless", True))
    return True

def apply_schedule() -> Dict[str, Any]:
    """현재 설정을 APScheduler에 반영(기존 작업 제거 후 재등록). 적용 결과 반환."""
    global scheduler
    if scheduler is None:
        return {"applied": False, "reason": "scheduler not started"}
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    # 기존 작업 제거
    existing = scheduler.get_job(_SCHEDULE_JOB_ID)
    if existing:
        scheduler.remove_job(_SCHEDULE_JOB_ID)

    cfg = get_schedule_config()
    if not cfg.get("enabled"):
        logger.info("[Scheduler] 스케줄 비활성화 상태.")
        return {"applied": True, "enabled": False}

    mode = cfg.get("mode", "daily")
    if mode == "interval":
        hours = max(1, int(cfg.get("interval_hours", 6)))
        trigger = IntervalTrigger(hours=hours)
    else:  # daily
        try:
            hh, mm = str(cfg.get("time", "09:00")).split(":")
            trigger = CronTrigger(hour=int(hh), minute=int(mm))
        except (ValueError, TypeError):
            trigger = CronTrigger(hour=9, minute=0)

    scheduler.add_job(
        _scheduled_scrape_job, trigger=trigger, id=_SCHEDULE_JOB_ID,
        replace_existing=True, max_instances=1, coalesce=True,
    )
    job = scheduler.get_job(_SCHEDULE_JOB_ID)
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
    logger.info(f"[Scheduler] 스케줄 적용됨 (mode={mode}), 다음 실행: {next_run}")
    return {"applied": True, "enabled": True, "next_run": next_run}

# ── (U2) 정책 최신화 무료 자동 감시 — Stage1(해시비교)만, AI 호출 없음. 기본 꺼짐(opt-in) ──
_POLICY_WATCH_JOB_ID = "policy_watch_stage1"
_POLICY_WATCH_CONFIG_KEY = "POLICY_WATCH_SCHEDULE"

def _default_policy_watch_schedule():
    return {"enabled": False, "day_of_week": "mon", "time": "09:00"}

def get_policy_watch_schedule() -> Dict[str, Any]:
    import json as _json
    raw = utils.get_env_value(_POLICY_WATCH_CONFIG_KEY)
    cfg = _default_policy_watch_schedule()
    if raw:
        try:
            cfg.update({k: v for k, v in _json.loads(raw).items() if k in cfg})
        except (ValueError, TypeError):
            pass
    return cfg

def save_policy_watch_schedule(cfg: Dict[str, Any]) -> None:
    import json as _json
    utils.set_env_value(_POLICY_WATCH_CONFIG_KEY, _json.dumps(cfg, ensure_ascii=False))

async def _policy_watch_job():
    """무료 Stage1 감시 실행. 변경 의심 있으면 Google Chat으로 알림만(AI 미사용, DB 미변경)."""
    from services import policy_sync
    import notifier
    if app_lifecycle.shutdown_requested:
        return
    app_lifecycle.active_scheduled_jobs += 1
    try:
        logger.info("⏰ [정책 최신화-무료감시] 예약 실행 시작")
        res = await policy_sync.watch_stage1_only([])
        if res.get('failed', 0):
            logger.warning('[정책 최신화-무료감시] 출처 확인 실패 %s건 / 점검 %s건. 변경 없음으로 확정하지 않았습니다.',
                           res['failed'], res.get('checked', 0))
        if res.get("changed", 0) > 0:
            titles = res.get("changed_titles", [])[:10]
            lines = [f"*🔍 정책 최신화 자동 감시* — 원문 변경 의심 {res['changed']}건"]
            lines += [f"• {t}" for t in titles]
            if res["changed"] > len(titles):
                lines.append(f"…외 {res['changed'] - len(titles)}건")
            if res.get('failed', 0):
                lines.append(f"출처 확인 실패 {res['failed']}건은 변경 여부를 확인하지 못했습니다.")
            lines.append("앱의 정책 최신화 화면에서 '선택 점검'을 실행해 확인하세요.")
            await notifier.send_google_chat_text("\n".join(lines))
    except Exception as e:
        logger.error(f"[정책 최신화-무료감시] 실행 실패: {e}")
    finally:
        app_lifecycle.active_scheduled_jobs -= 1

def apply_policy_watch_schedule() -> Dict[str, Any]:
    global scheduler
    if scheduler is None:
        return {"applied": False, "reason": "scheduler not started"}
    from apscheduler.triggers.cron import CronTrigger

    existing = scheduler.get_job(_POLICY_WATCH_JOB_ID)
    if existing:
        scheduler.remove_job(_POLICY_WATCH_JOB_ID)

    cfg = get_policy_watch_schedule()
    if not cfg.get("enabled"):
        logger.info("[정책 최신화-무료감시] 비활성화 상태.")
        return {"applied": True, "enabled": False}

    try:
        hh, mm = str(cfg.get("time", "09:00")).split(":")
        trigger = CronTrigger(day_of_week=cfg.get("day_of_week", "mon"), hour=int(hh), minute=int(mm))
    except (ValueError, TypeError):
        trigger = CronTrigger(day_of_week="mon", hour=9, minute=0)

    scheduler.add_job(
        _policy_watch_job, trigger=trigger, id=_POLICY_WATCH_JOB_ID,
        replace_existing=True, max_instances=1, coalesce=True,
    )
    job = scheduler.get_job(_POLICY_WATCH_JOB_ID)
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
    logger.info(f"[정책 최신화-무료감시] 스케줄 적용됨, 다음 실행: {next_run}")
    return {"applied": True, "enabled": True, "next_run": next_run}

def update_progress(current, total, message, done=False):
    # (버그 2026-07-21) 여기 저장하는 값이 프론트에서 그대로 "{progress}%"로 표시되는데
    # 예전엔 완료된 사이트 "개수"(current)를 그대로 넣어서, 사이트가 ~40개면 전부 끝나도
    # "40%"로 보이고 하나도 안 끝난 구간(여러 사이트 병렬 진행 중)은 계속 "0%"로 보였다.
    # total 대비 백분율로 환산해서 저장해야 실제 진행률과 표시가 맞는다.
    #
    # (버그 2026-07-21 #2) 그 수정 직후 새 문제가 드러남 — 사이트 스크래핑이 끝나고 AI
    # 분석 단계로 넘어갈 때 scraper_service가 (total_sites, total_sites)를 그대로 넘겨서
    # 100%로 계산돼버렸다. 실제로는 그 뒤로 Gemini 분당 호출 한도 대기(59초씩 반복)를
    # 포함한 AI 분석이 몇 분씩 더 진행되는데, 진행률만 보면 이미 다 끝난 것처럼 보였다.
    # AI 분석 단계는 항목별 실시간 카운트가 없어 세밀한 진행률을 못 내므로, 진짜 완료
    # (done=True) 신호가 오기 전까지는 90%를 넘기지 않게 막아 "아직 안 끝났다"는 사실만은
    # 정확히 전달한다.
    pct = round(current / total * 100) if total else 0
    SCRAPER_STATUS["progress"] = 100 if done else min(pct, 90)
    SCRAPER_STATUS["total"] = total
    SCRAPER_STATUS["message"] = message

async def _shutdown_resources():
    """보류 예약을 정리하되 실행 중 writer의 연결/락을 강제로 회수하지 않는다."""
    global _SCHEDULE_DEFER_TASK, _SCHEDULE_DEFERRED_AT
    STOP_EVENT.set()
    BATCH_CANCEL_EVENT.set()
    from services.site_test_jobs import jobs as site_test_jobs
    await site_test_jobs.shutdown()
    task = _SCHEDULE_DEFER_TASK
    if task is not None and not task.done():
        if not SCRAPER_STATUS.get("is_running"):
            task.cancel()
        done, _ = await asyncio.wait({task}, timeout=5)
        if not done:
            logger.warning("종료 중 수집 정리가 아직 진행 중입니다. DB 연결을 강제로 닫지 않습니다.")
            return
    if task is not None and task.done() and not task.cancelled():
        # 예외를 회수해 'Task exception was never retrieved'를 방지한다.
        error = task.exception()
        if error:
            logger.warning(f"보류 예약 종료 오류: {error}")
    _SCHEDULE_DEFER_TASK = None
    _SCHEDULE_DEFERRED_AT = None
    from services import ai_coordinator
    if (SCRAPER_STATUS.get("is_running") or BATCH_STATUS.get("is_running")
            or BOKJIRO_STATUS.get("is_running") or ai_coordinator.busy_message()):
        return
    database.close_all_connections()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("--- API Server Startup Sequence Initialized ---")
    logger.info(f"Process ID: {os.getpid()}")
    try:
        database.auto_backup_db()
        logger.info("Database auto-backup completed.")
    except Exception as e:
        logger.error(f"Database auto-backup failed during startup: {e}")

    # Only the whole-workflow OS lease proves that no GUI/run-once worker is
    # alive. A new server's empty task dictionary alone cannot prove orphanhood.
    try:
        from services.news_profiles_api import recover_idle_workflows
        await recover_idle_workflows()
    except Exception:
        # Keep read-only UI available. Actual start/resume retries admission and
        # recovery; never reclaim a live scheduled worker at GUI startup.
        logger.warning('중단 작업 복구를 보류합니다. 다른 작업 또는 DB 잠금 상태를 확인해주세요.')

    # Disabled until the user explicitly saves a 7/30-day news retention choice.
    try:
        from services.news_record_retention import run_configured_cleanup
        await run_configured_cleanup()
    except Exception:
        logger.warning('오래된 뉴스 기록 자동 정리를 보류합니다. 기록은 그대로 유지됩니다.')

    # 앱 전용 폴더만 정리한다. 공용 Temp의 과거 파일은 소유권을 알 수 없어 보존한다.
    try:
        _cleaned = cleanup_stale_temp_files()
        if _cleaned:
            logger.info(f"Cleaned up {_cleaned} stale temp file(s) from previous run")
    except Exception as e:
        logger.warning(f"Stale temp file cleanup failed (ignorable): {e}")

    # 인앱 스케줄러 시작 + 저장된 스케줄 적용
    try:
        global scheduler
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        scheduler = AsyncIOScheduler()
        scheduler.start()
        apply_schedule()
        apply_policy_watch_schedule()
        logger.info("In-app scheduler started.")
    except Exception as e:
        logger.error(f"Scheduler startup failed (ignorable): {e}")

    parent_task = None
    if app_lifecycle.token() and os.environ.get('WELFARE_DESKTOP_PARENT_PID'):
        parent_task = asyncio.create_task(_watch_desktop_parent())
    try:
        yield
    finally:
        if parent_task:
            parent_task.cancel()
            try:
                await parent_task
            except asyncio.CancelledError:
                pass
    # Shutdown
    logger.info("API Server Shutting down...")
    STOP_EVENT.set() # Ensure cleanup on shutdown
    try:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
    except Exception as e:
        logger.warning(f"Scheduler shutdown error (ignorable): {e}")
    await _shutdown_resources()

app = FastAPI(title="Welfare Scraper API", lifespan=lifespan)
app.add_middleware(app_lifecycle.DesktopAdmissionMiddleware)
from services.news_jobs_api import router as news_jobs_router
app.include_router(news_jobs_router)
from services.news_profiles_api import router as news_profiles_router
app.include_router(news_profiles_router)
from services.news_unified_feed_api import router as news_feed_router
app.include_router(news_feed_router)
from services.news_record_retention_api import router as news_retention_router
app.include_router(news_retention_router)

_DESKTOP_SERVER = None
_DESKTOP_SHUTDOWN_TASK = None


async def _watch_desktop_parent():
    from services.desktop_parent import ParentWatch
    watch = ParentWatch(int(os.environ['WELFARE_DESKTOP_PARENT_PID']))
    try:
        while watch.alive():
            await asyncio.sleep(1)
        logger.info('앱 창 프로세스가 종료되어 백엔드 작업을 안전하게 마무리합니다.')
        await desktop_shutdown()
    finally:
        watch.close()


def _loaded_policy_busy():
    """Observe existing policy work without importing the policy/AI pipeline.

    An absent module cannot own its module-local maintenance lock. A partial
    import or an unreadable state is not proof of idleness: keep shutdown held.
    """
    missing = object()
    policy = sys.modules.get('services.policy_sync', missing)
    if policy is missing:
        return False
    try:
        if getattr(getattr(policy, '__spec__', None), '_initializing', False):
            return True
        return bool(policy.maintenance_busy_message())
    except Exception:
        return True


def _desktop_busy(ai_job=None):
    from services import ai_coordinator
    if ai_job is None:
        ai_job = ai_coordinator.local_job_status()
    return bool(app_lifecycle.active_requests or app_lifecycle.active_scheduled_jobs
                or app_lifecycle.active_profile_jobs
                or database.has_active_accesses()
                or SCRAPER_STATUS.get('is_running') or BATCH_STATUS.get('is_running')
                or BOKJIRO_STATUS.get('is_running') or _loaded_policy_busy()
                or ai_job.get('job_kind'))


@app.get('/api/desktop/status')
async def desktop_status():
    if not app_lifecycle.token():
        raise HTTPException(status_code=404, detail='Desktop session is not enabled')
    from services import ai_coordinator
    ai_job = ai_coordinator.local_job_status()
    busy = _desktop_busy(ai_job)
    return {'instance': os.environ.get('WELFARE_DESKTOP_INSTANCE', ''),
            'busy': busy, 'shutdown_requested': app_lifecycle.shutdown_requested,
            'job': ai_job.get('job_label') or '',
            'shutdown_stage': ('진행 중인 작업·저장 완료 대기' if busy else '통신 연결 정리 중') if app_lifecycle.shutdown_requested else '',
            'pid': os.getpid()}


async def _drain_desktop_shutdown():
    # No deadline-based kill: restore/rollback and background responses must finish.
    logger.info('안전 종료: 진행 중인 작업과 DB 저장 완료를 기다립니다.')
    while app_lifecycle.shutdown_requested:
        if not _desktop_busy():
            logger.info('안전 종료: 작업 완료. 서버 연결 정리를 시작합니다.')
            _DESKTOP_SERVER.should_exit = True
            return
        await asyncio.sleep(0.2)


@app.post('/api/desktop/shutdown')
async def desktop_shutdown():
    global _DESKTOP_SHUTDOWN_TASK
    if not app_lifecycle.token() or _DESKTOP_SERVER is None:
        raise HTTPException(status_code=409, detail='안전 종료 연결을 확인할 수 없습니다.')
    if not app_lifecycle.shutdown_requested:
        app_lifecycle.shutdown_requested = True
        STOP_EVENT.set()
        BATCH_CANCEL_EVENT.set()
        from services.site_test_jobs import jobs as site_test_jobs
        site_test_jobs.request_shutdown()
        if scheduler is not None:
            try:
                scheduler.pause()
            except Exception:
                logger.warning('예약 실행 일시정지 실패. 종료 게이트로 신규 작업을 차단합니다.')
        # Only a waiting reservation is cancelled. An active scraper drains normally.
        if _SCHEDULE_DEFER_TASK and not SCRAPER_STATUS.get('is_running'):
            _SCHEDULE_DEFER_TASK.cancel()
        _DESKTOP_SHUTDOWN_TASK = asyncio.create_task(_drain_desktop_shutdown())
    return {'accepted': True}


@app.exception_handler(database.DatabaseMaintenanceError)
async def database_maintenance_error_handler(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=503, content={'detail': str(exc)}, headers={'Retry-After': '2'})


@app.exception_handler(sqlite3.Error)
async def database_error_handler(request, exc):
    from fastapi.responses import JSONResponse
    logger.warning('저장된 자료에 접근하지 못했습니다 (%s).', type(exc).__name__)
    return JSONResponse(status_code=503,
                        content={'detail': '저장된 자료에 접근하지 못했습니다. 잠시 후 다시 시도해주세요. 문제가 계속되면 앱을 다시 실행하고 연결 상태를 확인해주세요.'},
                        headers={'Retry-After': '2'})

# CORS Configuration (Allow Tauri frontend only)
origins = [
    "http://127.0.0.1:1420",
    "http://localhost:1420",
    "http://localhost:8421",
    "tauri://localhost",
    "https://tauri.localhost",
    "http://tauri.localhost",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Welfare-Session"],
)

# Models
class SiteConfig(BaseModel):
    name: str
    url: str
    selectors: Dict[str, str] = {}
    base_url: Optional[str] = ""

class ScrapeRequest(BaseModel):
    headless: bool = True

# --- Endpoints ---

@app.get("/api/status")
async def get_status():
    """Get current scraper status"""
    return SCRAPER_STATUS


@app.get("/api/ai/status")
async def get_ai_status():
    """현재 전역 AI 대량 작업과 모델별 대기·냉각 상태."""
    from services import ai_coordinator
    return ai_coordinator.status()

@app.post("/api/scrape")
async def start_scrape(req: ScrapeRequest, background_tasks: BackgroundTasks):
    """Start the scraping process in background"""
    from services.news_profiles_api import start_default_condition
    result = await start_default_condition(headless=req.headless)
    if result is not None:
        return {'status': 'started', **result}
    async with _scraper_lock:
        from services import policy_sync, ai_coordinator
        busy = policy_sync.maintenance_busy_message()
        if busy:
            raise HTTPException(status_code=409, detail=busy)
        if SCRAPER_STATUS["is_running"]:
            raise HTTPException(status_code=400, detail="스크레이퍼가 이미 실행 중입니다")
        if not await ai_coordinator.try_start_job("scrape", "정보 수집"):
            raise HTTPException(status_code=409, detail=ai_coordinator.busy_message())

        STOP_EVENT.clear()
        SCRAPER_STATUS["is_running"] = True
        SCRAPER_STATUS["status_code"] = "running"
        SCRAPER_STATUS["stopped"] = False
        SCRAPER_STATUS["message"] = "시작 중..."
        SCRAPER_STATUS["progress"] = 0
        SCRAPER_STATUS["save_result"] = None

    background_tasks.add_task(run_scraper_task, req.headless)
    return {"status": "started"}

class ScheduleConfigRequest(BaseModel):
    enabled: bool = False
    mode: str = "daily"            # "daily" | "interval"
    time: str = "09:00"            # daily 모드 실행 시각 (HH:MM)
    interval_hours: int = 6        # interval 모드 실행 간격(시간)
    headless: bool = True

@app.get("/api/schedule")
async def get_schedule():
    """현재 스케줄 설정 + 다음 실행 시각."""
    cfg = get_schedule_config()
    next_run = None
    if scheduler is not None:
        job = scheduler.get_job(_SCHEDULE_JOB_ID)
        if job and job.next_run_time:
            next_run = job.next_run_time.isoformat()
    return {
        **cfg,
        "next_run": next_run,
        "scheduler_running": scheduler is not None,
        "deferred": _SCHEDULE_DEFERRED_AT is not None,
        "deferred_at": _SCHEDULE_DEFERRED_AT,
    }

# ── Windows 작업 스케줄러 연동 (앱 종료 중에도 자동 수집) ──────────────────────
# APScheduler는 앱(서버)이 켜져 있을 때만 동작한다. 앱을 닫아도 정해진 시각에
# 수집하려면 OS 작업 스케줄러에 '--run-once' 모드 실행을 등록한다.
_OS_TASK_NAME = "SearchMaster_AutoScrape"

def _run_once_command() -> str:
    """현재 실행 환경에 맞는 1회 수집 명령 문자열(schtasks /TR 인자)."""
    if getattr(sys, 'frozen', False):
        # PyInstaller 패키지: exe 자체를 --run-once로 호출
        return f'"{sys.executable}" --run-once'
    # 개발 환경: python 인터프리터로 이 스크립트를 --run-once 실행
    return f'"{sys.executable}" "{os.path.abspath(__file__)}" --run-once'

def os_task_status() -> Dict[str, Any]:
    """Distinguish absence from denied/unavailable Task Scheduler queries."""
    if sys.platform != 'win32':
        return {"supported": False, "registered": False}
    try:
        import subprocess
        options = dict(capture_output=True, text=True, timeout=10,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        result = subprocess.run(["schtasks", "/Query", "/TN", _OS_TASK_NAME], **options)
        if result.returncode == 0:
            return {"supported": True, "registered": True}
        # /Query /TN uses a nonzero exit for both a missing task and access
        # denial. A successful inventory can prove absence without matching
        # localized Windows error prose. Other task names never leave here.
        inventory = subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/NH"], **options)
        if inventory.returncode == 0:
            import csv
            rows = list(csv.reader(inventory.stdout.splitlines()))
            rows = [row for row in rows if row]
            if all(len(row) >= 3 and row[0].startswith('\\') for row in rows):
                return {"supported": True, "registered": any(
                    row[0].casefold() == ('\\' + _OS_TASK_NAME).casefold() for row in rows)}
    except Exception:
        logger.warning('Windows task registration query unavailable')
    return {"supported": True, "registered": None,
            "error": "Windows 자동 실행의 등록 상태를 확인하지 못했습니다. 다시 확인해주세요."}


@app.post("/api/schedule")
async def set_schedule(req: ScheduleConfigRequest):
    """스케줄 설정 저장 후 즉시 반영."""
    if req.mode not in ("daily", "interval"):
        raise HTTPException(status_code=400, detail="mode는 'daily' 또는 'interval'이어야 합니다")
    if req.mode == "daily":
        if not re.match(r'^\d{1,2}:\d{2}$', req.time):
            raise HTTPException(status_code=400, detail="time 형식은 HH:MM 이어야 합니다")
        hh, mm = req.time.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise HTTPException(status_code=400, detail="유효하지 않은 시각입니다")
    if req.mode == "interval" and not (1 <= req.interval_hours <= 168):
        raise HTTPException(status_code=400, detail="interval_hours는 1~168 사이여야 합니다")

    cfg = req.model_dump()
    save_schedule_config(cfg)
    result = apply_schedule()
    return {"status": "success", **cfg, "next_run": result.get("next_run")}

class PolicyWatchScheduleRequest(BaseModel):
    enabled: bool = False
    day_of_week: str = "mon"
    time: str = "09:00"

@app.get("/api/policy/watch-schedule")
async def get_policy_watch_schedule_endpoint():
    """(U2) 정책 최신화 무료 자동 감시(Stage1만, AI 없음) 스케줄 조회."""
    cfg = get_policy_watch_schedule()
    job = scheduler.get_job(_POLICY_WATCH_JOB_ID) if scheduler else None
    cfg["next_run"] = job.next_run_time.isoformat() if job and job.next_run_time else None
    return cfg

@app.post("/api/policy/watch-schedule")
async def set_policy_watch_schedule_endpoint(req: PolicyWatchScheduleRequest):
    """스케줄 저장 후 즉시 반영."""
    valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    if req.day_of_week not in valid_days:
        raise HTTPException(status_code=400, detail=f"day_of_week은 {valid_days} 중 하나여야 합니다")
    if not re.match(r'^\d{1,2}:\d{2}$', req.time):
        raise HTTPException(status_code=400, detail="time 형식은 HH:MM 이어야 합니다")
    hh, mm = req.time.split(":")
    if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
        raise HTTPException(status_code=400, detail="유효하지 않은 시각입니다")

    cfg = req.model_dump()
    save_policy_watch_schedule(cfg)
    result = apply_policy_watch_schedule()
    return {"status": "success", **cfg, "next_run": result.get("next_run")}


class OsTaskRequest(BaseModel):
    mode: str = "daily"          # "daily" | "interval"
    time: str = "09:00"          # daily 실행 시각
    interval_hours: int = 6      # interval 실행 간격

@app.get("/api/schedule/os-task")
async def get_os_task():
    """Windows 작업 스케줄러 등록 상태."""
    return await database.run_db(os_task_status)

@app.post("/api/schedule/os-task")
async def register_os_task(req: OsTaskRequest):
    """앱이 꺼져 있어도 동작하도록 Windows 작업 스케줄러에 1회 수집 작업 등록."""
    if sys.platform != 'win32':
        raise HTTPException(status_code=400, detail="Windows에서만 지원됩니다")
    if req.mode not in ('daily', 'interval'):
        raise HTTPException(status_code=400, detail="실행 방식은 매일 지정 시각 또는 일정 간격이어야 합니다")
    import subprocess
    cmd = ["schtasks", "/Create", "/TN", _OS_TASK_NAME, "/TR", _run_once_command(), "/F"]
    if req.mode == "interval":
        if not (1 <= req.interval_hours <= 168):
            raise HTTPException(status_code=400, detail="interval_hours는 1~168 사이여야 합니다")
        cmd += ["/SC", "HOURLY", "/MO", str(req.interval_hours)]
    else:
        if not re.match(r'^\d{1,2}:\d{2}$', req.time):
            raise HTTPException(status_code=400, detail="time 형식은 HH:MM 이어야 합니다")
        hour, minute = map(int, req.time.split(':'))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise HTTPException(status_code=400, detail="유효하지 않은 실행 시각입니다")
        cmd += ["/SC", "DAILY", "/ST", req.time]
    try:
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True,
            timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or 'unknown error').strip()
            raise HTTPException(status_code=500, detail=f"작업 등록 실패: {detail}")
        current = await database.run_db(os_task_status)
        if current.get('registered') is not True or current.get('error'):
            raise HTTPException(status_code=502, detail={"error": "OS_TASK_STATUS_UNAVAILABLE",
                "message": "등록 요청은 전송했지만 현재 상태를 확인하지 못했습니다. 다시 확인해주세요."})
        logger.info(f"Windows 작업 등록됨: {_OS_TASK_NAME} ({req.mode})")
        return {"status": "success", "registered": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"작업 등록 실패: {e}")

@app.delete("/api/schedule/os-task")
async def delete_os_task():
    """등록된 Windows 작업 제거."""
    if sys.platform != 'win32':
        raise HTTPException(status_code=400, detail="Windows에서만 지원됩니다")
    import subprocess
    try:
        result = await asyncio.to_thread(
            subprocess.run, ["schtasks", "/Delete", "/TN", _OS_TASK_NAME, "/F"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        current = await database.run_db(os_task_status)
        registered = current.get('registered')
        if registered is True:
            raise HTTPException(status_code=409, detail={"error": "OS_TASK_STILL_REGISTERED",
                "message": "Windows 자동 실행 작업이 아직 등록되어 있습니다. 해제되지 않았습니다."})
        if registered is not False or current.get('error'):
            raise HTTPException(status_code=502, detail={"error": "OS_TASK_STATUS_UNAVAILABLE",
                "message": "해제 여부를 확인하지 못했습니다. 자동 실행이 남아 있을 수 있으니 다시 확인해주세요."})
        # A task proven absent is an idempotent success even if /Delete failed.
        return {"status": "success", "registered": False}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"작업 제거 실패: {e}")

@app.post("/api/scrape/stop")
async def stop_scrape():
    """Stop the running scraper"""
    async with _scraper_lock:
        if not SCRAPER_STATUS["is_running"]:
            raise HTTPException(status_code=409, detail="실행 중인 작업이 없습니다")
        STOP_EVENT.set()
        SCRAPER_STATUS["message"] = "중지 요청됨..."
    return {"status": "stopping", "message": "중지 신호를 보냈습니다"}

async def run_scraper_task(headless: bool):
    """Background task wrapper for scraper"""
    from services.scraper_service import ScraperService
    service = ScraperService(headless=headless)
    try:
        await service.run_scraper_async(progress_callback=update_progress, stop_event=STOP_EVENT)

        # AI 호출 절감 통계 노출 (캐시/의미중복/한도초과로 아낀 Gemini 호출 수)
        SCRAPER_STATUS["ai_savings"] = getattr(service, "last_ai_savings", None)
        SCRAPER_STATUS["save_result"] = getattr(service, "last_save_result", None)

        if STOP_EVENT.is_set():
            SCRAPER_STATUS["status_code"] = "stopped"
            SCRAPER_STATUS["message"] = "사용자에 의해 취소됨"
            SCRAPER_STATUS["stopped"] = True
        elif (getattr(service, 'last_run_outcome', None) or {}).get('state') == 'partial_failure':
            outcome = service.last_run_outcome
            SCRAPER_STATUS["status_code"] = "partial_failure"
            SCRAPER_STATUS["message"] = outcome.get('message') or (
                f"일부 미완료 — {int((SCRAPER_STATUS.get('save_result') or {}).get('saved') or 0)}건 저장/갱신"
                f" · 수집 실패 {int(outcome.get('sites_failed') or 0)}개 사이트")
        elif (getattr(service, 'last_run_outcome', None) or {}).get('state') in ('not_ready', 'busy', 'failed'):
            SCRAPER_STATUS["status_code"] = "error"
            SCRAPER_STATUS["message"] = "수집을 시작하지 못했습니다. 사이트 설정과 실행 중인 작업을 확인해주세요."
        else:
            SCRAPER_STATUS["status_code"] = "completed"
            save_result = SCRAPER_STATUS.get("save_result") or {}
            saved = int(save_result.get("saved") or 0)
            skipped = int(save_result.get("skipped") or 0)
            if saved or skipped:
                SCRAPER_STATUS["message"] = (
                    f"완료 — {saved}건 저장/갱신"
                    + (f" · {skipped}건 중복 제외" if skipped else ""))
            else:
                SCRAPER_STATUS["message"] = "완료 — 새로 저장할 항목 없음"

    except Exception as e:
        logger.error(f"Scraper Error: {e}")
        SCRAPER_STATUS["status_code"] = "error"
        SCRAPER_STATUS["message"] = f"Error: {str(e)}"
    finally:
        from services import ai_coordinator
        ai_coordinator.finish_job("scrape")
        SCRAPER_STATUS["is_running"] = False
        SCRAPER_STATUS["last_run"] = datetime.datetime.now().isoformat()

@app.get("/api/logs")
async def get_logs():
    """Get recent logs from memory"""
    from services.logger import get_recent_logs
    return {"logs": get_recent_logs()}

@app.get("/api/data")
async def get_data(limit: int = Query(10000, ge=1, le=10000)):
    """Get scraped data. limit 기본값 10,000건 (최신순). 최대 100,000건."""
    try:
        data = await database.run_db(history_manager.load_data, limit=limit)
        return data
    except (sqlite3.Error, database.DatabaseMaintenanceError):
        raise
    except Exception as e:
        logger.error(f"Failed to load data: {e}")
        raise HTTPException(status_code=500, detail="데이터를 불러오는 중 오류가 발생했습니다.")

@app.get("/api/data/recent")
async def get_recent_data(limit: int = Query(10, ge=1, le=50)):
    """Get most recent items for activity feed"""
    try:
        return await database.run_db(history_manager.get_recent_items, limit=limit)
    except (sqlite3.Error, database.DatabaseMaintenanceError):
        raise
    except Exception as e:
        logger.error(f"Failed to load recent items: {e}")
        raise HTTPException(status_code=500, detail="최근 항목을 불러오는 중 오류가 발생했습니다.")

@app.get("/api/sites")
async def get_sites():
    """Get configured sites"""
    from services.site_test_jobs import jobs
    sites = await database.run_db(utils.load_sites)
    return [{**site, 'test_task': jobs.status(site)} for site in sites]

@app.post("/api/sites")
async def save_sites(sites: List[Dict]):
    """Save sites configuration"""
    try:
        await database.run_db(utils.save_sites, sites)
        return {"status": "success", "count": len(sites)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"사이트 설정을 확인해주세요: {e}")
    except Exception as e:
        logger.error(f"Site save error: {e}")
        raise HTTPException(status_code=500, detail="사이트 설정 저장에 실패했습니다")

@app.delete("/api/sites/{site_id}")
async def delete_site(site_id: int):
    """Delete a site by its ID"""
    from services.site_test_jobs import jobs
    jobs.invalidate(site_id)
    deleted = await database.run_db(utils.delete_site, site_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="사이트를 찾을 수 없습니다")
    return {"status": "success"}


@app.post("/api/sites/{site_id}/test")
async def test_site(site_id: int):
    """Start a diagnostic independently of the short HTTP request."""
    from services.site_test_jobs import jobs, SiteTestBusy
    sites = await database.run_db(utils.load_sites)
    site = next((s for s in sites if s.get('id') == site_id), None)
    if not site:
        raise HTTPException(status_code=404, detail="사이트를 찾을 수 없습니다")
    try:
        return await jobs.start(site, _run_site_test)
    except SiteTestBusy as error:
        raise HTTPException(status_code=409, detail=str(error))


@app.get("/api/sites/{site_id}/test-status")
async def site_test_status(site_id: int):
    from services.site_test_jobs import jobs
    sites = await database.run_db(utils.load_sites)
    site = next((s for s in sites if s.get('id') == site_id), None)
    if not site:
        raise HTTPException(status_code=404, detail="사이트를 찾을 수 없습니다")
    return jobs.status(site)


async def _run_site_test(site):
    from services.site_test_jobs import jobs, identity
    if site.get('registration_status') in ('needs_check', 'basic_verified'):
        from services.site_registration import probe_site
        verified = await probe_site(site, raise_errors=True)
        status = 'basic_verified' if verified else 'needs_check'
        def save_check():
            with database.get_connection() as conn:
                # Keep identity validation and its update in one transaction:
                # editing/deleting/re-registering cannot acquire an old result.
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT * FROM sites WHERE id=?', (site['id'],)).fetchone()
                current = dict(row) if row else None
                if current and isinstance(current.get('selectors'), str):
                    current['selectors'] = json.loads(current['selectors']) if current['selectors'] else {}
                if not current or identity(current) != identity(site) or not jobs.is_current(site):
                    conn.rollback()
                    return
                conn.execute('UPDATE sites SET registration_status=? WHERE id=? AND url=?',
                             (status, site['id'], site['url']))
                conn.commit()
        await _settle_restore_operation(database.run_db(save_check))
        return {'status': 'success', 'site': site['name'], 'registration_status': status}
    from services.scraper_service import ScraperService
    service = ScraperService(headless=True)
    outcome = await service.test_single_site(site, with_status=True)
    if outcome.get('state') == 'failed':
        raise RuntimeError('사이트 목록을 확인하지 못했습니다.')
    items = outcome['items']
    return {'status': 'success', 'site': site['name'], 'count': len(items),
            'items': items[:5], 'collection_state': outcome['state'],
            'vision_review_required': outcome.get('vision_review_required', False)}

# --- Log Management ---

@app.get("/api/logs/files")
async def list_log_files():
    """List available log files"""
    # Use same logic as logger.py to find logs
    app_data = os.getenv('LOCALAPPDATA')
    if not app_data:
        app_data = os.path.expanduser('~')
    log_dir = os.path.join(app_data, 'welfare-scraper', 'logs')
    
    if not os.path.exists(log_dir):
        return []
    
    files = []
    for f in os.listdir(log_dir):
        if f.endswith('.log') or '.log.' in f:
            path = os.path.join(log_dir, f)
            size = os.path.getsize(path)
            files.append({
                "name": f,
                "size": size,
                "modified": os.path.getmtime(path)
            })
    
    # Sort by modification time (newest first)
    files.sort(key=lambda x: x['modified'], reverse=True)
    return files

def _validate_log_path(filename: str) -> str:
    """로그 파일 경로 검증: 경로 탐색 방지 + 파일 존재 확인. 검증된 절대 경로 반환."""
    app_data = os.getenv('LOCALAPPDATA') or os.path.expanduser('~')
    log_dir = os.path.join(app_data, 'welfare-scraper', 'logs')
    file_path = os.path.join(log_dir, filename)
    real_dir, real_file = os.path.realpath(log_dir), os.path.realpath(file_path)
    # (2026-07-20) startswith는 접두사가 같은 형제 폴더(예: logs2)를 우회할 수 있어
    # commonpath로 교체 — real_file이 real_dir 자손일 때만 두 경로의 공통 조상이 real_dir 자신이 된다.
    # 드라이브가 다르면(윈도우) commonpath가 ValueError를 던지는데, 이 역시 거부 대상이다.
    try:
        is_inside = os.path.commonpath([real_dir, real_file]) == real_dir
    except ValueError:
        is_inside = False
    if not is_inside:
        raise HTTPException(status_code=403, detail="접근이 거부되었습니다")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다")
    return file_path

@app.get("/api/logs/download/{filename}")
async def download_log(filename: str):
    """Download a specific log file"""
    file_path = _validate_log_path(filename)
    return FileResponse(path=file_path, filename=filename, media_type='text/plain')

@app.delete("/api/logs/{filename}")
async def delete_log(filename: str):
    """Delete a specific log file"""
    file_path = _validate_log_path(filename)
        
    try:
        os.remove(file_path)
        return {"status": "success", "message": f"{filename} deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="로그 파일 삭제에 실패했습니다")


class ConfigRequest(BaseModel):
    items: Dict[str, str]

@app.get("/api/config")
async def get_config():
    """Get current configuration (masked)"""
    from services import ai_coordinator
    api_key = utils.get_env_value("GOOGLE_API_KEY")
    masked_key = ""
    if api_key:
        if len(api_key) > 8:
            masked_key = f"{api_key[:4]}...{api_key[-4:]}"
        else:
            masked_key = "****"
            
    custom_prompt = utils.get_env_value("CUSTOM_AI_PROMPT")

    def _mask_url(u: str) -> str:
        if not u:
            return ""
        return f"{u[:24]}…{u[-6:]}" if len(u) > 36 else "****"

    discord_url = utils.get_env_value("DISCORD_WEBHOOK_URL")
    gchat_url = utils.get_env_value("GOOGLE_CHAT_WEBHOOK_URL")

    try:
        retention_days = int(utils.get_env_value("DATA_RETENTION_DAYS") or 365)
    except (ValueError, TypeError):
        retention_days = 365

    return {
        "GOOGLE_API_KEY": masked_key,
        "revision": CONFIG_REVISION,
        "is_configured": bool(api_key),
        "CUSTOM_AI_PROMPT": custom_prompt,
        "AI_DAILY_REQUEST_LIMIT": ai_coordinator.daily_request_limit(),
        "ai_quota_policy": ai_coordinator._quota_policy_status(),
        "DISCORD_WEBHOOK_URL": _mask_url(discord_url),
        "GOOGLE_CHAT_WEBHOOK_URL": _mask_url(gchat_url),
        "discord_configured": bool(discord_url),
        "google_chat_configured": bool(gchat_url),
        "DATA_RETENTION_DAYS": retention_days,
        "NOTION_DATABASE_ID": utils.get_env_value("NOTION_DATABASE_ID"),
        "notion_token_configured": bool(utils.get_env_value("NOTION_TOKEN")),
        "notion_configured": bool(utils.get_env_value("NOTION_TOKEN") and utils.get_env_value("NOTION_DATABASE_ID")),
        "NOTION_POLICY_DBS": utils.get_env_value("NOTION_POLICY_DBS"),
        "NOTION_POLICY_PROP_MAP": utils.get_env_value("NOTION_POLICY_PROP_MAP"),
        "POLICY_STYLE_GUIDE": utils.get_env_value("POLICY_STYLE_GUIDE"),
        "TARGET_REGION": utils.get_env_value("TARGET_REGION") or "도봉",
        "TARGET_AGE_MAX_MONTHS": utils.safe_config_int("TARGET_AGE_MAX_MONTHS", 108, 0, 240),
    }

# 사용자가 /api/config로 저장 가능한 설정 키 화이트리스트
_ALLOWED_CONFIG_KEYS = {
    "GOOGLE_API_KEY", "CUSTOM_AI_PROMPT",
    "DISCORD_WEBHOOK_URL", "GOOGLE_CHAT_WEBHOOK_URL",
    "DATA_RETENTION_DAYS",
    "NOTION_TOKEN", "NOTION_DATABASE_ID", "NOTION_POLICY_DBS", "POLICY_STYLE_GUIDE",
    "TARGET_REGION", "TARGET_AGE_MAX_MONTHS", "NOTION_POLICY_PROP_MAP",
}

# (2026-07-20) 화면에 마스킹되어 표시되는 비밀값들 — 이 키들만 "마스킹된 값 재저장 방지"
# 검사 대상이다. CUSTOM_AI_PROMPT 같은 일반 텍스트값은 마스킹되지 않으므로 검사 대상이면
# 안 된다(실측 버그: 사용자가 지시문에 '…'을 그냥 문장부호로 썼는데 조용히 저장 실패).
_MASKABLE_CONFIG_KEYS = {"GOOGLE_API_KEY", "NOTION_TOKEN", "DISCORD_WEBHOOK_URL", "GOOGLE_CHAT_WEBHOOK_URL"}


def _looks_masked(value: str) -> bool:
    """마스킹된 표시값(get_config가 만드는 두 형태)을 재저장 시 걸러낸다.

    get_config()는 API 키를 ASCII '...'(f"{앞4자}...{뒤4자}")로, 웹훅 URL은 유니코드
    '…'(f"{앞24자}…{뒤6자}")로 마스킹한다 — 형식이 서로 달라 한쪽만 검사하면 다른 쪽이
    새서 진짜 값을 마스킹 문자열로 덮어쓸 수 있었다(실측). 두 형태 모두, 그리고 짧은
    값의 '****' 대체도 함께 막는다.
    """
    v = value or ''
    return '…' in v or '...' in v or v == '****'


@app.post("/api/config")
async def save_config(req: ConfigRequest):
    """Save configuration"""
    global CONFIG_REVISION
    try:
        updates = {}
        for key, value in req.items.items():
            if key in _ALLOWED_CONFIG_KEYS:
                if key in _MASKABLE_CONFIG_KEYS and _looks_masked(value):
                    continue
                updates[key] = value
        for key, maximum in (("TARGET_AGE_MAX_MONTHS", 240), ("DATA_RETENTION_DAYS", 3650)):
            if key in updates:
                raw = updates[key].strip()
                if not raw.isascii() or not raw.isdigit() or len(raw) > len(str(maximum)) or not 0 <= int(raw) <= maximum:
                    raise HTTPException(status_code=400, detail=f"{key}: 0~{maximum} 범위의 정수를 입력해주세요.")
                updates[key] = str(int(raw))
        for key, expected_type in (("NOTION_POLICY_DBS", list), ("NOTION_POLICY_PROP_MAP", dict)):
            if key in updates and updates[key].strip():
                import json
                try:
                    parsed = json.loads(updates[key])
                    if not isinstance(parsed, expected_type):
                        raise ValueError()
                    if key == 'NOTION_POLICY_DBS':
                        seen = set()
                        seen_sources = set()
                        from services import notion_service
                        for db in parsed:
                            if not isinstance(db, dict) or not all(isinstance(db.get(k), str) and db[k].strip() for k in ('key', 'data_source_id')) or db['key'] in seen:
                                raise ValueError()
                            seen.add(db['key'])
                            normalized_source = notion_service.normalize_notion_id(db['data_source_id'])
                            if not normalized_source or normalized_source in seen_sources:
                                raise HTTPException(status_code=400, detail='같은 Notion 정책 보관함을 두 번 등록할 수 없습니다.')
                            seen_sources.add(normalized_source)
                    elif not all(isinstance(v, str) for v in parsed.values()):
                        raise ValueError()
                except (ValueError, TypeError):
                    raise HTTPException(status_code=400, detail=f'{key}: 설정 형식을 확인해주세요.')
        if updates:
            if 'GOOGLE_API_KEY' in updates:
                from services import ai_coordinator
                try:
                    with ai_coordinator.request_budget_config_lock():
                        utils.set_env_values(updates)
                except ai_coordinator.AiQuotaStateUnavailable:
                    raise HTTPException(409, 'AI 요청 기록을 처리 중입니다. 잠시 후 연결 설정을 다시 저장해주세요.')
            else:
                utils.set_env_values(updates)
            CONFIG_REVISION = uuid.uuid4().hex
        return {"status": "success", "revision": CONFIG_REVISION}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Config Save Error: {e}")
        raise HTTPException(status_code=500, detail="설정 저장에 실패했습니다")

# --- Keywords ---

class QuotaObservationRequest(BaseModel):
    confirmed: StrictBool
    models: Dict[str, dict]


@app.post('/api/ai/quota-observation')
async def save_quota_observation(req: QuotaObservationRequest):
    """Record a dated observation, never query/buy/raise Google's quota."""
    global CONFIG_REVISION
    from services import ai_coordinator, ai_rate_policy
    if req.confirmed is not True:
        raise HTTPException(400, '현재 Google 프로젝트의 한도 값을 확인해주세요.')
    try:
        with ai_coordinator.request_budget_config_lock():
            # Read the authoritative current file; no env/backup fallback here.
            config_path = os.path.join(utils.get_app_data_dir(), 'config.json')
            try:
                with open(config_path, encoding='utf-8-sig') as handle:
                    current = json.load(handle)
                key = current.get('GOOGLE_API_KEY') if isinstance(current, dict) else None
            except (OSError, ValueError):
                raise HTTPException(409, '현재 AI 연결 설정을 읽을 수 없습니다. 연결을 확인해주세요.') from None
            if not isinstance(key, str) or not key.strip():
                raise HTTPException(400, 'AI 키를 먼저 연결해주세요.')
            try:
                observation = ai_rate_policy.observe(req.models, key=key)
            except (ValueError, TypeError, KeyError):
                raise HTTPException(400, '두 모델의 RPM·TPM·RPD 한도를 0 이상의 정수로 입력해주세요.') from None
            utils.set_env_values({ai_rate_policy.POLICY_KEY: json.dumps(observation, ensure_ascii=False)})
        CONFIG_REVISION = uuid.uuid4().hex
        return {'status': 'success', 'revision': CONFIG_REVISION,
                'quota_policy': ai_rate_policy.public_status({'state': 'current', **observation})}
    except ai_coordinator.AiQuotaStateUnavailable:
        raise HTTPException(409, 'AI 요청 기록을 처리 중입니다. 잠시 후 다시 저장해주세요.') from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, 'AI 한도 관측값을 저장하지 못했습니다.') from None

@app.get("/api/keywords")
async def get_keywords():
    """Get all configured keywords"""
    keywords = content_filter.load_keywords()
    return {"keywords": keywords}

class KeywordRequest(BaseModel):
    keyword: str

@app.post("/api/keywords")
async def add_keyword(req: KeywordRequest):
    """Add a keyword"""
    keywords = content_filter.load_keywords()
    if req.keyword in keywords:
        raise HTTPException(status_code=400, detail="이미 존재하는 키워드입니다")
    keywords.append(req.keyword)
    content_filter.save_keywords(keywords)
    content_filter.clear_cache()
    return {"status": "success", "keywords": keywords}

@app.delete("/api/keywords/{keyword}")
async def delete_keyword(keyword: str):
    """Delete a keyword"""
    keywords = content_filter.load_keywords()
    if keyword not in keywords:
        raise HTTPException(status_code=404, detail="키워드를 찾을 수 없습니다")
    keywords.remove(keyword)
    content_filter.save_keywords(keywords)
    content_filter.clear_cache()
    return {"status": "success", "keywords": keywords}

# --- Excluded Keywords ---

@app.get("/api/keywords/excluded")
async def get_excluded_keywords():
    """Get all excluded keywords"""
    keywords = content_filter.load_excluded_keywords()
    return {"keywords": keywords}

@app.post("/api/keywords/excluded")
async def add_excluded_keyword(req: KeywordRequest):
    """Add an excluded keyword"""
    keywords = content_filter.load_excluded_keywords()
    if req.keyword in keywords:
        raise HTTPException(status_code=400, detail="이미 존재하는 제외 키워드입니다")
    content_filter.add_excluded_keyword(req.keyword)
    content_filter.clear_cache()
    return {"status": "success", "keywords": content_filter.load_excluded_keywords()}

@app.delete("/api/keywords/excluded/{keyword}")
async def delete_excluded_keyword(keyword: str):
    """Delete an excluded keyword"""
    keywords = content_filter.load_excluded_keywords()
    if keyword not in keywords:
        raise HTTPException(status_code=404, detail="제외 키워드를 찾을 수 없습니다")
    content_filter.delete_excluded_keyword(keyword)
    content_filter.clear_cache()
    return {"status": "success", "keywords": content_filter.load_excluded_keywords()}

@app.get("/api/keywords/analyze")
async def analyze_keywords():
    """Analyze recent items for keyword suggestions"""
    suggestions = await database.run_db(history_manager.get_keyword_suggestions)
    return suggestions

# --- System & Admin ---

@app.get("/api/ready")
async def get_readiness():
    """프로세스뿐 아니라 운영 DB까지 실제로 사용할 수 있는지 확인한다."""
    def _check():
        with database.get_connection() as conn:
            value = conn.execute("SELECT 1").fetchone()
            if not value or value[0] != 1:
                raise RuntimeError("데이터베이스 준비 확인에 실패했습니다")

    try:
        await database.run_db(_check)
    except Exception as e:
        logger.error(f"Readiness check failed: {e}")
        raise HTTPException(status_code=503, detail="데이터베이스를 준비하지 못했습니다") from e
    return {"status": "ready"}

@app.get("/api/health")
async def get_health():
    """Get system health status"""
    stats = await database.run_db(history_manager.get_health_stats)
    return stats

@app.post("/api/data/cleanup")
async def cleanup_data(days: int = Query(default=90, ge=1, le=365)):
    """Delete old or expired data"""
    count = await database.run_db(history_manager.cleanup_data, days)
    return {"status": "success", "deleted_count": count}

@app.post("/api/data/archive-expired")
async def archive_expired():
    """마감일이 지난 항목을 아카이브(숨김) 처리"""
    count = await database.run_db(history_manager.archive_expired_items)
    return {"status": "success", "archived_count": count}

@app.get("/api/data/expiring")
async def get_expiring(days: int = Query(default=7, ge=1, le=30)):
    """days일 이내 마감 임박 항목 목록 반환"""
    items = await database.run_db(history_manager.get_expiring_items, days=days)
    return {"items": items, "total": len(items), "days": days}

@app.get("/api/data/changed")
async def get_changed(days: int = Query(default=14, ge=1, le=90)):
    """최근 변경된 항목 (정책 내용/마감일 등) — Notion 수정 필요 검토용. 내용변경 우선."""
    items = await database.run_db(history_manager.get_recently_changed_items, days, 100)
    content_changed = sum(1 for i in items if i.get('content_changed'))
    return {"items": items, "total": len(items), "content_changed": content_changed, "days": days}

# --- 정책 Watch (정해진 정책 페이지의 변경점 집중 감지) ---
_policy_watch_lock = asyncio.Lock()

class PolicyWatchRequest(BaseModel):
    name: str
    url: str
    scope: str = ""

@app.get("/api/policy-watch")
async def get_policy_watch():
    """등록된 정책 watch 목록 + 마지막 점검/변경 상태."""
    return await database.run_db(policy_watch.list_watch)

@app.post("/api/policy-watch")
async def add_policy_watch(req: PolicyWatchRequest):
    """정책 watch 페이지 등록."""
    url = req.url.strip()
    if not (url.startswith('http://') or url.startswith('https://')):
        raise HTTPException(status_code=400, detail="유효한 URL(http/https)이 아닙니다")
    await database.run_db(policy_watch.add_watch, req.name.strip() or url, url, req.scope.strip())
    return {"status": "success"}

@app.delete("/api/policy-watch/{watch_id}")
async def delete_policy_watch(watch_id: int):
    await database.run_db(policy_watch.remove_watch, watch_id)
    return {"status": "success"}

@app.post("/api/policy-watch/{watch_id}/enabled")
async def toggle_policy_watch(watch_id: int, value: bool = Query(...)):
    await database.run_db(policy_watch.set_enabled, watch_id, value)
    return {"status": "success", "enabled": value}

@app.get("/api/policy-watch/{watch_id}/diff")
async def policy_watch_diff(watch_id: int):
    """직전 본문 vs 현재 본문 (변경 내용 확인용)."""
    d = await database.run_db(policy_watch.get_watch_diff, watch_id)
    if not d:
        raise HTTPException(status_code=404, detail="watch 항목을 찾을 수 없습니다")
    return d

@app.post("/api/policy-watch/check")
async def run_policy_watch_check():
    """모든 활성 watch 페이지를 점검(저비용, AI 불필요). 동시 실행 방지."""
    from services import policy_sync
    if _policy_watch_lock.locked():
        raise HTTPException(status_code=409, detail="이미 점검이 진행 중입니다")
    if not await policy_sync.acquire_db_mutation_lock("정책 Watch 점검"):
        raise HTTPException(status_code=409, detail=policy_sync.maintenance_busy_message())
    try:
        async with _policy_watch_lock:
            return await policy_watch.run_watch_check()
    finally:
        policy_sync.release_maintenance_lock()

# --- 복지로 영유아 복지서비스 일괄 수집 (API 기반, AI 불필요) ---
@app.post("/api/bokjiro/collect-infant")
async def bokjiro_collect_infant(background_tasks: BackgroundTasks):
    """Retired: never execute the old infant/region-specific collector."""
    from services.news_auto_sources import RETIRED_NEWS_BOKJIRO_MESSAGE
    raise HTTPException(410, RETIRED_NEWS_BOKJIRO_MESSAGE)

@app.get("/api/bokjiro/status")
async def bokjiro_status():
    return BOKJIRO_STATUS

# --- Notion 업로드 (단발성 행사) ---

def _is_event_candidate(item: dict) -> bool:
    """미종료 행사/날짜 보유 자료 또는 DB에서 명시적으로 가져온 새 소식."""
    ai = history_manager._parse_ai_data(item.get("ai_data")) or {}
    it = (ai.get("item_type") or "").lower()
    has_date = any(item.get(k) for k in ("event_date", "deadline", "event_end_date"))
    if it != "event" and not has_date and not item.get('manual_news'):
        return False
    # 종료된 행사 제외 (마감/종료일 < 오늘)
    from services import notion_service
    if notion_service.compute_status(item) == "종료":
        return False
    return True

@app.get("/api/notion/candidates")
async def notion_candidates():
    """미등록·미종료 행사 및 사용자가 가져온 새 소식 후보 목록."""
    from services import notion_service, notion_upload_state

    def _load():
        from services.news_result_import import MANUAL_NEWS_SQL
        rows = database.execute_query(
            f"""SELECT id, site_name, title, link, support_detail, contact,
                      event_date, deadline, event_end_date, ai_data, scraped_at,
                      {MANUAL_NEWS_SQL} AS manual_news
               FROM welfare_items
               WHERE is_archived=0 AND (notion_page_id IS NULL OR notion_page_id='')
                     AND (notion_dismissed IS NULL OR notion_dismissed=0)
                     AND link LIKE 'http%'"""
        )
        return [dict(r) for r in rows]

    rows = await database.run_db(_load)
    out = []
    uncertain = await database.run_db(notion_upload_state.uncertain_links)
    for r in rows:
        if not _is_event_candidate(r):
            continue
        ai = history_manager._parse_ai_data(r.get('ai_data')) or {}
        attachment_state = ai.get('source_attachments')
        if not isinstance(attachment_state, dict):
            attachment_state = {}
        input_state = ai.get('source_input')
        if not isinstance(input_state, dict):
            input_state = {}
        from services.page_coverage import input_note as source_input_note
        input_note = source_input_note(ai)
        from services.news_timing import timing_review_note
        input_note = ' '.join(filter(None, (input_note, timing_review_note(r))))
        start, end = notion_service.compute_period(r)
        out.append({
            "id": r["id"],
            "title": notion_service.clean_title(r["title"]),
            "upload_uncertain": r['link'] in uncertain,
            "org": notion_service.site_to_org(r.get("site_name", ""), r.get("contact", "")),
            "site_name": r.get("site_name", ""),
            "link": r.get("link", ""),
            "start": start, "end": end,
            "status": '기간 확인 필요' if r.get('manual_news') and not start else notion_service.compute_status(r),
            "support_preview": (r.get("support_detail") or "")[:180],
            "manual_news": bool(r.get('manual_news')),
            "attachment_review_required": attachment_state.get('status') == 'review_required',
            "attachment_note": str(attachment_state.get('note') or '')[:1000],
            "input_review_required": bool(input_note),
            "input_note": input_note,
        })
    out.sort(key=lambda x: (x["start"] or "9999"))
    return {"configured": notion_service.is_configured(), "count": len(out), "items": out}

@app.get("/api/notion/verify")
async def notion_verify():
    """Notion 토큰/DB 연결 확인."""
    from services import notion_service
    revision = CONFIG_REVISION
    try:
        result = await asyncio.wait_for(notion_service.verify_connection(), timeout=75)
    except asyncio.TimeoutError:
        result = {'ok': False, 'error': 'Notion 응답이 늦어 연결 확인을 중단했습니다. 저장된 설정은 유지됩니다. 잠시 후 다시 확인해주세요.'}
    return {**result, 'revision': revision}


@app.get("/api/policy/verify-dbs")
async def policy_verify_dbs():
    """정책 DB별 Notion 접근 권한과 필수 속성 구성을 검사한다."""
    from services import notion_service, policy_sync
    revision = CONFIG_REVISION
    try:
        result = await asyncio.wait_for(notion_service.verify_policy_databases(), timeout=75)
    except asyncio.TimeoutError:
        return {'ok': False, 'items': [], 'revision': revision,
                'error': '정책표 연결 확인 시간이 초과됐습니다. 저장된 설정은 유지됩니다. 인터넷 연결을 확인한 뒤 다시 시도해주세요.'}
    checked_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    result["checked_at"] = checked_at
    result["items"] = [{**item, "checked_at": checked_at} for item in result.get("items", [])]
    try:
        await database.run_db(policy_sync.record_policy_verification, result["items"])
    except Exception as e:
        # 외부 연결 확인 결과는 그대로 돌려주고, 복원 등으로 상태 기록만 실패한 경우 로그로 남긴다.
        logger.warning(f"정책 보관함 확인 상태 저장 실패: {e}")
    return {**result, 'revision': revision}


@app.get("/api/config/google/verify")
async def google_api_key_verify():
    revision = CONFIG_REVISION
    return {**await _google_api_key_verify(), 'revision': revision}


async def _google_api_key_verify():
    """저장된 키로 실제 기본 모델 조회가 가능한지, 생성 호출 없이 확인한다."""
    import aiohttp
    import json
    api_key = utils.get_env_value("GOOGLE_API_KEY").strip()
    if not api_key:
        return {"ok": False, "status": "missing", "error": "Google API 키가 설정되지 않았습니다."}
    model = (utils.get_env_value('CLASSIFY_MODEL') or 'gemini-3.5-flash-lite').strip()
    if not re.fullmatch(r'[A-Za-z0-9._-]+', model):
        return {"ok": False, "status": "invalid_model", "error": "사용할 AI 모델 설정을 확인해주세요."}
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"x-goog-api-key": api_key}, allow_redirects=False) as response:
                data = await response.json()
                if response.status == 200:
                    return {"ok": True, "status": "connected", "message": "Google Gemini API와 사용할 모델 연결이 확인되었습니다."}
                status = "invalid_key" if response.status in (400, 401, 403) else "unavailable"
                message = ('Google API 키와 프로젝트 접근 권한을 확인해주세요.' if status == 'invalid_key'
                           else f'Google 연결을 확인하지 못했습니다 (HTTP {response.status}). 잠시 후 다시 시도해주세요.')
                return {"ok": False, "status": status, "error": message}
    except Exception as e:
        logger.warning('Google 연결 확인 실패 (%s).', type(e).__name__)
        return {"ok": False, "status": "network_error", "error": "Google 연결을 확인하지 못했습니다. 인터넷 연결을 확인하고 잠시 후 다시 시도해주세요."}

class NotionDismissRequest(BaseModel):
    ids: List[int]

@app.post("/api/notion/dismiss")
async def notion_dismiss(req: NotionDismissRequest):
    """선택한 항목을 Notion 업로드 후보에서 영구 숨김 (다시 안 뜸)."""
    if not req.ids:
        return {"dismissed": 0}

    def _dismiss(ids):
        q = ",".join("?" * len(ids))
        with database.get_connection() as conn:
            cursor = conn.execute(
                f"UPDATE welfare_items SET notion_dismissed=1 WHERE id IN ({q})", tuple(ids))
            conn.commit()
            return cursor.rowcount

    dismissed = await database.run_db(_dismiss, req.ids)
    return {"dismissed": dismissed}

class NotionUploadRequest(BaseModel):
    ids: List[int]


def _load_news_media_cache(link: str) -> Dict[str, Any]:
    from services import news_gallery
    with database.get_connection() as conn:
        return news_gallery.get_cached_poster(conn, link)


def _store_news_media_cache(link: str, poster_url: str) -> None:
    from services import news_gallery
    with database.get_connection() as conn:
        news_gallery.store_poster(conn, link, poster_url)


async def _poster_for_news_item(item: Dict[str, Any], engine, engine_state: Dict[str, bool]) -> tuple[str, bool]:
    """포스터 캐시를 우선 사용하고, 없을 때만 페이지를 한 번 확인한다.

    두 번째 반환값은 캐시 적중 여부다. 포스터 판정은 HTML 규칙 기반이며 Gemini를 호출하지 않는다.
    """
    link = str(item.get("link") or "")
    cached = await database.run_db(_load_news_media_cache, link)
    if cached.get("hit"):
        return str(cached.get("poster_url") or ""), True
    poster = ""
    try:
        if not engine_state.get("started"):
            await engine.start()
            engine_state["started"] = True
        poster = await engine.extract_poster_url(link)
    except Exception as exc:
        logger.info(f"[새소식 포스터] 추출 생략 {link[:100]}: {exc}")
    await database.run_db(_store_news_media_cache, link, poster)
    return poster, False

@app.post("/api/notion/upload")
async def notion_upload(req: NotionUploadRequest):
    """DB 복원과 겹치지 않도록 업로드 전체를 DB 변경 게이트 안에서 실행."""
    from services import policy_sync
    if not await policy_sync.acquire_db_mutation_lock("Notion 업로드"):
        raise HTTPException(status_code=409, detail=policy_sync.maintenance_busy_message())
    try:
        if _news_media_lock.locked():
            raise HTTPException(status_code=409, detail="다른 새소식 파일 또는 Notion 등록 작업이 진행 중입니다.")
        await _news_media_lock.acquire()
        try:
            return await _notion_upload_locked(req)
        finally:
            _news_media_lock.release()
    finally:
        policy_sync.release_maintenance_lock()


async def _notion_upload_locked(req: NotionUploadRequest):
    """선택한 행사들을 Notion에 업로드. 성공 시 notion_page_id 기록.

    포스터는 JS 렌더링 후 추출(familynet 등 동적 로드 대응, 로고/헤더 제외).
    """
    from services import notion_service
    from services import notion_upload_state
    from scraper_engine import ScraperEngine
    if not notion_service.is_configured():
        raise HTTPException(status_code=400, detail="Notion 토큰/DB가 설정되지 않았습니다 (Settings).")
    if not req.ids:
        return {"results": [], "uploaded": 0, "total": 0}

    ids = list(dict.fromkeys(req.ids))

    def _load(ids):
        from services.news_result_import import MANUAL_NEWS_SQL
        from services.news_display_projection import with_stored_provenance
        q = ",".join("?" * len(ids))
        with database.get_connection() as conn:
            rows = conn.execute(
                f"SELECT *, {MANUAL_NEWS_SQL} AS manual_news FROM welfare_items WHERE id IN ({q})", tuple(ids)).fetchall()
            return {r["id"]: with_stored_provenance(dict(r), conn) for r in rows}

    items = await database.run_db(_load, ids)
    results = []
    uploaded = 0
    eng = ScraperEngine(headless=True)
    engine_state = {"started": False}
    try:
        for _id in ids:
            item = items.get(_id)
            if not item:
                results.append({"id": _id, "ok": False, "error": "항목 없음"})
                continue
            state = await database.run_db(notion_upload_state.claim, _id)
            if state['state'] == 'completed':
                uploaded += 1
                results.append({'id': _id, 'ok': True, 'already_uploaded': True})
                continue
            if state['state'] != 'claimed':
                results.append({'id': _id, 'ok': False, 'error': '이전 업로드 결과 확인이 필요합니다. Notion에 생성됐는지 확인해주세요.'})
                continue
            poster = ''
            try:
                poster, _cache_hit = await _poster_for_news_item(item, eng, engine_state)
            except Exception:
                pass
            try:
                res = await notion_service.create_event_page(item, poster)
                if res.get("ok"):
                    await database.run_db(notion_upload_state.complete, state['link'], res['page_id'])
                    uploaded += 1
                elif (res.get('code') in ('NOTION_PAYLOAD_TOO_LARGE', 'NOTION_PAYLOAD_INVALID')
                      and res.get('request_sent') is False and res.get('uncertain') is False):
                    # Only a proven local preflight rejection can safely undo
                    # this fresh claim. Sent/unknown/old attempts stay blocked.
                    released = await database.run_db(notion_upload_state.release_unsubmitted, state)
                    if not released:
                        res = {'ok': False, 'error': '등록 요청은 보내지 않았지만 로컬 등록 상태를 확인하지 못했습니다. 다시 시도하기 전에 상태를 확인해주세요.'}
            except Exception:
                res = {'ok': False, 'error': '업로드 결과를 확인하지 못했습니다. 중복 방지를 위해 Notion 확인 후 재시도해주세요.'}
            results.append({"id": _id, "ok": res.get("ok"),
                            "error": res.get("error"), "poster": bool(poster)})
    finally:
        await eng.close()
    return {"uploaded": uploaded, "total": len(ids), "results": results}


@app.post("/api/news/gallery-html")
async def export_news_gallery(req: NotionUploadRequest, background_tasks: BackgroundTasks):
    """선택한 기존 새소식을 AI 호출 없이 독립 HTML 갤러리로 내보낸다."""
    from services import policy_sync
    if not await policy_sync.acquire_db_mutation_lock("새소식 HTML 저장"):
        raise HTTPException(status_code=409, detail=policy_sync.maintenance_busy_message())
    try:
        if _news_media_lock.locked():
            raise HTTPException(status_code=409, detail="다른 새소식 파일 또는 Notion 등록 작업이 진행 중입니다.")
        await _news_media_lock.acquire()
        try:
            return await _export_news_gallery_locked(req, background_tasks)
        finally:
            _news_media_lock.release()
    finally:
        policy_sync.release_maintenance_lock()


async def _export_news_gallery_locked(req: NotionUploadRequest, background_tasks: BackgroundTasks):
    from services import news_gallery, notion_service
    from scraper_engine import ScraperEngine

    ids = list(dict.fromkeys(req.ids))
    if not ids:
        raise HTTPException(status_code=400, detail="HTML로 저장할 새소식을 선택해주세요.")
    if len(ids) > 100:
        raise HTTPException(status_code=400, detail="한 번에 최대 100건까지 HTML로 저장할 수 있습니다.")
    # 전체 수집과 포스터용 브라우저를 동시에 띄우면 저사양 PC에서 불안정해질 수 있다.
    if SCRAPER_STATUS.get("is_running") or BATCH_STATUS.get("is_running") or BOKJIRO_STATUS.get("is_running"):
        raise HTTPException(status_code=409, detail="정보 수집이 끝난 뒤 HTML 갤러리를 저장해주세요.")

    def _load(selected_ids):
        from services.news_result_import import MANUAL_NEWS_SQL
        from services.news_display_projection import with_stored_provenance
        q = ",".join("?" * len(selected_ids))
        with database.get_connection() as conn:
            rows = conn.execute(
                f"SELECT *, {MANUAL_NEWS_SQL} AS manual_news FROM welfare_items WHERE id IN ({q}) AND is_archived=0", tuple(selected_ids)).fetchall()
            return {row["id"]: with_stored_provenance(dict(row), conn) for row in rows}

    item_map = await database.run_db(_load, ids)
    items = []
    for item_id in ids:
        item = item_map.get(item_id)
        if not item or not _is_event_candidate(item):
            continue
        item["title"] = notion_service.clean_title(item.get("title", ""))
        item["org"] = notion_service.site_to_org(item.get("site_name", ""), item.get("contact", ""))
        item["status"] = ('기간 확인 필요' if item.get('manual_news') and not notion_service.compute_period(item)[0]
                          else notion_service.compute_status(item))
        items.append(item)
    if not items:
        raise HTTPException(status_code=404, detail="저장할 수 있는 진행 중 새소식이 없습니다.")

    engine = ScraperEngine(headless=True)
    engine_state = {"started": False}
    cache_hits = 0
    posters = 0
    try:
        for item in items:
            poster, cache_hit = await _poster_for_news_item(item, engine, engine_state)
            item["poster_url"] = poster
            cache_hits += int(cache_hit)
            posters += int(bool(poster))
    finally:
        await engine.close()

    try:
        file_path = await database.run_db(news_gallery.generate_html, items)
    except news_gallery.GalleryTooLargeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=500, detail="HTML 갤러리를 만들지 못했습니다.")
    background_tasks.add_task(_delete_temp_file, file_path)
    filename = f"WelfareF5_News_{datetime.date.today().isoformat()}.html"
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type="text/html",
        headers={
            "X-WelfareF5-AI-Calls": "0",
            "X-WelfareF5-Poster-Cache-Hits": str(cache_hits),
            "X-WelfareF5-Posters": str(posters),
        },
    )


@app.post('/api/notion/upload/allow-retry')
async def allow_notion_upload_retry(req: NotionUploadRequest):
    from services import policy_sync, notion_upload_state
    if not await policy_sync.acquire_db_mutation_lock('Notion 업로드 재시도 확인'):
        raise HTTPException(status_code=409, detail=policy_sync.maintenance_busy_message())
    try:
        await database.run_db(notion_upload_state.allow_retry, req.ids)
        return {'ok': True}
    finally:
        policy_sync.release_maintenance_lock()

# --- 정책 최신화 (연결된 정책 보관함 변경 감지) ---

class PolicyDbKeys(BaseModel):
    db_keys: List[str] = []


async def _acquire_policy_ai_job(kind: str, label: str) -> None:
    """정책 유지보수 락과 전역 AI 게이트를 항상 같은 순서로 확보한다."""
    from services import policy_sync, ai_coordinator
    if not await policy_sync.try_acquire_maintenance_lock(label):
        raise HTTPException(status_code=409, detail=policy_sync.maintenance_busy_message())
    if not await ai_coordinator.try_start_job(kind, label):
        policy_sync.release_maintenance_lock()
        raise HTTPException(status_code=409, detail=ai_coordinator.busy_message())


def _clear_completed_full_check_status() -> None:
    """A newer standalone task replaces the old full-check result in the UI."""
    from services import policy_sync
    if not policy_sync.ONE_CLICK_STATUS.get('running'):
        policy_sync.ONE_CLICK_STATUS.clear()
        policy_sync.ONE_CLICK_STATUS.update(running=False, state='idle', phase='',
                                            checked=0, total=0, message='')

# (2026-08-28, GPT Codex 지적 ③+⑦) 아래 6개 백그라운드 작업 + DB복원은 정책 데이터를 같이
# 건드리므로 policy_sync._MAINTENANCE_LOCK 하나를 공유한다. 예전엔 각자 독립된 *_STATUS["running"]
# 플래그만 확인해 (a) run_full_check()가 sync_policies/check_policies를 직접 호출해 엔드포인트
# 가드를 우회하고, (b) "확인 후 백그라운드 등록" 사이에 await 지점이 있어 거의 동시 요청이 둘 다
# 통과할 수 있었다. 이제 엔드포인트에서 즉시(원자적으로) 락을 확보한 뒤에만 백그라운드로 넘기고,
# 백그라운드 함수가 끝나면(성공/실패 무관) 반드시 락을 해제한다.
async def _run_policy_sync(db_keys: List[str]):
    from services import policy_sync, ai_coordinator
    try:
        await policy_sync.sync_policies(db_keys)
    except Exception as e:
        logger.error(f"정책 동기화 실패: {e}")
        policy_sync.SYNC_STATUS.update(running=False, message=f"오류: {e}")
    finally:
        ai_coordinator.finish_job("policy_sync")
        policy_sync.release_maintenance_lock()

async def _run_full_check(db_keys: List[str]):
    from services import policy_sync, ai_coordinator
    try:
        # run_full_check() 내부에서 sync_policies()/check_policies()를 직접 호출하지만,
        # 이 함수들 자체엔 락을 걸지 않는다 — 이미 이 바깥 호출이 유지보수 락을 쥐고 있으므로
        # (asyncio.Lock은 재진입 불가) 내부에서 또 잡으면 데드락이 난다.
        await policy_sync.run_full_check(db_keys)
    except Exception as e:
        logger.error(f"정책 한번에 점검 실패: {e}")
        policy_sync.ONE_CLICK_STATUS.update(running=False, state='error',
            message='정책 점검을 완료하지 못했습니다. 저장된 결과는 유지됩니다. 다시 점검해주세요.')
    finally:
        ai_coordinator.finish_job("policy_full_check")
        policy_sync.release_maintenance_lock()

@app.get('/api/policy/full-check-plan')
async def policy_full_check_plan_endpoint(db_keys: Optional[str] = None):
    """Local counts only; no Notion fetch, AI request, or policy mutation."""
    from services.policy_full_check import get_plan
    keys = [key for key in (db_keys or '').split(',') if key]
    return await database.run_db(get_plan, keys)


@app.post("/api/policy/full-check")
async def policy_full_check_endpoint(req: PolicyDbKeys, background_tasks: BackgroundTasks):
    """Necessary first/change comparisons and proposal preparation; never auto-apply."""
    await _acquire_policy_ai_job("policy_full_check", "정책 통합 점검")
    background_tasks.add_task(_run_full_check, req.db_keys)
    return {"status": "started"}

@app.get("/api/policy/full-check-status")
async def policy_full_check_status_endpoint():
    from services import policy_sync
    return policy_sync.ONE_CLICK_STATUS

@app.get('/api/policy/task-status')
async def policy_task_status_endpoint():
    """One local snapshot for sidebar polling; no DB, remote check or mutation."""
    from services import policy_sync
    return [{'label': label, **status} for label, status in (
        ('정책 최신화', policy_sync.ONE_CLICK_STATUS),
        ('Notion 불러오기', policy_sync.SYNC_STATUS),
        ('정책 비교', policy_sync.AUDIT_STATUS),
        ('스타일 제안 생성', policy_sync.STYLE_STATUS),
        ('변경 제안 다시 확인', policy_sync.RECOMPUTE_STATUS),
        ('출처 변경 확인', policy_sync.POLICY_STATUS),
    )]

@app.post("/api/policy/sync")
async def policy_sync_endpoint(req: PolicyDbKeys, background_tasks: BackgroundTasks):
    """선택 정책 DB의 Notion 페이지를 로컬 미러로 동기화 (출처 URL·필드·임베딩). 백그라운드.

    150건+ 규모면 임베딩 호출로 수 분 걸릴 수 있어 HTTP 타임아웃을 피해 백그라운드로 실행.
    """
    await _acquire_policy_ai_job("policy_sync", "정책 동기화")
    _clear_completed_full_check_status()
    background_tasks.add_task(_run_policy_sync, req.db_keys)
    return {"status": "started"}

@app.get("/api/policy/sync-status")
async def policy_sync_status_endpoint():
    from services import policy_sync
    return policy_sync.SYNC_STATUS

async def _run_policy_check(db_keys: List[str]):
    from services import policy_sync, ai_coordinator
    from services.policy_notifications import policy_result_message
    import notifier
    try:
        res = await policy_sync.check_policies(db_keys)
        cands = await database.run_db(policy_sync.get_new_candidates, db_keys)
        policy_sync.POLICY_STATUS["candidates"] = len(cands)
        # ⑦ 점검 요약 Google Chat 알림
        try:
            await notifier.send_google_chat_text(policy_result_message(res, candidates=len(cands)))
        except Exception:
            pass
    except Exception as e:
        logger.error(f"정책 점검 실패: {e}")
        policy_sync.POLICY_STATUS.update(running=False, message=f"오류: {e}")
    finally:
        ai_coordinator.finish_job("policy_check")
        policy_sync.release_maintenance_lock()

@app.post("/api/policy/check")
async def policy_check_endpoint(req: PolicyDbKeys, background_tasks: BackgroundTasks):
    """선택 정책 DB의 출처를 점검 (Stage1 해시 → 변경분만 Stage2 AI). 백그라운드."""
    await _acquire_policy_ai_job("policy_check", "정책 점검")
    _clear_completed_full_check_status()
    background_tasks.add_task(_run_policy_check, req.db_keys)
    return {"status": "started"}

@app.get("/api/policy/status")
async def policy_status_endpoint():
    from services import policy_sync
    return policy_sync.POLICY_STATUS

async def _run_policy_audit(db_keys: List[str]):
    from services import policy_sync, ai_coordinator
    from services.policy_notifications import policy_result_message
    import notifier
    try:
        res = await policy_sync.run_initial_audit(db_keys)
        try:
            await notifier.send_google_chat_text(policy_result_message(res, audit=True))
        except Exception:
            pass
    except Exception as e:
        logger.error(f"정책 최초 감사 실패: {e}")
        policy_sync.AUDIT_STATUS.update(running=False, message=f"오류: {e}")
    finally:
        ai_coordinator.finish_job("policy_audit")
        policy_sync.release_maintenance_lock()

@app.post("/api/policy/audit-baseline")
async def policy_audit_endpoint(req: PolicyDbKeys, background_tasks: BackgroundTasks):
    """최초 감사: 베이스라인만 있고 아직 비교 안 된 정책을 저장된 원문으로 1회 AI 비교. 백그라운드."""
    await _acquire_policy_ai_job("policy_audit", "정책 최초 감사")
    _clear_completed_full_check_status()
    background_tasks.add_task(_run_policy_audit, req.db_keys)
    return {"status": "started"}

@app.get("/api/policy/audit-status")
async def policy_audit_status_endpoint():
    from services import policy_sync
    return policy_sync.AUDIT_STATUS

async def _run_policy_recompute(db_keys: List[str]):
    from services import policy_sync
    try:
        await policy_sync.recompute_diffs(db_keys)
    except Exception as e:
        logger.error(f"정책 진단 재계산 실패: {e}")
        policy_sync.RECOMPUTE_STATUS.update(running=False, message=f"오류: {e}")
    finally:
        policy_sync.release_maintenance_lock()

@app.post("/api/policy/recompute")
async def policy_recompute_endpoint(req: PolicyDbKeys, background_tasks: BackgroundTasks):
    """(B1+C2) AI 재호출 없이 저장된 추출값으로 대기 중인 진단을 최신 비교 로직으로 재계산. 백그라운드."""
    from services import policy_sync
    if not await policy_sync.try_acquire_maintenance_lock("진단 재계산"):
        raise HTTPException(status_code=409, detail=policy_sync.maintenance_busy_message())
    _clear_completed_full_check_status()
    background_tasks.add_task(_run_policy_recompute, req.db_keys)
    return {"status": "started"}

@app.get("/api/policy/recompute-status")
async def policy_recompute_status_endpoint():
    from services import policy_sync
    return policy_sync.RECOMPUTE_STATUS

@app.post("/api/policy/style-guide/generate")
async def policy_style_guide_generate_endpoint(req: PolicyDbKeys):
    """(T2) 기존 Notion 작성 내용을 분석해 작성 스타일 가이드를 1회 생성·저장."""
    from services import policy_sync, ai_coordinator
    await _acquire_policy_ai_job("policy_style_guide", "정책 스타일 가이드 생성")
    try:
        res = await policy_sync.generate_style_guide(req.db_keys)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("error", "생성 실패"))
        return res
    finally:
        ai_coordinator.finish_job("policy_style_guide")
        policy_sync.release_maintenance_lock()

async def _run_policy_restyle(db_keys: List[str]):
    from services import policy_sync, ai_coordinator
    try:
        await policy_sync.restyle_pending_diffs(db_keys)
    except Exception as e:
        logger.error(f"정책 스타일 변환 실패: {e}")
        policy_sync.STYLE_STATUS.update(running=False, message=f"오류: {e}")
    finally:
        ai_coordinator.finish_job("policy_restyle")
        policy_sync.release_maintenance_lock()

@app.post("/api/policy/restyle")
async def policy_restyle_endpoint(req: PolicyDbKeys, background_tasks: BackgroundTasks):
    """(T3) 감지된 변경의 제안값을 사용자 문체로 재작성. 백그라운드."""
    await _acquire_policy_ai_job("policy_restyle", "정책 스타일 재작성")
    _clear_completed_full_check_status()
    background_tasks.add_task(_run_policy_restyle, req.db_keys)
    return {"status": "started"}

@app.get("/api/policy/restyle-status")
async def policy_restyle_status_endpoint():
    from services import policy_sync
    return policy_sync.STYLE_STATUS

@app.get("/api/policy/changes")
async def policy_changes_endpoint(db_keys: Optional[str] = None):
    """변경 감지된 정책 목록 (중요순)."""
    from services import policy_sync
    keys = [k for k in (db_keys or "").split(",") if k]
    return {"items": await database.run_db(policy_sync.get_changes, keys)}

@app.get("/api/policy/reviews")
async def policy_reviews_endpoint(db_keys: Optional[str] = None,
                                  limit: int = Query(100, ge=1, le=200),
                                  offset: int = Query(0, ge=0),
                                  exclude_policy_ids: Optional[str] = None):
    """미해결 원문 보류 목록. 조회는 재점검·AI 요청·보류 해소를 하지 않는다."""
    from services import policy_sync
    keys = [key for key in (db_keys or '').split(',') if key]
    values = (exclude_policy_ids or '').split(',')
    if any(value and (not value.isdecimal() or len(value) > 16) for value in values) or len(values) > 5000:
        raise HTTPException(status_code=422, detail='정책 제외 목록의 형식이 올바르지 않습니다.')
    excluded = [int(value) for value in values if value]
    return await database.run_db(policy_sync.get_source_reviews, keys, limit, offset, excluded)


@app.get("/api/policy/candidates")
async def policy_candidates_endpoint(db_keys: Optional[str] = None):
    """어느 정책 DB에도 없는 신규 후보 (유사정책 ⚠️ 포함)."""
    from services import policy_sync
    keys = [k for k in (db_keys or "").split(",") if k]
    return {"items": await database.run_db(policy_sync.get_new_candidates, keys)}

@app.get("/api/policy/health")
async def policy_health_endpoint(db_keys: Optional[str] = None):
    """변경 감지 여부와 무관한 상시 상태 요약 (깨진 출처 링크·출처 URL 없음)."""
    from services import policy_sync
    keys = [k for k in (db_keys or "").split(",") if k]
    return await database.run_db(policy_sync.get_health_summary, keys)

@app.get("/api/policy/dbs")
async def policy_dbs_endpoint():
    """설정된 정책 DB 목록 — 이름은 Notion에서 동기화한 실제 제목(캐시), 없으면 기본값."""
    from services import policy_sync
    return {"dbs": await database.run_db(policy_sync.get_configured_policy_dbs)}


@app.get("/api/policy/disconnected-data")
async def policy_disconnected_data_endpoint():
    """연결 목록에서 빠졌지만 로컬에 남아 있는 정책 자료를 삭제 없이 조회한다."""
    from services import policy_sync
    return await database.run_db(policy_sync.get_disconnected_policy_data)


@app.post("/api/policy/disconnected-data/cleanup")
async def policy_disconnected_data_cleanup_endpoint():
    """사용자 확인 후 연결 해제된 로컬 정책 자료만 백업 뒤 정리한다."""
    from services import policy_sync
    if not await policy_sync.acquire_db_mutation_lock("연결 해제 자료 정리"):
        raise HTTPException(status_code=409, detail=policy_sync.maintenance_busy_message())
    try:
        return await database.run_db(policy_sync.cleanup_disconnected_policy_data)
    finally:
        policy_sync.release_maintenance_lock()

class PolicyIdReq(BaseModel):
    id: int

@app.post("/api/policy/confirm")
async def policy_confirm_endpoint(req: PolicyIdReq):
    """변경 확인 처리 (스냅샷 보존 후 상태 confirmed)."""
    from services import policy_sync
    # (2026-08-28) 필드 적용과 같은 정책별 락을 타야 서로 경합하지 않는다 — set_status_locked() 참조.
    res = await policy_sync.set_status_locked(req.id, "confirmed")
    if not res.get("ok"):
        code = 409 if res.get("error") == "MAINTENANCE_BUSY" else 404 if res.get("error") == "NOT_FOUND" else 500
        raise HTTPException(status_code=code, detail=res.get("message", "처리할 수 없습니다"))
    return {"ok": True}

@app.post("/api/policy/dismiss")
async def policy_dismiss_endpoint(req: PolicyIdReq):
    """변경 무시 (재점검 시 제외)."""
    from services import policy_sync
    res = await policy_sync.set_status_locked(req.id, "dismissed")
    if not res.get("ok"):
        code = 409 if res.get("error") == "MAINTENANCE_BUSY" else 404 if res.get("error") == "NOT_FOUND" else 500
        raise HTTPException(status_code=code, detail=res.get("message", "처리할 수 없습니다"))
    return {"ok": True}

class PolicyApplyFieldReq(BaseModel):
    id: int
    field: str
    force: bool = False
    force_semantic: bool = False

@app.post("/api/policy/apply-field")
async def policy_apply_field_endpoint(req: PolicyApplyFieldReq):
    """(U1) 감지된 변경 필드 1개를 Notion 페이지에 직접 반영 (PATCH).

    (B3) 다중값 필드가 기존보다 좁아지면 force=False에서는 거부되고 error="NARROWING"이 반환된다
    — 프론트가 사용자에게 확인시킨 뒤 force=True로 재요청해야 실제로 반영된다.
    (C1) error="STALE"이면 그 사이 Notion에서 직접 수정된 필드라 적용을 막은 것이다.
    (2026-08-07) error="SUSPICIOUS_VALUE"는 force가 아니라 force_semantic으로만 우회된다
    — 서로 무관한 안전장치를 하나의 확인창으로 한꺼번에 풀지 않기 위함(2차 리뷰).
    """
    from services import policy_sync
    res = await policy_sync.apply_field_to_notion(req.id, req.field, req.force, req.force_semantic)
    if not res.get("ok"):
        # (2026-08-28, ②) 유지보수 작업 진행 중이라 거부된 경우엔 409(충돌/잠김)가 더 정확하다.
        status_code = 409 if res.get("error") == "MAINTENANCE_BUSY" else 500 if res.get("error") == "LOCAL_SAVE_FAILED" else 400
        raise HTTPException(status_code=status_code, detail={
            "error": res.get("error", "반영 실패"),
            "message": res.get("message") or res.get("error", "반영 실패"),
        })
    return res

class PolicyUndoFieldReq(BaseModel):
    id: int
    field: str
    receipt: str


@app.post("/api/policy/undo-field")
async def policy_undo_field_endpoint(req: PolicyUndoFieldReq):
    from services import policy_sync
    result = await policy_sync.undo_policy_field(req.id, req.field, req.receipt)
    if not result.get('ok'):
        raise HTTPException(status_code=409, detail=result)
    return result


class PolicyFieldItem(BaseModel):
    id: int
    field: str

class PolicyApplyFieldBulkReq(BaseModel):
    items: List[PolicyFieldItem]

@app.post("/api/policy/apply-field-bulk")
async def policy_apply_field_bulk_endpoint(req: PolicyApplyFieldBulkReq):
    """(2026-07-20) 사용자가 이미 개별 검토·체크한 필드들을 한 번에 반영.

    force는 절대 쓰지 않는다 — 축소(NARROWING)·과다팽창(BLOAT) 등 안전장치에 걸리는
    필드는 자동으로 건너뛰고 사유와 함께 보고한다. 일괄 처리라고 해서 사람이 직접
    확인해야 하는 케이스까지 강제로 밀어붙이면 안 되기 때문(이 앱 전체의 설계 원칙).
    """
    from services import policy_sync
    applied, skipped, failed = 0, [], []
    seen = set()
    for index, item in enumerate(req.items):
        key = (item.id, item.field)
        if key in seen:
            skipped.append({"id": item.id, "field": item.field, "reason": "DUPLICATE",
                            "message": "같은 항목이 중복 선택되어 한 번만 처리했습니다."})
            continue
        seen.add(key)
        if policy_sync.requires_individual_review(item.field):
            skipped.append({"id": item.id, "field": item.field,
                            "reason": "INDIVIDUAL_REVIEW_REQUIRED",
                            "message": "조건·혜택의 원문 대조가 필요합니다. 해당 카드에서 개별 반영해주세요."})
            continue
        try:
            res = await policy_sync.apply_field_to_notion(item.id, item.field, force=False)
        except Exception:
            # The remote write may have succeeded before its response failed.
            # Do not retry it automatically or lose earlier successful results.
            failed.append({"id": item.id, "field": item.field, "error": "UPDATE_UNCERTAIN",
                           "message": "반영 여부를 확인하지 못했습니다. Notion의 현재 내용을 먼저 확인해주세요."})
            skipped.extend({"id": pending.id, "field": pending.field,
                            "reason": "NOT_ATTEMPTED",
                            "message": "앞선 항목의 결과를 확인하지 못해 요청하지 않았습니다."}
                           for pending in req.items[index + 1:])
            break
        if res.get("ok"):
            applied += 1
        elif res.get("error") in ("UPDATE_UNCERTAIN", "LOCAL_SAVE_FAILED", "APPLY_RESULT_UNCERTAIN"):
            failed.append({"id": item.id, "field": item.field, "error": res.get("error"),
                           "message": res.get("message") or "Notion 반영 여부를 확인하지 못했습니다. 현재 내용을 먼저 확인해주세요."})
            skipped.extend({"id": pending.id, "field": pending.field,
                            "reason": "NOT_ATTEMPTED",
                            "message": "앞선 항목의 반영 여부를 확인하지 못해 요청하지 않았습니다."}
                           for pending in req.items[index + 1:])
            break
        elif res.get("error") in ("NARROWING", "BLOAT", "SUSPICIOUS_VALUE", "NEEDS_LINKED_REVIEW", "NEEDS_SOURCE_REVIEW"):
            skipped.append({"id": item.id, "field": item.field, "reason": res.get("error"),
                            "message": res.get("message")})
        elif res.get("error") == "MAINTENANCE_BUSY":
            # (2026-08-28, ②) 유지보수 작업이 루프 도중 시작되면 이후 항목은 실패로 몰지 않고
            # 그냥 중단한다 — 남은 항목은 다음에 다시 시도하면 된다.
            skipped.extend({"id": pending.id, "field": pending.field,
                            "reason": "MAINTENANCE_BUSY",
                            "message": "다른 정책 작업이 진행 중이어서 반영하지 않았습니다. 작업이 끝난 뒤 다시 확인해주세요."}
                           for pending in req.items[index:])
            break
        else:
            failed.append({"id": item.id, "field": item.field, "error": res.get("error"),
                           "message": res.get("message") or res.get("error")})
    return {"ok": True, "applied": applied, "skipped": skipped, "failed": failed}

class PolicyBulkReq(BaseModel):
    ids: List[int]

@app.post("/api/policy/confirm-bulk")
async def policy_confirm_bulk_endpoint(req: PolicyBulkReq):
    """(S1) 선택한 여러 건을 한 번에 확인 처리."""
    from services import policy_sync
    done = 0
    for _id in req.ids:
        res = await policy_sync.set_status_locked(_id, "confirmed")
        # (2026-08-28) 유지보수 작업이 루프 도중 시작되면 이후 건은 건너뛴다 — 이미 처리된
        # 건까지 실패로 되돌리지 않고, 처리된 개수만 정확히 보고한다.
        if not res.get("ok"):
            break
        done += 1
    return {"ok": True, "count": done, "total": len(req.ids)}

@app.post("/api/policy/dismiss-bulk")
async def policy_dismiss_bulk_endpoint(req: PolicyBulkReq):
    """(S1) 선택한 여러 건을 한 번에 무시 처리."""
    from services import policy_sync
    done = 0
    for _id in req.ids:
        res = await policy_sync.set_status_locked(_id, "dismissed")
        if not res.get("ok"):
            break
        done += 1
    return {"ok": True, "count": done, "total": len(req.ids)}

class PolicyDraftReq(BaseModel):
    item_id: int
    db_key: str


class PolicyUrlDraftPreviewReq(BaseModel):
    url: str
    db_key: str
    title: Optional[str] = None
    privacy_reviewed: StrictBool = False


class PolicyUrlDraftCreateReq(BaseModel):
    url: str
    db_key: str
    fields: Dict[str, Any]
    source_review_confirmed: StrictBool = False


async def _validate_public_policy_url(raw_url: str) -> str:
    """URL 직접 초안이 로컬·사설망 주소를 읽지 못하게 하는 최소 SSRF 방어."""
    value = str(raw_url or "").strip()
    if len(value) > 2048:
        raise HTTPException(status_code=400, detail="웹주소가 너무 깁니다. 2,048자 이하 주소를 입력해주세요.")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        raise HTTPException(status_code=400, detail="주소의 포트 형식이 올바르지 않습니다.")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="http 또는 https로 시작하는 전체 주소를 입력해주세요.")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="로그인 정보가 포함된 주소는 사용할 수 없습니다.")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise HTTPException(status_code=400, detail="내 컴퓨터나 내부망 주소는 사용할 수 없습니다.")
    if port is not None and not (1 <= port <= 65535):
        raise HTTPException(status_code=400, detail="주소의 포트 범위를 확인해주세요.")

    def _resolve():
        return socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80),
                                  type=socket.SOCK_STREAM)
    try:
        infos = await asyncio.wait_for(asyncio.to_thread(_resolve), timeout=5)
    except Exception:
        raise HTTPException(status_code=400, detail="주소의 서버를 찾을 수 없습니다. 주소를 다시 확인해주세요.")
    addresses = {item[4][0].split("%", 1)[0] for item in infos if item[4]}
    if not addresses:
        raise HTTPException(status_code=400, detail="주소의 서버를 찾을 수 없습니다.")
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_global:
                raise HTTPException(status_code=400, detail="내 컴퓨터나 내부망 주소는 사용할 수 없습니다.")
        except ValueError:
            raise HTTPException(status_code=400, detail="주소의 서버 정보를 확인할 수 없습니다.")
    # 조각 식별자는 서버 요청에 쓰이지 않으므로 제거해 중복 판정을 안정화한다.
    return parsed._replace(fragment="").geturl()


def _fallback_policy_title(url: str) -> str:
    path = unquote(urlparse(url).path or "").rstrip("/")
    name = path.rsplit("/", 1)[-1]
    name = re.sub(r"\.(?:html?|php|asp|aspx|jsp|pdf|png|jpe?g)$", "", name, flags=re.I)
    return re.sub(r"[-_]+", " ", name).strip()[:200]


def _sanitize_policy_draft_fields(raw: Dict[str, Any], url: str,
                                  fallback_title: str = "") -> Dict[str, Any]:
    """클라이언트 미리보기 값을 Notion 쓰기 전 허용 필드·자료형으로 다시 제한한다."""
    raw = raw if isinstance(raw, dict) else {}
    fields: Dict[str, Any] = {
        "title": str(raw.get("title") or fallback_title or "").strip()[:200],
    }
    from services.policy_source_plan import MAX_POLICY_SOURCE_CHARS
    for key in ("category", "contact", "support_detail", "age_detail", "target_income", "eligibility_detail"):
        value = str(raw.get(key) or "")
        prose = key in ("support_detail", "age_detail", "target_income", "eligibility_detail")
        limit = MAX_POLICY_SOURCE_CHARS if prose else 5000
        if len(value) > limit:
            # The same helper runs at preview and immediately before creation.
            # Never silently drop a tail condition after evidence verification.
            raise HTTPException(status_code=422, detail={
                'error': 'POLICY_DRAFT_FIELD_TOO_LONG',
                'message': f'정책 내용이 저장 가능한 길이({limit:,}자)를 넘었습니다. 내용을 줄이거나 상세 출처를 확인해주세요. 아직 등록하지 않았습니다.',
                'field': key,
            })
        fields[key] = value if prose else value.strip()
    for key in ("residence", "target_characteristics", "life_cycle"):
        value = raw.get(key) or []
        values = value if isinstance(value, list) else str(value).split(",")
        clean = []
        for item in values:
            item = str(item or "").strip()[:100]
            if item and item not in clean:
                clean.append(item)
        fields[key] = clean
    for key in ("start_age_months", "end_age_months"):
        value = raw.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            fields[key] = None
            continue
        try:
            import math
            number = float(value)
            if isinstance(value, bool) or not math.isfinite(number) or not number.is_integer() or not 0 <= number <= 1800:
                raise ValueError('Invalid age')
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(status_code=422, detail={
                'error': 'POLICY_DRAFT_INVALID_AGE',
                'message': '월령은 0~1800 사이의 정수로 입력하거나, 알 수 없으면 비워주세요. 아직 등록하지 않았습니다.',
                'field': key,
            })
        fields[key] = int(number)
    start, end = fields['start_age_months'], fields['end_age_months']
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail={
            'error': 'POLICY_DRAFT_INVALID_AGE_RANGE',
            'message': '시작 월령은 종료 월령보다 클 수 없습니다. 원문을 확인해 수정해주세요. 아직 등록하지 않았습니다.',
        })
    fields["_source_urls"] = [url]
    return fields


def _policy_draft_source_review(raw: Any = None) -> Dict[str, Any]:
    """Keep OCR review metadata separate from writable policy fields.

    A model transcription is a review aid, never independently verified source
    evidence. Missing/oversized text must not look like a complete transcript.
    """
    raw = raw if isinstance(raw, dict) else {}
    transcript = raw.get("source_text")
    valid_text = isinstance(transcript, str) and 0 < len(transcript.strip()) <= 30000
    reasons = raw.get("reasons")
    reasons = [value for value in reasons[:30]
               if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_:-]{1,100}", value)] if isinstance(reasons, list) else []
    if not valid_text:
        reasons.append("transcription_unavailable")
    allowed = {"title", "category", "target", "age_detail", "target_income", "eligibility_detail",
               "contact", "support_detail", "residence", "target_characteristics",
               "life_cycle", "start_age_months", "end_age_months", "event_date",
               "event_end_date", "deadline", "related_urls", "registration_start_date", "registration_end_date", "cost"}
    unverified = raw.get("unverified_fields")
    clean = {}
    if isinstance(unverified, dict):
        for key, value in unverified.items():
            if key not in allowed:
                continue
            if isinstance(value, str):
                clean[key] = value[:5000]
            elif isinstance(value, list):
                clean[key] = [item[:500] for item in value[:30] if isinstance(item, str)]
            elif value is None or type(value) in (int, bool):
                clean[key] = value
    from services import page_coverage
    coverage_note = page_coverage.normalize({'reasons': reasons}).get('note', '')
    return {"required": True, "source_text": transcript if valid_text else "",
            "source_complete": raw.get("source_complete") is True and valid_text,
            "reasons": list(dict.fromkeys(reasons)), "unverified_fields": clean,
            "note": (coverage_note + ' ' if coverage_note else '') + "AI가 읽은 문장도 틀리거나 빠질 수 있습니다. 원본에서 비용·대상·접수 기간과 이용 기간·연락처를 대조해주세요."}


async def _policy_draft_target_schema(db: Dict[str, Any]) -> Dict[str, Any]:
    """초안 생성 직전 실제 Notion 선택지를 읽고 로컬 캐시도 갱신한다."""
    from services import notion_service
    import aiohttp
    token, _ = notion_service.get_credentials()
    if not token:
        raise HTTPException(status_code=409, detail="Notion 연결 토큰을 먼저 설정해주세요.")
    async with aiohttp.ClientSession() as session:
        target = await notion_service.resolve_target(session, token, db["data_source_id"])
        if not target.get("ok"):
            raise HTTPException(status_code=409, detail=f"정책 보관함 연결 확인 필요: {target.get('error', '')}")
        schema_result = await notion_service.get_policy_source_schema(session, token, target)
    if not schema_result.get("ok"):
        raise HTTPException(status_code=409, detail=f"정책 보관함 속성 확인 필요: {schema_result.get('error', '')}")
    missing = schema_result.get("missing_properties") or []
    mismatches = schema_result.get("type_mismatches") or []
    if missing:
        preview = ", ".join(str(name) for name in missing[:4])
        suffix = f" 외 {len(missing) - 4}개" if len(missing) > 4 else ""
        raise HTTPException(
            status_code=409,
            detail=f"정책 보관함에 필요한 속성이 없습니다: {preview}{suffix}. 설정의 Notion 연결 확인에서 안내를 확인해주세요.",
        )
    if mismatches:
        preview = ", ".join(str(item.get("name") or "") for item in mismatches[:4])
        raise HTTPException(
            status_code=409,
            detail=f"정책 보관함 속성 종류가 맞지 않습니다: {preview}. 설정의 Notion 연결 확인에서 안내를 확인해주세요.",
        )
    schema = schema_result["schema"]
    await database.run_db(
        database.execute_write,
        "INSERT INTO policy_db_schema (db_key, schema_json, updated_at) VALUES (?,?,CURRENT_TIMESTAMP) "
        "ON CONFLICT(db_key) DO UPDATE SET schema_json=excluded.schema_json, updated_at=CURRENT_TIMESTAMP",
        (db["key"], json.dumps(schema, ensure_ascii=False)), raise_on_error=True)
    return schema


async def _policy_draft_state_db(function, *args, **kwargs):
    def work():
        with database.get_connection() as conn:
            return function(conn, *args, **kwargs)
    return await database.run_db(work)


async def _create_policy_draft(db: Dict[str, Any], fields: Dict[str, Any]) -> Dict[str, Any]:
    """중복 확인부터 Notion 생성·로컬 영수증 기록까지 하나의 정책 변경 락에서 처리한다."""
    from services import notion_service, policy_sync, policy_draft_state as receipts
    from services.news_analysis_worker import _settle_db
    if not await policy_sync.acquire_policy_mutation_lock("정책 초안 생성"):
        raise HTTPException(status_code=409, detail=policy_sync.maintenance_busy_message())
    try:
        source_url = _policy_draft_recovery_url((fields.get("_source_urls") or [""])[0])
        fields = dict(fields, _source_urls=[source_url, *(fields.get('_source_urls') or [])[1:]])
        previous = await _policy_draft_state_db(receipts.lookup, source_url)
        if previous:
            if previous['page_id']:
                return {"ok": True, "page_id": previous['page_id'], "already_created": True,
                        "deeplink": notion_service.page_deeplink(previous['page_id']),
                        "warning": "이 주소의 초안은 이미 Notion에 추가했습니다. 다시 만들지 않았습니다."}
            raise HTTPException(409, "이 주소의 이전 초안 등록 결과를 확인해야 합니다. 'Notion 등록 상태 확인'을 눌러주세요. 중복 방지를 위해 다시 전송하지 않았습니다.")
        if await database.run_db(policy_sync.policy_source_url_exists, source_url):
            raise HTTPException(status_code=409, detail="이미 정책 보관함에 등록된 주소입니다.")
        schema = await _policy_draft_target_schema(db)
        policy_sync.normalize_draft_fields(fields, db["key"], schema)
        if not str(fields.get("title") or "").strip():
            raise HTTPException(status_code=400, detail="정책명을 입력해주세요.")
        receipt, cancelled = await _settle_db(_policy_draft_state_db(receipts.claim, source_url, db, fields))
        if cancelled:
            if receipt['claimed']:
                await _settle_db(_policy_draft_state_db(receipts.release_rejected, source_url, receipt['attempt_id']))
            raise asyncio.CancelledError
        if not receipt['claimed']:
            raise HTTPException(409, "같은 주소의 초안 등록이 이미 진행되었습니다. Notion 등록 상태를 확인해주세요.")
        # The durable receipt survives cancellation, response loss and local
        # persistence failures. No automatic retries of the create request.
        res = await notion_service.create_policy_page(fields, db["data_source_id"])
        if not res.get("ok"):
            if res.get('uncertain') is False:
                _, cancelled = await _settle_db(_policy_draft_state_db(
                    receipts.release_rejected, source_url, receipt['attempt_id']))
                if cancelled:
                    raise asyncio.CancelledError
                if res.get('request_sent') is False and res.get('error') in {
                        'NOTION_PAYLOAD_TOO_LARGE', 'NOTION_PAYLOAD_INVALID'}:
                    raise HTTPException(422, res.get('message') or
                        '저장할 내용의 길이와 형식을 확인해주세요. Notion에는 전송하지 않았습니다.')
                raise HTTPException(409, "Notion이 초안 등록을 거절했습니다. 연결·권한·입력 내용을 확인한 뒤 다시 시도할 수 있습니다.")
            raise HTTPException(409, "Notion 초안 등록 결과를 확인하지 못했습니다. 'Notion 등록 상태 확인'으로 생성 여부를 확인해주세요. 자동으로 다시 전송하지 않습니다.")
        if not isinstance(res.get('page_id'), str) or not res['page_id'].strip():
            raise HTTPException(409, "Notion 초안 등록 결과의 페이지 정보가 없습니다. Notion 등록 상태를 확인해주세요.")
        warning = ""
        try:
            _, cancelled = await _settle_db(_policy_draft_state_db(receipts.complete,
                source_url, receipt['attempt_id'], res['page_id']))
            if cancelled:
                raise asyncio.CancelledError
            _, cancelled = await _settle_db(database.run_db(
                policy_sync.record_created_policy, db["key"], res["page_id"], fields))
            if cancelled:
                raise asyncio.CancelledError
        except Exception:
            logger.error("Notion 초안 생성 후 로컬 영수증 또는 미러 기록 실패")
            warning = "Notion 초안은 만들었지만 목록 갱신에 실패했습니다. 다음 동기화 때 자동으로 표시됩니다."
        return {"ok": True, "page_id": res["page_id"],
                "deeplink": notion_service.page_deeplink(res["page_id"]),
                "warning": warning}
    finally:
        policy_sync.release_maintenance_lock()


class PolicyDraftStatusReq(BaseModel):
    url: str


class PolicyDraftRetryReq(PolicyDraftStatusReq):
    attempt_id: str
    confirmed: Any


def _policy_draft_recovery_url(raw):
    # Recovery only queries Notion. Do not require the original public site to
    # still resolve or be reachable merely to check an earlier create receipt.
    value = str(raw or '').strip()
    try:
        parsed = urlparse(value)
        valid = parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password
    except ValueError:
        valid = False
    if len(value) > 10000 or not valid:
        raise HTTPException(400, "확인할 원문 주소를 입력해주세요.")
    return parsed._replace(fragment='').geturl()


async def _check_policy_draft_receipt(attempt):
    from services import policy_draft_state as receipts, notion_service, policy_sync
    from services.news_analysis_worker import _settle_db
    import aiohttp
    token, _ = notion_service.get_credentials()
    result = {'state': 'unavailable'}
    if token:
        try:
            async with aiohttp.ClientSession() as session:
                result = await receipts.find_remote(attempt, session=session, token=token, notion=notion_service)
        except Exception:
            result = {'state': 'unavailable'}
    if result['state'] == 'completed':
        _, cancelled = await _settle_db(_policy_draft_state_db(receipts.complete,
            attempt['source_url'], attempt['attempt_id'], result['page_id']))
        if cancelled:
            raise asyncio.CancelledError
        try:
            _, cancelled = await _settle_db(database.run_db(policy_sync.record_created_policy,
                attempt['db_key'], result['page_id'], result['fields']))
            if cancelled:
                raise asyncio.CancelledError
        except Exception:
            result['warning'] = 'Notion에서 생성된 페이지를 확인했습니다. 정책 목록은 다음 동기화 때 갱신됩니다.'
    return result


@app.post('/api/policy/draft-from-url/status')
async def policy_draft_status_endpoint(req: PolicyDraftStatusReq):
    from services import policy_draft_state as receipts, policy_sync, notion_service
    url = _policy_draft_recovery_url(req.url)
    if not await policy_sync.acquire_policy_mutation_lock('정책 초안 등록 상태 확인'):
        raise HTTPException(409, policy_sync.maintenance_busy_message())
    try:
        attempt = await _policy_draft_state_db(receipts.lookup, url)
        if not attempt:
            return {'state': 'clear', 'message': '이 주소로 진행한 초안 등록 기록이 없습니다.', 'can_retry': False}
        result = await _check_policy_draft_receipt(attempt)
        messages = {
            'completed': 'Notion에 등록된 페이지를 확인했습니다. 다시 생성하지 않습니다.',
            'not_found': '원래 정책표에서 같은 출처 주소를 찾지 못했습니다. Notion에서도 미등록 상태를 직접 확인한 뒤 재시도를 허용해주세요.',
            'multiple': '같은 출처 주소의 페이지가 여러 개 있습니다. Notion에서 내용을 확인해주세요. 다시 생성하지 않습니다.',
            'unavailable': '원래 정책표를 조회하지 못했습니다. 연결을 확인하거나 Notion에서 등록 여부를 직접 확인해주세요.',
        }
        page_id = result.get('page_id') or attempt.get('page_id')
        target_id = notion_service.normalize_notion_id(attempt['source_id'])
        return {'state': result['state'], 'attempt_id': attempt['attempt_id'],
                'message': result.get('warning') or messages[result['state']],
                'db_key': attempt['db_key'], 'source_url': attempt['source_url'],
                'target_name': attempt.get('db_name') or attempt['db_key'],
                'target_deeplink': notion_service.page_deeplink(target_id) if re.fullmatch(r'[0-9a-fA-F]{32}', target_id) else '',
                'deeplink': notion_service.page_deeplink(page_id) if page_id else '',
                'can_retry': result['state'] == 'not_found' or
                    (result['state'] == 'unavailable' and not attempt.get('page_id'))}
    finally:
        policy_sync.release_maintenance_lock()


@app.post('/api/policy/draft-from-url/allow-retry')
async def policy_draft_allow_retry_endpoint(req: PolicyDraftRetryReq):
    from services import policy_draft_state as receipts, policy_sync
    from services.news_analysis_worker import _settle_db
    url = _policy_draft_recovery_url(req.url)
    if req.confirmed is not True:
        raise HTTPException(400, 'Notion에 등록되지 않았음을 직접 확인한 뒤 진행해주세요.')
    if not await policy_sync.acquire_policy_mutation_lock('정책 초안 재시도 확인'):
        raise HTTPException(409, policy_sync.maintenance_busy_message())
    try:
        attempt = await _policy_draft_state_db(receipts.lookup, url)
        if not attempt or attempt['attempt_id'] != req.attempt_id:
            raise HTTPException(409, '등록 기록이 바뀌었습니다. 상태를 다시 확인해주세요.')
        checked = await _check_policy_draft_receipt(attempt)
        if checked['state'] in ('completed', 'multiple') or (attempt.get('page_id') and checked['state'] != 'not_found'):
            raise HTTPException(409, '등록된 페이지가 있거나 기존 등록 상태를 확인하지 못했습니다. Notion에서 먼저 확인해주세요.')
        if await database.run_db(policy_sync.policy_source_url_exists, url):
            raise HTTPException(409, '연결된 정책 목록에 같은 출처가 있습니다. Notion에서 삭제했다면 정책표를 동기화한 뒤 다시 확인해주세요.')
        _, cancelled = await _settle_db(_policy_draft_state_db(receipts.allow_retry,
            url, req.attempt_id, confirmed=True))
        if cancelled:
            raise asyncio.CancelledError
        return {'ok': True, 'message': '재시도를 허용했습니다. 내용을 확인한 뒤 추가 버튼을 눌러주세요. 아직 Notion에 전송하지 않았습니다.'}
    finally:
        policy_sync.release_maintenance_lock()


@app.post("/api/policy/draft-from-url/preview")
async def policy_url_draft_preview_endpoint(req: PolicyUrlDraftPreviewReq):
    """공개 URL을 읽어 정책 필드 미리보기만 만든다. 이 단계에서는 Notion을 수정하지 않는다."""
    from services import notion_service, policy_sync, ai_coordinator
    import aiohttp
    import ai_classifier
    from scraper_engine import ScraperEngine

    url = await _validate_public_policy_url(req.url)
    from services import ocr_service
    def require_document_review():
        if req.privacy_reviewed is not True:
            raise HTTPException(status_code=409, detail={
                'error': ocr_service.DOCUMENT_REVIEW_CODE,
                'message': ocr_service.DOCUMENT_REVIEW_MESSAGE,
            })
    path_lower = urlparse(url).path.lower()
    document_mime = next((mime for suffix, mime in (
        ('.pdf', 'application/pdf'), ('.png', 'image/png'), ('.jpg', 'image/jpeg'),
        ('.jpeg', 'image/jpeg'), ('.webp', 'image/webp'),
    ) if path_lower.endswith(suffix)), '')
    if document_mime:
        require_document_review()
    db = notion_service.get_policy_db(req.db_key)
    if not db:
        raise HTTPException(status_code=400, detail="저장할 정책 보관함을 선택해주세요.")
    if await database.run_db(policy_sync.policy_source_url_exists, url):
        raise HTTPException(status_code=409, detail="이미 정책 보관함에 등록된 주소입니다.")
    await _acquire_policy_ai_job("policy_url_draft", "URL 정책 초안 분석")
    engine = None
    try:
        schema = await _policy_draft_target_schema(db)
        # 확장자만으로 문서가 확실하면 웹 본문용으로 한 번 받은 뒤 OCR용으로 다시 받는
        # 중복 다운로드를 생략한다. 확장자가 없는 다운로드 주소는 Content-Type 확인을
        # 위해 기존 정적 fetch를 사용한다.
        source = {
            "text": "", "title": "", "content_type": document_mime,
            "final_url": url,
        }
        if not document_mime:
            connector = aiohttp.TCPConnector(ssl=policy_sync._relaxed_ssl_context())
            async with aiohttp.ClientSession(connector=connector) as session:
                source = await ai_classifier.fetch_page_content_async(
                    session, url, url_validator=_validate_public_policy_url)
        content = str(source.get("draft_text") or source.get("policy_text") or source.get("text") or "")
        content_type = str(source.get("content_type") or "").lower()
        is_document = bool(document_mime or "application/pdf" in content_type or "image/" in content_type)
        if not is_document and len(content.strip()) < 300:
            try:
                engine = ScraperEngine(headless=True)
                await engine.start()
                rendered = await engine.fetch_rendered_text(
                    url, url_validator=_validate_public_policy_url, policy_draft=True)
                if isinstance(rendered, dict):
                    if rendered.get('draft_source_failed'):
                        raise HTTPException(409, '정책 상세 본문을 모두 읽지 못했습니다. 원문이 정상적으로 열리는지 확인해주세요.')
                    rendered_text = str(rendered.get('draft_text') or rendered.get('policy_text') or rendered.get('text') or '')
                    if len(rendered_text) > len(content):
                        content = rendered_text
                        source.update(rendered)
                elif len(rendered) > len(content):
                    content = rendered
            except HTTPException:
                raise
            except Exception as exc:
                logger.info(f"URL 초안 렌더 본문 보강 생략: {exc}")

        title = str(req.title or source.get("draft_title") or source.get("title") or _fallback_policy_title(url)).strip()[:200]
        fields = None
        analyzed_from = "web"
        source_review = _policy_draft_source_review()
        if is_document:
            require_document_review()
            ocr = await ocr_service.analyze_image_or_pdf(
                url,
                content_type.split(";", 1)[0] or "application/pdf",
                url_validator=_validate_public_policy_url,
                taxonomy=schema,
                privacy_reviewed=req.privacy_reviewed,
            )
            if not isinstance(ocr, dict):
                raise HTTPException(status_code=503, detail="문서 AI 응답 형식을 확인하지 못했습니다. 잠시 후 다시 시도해주세요.")
            if ocr.get("error_code") == "OCR_SOURCE_REVIEW_REQUIRED":
                raise HTTPException(status_code=409, detail={
                    "error": "OCR_SOURCE_REVIEW_REQUIRED",
                    "message": "문서에서 대조 가능한 전사문을 확보하지 못했습니다. 원본을 직접 확인해주세요. 자동 등록하지 않았습니다.",
                })
            if not ocr or ocr.get("error"):
                error = str((ocr or {}).get("error") or "문서 분석 실패")
                if ocr.get('error_code') in ('AI_DOCUMENT_INPUT_COUNT_UNAVAILABLE', 'AI_DOCUMENT_INPUT_TOO_LARGE'):
                    raise HTTPException(status_code=409, detail={
                        'error': ocr['error_code'], 'message': error,
                    })
                if "API Key missing" in error:
                    raise HTTPException(status_code=409, detail="Gemini API 키를 먼저 설정해주세요.")
                if "timeout" in error.lower():
                    raise HTTPException(status_code=504, detail="문서 분석 시간이 초과되었습니다. 잠시 후 다시 시도해주세요.")
                if "Download failed" in error:
                    raise HTTPException(
                        status_code=422,
                        detail="PDF·이미지 파일을 내려받지 못했습니다. 공개 파일인지 또는 용량이 너무 크지 않은지 확인해주세요.")
                raise HTTPException(status_code=503, detail="문서 AI 분석을 완료하지 못했습니다. 잠시 후 다시 시도해주세요.")
            if type(ocr.get("is_relevant")) is not bool:
                raise HTTPException(status_code=503, detail="문서 AI 응답 형식을 확인하지 못했습니다. 잠시 후 다시 시도해주세요.")
            if ocr.get("is_relevant") is not True:
                raise HTTPException(status_code=422, detail="이 문서에서 정책 정보를 찾지 못했습니다.")
            fields = dict(ocr)
            source_review = _policy_draft_source_review(ocr.get("_ocr_review"))
            title = str(req.title or ocr.get("title") or title).strip()[:200]
            analyzed_from = "document"
        if fields is None:
            if len(content.strip()) < 80:
                raise HTTPException(
                    status_code=422,
                    detail="이 주소에서 정책 내용을 읽지 못했습니다. 공개된 상세 페이지 주소인지 확인해주세요.")
            if not title:
                raise HTTPException(status_code=422, detail="정책명을 자동으로 찾지 못했습니다. 정책명을 함께 입력해주세요.")
            client = ai_classifier.get_client()
            if not client:
                raise HTTPException(status_code=409, detail="Gemini API 키를 먼저 설정해주세요.")
            # A new URL draft has no existing policy baseline. On real detail
            # pages the title and 이용대상/신청방법 can be sibling headings;
            # slicing by title silently discards those essential conditions.
            # Keep the complete extracted body. Size/privacy guards still apply.
            snippet = content
            extraction_review = {}
            fields = await ai_classifier.extract_policy_fields_async(
                client, title, url, content=snippet, taxonomy=schema,
                review=extraction_review, draft_preview=True)
            if not fields and extraction_review:
                # A source/evidence hold is not a temporary provider outage.
                note = str(extraction_review.get('note') or '원문의 조건을 확인해주세요.')
                note = note.replace('기존 내용은 유지됩니다.', '').strip()
                raise HTTPException(status_code=409, detail={
                    'error': 'POLICY_DRAFT_REVIEW_REQUIRED',
                    'message': note,
                    'review_code': extraction_review.get('code'),
                })
            from services import page_coverage
            coverage = page_coverage.normalize(source.get('source_page'))
            review_reasons = list(coverage.get('reasons', []))
            if extraction_review.get('code'):
                review_reasons.append(extraction_review['code'])
            source_review = _policy_draft_source_review({
                'source_text': snippet,
                'source_complete': False,  # Extracted text is not the entire original page.
                'reasons': review_reasons,
                'unverified_fields': {key: None for key in extraction_review.get('fields', [])},
            })
        if not fields:
            raise HTTPException(status_code=503, detail="AI 분석을 완료하지 못했습니다. 잠시 후 다시 시도해주세요.")

        fields = _sanitize_policy_draft_fields(fields, url, title)
        policy_sync.normalize_draft_fields(fields, db["key"], schema)
        labels = {key: name for key, name, _type in notion_service.get_policy_props()}
        missing = []
        for key, name, _type in notion_service.get_policy_props():
            value = fields.get(key)
            if value in (None, "", []):
                missing.append(name)
        options = {key: list(schema.get(key) or [])
                   for key in ("category", "residence", "target_characteristics", "life_cycle")}
        return {"ok": True, "url": url, "db_key": db["key"],
                "db_name": schema.get("_title") or db.get("name") or db["key"],
                "fields": fields, "options": options, "missing_fields": missing,
                "source_chars": len(content), "analyzed_from": analyzed_from,
                "field_labels": labels, "source_review": source_review}
    finally:
        if engine is not None:
            try:
                await engine.close()
            except Exception as exc:
                logger.warning(f"URL 초안 렌더러 종료 실패: {exc}")
        ai_coordinator.finish_job("policy_url_draft")
        policy_sync.release_maintenance_lock()


@app.post("/api/policy/draft-from-url/create")
async def policy_url_draft_create_endpoint(req: PolicyUrlDraftCreateReq):
    """사용자가 미리보기를 확인한 뒤에만 Notion 초안을 실제 생성한다."""
    if req.source_review_confirmed is not True:
        raise HTTPException(status_code=409, detail={
            "error": "SOURCE_REVIEW_CONFIRMATION_REQUIRED",
            "message": "원문에서 비용·대상·접수 기간과 이용 기간·연락처를 대조한 뒤 확인해주세요.",
        })
    from services import notion_service
    url = await _validate_public_policy_url(req.url)
    db = notion_service.get_policy_db(req.db_key)
    if not db:
        raise HTTPException(status_code=400, detail="저장할 정책 보관함을 선택해주세요.")
    fields = _sanitize_policy_draft_fields(req.fields, url)
    return await _create_policy_draft(db, fields)

@app.post("/api/policy/draft")
async def policy_draft_endpoint(req: PolicyDraftReq):
    """신규 후보(welfare_item)를 지정 정책 DB에 구조화 초안 페이지로 생성.

    연결된 보관함 간 링크 중복 방지: 해당 링크가 이미 어느 정책에든 등재됐으면 차단.
    """
    from services import notion_service, policy_sync
    db = notion_service.get_policy_db(req.db_key)
    if not db:
        raise HTTPException(status_code=400, detail="알 수 없는 정책 DB")

    def _load():
        r = database.execute_query(
            "SELECT * FROM welfare_items WHERE id=?", (req.item_id,), fetch_one=True)
        return dict(r) if r else None
    item = await database.run_db(_load)
    if not item:
        raise HTTPException(status_code=404, detail="항목 없음")

    ai = history_manager._parse_ai_data(item.get("ai_data")) or {}
    fields = {
        "title": notion_service.clean_title(item.get("title", "")),
        "category": db.get("category") or ai.get("category", ""),
        "residence": ai.get("residence", []),
        "target_characteristics": ai.get("target_characteristics", []),
        "life_cycle": ai.get("life_cycle", []),
        "start_age_months": ai.get("start_age_months"),
        "end_age_months": ai.get("end_age_months"),
        "age_detail": ai.get("age_detail", ""),
        "target_income": ai.get("target_income", ""),
        "eligibility_detail": ai.get("eligibility_detail", ""),
        "support_detail": item.get("support_detail", ""),
        "contact": item.get("contact", ""),
        "_source_urls": [item.get("link", "")],
    }
    return await _create_policy_draft(db, fields)

# --- Calendar, Search, Reports ---


def _search_display_results(results):
    """Add read-only news prose without changing search text, scores or policy rows."""
    from services.news_display_projection import is_policy_item, item_presentation
    rows = results.get('items', [])
    if not rows or all(is_policy_item(item) for item in rows):
        return results
    with database.get_connection() as conn:
        displayed = [dict(item, news_presentation=item_presentation(item, conn=conn))
                     if not is_policy_item(item) else dict(item) for item in rows]
    return dict(results, items=displayed)


def _search_items_with_presentation(*args, **kwargs):
    return _search_display_results(history_manager.search_items(*args, **kwargs))


def _hybrid_search_with_presentation(*args, **kwargs):
    return _search_display_results(history_manager.hybrid_search(*args, **kwargs))

@app.get("/api/search")
async def search_welfare(
    q: Optional[str] = Query(None, min_length=1),
    category: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    active_only: bool = Query(False),
    include_archived: bool = Query(False)
):
    """
    Advanced search with filters and pagination.
    active_only: 마감일 경과 항목 제외
    include_archived: 아카이브 항목 포함
    """
    results = await database.run_db(
        _search_items_with_presentation,
        query=q, category=category, status=status,
        limit=limit, offset=offset,
        active_only=active_only, include_archived=include_archived
    )
    return results

# (2026-08-05, GPT Codex 3차 리뷰) 임베딩 사용 불가 사유별 문구 — "API 키 확인"이라는
# 한 가지 메시지가 일일 한도 소진 때도 그대로 떠서, 실제론 설정이 멀쩡한데도 사용자가
# 키를 재발급하거나 설정을 반복 확인하게 만들 수 있었다(특히 한도 상태를 영속화한
# 뒤로는 재시작해도 태평양 자정까지 이 메시지가 계속 뜸).
def _embedding_unavailable_detail(reason: str) -> str:
    if reason == 'ai_input_size_review_required':
        return ('검색어가 의미 검색의 길이 보호값을 넘어 단어 중심으로 검색했습니다. '
                '의미 검색을 사용하려면 핵심 검색어로 줄여주세요.')
    if reason == 'missing_api_key':
        return "Gemini API 키가 설정되지 않았습니다 (설정 화면에서 등록해주세요)."
    if reason == 'daily_quota_exhausted':
        return ("임베딩 일일 한도가 소진되어 의미 검색을 잠시 사용할 수 없습니다 "
                "(태평양 자정 이후 재개됩니다). 정책 원문 점검 등 다른 기능은 계속 이용할 수 있습니다.")
    return "임베딩을 생성할 수 없습니다."


@app.get("/api/search/semantic")
async def semantic_search(
    q: str = Query(..., min_length=1),
    category: str = Query(None),
    limit: int = Query(20, ge=1, le=100),
    min_score: float = Query(0.55, ge=0.0, le=1.0),
    active_only: bool = Query(False),
    include_archived: bool = Query(False),
):
    """의미 기반(임베딩) 검색. 키워드가 달라도 뜻이 유사한 항목을 찾는다.

    GOOGLE_API_KEY 미설정 시 503. 임베딩이 저장된 항목만 검색 대상이므로
    기존 데이터는 /api/embeddings/backfill 로 1회 임베딩 생성이 필요하다.
    """
    from services import embedding_service
    from services.embedding_input_limits import embedding_input_fits
    _reason = ('ai_input_size_review_required' if not embedding_input_fits(q)
               else embedding_service.get_unavailable_reason())
    query_vec = None
    if not _reason:
        query_vec = await embedding_service.embed_text_async(q, task_type='RETRIEVAL_QUERY')
    if query_vec is None and not _reason:
        # (2026-08-05, GPT Codex 4차 리뷰) 사전 판정을 통과한 뒤 이 호출 자체에서 "처음"
        # 일일 한도에 도달했을 수 있다(embed_text_async 내부가 그 시점에 이미 소진 플래그를
        # 세워둠) — 그 경우까지 "일시적 오류"로만 답하면 정확한 사유를 첫 요청에서 놓친다.
        # 실패 직후 한 번 더 확인해, 방금 소진된 것이면 전용 문구로, 아니면(분당 한도·
        # 네트워크 오류 등 사전에 알 수 없던 원인) 일반 오류로 안내한다.
        _reason_after = embedding_service.get_unavailable_reason()
        _reason = _reason_after or 'temporary_error'
    result = await database.run_db(
        _hybrid_search_with_presentation,
        q, query_vec, limit=limit, min_score=min_score, category=category,
        active_only=active_only, include_archived=include_archived,
    )
    if _reason:
        result['semantic_unavailable_reason'] = _reason
        result['notice'] = (_embedding_unavailable_detail(_reason) if _reason != 'temporary_error'
                            else 'AI 의미 확장은 잠시 사용할 수 없어 정확한 단어 중심으로 검색했습니다.')
    return result


@app.get("/api/embeddings/status")
async def embeddings_status():
    """임베딩 보유/미보유 항목 수 (백필 진행률)."""
    return await database.run_db(history_manager.count_embeddings)


@app.get("/api/ai-backlog/status")
async def ai_backlog_status():
    """AI 분석 백로그(하루 상한 초과 등으로 미분석 상태로 남은 항목) 대기 건수.

    (2026-08-06) "수집 완료"와 "AI 구조화 완료"가 다르다는 걸 화면에서 구분하기 위한
    엔드포인트. 매 수집 실행마다 이 값이 그대로거나 늘어난다면 정상적으로 줄어들지 못하고
    있다는 신호이므로, 대시보드에 노출해 관찰 가능하게 한다.

    permanently_excluded: (2026-08-06, 3차 GPT Codex 리뷰) AI 오분류·일시적 로그인
    리다이렉트 한 번만으로 실제 복지정보가 영구히 자동분석 대상에서 빠질 위험이 있어,
    이 수가 늘어나는지 관찰할 수 있게 별도로 노출한다. 초기화·재시도 UI는 아직 없음
    (이 수치가 실제로 우려될 만큼 늘어나면 다음 개선으로 추가 검토)."""
    total = await database.run_db(history_manager.count_ai_backlog)
    permanently_excluded = await database.run_db(history_manager.count_ai_permanently_excluded)
    return {"backlog_total": total, "permanently_excluded": permanently_excluded}


# (2026-08-05, GPT Codex 3차 리뷰) 정책 제목 임베딩은 청크 20개로 나눠 보내는데, 이
# 일반 백필은 요청받은 batch(최대 500)를 통째로 한 번의 embed_content 호출로 보내고
# 있었다 — 텍스트가 각각 최대 8000자까지 허용돼 요청 본문이 커질 수 있고, 그 한 번의
# 호출이 실패(네트워크 오류·한도 등)하면 500건 전부가 이번 회차엔 임베딩 없이 남는다.
# 청크로 나눠 한 청크의 실패가 다른 청크에 전파되지 않게 한다(정책 쪽과 동일한 원리).
_BACKFILL_CHUNK = 20


@app.post("/api/embeddings/backfill")
async def embeddings_backfill(batch: int = Query(100, ge=1, le=500),
                              cursor: int = Query(0, ge=0, le=9223372036854775807)):
    """DB 복원과 겹치지 않도록 백필 전체를 DB 변경 게이트 안에서 실행."""
    from services import policy_sync, ai_coordinator
    if not await policy_sync.acquire_db_mutation_lock("임베딩 생성"):
        raise HTTPException(status_code=409, detail=policy_sync.maintenance_busy_message())
    if not await ai_coordinator.try_start_job("embedding_backfill", "검색 데이터 준비"):
        policy_sync.release_maintenance_lock()
        raise HTTPException(status_code=409, detail=ai_coordinator.busy_message())
    try:
        return await _embeddings_backfill_locked(batch, cursor=cursor)
    finally:
        ai_coordinator.finish_job("embedding_backfill")
        policy_sync.release_maintenance_lock()


async def _embeddings_backfill_locked(batch: int, *, cursor: int = 0):
    """기존 항목에 임베딩을 생성(청크 단위 배치). 반복 호출로 전체 백필.

    길이 보류 항목이 뒤쪽의 짧은 자료를 계속 막지 않도록 유한 키셋 페이지를 훑는다.
    batch는 AI 후보 수의 상한이며, 한 번에 가져오는 미생성 후보도 최대 1,000행
    (+1 lookahead)으로 제한한다. DB의 내부 실행 계획/필터 스캔 수를 뜻하지 않는다.
    next_cursor가 있으면 이후 페이지, 없으면 이번 전체 순회가 끝났다. 보류/실패 항목은
    DB에 그대로 남아 다음 수동 실행(cursor=0)에서 다시 확인할 수 있다.
    """
    from services import embedding_service
    _reason = embedding_service.get_unavailable_reason()
    if _reason:
        raise HTTPException(status_code=503, detail=_embedding_unavailable_detail(_reason))

    from services.embedding_input_limits import embedding_input_fits
    scan_limit = min(max(batch * 4, 100), 1000)
    candidates = await database.run_db(
        history_manager.get_items_missing_embedding, scan_limit + 1,
        raise_on_error=True, after_id=cursor)
    targets, prepared = [], []
    scanned, held_for_input_size, last_id = 0, 0, cursor
    for item in candidates[:scan_limit]:
        item_id = item.get('id')
        if type(item_id) is not int or item_id <= last_id:
            raise HTTPException(status_code=500, detail='검색 데이터의 순서를 확인하지 못했습니다. 다시 확인해주세요.')
        text = embedding_service.build_search_embedding_text(item)
        scanned += 1
        last_id = item_id
        if not embedding_input_fits(text):
            held_for_input_size += 1
            continue
        targets.append(item)
        prepared.append(text)
        if len(targets) >= batch:
            break
    next_cursor = last_id if len(candidates) > scanned else None
    done = 0
    failed = 0
    failed_ids = []
    for i in range(0, len(targets), _BACKFILL_CHUNK):
        chunk = targets[i:i + _BACKFILL_CHUNK]
        texts = prepared[i:i + _BACKFILL_CHUNK]
        try:
            vecs = await embedding_service.embed_texts_batch_async(texts, task_type='RETRIEVAL_DOCUMENT')
        except Exception as e:
            logger.warning(f"임베딩 백필 청크 실패({len(chunk)}건): {e}")
            vecs = [None] * len(chunk)
        if not isinstance(vecs, (list, tuple)) or len(vecs) != len(chunk):
            vecs = [None] * len(chunk)
        for it, vec in zip(chunk, vecs):
            if vec is not None:
                blob = embedding_service.to_blob(vec)
                try:
                    saved = await database.run_db(
                        history_manager.save_embedding, it['link'], blob,
                        embedding_service.SEARCH_EMBEDDING_RECIPE)
                except Exception as e:
                    logger.warning(f"임베딩 저장 실패({it.get('link', '')[:80]}): {e}")
                    failed += 1
                    failed_ids.append(it['id'])
                    continue
                if saved:
                    done += 1
                else:
                    failed += 1
                    failed_ids.append(it['id'])
            else:
                failed += 1
                failed_ids.append(it['id'])
    stats = await database.run_db(history_manager.count_embeddings, raise_on_error=True)
    # (2026-08-05, GPT Codex 4차 리뷰) 처리 도중(예: 첫 청크에서) 일일 한도를 새로 소진했을
    # 수 있다 — 그 사유를 응답에 실어 프론트가 "그냥 실패"와 "한도 소진"을 구분해 안내하게
    # 한다(백필 시작 시점의 사전 판정만으로는 이 케이스를 못 잡음).
    _reason_after = embedding_service.get_unavailable_reason() if failed > 0 else None
    if failed_ids:
        # A caller may explicitly resume this cursor after fixing the error.
        # Never advance it past the first failed/denied eligible row. Successful
        # rows disappear from the missing query, so replay cannot skip failures
        # or make duplicate requests for rows already saved in this pass.
        retry_cursor = min(failed_ids) - 1
        next_cursor = min(next_cursor, retry_cursor) if next_cursor is not None else retry_cursor
    return {"embedded": done, "failed": failed, "remaining": stats.get("missing", 0),
            "stats": stats, "unavailable_reason": _reason_after,
            "held_for_input_size": held_for_input_size, "scanned": scanned,
            "next_cursor": next_cursor, "scan_complete": next_cursor is None}


@app.get("/api/reports/summary")
async def get_report_summary():
    """
    Get summary stats for dashboard (includes new_this_week and expiring_soon).
    """
    return await database.run_db(history_manager.get_summary_stats)


@app.get("/api/reports/trends")
async def get_trends(days: int = Query(default=30, ge=7, le=90)):
    """
    Returns daily item collection counts for the trend chart.
    days: 집계 기간 (7~90일, 기본 30일)
    응답: [{"day": "YYYY-MM-DD", "count": N}, ...]
    """
    return await database.run_db(history_manager.get_trend_stats, days)


def _delete_temp_file(path: str):
    """BackgroundTask용 정리: 앱 소유 임시 파일 외에는 삭제하지 않는다."""
    return delete_temp_file(path)


_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

def _validate_date_params(date_from: Optional[str], date_to: Optional[str]) -> None:
    """날짜 파라미터 형식 및 논리 검증. 문제 있으면 HTTPException 발생."""
    for label, val in (("date_from", date_from), ("date_to", date_to)):
        if val:
            if not _DATE_RE.match(val):
                raise HTTPException(status_code=400, detail=f"날짜 형식이 올바르지 않습니다: {label} (YYYY-MM-DD)")
            try:
                datetime.date.fromisoformat(val)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"유효하지 않은 날짜: {label}")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=400, detail="date_from은 date_to보다 이전이어야 합니다")


@app.get("/api/reports/export")
async def export_report(
    background_tasks: BackgroundTasks,
    format: str = "csv",
    category: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    site_name: Optional[str] = None,
):
    """Export data to CSV or Excel. 필터 파라미터 지원."""
    _validate_date_params(date_from, date_to)
    file_path = await database.run_db(
        history_manager.export_data,
        format=format, category=category, status=status,
        date_from=date_from, date_to=date_to, site_name=site_name
    )
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=500, detail="내보내기에 실패했습니다")

    filename = os.path.basename(file_path)
    background_tasks.add_task(_delete_temp_file, file_path)
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type='application/octet-stream'
    )

@app.get("/api/reports/notion")
async def generate_notion_style_report(
    background_tasks: BackgroundTasks,
    category: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    site_name: Optional[str] = None,
):
    """Generate a Notion-style HTML report. 필터 파라미터 지원."""
    _validate_date_params(date_from, date_to)
    export_limit = int(os.getenv('EXPORT_MAX_ROWS', '50000'))
    has_filter = any([category, status, date_from, date_to, site_name])
    if has_filter:
        result = await database.run_db(
            history_manager.search_items,
            category=category, status=status,
            date_from=date_from, date_to=date_to,
            site_name=site_name, limit=export_limit
        )
        items = result.get("items", [])
    else:
        # 필터 없어도 EXPORT_MAX_ROWS 적용하여 OOM 방지
        result = await database.run_db(history_manager.search_items, limit=export_limit)
        items = result.get("items", [])

    if not items:
        raise HTTPException(status_code=404, detail="리포트를 생성할 데이터가 없습니다")

    file_path = await database.run_db(notifier.generate_notion_report, items)
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=500, detail="리포트 생성에 실패했습니다")

    filename = os.path.basename(file_path)
    background_tasks.add_task(_delete_temp_file, file_path)
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type='text/html'
    )

@app.get("/api/sites/template/download")
async def download_site_template(background_tasks: BackgroundTasks, format: str = "excel"):
    """Download a template for bulk site import."""
    from pathlib import Path
    from services.site_import import HEADERS
    from fastapi.responses import Response
    if format == 'csv':
        return Response(('\ufeff' + ','.join(HEADERS) + '\r\n').encode('utf-8'),
                        media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="site_import_template.csv"'})
    return FileResponse(Path(__file__).resolve().parent / 'assets/site_import_template.xlsx',
                        filename='site_import_template.xlsx',
                        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.post("/api/sites/import")
async def import_sites(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    """Validate the address file, then use the standard registration job."""
    from services.site_import import read_address_file
    
    ALLOWED_EXTENSIONS = ('.csv', '.xlsx', '.xls')
    ALLOWED_MIME_TYPES = {
        'text/csv', 'application/csv', 'text/plain',
        'application/vnd.ms-excel',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    }
    ext = os.path.splitext(file.filename or '')[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Excel 또는 CSV 파일만 허용됩니다.")
    if file.content_type and file.content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(status_code=400, detail=f"허용되지 않는 MIME 타입: {file.content_type}")
    
    # Save upload to temp file
    suffix = ext
    fd, tmp_path = create_temp_file(prefix='site_import_', suffix=suffix)
    try:
        with os.fdopen(fd, 'wb') as tmp:
            size = 0
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 10 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail='사이트 파일은 10MB 이하로 올려주세요.')
                await asyncio.to_thread(tmp.write, chunk)
        records, duplicates = await asyncio.to_thread(read_address_file, tmp_path, suffix)
        current_sites = await database.run_db(utils.load_sites)
        existing = {site['url'] for site in current_sites}
        pending = [row for row in records if row['url'] not in existing]
        skipped = len(records) - len(pending) + duplicates
        if not pending:
            return {'status': 'success', 'count': 0, 'skipped': skipped}
        result = await _start_site_batch([row['url'] for row in pending], background_tasks,
                                         {row['url']: row['name'] for row in pending if row['name']})
        return {**result, 'skipped': skipped}

    except HTTPException:
        raise  # 400 등 의도적 HTTP 에러는 그대로 전파
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"사이트 파일의 내용을 확인해주세요: {e}")
    except Exception as e:
        logger.error(f"Import failed: {e}")
        raise HTTPException(status_code=500, detail="가져오기에 실패했습니다. 파일 형식을 확인해주세요.")
    finally:
        if os.path.exists(tmp_path):
            _delete_temp_file(tmp_path)

class BatchSiteRequest(BaseModel):
    urls: List[str]

@app.post("/api/sites/batch")
async def batch_add_sites(req: BatchSiteRequest, background_tasks: BackgroundTasks):
    """
    Batch analyze and add sites.
    """
    return await _start_site_batch(req.urls, background_tasks)


async def _start_site_batch(urls, background_tasks, names=None):
    async with _batch_lock:
        from services import policy_sync, ai_coordinator
        busy = policy_sync.maintenance_busy_message()
        if busy:
            raise HTTPException(status_code=409, detail=busy)
        if BATCH_STATUS["is_running"]:
            raise HTTPException(status_code=400, detail="이미 다른 일괄 분석이 진행 중입니다.")
        if not await ai_coordinator.try_start_job("site_analysis", "수집 사이트 분석"):
            raise HTTPException(status_code=409, detail=ai_coordinator.busy_message())
        # Lock 안에서 is_running 선점 — 동시 요청 Race Condition 방지
        BATCH_STATUS["is_running"] = True
        
    background_tasks.add_task(process_batch_sites, urls, names)
    return {"status": "started", "count": len(urls)}

@app.get("/api/sites/batch/status")
async def get_batch_status():
    """Get the status of the background site analysis."""
    return BATCH_STATUS

@app.post("/api/sites/batch/cancel")
async def cancel_batch_analysis():
    """Cancel the currently running batch site analysis."""
    global BATCH_CANCEL_EVENT
    if not BATCH_STATUS["is_running"]:
        return {"status": "not_running", "message": "실행 중인 분석이 없습니다"}
    BATCH_CANCEL_EVENT.set()
    return {"status": "cancelling", "message": "취소 신호를 보냈습니다"}

async def process_batch_sites(urls: List[str], names=None):
    from contextlib import AsyncExitStack
    from services.site_registration import prepare_site, is_likely_homepage_url
    from services import ai_coordinator
    BATCH_CANCEL_EVENT.clear()
    BATCH_STATUS.update(success_count=0, fail_count=0, failed_urls=[],
                        review_count=0, cancelled=False, progress=0, total=len(urls),
                        current_url='', message='등록 준비 중...')
    analysis_available = True
    try:
        async with AsyncExitStack() as stack:
            service = site_analyzer.SiteAnalyzerService()
            try:
                await service.start()
            except Exception:
                analysis_available = False
            stack.push_async_callback(service.stop)
            for url in urls:
                if BATCH_CANCEL_EVENT.is_set():
                    break
                BATCH_STATUS['current_url'] = url
                try:
                    current_sites = await database.run_db(utils.load_sites)
                    if any(site['url'] == url for site in current_sites):
                        BATCH_STATUS['success_count'] += 1
                        continue
                    valid, reason = utils.validate_site({'name': url, 'url': url})
                    if not valid:
                        raise ValueError(reason)
                    result = None
                    if analysis_available and not is_likely_homepage_url(url):
                        BATCH_STATUS['message'] = '수집 규칙 확인 중...'
                        try:
                            result = await service.analyze_site(url)
                        except Exception:
                            result = None
                        if isinstance(result, dict) and '일일 AI 분석 한도' in str(result.get('error', '')):
                            analysis_available = False
                    if BATCH_CANCEL_EVENT.is_set():
                        break
                    BATCH_STATUS['message'] = '주소 등록 및 기본 수집 확인 중...'
                    site = await prepare_site(url, result)
                    if names and names.get(url):
                        site['name'] = names[url]
                    if BATCH_CANCEL_EVENT.is_set():
                        break
                    # Save only this address, preserving concurrent site edits/deletions.
                    await database.run_db(utils.save_sites, [site])
                    BATCH_STATUS['success_count'] += 1
                    if site['registration_status'] == 'needs_check':
                        BATCH_STATUS['review_count'] += 1
                except Exception as error:
                    logger.warning('Site registration failed: %s', error)
                    BATCH_STATUS['fail_count'] += 1
                    BATCH_STATUS['failed_urls'].append(url)
                finally:
                    BATCH_STATUS['progress'] += 1
        BATCH_STATUS['cancelled'] = BATCH_CANCEL_EVENT.is_set()
        prefix = '취소됨' if BATCH_STATUS['cancelled'] else '완료'
        BATCH_STATUS['message'] = (
            f"{prefix}: 등록 {BATCH_STATUS['success_count']}건 "
            f"(수집 확인 필요 {BATCH_STATUS['review_count']}건), "
            f"등록 실패 {BATCH_STATUS['fail_count']}건")
    except Exception:
        logger.exception('Site registration batch interrupted')
        BATCH_STATUS['message'] = '등록이 중단되었습니다. 등록 목록을 확인한 뒤 다시 시도해주세요.'
    finally:
        ai_coordinator.finish_job('site_analysis')
        BATCH_STATUS['is_running'] = False
        BATCH_STATUS['current_url'] = ''

# --- Item Detail ---

@app.get("/api/data/{item_id}")
async def get_item_detail(item_id: int):
    """Get a single item with full details for the detail modal."""
    def _load():
        from services.news_display_projection import item_presentation
        item = history_manager.get_item_by_id(item_id)
        if item:
            with database.get_connection() as conn:
                return dict(item, news_presentation=item_presentation(item, conn=conn))
        return None
    item = await database.run_db(_load)
    if not item:
        raise HTTPException(status_code=404, detail="항목을 찾을 수 없습니다")
    return item


@app.get("/api/data/{item_id}/similar")
async def get_similar_items(item_id: int, limit: int = Query(default=5, ge=1, le=20)):
    """
    FTS5 BM25를 이용해 주어진 항목과 유사한 항목을 반환.
    상세 모달의 '관련 항목' 섹션에 사용.
    """
    return await database.run_db(history_manager.get_similar_items, item_id, limit)


# --- 변경 내역 스냅샷 ---

@app.get("/api/data/{item_id}/snapshots")
async def get_item_snapshots(item_id: int):
    """항목의 변경 스냅샷 목록 반환."""
    return await database.run_db(history_manager.get_item_snapshots, item_id=item_id)


@app.get("/api/data/{item_id}/diff/{snapshot_id}")
async def get_item_diff(item_id: int, snapshot_id: int):
    """특정 스냅샷과 현재 상태의 diff 반환."""
    diff = await database.run_db(history_manager.get_item_diff, item_id, snapshot_id)
    if not diff:
        raise HTTPException(status_code=404, detail="스냅샷을 찾을 수 없습니다")
    return diff


# --- 사이트 건강 대시보드 ---

@app.get("/api/sites/health")
async def get_sites_health():
    """모든 사이트의 건강 상태 대시보드 데이터."""
    return await database.run_db(history_manager.get_site_health_dashboard)


@app.get("/api/sites/{site_id}/health")
async def get_site_health_detail(site_id: int, limit: int = Query(default=30, ge=1, le=100)):
    """특정 사이트의 스크래핑 실행 이력."""
    return await database.run_db(history_manager.get_site_scrape_history, site_id, limit)


# --- Notion Markdown Report ---

@app.get("/api/reports/notion-md")
async def generate_notion_markdown_report(
    background_tasks: BackgroundTasks,
    category: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    site_name: Optional[str] = None,
):
    """Generate a Notion-friendly Markdown report. 필터 파라미터 지원."""
    _validate_date_params(date_from, date_to)
    export_limit = int(os.getenv('EXPORT_MAX_ROWS', '50000'))
    has_filter = any([category, status, date_from, date_to, site_name])
    if has_filter:
        result = await database.run_db(
            history_manager.search_items,
            category=category, status=status,
            date_from=date_from, date_to=date_to,
            site_name=site_name, limit=export_limit
        )
        items = result.get("items", [])
    else:
        # 필터 없어도 EXPORT_MAX_ROWS 적용하여 OOM 방지
        result = await database.run_db(history_manager.search_items, limit=export_limit)
        items = result.get("items", [])

    if not items:
        raise HTTPException(status_code=404, detail="리포트를 생성할 데이터가 없습니다")

    file_path = await database.run_db(notifier.generate_notion_markdown_report, items)
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=500, detail="리포트 생성에 실패했습니다")

    filename = os.path.basename(file_path)
    background_tasks.add_task(_delete_temp_file, file_path)
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type='text/markdown'
    )

# --- DB Backup / Restore ---

@app.get("/api/db/backup")
async def backup_database(background_tasks: BackgroundTasks):
    """Download the SQLite database file as a backup."""
    import shutil
    db_path = database.DB_FILE
    if not os.path.exists(db_path):
        raise HTTPException(status_code=404, detail="데이터베이스 파일을 찾을 수 없습니다")

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    # This download copy is disposable, unlike persistent recovery/daily backups.
    fd, backup_path = create_temp_file(prefix=f"welfare_backup_{timestamp}_", suffix='.db')
    os.close(fd)

    try:
        # 온라인 백업 API: WAL 미병합 데이터까지 포함한 일관된 핫 백업
        await database.run_db(database.backup_to, backup_path)
        background_tasks.add_task(_delete_temp_file, backup_path)
        return FileResponse(
            path=backup_path,
            filename=f"welfare_backup_{timestamp}.db",
            media_type='application/octet-stream'
        )
    except Exception as e:
        _delete_temp_file(backup_path)
        logger.error(f"Backup error: {e}")
        raise HTTPException(status_code=500, detail="백업에 실패했습니다")


_SQLITE_MAGIC = b'SQLite format 3\x00'
_RESTORE_ALLOWED_MIME = {
    'application/octet-stream', 'application/x-sqlite3', 'application/vnd.sqlite3'
}
# (2026-08-28, GPT Codex 지적 ⑦) 이 앱이 실제로 쓰는 핵심 테이블 — magic byte만 통과한
# "SQLite이긴 한데 전혀 다른 프로그램의 DB"를 여기서 걸러낸다.
_RESTORE_REQUIRED_TABLES = {
    'sites', 'welfare_items', 'welfare_item_snapshots', 'scrape_runs',
    'policy_watch', 'notion_policies',
}
_RESTORE_REQUIRED_COLUMNS = {
    'sites': {'id', 'name', 'url', 'selectors'},
    'welfare_items': {'id', 'title', 'link', 'site_name', 'scraped_at'},
    'welfare_item_snapshots': {'id', 'item_link', 'snapshot_data'},
    'scrape_runs': {'id', 'site_id', 'status'},
    'policy_watch': {'id', 'url', 'enabled'},
    'notion_policies': {'id', 'page_id', 'db_key', 'notion_fields', 'source_urls',
                        'change_status', 'pending_diff'},
}


def _validate_restore_candidate(path: str) -> Optional[str]:
    """업로드된 파일이 실제로 이 앱의 정상 SQLite DB인지 검증. 문제 있으면 사유 문자열, 없으면 None.

    (2026-08-28, ⑦) magic byte 검사만으론 "SQLite 파일이지만 손상됐거나 전혀 다른 프로그램의
    DB"를 걸러내지 못했다 — PRAGMA integrity_check와 필수 테이블 존재 여부까지 확인한다.
    """
    import sqlite3
    try:
        from pathlib import Path
        # 업로드 완료된 단독 DB 파일만 검사한다. immutable은 읽기 검증 중
        # candidate-wal/shm 생성 및 취소 후 임시 보조파일 잔류를 방지한다.
        conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    except sqlite3.Error as e:
        return f"SQLite로 열 수 없습니다: {e}"
    try:
        cur = conn.execute("PRAGMA integrity_check;")
        result = cur.fetchone()
        if not result or result[0] != "ok":
            return f"데이터베이스 무결성 검사 실패: {result[0] if result else '알 수 없음'}"
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}
        missing = _RESTORE_REQUIRED_TABLES - tables
        if missing:
            return f"이 앱의 데이터베이스가 아닌 것 같습니다 (필수 테이블 누락: {', '.join(sorted(missing))})"
        for table, required in _RESTORE_REQUIRED_COLUMNS.items():
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
            missing_columns = required - columns
            if missing_columns:
                return (f"데이터베이스 스키마가 올바르지 않습니다 "
                        f"({table} 필수 컬럼 누락: {', '.join(sorted(missing_columns))})")
        from services.age_values import validate_restored_months
        invalid_months = validate_restored_months(conn, tables)
        if invalid_months:
            return invalid_months
        fk_errors = conn.execute("PRAGMA foreign_key_check;").fetchall()
        if fk_errors:
            return f"데이터베이스 외래키 검사 실패: {len(fk_errors)}건"
        return None
    except sqlite3.Error as e:
        return f"검증 중 오류: {e}"
    finally:
        conn.close()


def _restore_safety_backup_atomically(safety_backup: str, db_path: str) -> None:
    """실패한 복원 뒤 정상본을 같은 볼륨의 임시파일을 거쳐 원자적으로 되돌린다."""
    import shutil
    import tempfile

    recovery_fd, recovery_path = tempfile.mkstemp(
        suffix='.db', prefix='welfare_recovery_', dir=os.path.dirname(db_path))
    os.close(recovery_fd)
    try:
        database.close_all_connections(strict=True)
        shutil.copy2(safety_backup, recovery_path)
        _remove_db_sidecars(db_path)
        os.replace(recovery_path, db_path)
        recovery_path = None
        database.init_db()
    finally:
        if recovery_path is not None and os.path.exists(recovery_path):
            try:
                os.remove(recovery_path)
            except OSError:
                pass


def _remove_db_sidecars(db_path):
    # 닫힌 연결의 WAL/SHM을 지울 수 없으면 교체하지 않는다.
    for suffix in ('-wal', '-shm'):
        try:
            os.remove(db_path + suffix)
        except FileNotFoundError:
            pass


def _discard_imported_trigger_code(path):
    """Restore data, not executable SQL supplied by a backup.

    The application's own derived-search triggers are recreated by init_db.
    Imported triggers must not run during migration or reactivate undo receipts.
    Imported views must not impersonate application tables during later reads.
    The application owns no views; retain table data, not imported SQL programs.
    """
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        for kind in ('trigger', 'view'):
            names = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type=?", (kind,))]
            for name in names:
                conn.execute('DROP ' + kind.upper() + ' "' + name.replace('"', '""') + '"')


def _commit_database_restore(candidate, db_path, safety_backup):
    """백업부터 교체/초기화/롤백까지 하나의 배타적 worker 구간으로 실행."""
    with database.exclusive_database():
        replaced = False
        try:
            if os.path.exists(db_path):
                database.backup_to(safety_backup)
            database.close_all_connections(strict=True)
            _remove_db_sidecars(db_path)
            os.replace(candidate, db_path)
            replaced = True
            _discard_imported_trigger_code(db_path)
            database.init_db()
            from services.policy_undo import mark_restored_history
            mark_restored_history()
        except Exception:
            if replaced and os.path.exists(safety_backup):
                try:
                    _restore_safety_backup_atomically(safety_backup, db_path)
                except Exception:
                    logger.critical('DB 복원 롤백 실패. 안전백업을 보존합니다.', exc_info=True)
            raise
        finally:
            # 교체가 끝난 직후 요청 취소를 전파해도 이전 DB의 키워드가 남지 않게
            # worker의 배타 구간 안에서 무효화한다. 롤백 결과에도 동일하게 적용한다.
            content_filter.clear_cache()


async def _settle_restore_operation(awaitable):
    # UploadFile.read/run_db/to_thread는 await 취소로 worker가 멈추지 않는다.
    # 현재 I/O가 끝나기 전 후보 파일이나 전체 workflow lease를 반환하지 않는다.
    task = asyncio.create_task(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    try:
        return task.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


async def _finish_restore_before_cancelling(candidate, db_path, safety_backup):
    return await _settle_restore_operation(asyncio.to_thread(
        _commit_database_restore, candidate, db_path, safety_backup))


async def _stream_restore_upload(file, output, max_size: int, chunk_size: int = 1024 * 1024):
    """업로드를 제한된 메모리로 저장한다. 미완성 파일은 호출자의 finally에서 제거."""
    total = 0
    header = bytearray()
    while True:
        chunk = await _settle_restore_operation(file.read(chunk_size))
        if not chunk:
            break
        total += len(chunk)
        if total > max_size:
            raise HTTPException(status_code=400, detail="파일 크기 제한을 초과했습니다.")
        if len(header) < len(_SQLITE_MAGIC):
            header.extend(chunk[:len(_SQLITE_MAGIC) - len(header)])
            if len(header) == len(_SQLITE_MAGIC) and bytes(header) != _SQLITE_MAGIC:
                raise HTTPException(status_code=400, detail="유효한 SQLite 데이터베이스 파일이 아닙니다")
        output.write(chunk)
    if bytes(header) != _SQLITE_MAGIC:
        raise HTTPException(status_code=400, detail="유효한 SQLite 데이터베이스 파일이 아닙니다")
    output.flush()
    os.fsync(output.fileno())
    return total


@app.post("/api/db/restore")
async def restore_database(file: UploadFile = File(...)):
    """Restore the database from an uploaded backup file.

    (2026-08-28, ②+⑦) 정책 작업과 공유하는 유지보수 락을 확보한 뒤에만 진행하고(동기화 중간에
    복원이 끼어들어 두 작업이 서로 다른 DB 상태를 참조하는 것을 방지), 업로드 파일은 운영 DB
    경로에 바로 쓰지 않고 같은 디렉터리의 임시 파일에 먼저 저장해 integrity_check·필수 테이블
    검증을 통과한 뒤에만 os.replace()로 원자 교체한다(중간에 프로세스가 죽어도 운영 DB는
    손상되지 않고 그대로 남는다 — 예전엔 업로드 내용을 통째로 열어 직접 wb로 덮어썼다).
    """
    import tempfile
    from contextlib import ExitStack
    from services import policy_sync, ai_coordinator, news_workflow_ownership
    from services.scraper_service import _acquire_scrape_lock, _release_scrape_lock

    if not (file.filename or '').endswith('.db'):
        raise HTTPException(status_code=400, detail="유효한 .db 파일만 업로드 가능합니다")

    if file.content_type and file.content_type not in _RESTORE_ALLOWED_MIME:
        raise HTTPException(status_code=400, detail="유효한 SQLite 데이터베이스 파일이 아닙니다")

    # 파일 크기 제한: 최대 500MB (SQLite DB 파일 합리적 상한)
    _MAX_RESTORE_SIZE = 500 * 1024 * 1024
    if file.size and file.size > _MAX_RESTORE_SIZE:
        raise HTTPException(status_code=400, detail=f"파일이 너무 큽니다 (최대 {_MAX_RESTORE_SIZE // (1024*1024)}MB)")

    # 프로필 작업과 동일한 순서: workflow -> maintenance -> AI -> scrape.
    # 단계별 잠금이 비는 수집/분석 전환도 막고, 다른 프로세스의 시작/복구도
    # 업로드부터 교체/롤백/취소 정리까지 막는다. GUI 유휴 수명은 잠그지 않는다.
    workflow_lease = news_workflow_ownership.acquire()
    if workflow_lease is None:
        raise HTTPException(status_code=409, detail="다른 수집 또는 복원 작업이 진행 중이거나 작업 잠금을 확인할 수 없습니다. 잠시 후 다시 시도해주세요.")
    releases = ExitStack()
    releases.callback(workflow_lease.close)

    db_path = database.DB_FILE
    tmp_fd = tmp_path = safety_backup = scrape_handle = None
    try:
        if not await policy_sync.try_acquire_maintenance_lock("DB 복원"):
            raise HTTPException(status_code=409, detail=policy_sync.maintenance_busy_message())
        releases.callback(policy_sync.release_maintenance_lock)
        if SCRAPER_STATUS.get("is_running") or BATCH_STATUS.get("is_running") or BOKJIRO_STATUS.get("is_running"):
            raise HTTPException(status_code=409, detail="수집 또는 일괄 작업이 끝난 후 DB를 복원해주세요.")

        if not await ai_coordinator.try_start_job("db_restore", "데이터베이스 복원"):
            raise HTTPException(status_code=409, detail=ai_coordinator.busy_message())
        releases.callback(ai_coordinator.finish_job, "db_restore")
        scrape_handle = _acquire_scrape_lock()
        if scrape_handle is None:
            raise HTTPException(status_code=409, detail="다른 프로그램의 수집이 진행 중이거나 잠금 파일에 접근할 수 없습니다. 복원을 중단했습니다.")
        releases.callback(_release_scrape_lock, scrape_handle)
        backup_dir = os.path.join(os.path.dirname(db_path), 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safety_backup = os.path.join(backup_dir, f"welfare_pre_restore_{timestamp}.db")
        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix='.db', prefix='welfare_restore_', dir=os.path.dirname(db_path))
        # Read and validate SQLite magic bytes, then persist to a temp file for validation —
        # 검증을 통과하기 전까지는 운영 DB 경로를 전혀 건드리지 않는다.
        with os.fdopen(tmp_fd, 'wb') as f:
            tmp_fd = None  # 실패·취소되어도 fdopen이 핸들을 닫는다.
            await _stream_restore_upload(file, f, _MAX_RESTORE_SIZE)

        invalid_reason = await _settle_restore_operation(
            database.run_db(_validate_restore_candidate, tmp_path))
        if invalid_reason:
            raise HTTPException(status_code=400, detail=invalid_reason)

        try:
            await _finish_restore_before_cancelling(tmp_path, db_path, safety_backup)
        except (database.ConnectionCloseError, database.DatabaseMaintenanceError) as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        tmp_path = None  # 이미 이동됐으므로 finally에서 지우지 않게

        logger.info(f"Database restored from uploaded file. Safety backup at: {safety_backup}")
        return {"status": "success", "message": "데이터베이스가 복원되었습니다", "safety_backup": safety_backup}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Restore error: {e}")
        raise HTTPException(status_code=500, detail="복원에 실패했습니다. 백업 파일이 손상되었을 수 있습니다.")
    finally:
        try:
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp_path is not None and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        finally:
            # ExitStack attempts every release even if one cleanup fails;
            # the cross-process workflow lease is always returned last.
            releases.close()

RUN_ONCE_SUCCESS = 0
RUN_ONCE_FAILED = 1
RUN_ONCE_NOT_READY = 2
RUN_ONCE_WAITING = 3
RUN_ONCE_CANCELLED = 130


async def _run_once_scrape():
    """Single scheduled run: return a truthful process exit code, never force kill.

    0 completed (including a genuine empty result), 1 failed, 2 configuration
    not ready, 3 busy/resumable waiting, 130 cancelled. Profile recovery and
    execution share the same OS lease used by manual and in-app scheduled runs.
    """
    from services.scraper_service import ScraperService
    from services import ai_coordinator
    from services.news_profiles_api import start_default_condition
    admitted = False
    try:
        outcome = await start_default_condition(headless=True, wait=True)
        if outcome is not None:
            state = outcome.get('state')
            code = {'complete': RUN_ONCE_SUCCESS, 'waiting': RUN_ONCE_WAITING,
                    'cancelled': RUN_ONCE_CANCELLED}.get(state, RUN_ONCE_FAILED)
            logger.info('[Run-Once] 프로필 수집 종료 코드: %s', code)
            return code
        # Only absence of a default condition selects the legacy implementation.
        # No configured-but-unready profile falls back to different rules.
        if not await database.run_db(utils.load_sites):
            logger.warning('[Run-Once] 수집 사이트 설정이 필요합니다.')
            return RUN_ONCE_NOT_READY
        admitted = await ai_coordinator.try_start_job('scrape', '예약 정보 수집')
        if not admitted:
            logger.info('[Run-Once] 다른 작업이 진행 중이거나 작업 잠금을 확인할 수 없습니다.')
            return RUN_ONCE_WAITING
        service = ScraperService(headless=True)
        service.track_collection_outcome = True
        result = await service.run_scraper_async(progress_callback=None, stop_event=None)
        outcome = getattr(service, 'last_run_outcome', None)
        if isinstance(outcome, dict):
            code = {'completed': RUN_ONCE_SUCCESS, 'not_ready': RUN_ONCE_NOT_READY,
                    'busy': RUN_ONCE_WAITING, 'cancelled': RUN_ONCE_CANCELLED}.get(
                        outcome.get('state'), RUN_ONCE_FAILED)
            if code != RUN_ONCE_SUCCESS:
                logger.warning('[Run-Once] 수집 전체가 완료되지 않았습니다. 종료 코드: %s', code)
                return code
        logger.info('[Run-Once] 완료 — 신규 %s건', len(result or []))
        return RUN_ONCE_SUCCESS
    except asyncio.CancelledError:
        logger.info('[Run-Once] 수집 취소 및 저장 정리 완료.')
        return RUN_ONCE_CANCELLED
    except HTTPException as error:
        unavailable = isinstance(error.detail, dict) and error.detail.get('error') == 'WORKFLOW_OWNER_UNAVAILABLE'
        logger.warning('[Run-Once] 수집 시작/재개를 보류했습니다. HTTP 상태: %s', error.status_code)
        if unavailable or error.status_code == 503:
            return RUN_ONCE_WAITING
        return RUN_ONCE_NOT_READY if error.status_code < 500 else RUN_ONCE_FAILED
    except Exception:
        logger.error('[Run-Once] 수집에 실패했습니다. 저장된 작업은 보존합니다.')
        return RUN_ONCE_FAILED
    finally:
        if admitted:
            ai_coordinator.finish_job('scrape')

if __name__ == "__main__":
    from services.windows_transport import install_reset_safe_close
    install_reset_safe_close()
    # OS 작업 스케줄러가 호출하는 단발 실행 모드 (서버 미기동)
    if '--run-once' in sys.argv:
        sys.exit(asyncio.run(_run_once_scrape()))

    import uvicorn
    # log_config=None prevents uvicorn from trying to setup its own logging
    # which crashes in windowed/sidecar mode because it tries to access sys.stdout.isatty()
    PORT = int(os.environ.get('WELFARE_API_PORT', '8421'))
    try:
        with bind_server_socket(PORT) as listener:
            _DESKTOP_SERVER = uvicorn.Server(uvicorn.Config(
                app, host='127.0.0.1', port=PORT, log_config=None))
            _DESKTOP_SERVER.run(sockets=[listener])
    except OSError:
        logger.critical('연결 포트를 사용할 수 없습니다. 기존 프로그램은 종료하지 않습니다.')
        sys.exit(1)
