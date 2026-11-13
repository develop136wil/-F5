"""애플리케이션 전체 Gemini 작업 조정기.

기능별로 제각각인 실행 플래그만 사용하면 일반 수집, 정책 점검, 사이트 분석 등이
동시에 시작되어 같은 Google 프로젝트의 무료 한도를 합산 초과할 수 있다. 이 모듈은
AI 호출량이 큰 작업을 한 번에 하나만 허용하고, 프론트에서 현재 점유 작업을 확인할 수
있는 단일 상태를 제공한다.

짧은 검색어 임베딩처럼 사용자 상호작용에 필요한 단발 호출은 이 작업 게이트의 대상이
아니다. 실제 API 호출 간격은 ``acquire_model_slot``의 모델별 대기열에서 별도로 제어한다.
"""

from __future__ import annotations

import asyncio
import datetime
import glob
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from typing import Deque, Dict, Optional

logger = logging.getLogger(__name__)


_JOB_LOCK = asyncio.Lock()
_JOB_KIND: Optional[str] = None
_JOB_LABEL: Optional[str] = None
_JOB_STARTED_AT: Optional[float] = None
_PROCESS_LOCK_HANDLE = None
_EXTERNAL_BUSY = False

_MODEL_LOCKS: Dict[str, asyncio.Lock] = {}
_MODEL_CALLS: Dict[str, Deque[float]] = defaultdict(deque)
_MODEL_WAITERS: Dict[str, int] = defaultdict(int)
_MODEL_COOLDOWN_UNTIL: Dict[str, float] = defaultdict(float)
_DAILY_CALLS: Dict[str, int] = defaultdict(int)
_DAILY_EXHAUSTED: Dict[str, str] = {}
_STATE_DATE: Optional[str] = None
_STATE_LOADED = False
_STATE_WARNING: Optional[str] = None
_QUOTA_THREAD_LOCK = threading.Lock()
_PENDING_LOCK = threading.Lock()
_PENDING_EXHAUSTED: Dict[str, str] = {}

try:
    from zoneinfo import ZoneInfo
    _PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
except Exception:
    _PACIFIC_TZ = None


class AiDailyQuotaExhausted(RuntimeError):
    pass


class AiLocalBudgetExhausted(AiDailyQuotaExhausted):
    """Historical compatibility only; live admission no longer raises this.

    Kept so old stored failures/tests cannot be mistaken for Google exhaustion.
    """


class AiRequestTimeout(TimeoutError):
    pass


class AiQuotaStateUnavailable(RuntimeError):
    """Do not dispatch an API request without a durable quota reservation."""


class _QuotaStateBusy(AiQuotaStateUnavailable):
    pass


class AiInputTokenBudgetExceeded(AiQuotaStateUnavailable):
    """Manual review/reduce input; not a daily provider exhaustion signal."""


class AiTemporaryRateLimit(AiQuotaStateUnavailable):
    """Fits baseline but a durable temporary throttle currently prevents dispatch."""
    def __init__(self, seconds):
        self.seconds = max(1.0, float(seconds))
        super().__init__('AI의 일시적인 전송 제한으로 기다리고 있습니다. 원문은 보존되며 제한 해제 후 다시 진행합니다.')


class _RateWindowWait(AiQuotaStateUnavailable):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__('분당 AI 전송량을 조절하고 있습니다. 잠시 후 이어서 진행합니다.')


def create_client(api_key: str):
    """동기·비동기 요청 모두 같은 네트워크 타임아웃을 쓰는 Gemini 클라이언트."""
    from google import genai
    from google.genai import types
    timeout_ms = int(max(1.0, float(os.getenv("AI_REQUEST_TIMEOUT_SEC", "75"))) * 1000)
    # Retries belong to the app's counted/coordinated queue, not an implicit
    # second SDK loop. Explicit even though the pinned SDK currently defaults
    # to one attempt; a future SDK update must not multiply paid/free requests.
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(
        timeout=timeout_ms, retry_options=types.HttpRetryOptions(attempts=1)))


def _job_lock_path() -> str:
    import utils
    return os.path.join(utils.get_app_data_dir(), "ai_job.lock")


def _acquire_process_lock():
    """별도 EXE/--run-once 프로세스까지 포함하는 비차단 OS 파일 락."""
    handle = None
    try:
        path = _job_lock_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = open(path, "a+")
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except (OSError, IOError):
        try:
            handle.close()
        except Exception:
            pass
        return None


def _release_process_lock(handle) -> None:
    if handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, IOError):
        pass
    finally:
        try:
            handle.close()
        except Exception:
            pass


def _pacific_today() -> str:
    if _PACIFIC_TZ is not None:
        return datetime.datetime.now(_PACIFIC_TZ).date().isoformat()
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _state_path() -> str:
    import utils
    return os.path.join(utils.get_app_data_dir(), "ai_quota_state.json")


def _atomic_json(path, payload) -> None:
    """Publish complete JSON; leave old bytes intact on any write failure."""
    tmp_path = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix='ai_quota_', suffix='.tmp', dir=os.path.dirname(path))
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _exhaustion_intents():
    """Immutable uniquely named intents; malformed markers fail closed.

    An error callback can publish one intent without waiting for the ledger
    lock. All readers merge it inside that lock before reserving a request.
    Different models never overwrite each other and no count is stored here.
    """
    for path in glob.glob(glob.escape(_state_path()) + '.exhausted.*.json'):
        try:
            with open(path, encoding='utf-8') as handle:
                value = json.load(handle)
            if not isinstance(value, dict) or not isinstance(value.get('model'), str) or not value['model']:
                raise ValueError('Invalid quota hold')
            datetime.date.fromisoformat(value.get('date_pacific'))
            yield path, value['model'], value['date_pacific']
        except (ValueError, TypeError):
            # Never delete malformed marker bytes or guess that its hold ended.
            yield path, '*', None


def _persist_exhaustion_intent(model, day):
    # No model/date text enters a path. Unique immutable names also prevent a
    # cleanup pass from deleting a newer publisher's replacement of one name.
    path = _state_path() + f'.exhausted.{uuid.uuid4().hex}.json'
    _atomic_json(path, {'model': model, 'date_pacific': day})


@contextmanager
def _quota_locked():
    """Short nonblocking ledger transaction, independent of the bulk-job lock.

    The thread lock also covers sync embedding error callbacks. Never await or
    sleep while holding this lock; the async admission loop retries outside it.
    """
    if not _QUOTA_THREAD_LOCK.acquire(blocking=False):
        raise _QuotaStateBusy('AI 사용량 기록을 다른 요청이 갱신 중입니다.')
    handle = None
    locked = False
    try:
        path = _state_path() + '.lock'
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = open(path, 'a+b')
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            import errno
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise _QuotaStateBusy('AI 사용량 기록을 다른 실행이 갱신 중입니다.') from error
            raise
        locked = True
        yield
    except OSError as error:
        raise AiQuotaStateUnavailable('AI 사용량 기록을 저장·확인할 수 없어 요청을 보류했습니다.') from error
    finally:
        try:
            if locked:
                _release_process_lock(handle)
            elif handle is not None:
                try:
                    handle.close()
                except OSError:
                    logger.warning('AI 사용량 잠금 파일을 닫지 못했습니다. 요청은 보류합니다.')
        finally:
            _QUOTA_THREAD_LOCK.release()


def _refresh_state_locked(*, preserve_corrupt=True) -> bool:
    """Read latest disk state and merge same-day knowledge; caller owns lock.

    Counts are snapshots, so merge by max, never sum. Different-model and
    wildcard holds are unioned. A new Pacific date alone resets old knowledge.
    Return whether the merged view needs persistence before it is trusted.
    """
    global _STATE_LOADED, _STATE_DATE
    today = _pacific_today()
    recovered = False
    try:
        with open(_state_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError('잘못된 AI 상태 형식')
        calls, exhausted = data.get('calls', {}), data.get('exhausted', {})
        if not isinstance(calls, dict) or not isinstance(exhausted, dict):
            raise ValueError('잘못된 AI 사용량 형식')
        if any(type(v) is not int or v < 0 for v in calls.values()):
            raise ValueError('잘못된 AI 사용 횟수')
        if any(not isinstance(v, str) for v in exhausted.values()):
            raise ValueError('잘못된 AI 한도 날짜')
        if data:
            datetime.date.fromisoformat(data.get('date_pacific'))
        for value in exhausted.values():
            datetime.date.fromisoformat(value)
    except FileNotFoundError:
        data = {}
    except (ValueError, TypeError):
        # 사용량을 알 수 없을 때 0으로 간주해 요청을 폭주시켜서는 안 된다.
        data = {'date_pacific': today, 'exhausted': {'*': today}}
        recovered = True
        logger.warning('AI 사용량 상태를 읽을 수 없어 오늘의 AI 요청을 안전하게 보류합니다.')
    # A clock moving backwards must not erase a later day's durable ledger.
    if data.get('date_pacific', today) > today or (_STATE_DATE is not None and _STATE_DATE > today):
        raise AiQuotaStateUnavailable('AI 사용량 기록 날짜가 현재보다 늦습니다. 시스템 날짜를 확인해주세요.')
    disk_calls = dict(data.get('calls') or {}) if data.get('date_pacific') == today else {}
    disk_exhausted = {k: v for k, v in (data.get('exhausted') or {}).items()
                      if v == today} if data.get('date_pacific') == today else {}
    calls, exhausted = dict(disk_calls), dict(disk_exhausted)
    intents = list(_exhaustion_intents())
    for _path, model, day in intents:
        if day is None or day == today:
            exhausted[model] = today
        elif day > today:
            raise AiQuotaStateUnavailable('AI 한도 소진 기록 날짜가 현재보다 늦습니다. 시스템 날짜를 확인해주세요.')
    if _STATE_DATE == today:
        for model, count in _DAILY_CALLS.items():
            calls[model] = max(calls.get(model, 0), count)
        exhausted.update({k: v for k, v in _DAILY_EXHAUSTED.items() if v == today})
    with _PENDING_LOCK:
        exhausted.update({k: v for k, v in _PENDING_EXHAUSTED.items() if v == today})
    _STATE_DATE = today
    _DAILY_CALLS.clear()
    _DAILY_CALLS.update(calls)
    _DAILY_EXHAUSTED.clear()
    _DAILY_EXHAUSTED.update(exhausted)
    _STATE_LOADED = True
    if recovered and preserve_corrupt:
        # Preserve damaged bytes before publishing the durable fail-closed hold.
        import shutil
        shutil.copy2(_state_path(), _state_path() + f'.corrupt.{time.time_ns()}')
    return (recovered or bool(intents) or data.get('date_pacific') != today
            or calls != disk_calls or exhausted != disk_exhausted)


def _load_state() -> None:
    """Refresh status without blocking the event loop on another process."""
    global _STATE_WARNING
    try:
        with _quota_locked():
            if _refresh_state_locked():
                _save_state()
            _STATE_WARNING = None
    except AiQuotaStateUnavailable as error:
        _STATE_WARNING = str(error)


def _roll_daily_state() -> None:
    _load_state()


def _save_state() -> None:
    """Atomic durable write inside _quota_locked; errors must deny admission."""
    path = _state_path()
    payload = {
        "date_pacific": _STATE_DATE or _pacific_today(),
        "calls": dict(_DAILY_CALLS),
        "exhausted": dict(_DAILY_EXHAUSTED),
    }
    _atomic_json(path, payload)
    with _PENDING_LOCK:
        for model, date in list(_PENDING_EXHAUSTED.items()):
            if date < payload['date_pacific'] or payload['exhausted'].get(model) == date:
                _PENDING_EXHAUSTED.pop(model, None)
    # A hold is removed only after the ledger durably contains that same hold,
    # or after its Pacific day ended. Cleanup failure must leave the hold safe.
    try:
        for intent, model, day in _exhaustion_intents():
            if day is not None and (day < payload['date_pacific'] or payload['exhausted'].get(model) == day):
                try:
                    os.remove(intent)
                except OSError:
                    pass
    except OSError:
        pass


def _current_loop_lock(lock: asyncio.Lock) -> asyncio.Lock:
    """해제된 테스트용 락이 다른 이벤트 루프에 묶였으면 새 락을 반환한다."""
    bound_loop = getattr(lock, "_loop", None)
    loop = asyncio.get_running_loop()
    if bound_loop is not None and bound_loop is not loop:
        if lock.locked():
            raise RuntimeError("AI 작업 락이 다른 이벤트 루프에서 사용 중입니다")
        return asyncio.Lock()
    return lock


def _job_lock() -> asyncio.Lock:
    global _JOB_LOCK
    _JOB_LOCK = _current_loop_lock(_JOB_LOCK)
    return _JOB_LOCK


async def try_start_job(kind: str, label: str) -> bool:
    """AI 대량 작업을 즉시 시작한다. 다른 작업이 진행 중이면 기다리지 않고 False."""
    global _JOB_KIND, _JOB_LABEL, _JOB_STARTED_AT, _PROCESS_LOCK_HANDLE, _EXTERNAL_BUSY
    from services import app_lifecycle
    if app_lifecycle.shutdown_requested:
        return False
    lock = _job_lock()
    if lock.locked():
        return False
    await lock.acquire()
    process_handle = _acquire_process_lock()
    if process_handle is None:
        _EXTERNAL_BUSY = True
        lock.release()
        return False
    _PROCESS_LOCK_HANDLE = process_handle
    _EXTERNAL_BUSY = False
    _JOB_KIND = kind
    _JOB_LABEL = label
    _JOB_STARTED_AT = time.time()
    return True


def finish_job(kind: Optional[str] = None) -> None:
    """현재 AI 대량 작업을 종료한다.

    kind가 주어졌는데 점유 작업과 다르면 다른 작업의 락을 잘못 해제하지 않는다.
    """
    global _JOB_KIND, _JOB_LABEL, _JOB_STARTED_AT, _PROCESS_LOCK_HANDLE, _EXTERNAL_BUSY
    if kind is not None and _JOB_KIND != kind:
        return
    _JOB_KIND = None
    _JOB_LABEL = None
    _JOB_STARTED_AT = None
    _release_process_lock(_PROCESS_LOCK_HANDLE)
    _PROCESS_LOCK_HANDLE = None
    _EXTERNAL_BUSY = False
    if _JOB_LOCK.locked():
        _JOB_LOCK.release()


def busy_message() -> Optional[str]:
    _refresh_external_busy()
    if _EXTERNAL_BUSY and not _JOB_LOCK.locked():
        return "다른 프로그램 창이나 예약 실행에서 AI 작업이 진행 중입니다. 완료 후 다시 시도해주세요."
    if not _JOB_LOCK.locked():
        return None
    return f"AI 작업({_JOB_LABEL or '진행 중'})이 끝난 후 다시 시도해주세요."


def local_job_status() -> dict:
    """In-memory work snapshot for desktop liveness, without quota/config I/O.

    Quota admission still uses status/reservation paths. Shutdown only needs the
    local job identity; external processes are not owned by this desktop.
    """
    return {'job_kind': _JOB_KIND, 'job_label': _JOB_LABEL}


def status() -> dict:
    _refresh_external_busy()
    _roll_daily_state()
    now_mono = time.monotonic()
    models = {}
    for model in (set(_MODEL_CALLS) | set(_MODEL_WAITERS) | set(_MODEL_COOLDOWN_UNTIL)
                  | set(_DAILY_CALLS) | set(_DAILY_EXHAUSTED)):
        cooldown = max(0, _MODEL_COOLDOWN_UNTIL[model] - now_mono)
        models[model] = {
            "recent_calls": len(_MODEL_CALLS[model]),
            "session_calls": _DAILY_CALLS[model],
            "daily_exhausted": _DAILY_EXHAUSTED.get(model) == _STATE_DATE,
            "waiting": _MODEL_WAITERS[model],
            "cooldown_seconds": round(cooldown),
        }
    request_limit = daily_request_limit()
    recorded_requests = sum(_DAILY_CALLS.values())
    return {
        "busy": _JOB_LOCK.locked() or _EXTERNAL_BUSY,
        "job_kind": _JOB_KIND,
        "job_label": _JOB_LABEL,
        "started_at": _JOB_STARTED_AT,
        "models": models,
        "recorded_requests_today": recorded_requests,
        "daily_request_limit": request_limit,
        "local_budget_remaining": None,
        "local_budget_exhausted": False,
        "usage_date": _STATE_DATE,
        "usage_timezone": 'America/Los_Angeles' if _PACIFIC_TZ is not None else 'UTC',
        "quota_policy": _quota_policy_status(),
        "state_warning": (_STATE_WARNING or ('AI 사용량 기록 복구로 오늘의 요청을 보류했습니다.'
                          if _DAILY_EXHAUSTED.get('*') == _STATE_DATE else None)),
    }


def daily_request_limit() -> None:
    """Compatibility field: the app no longer imposes a daily request cap.

    Legacy AI_DAILY_REQUEST_LIMIT values (including zero) are intentionally
    ignored. Google-confirmed exhaustion and durable usage/minute safeguards
    are still checked before every request; this does not promise free use.
    """
    return None


def _quota_policy():
    from services import ai_rate_policy
    return ai_rate_policy.load_policy(os.path.join(os.path.dirname(_state_path()), 'config.json'))


def _quota_policy_status():
    from services import ai_rate_policy
    return ai_rate_policy.public_status(_quota_policy())


def estimate_input_tokens(contents, *, media_bytes=0):
    from services.ai_rate_policy import estimate_input_tokens as estimate
    return estimate(contents, media_bytes=media_bytes)


def estimate_generation_input_tokens(prompt, response_json_schema=None):
    """Plan and admit the same complete text request, including its schema.

    This uses the existing conservative UTF-8 allowance. Output token caps are
    a separate response-integrity guard, not additional input tokens. Keeping
    one serializer avoids sizing just the article while dispatch counts all
    instructions and the structured response contract as well.
    """
    contents = prompt if response_json_schema is None else [prompt,
        json.dumps(response_json_schema, ensure_ascii=False, separators=(',', ':'))]
    return estimate_input_tokens(contents)


def input_token_budget(model):
    """Read-only chunk planning; admission still reserves/rechecks every chunk.

    0: cannot safely plan, int: estimated input
    allowance. No request text is recorded, and this is not exact Google TPM.
    """
    return _input_token_allowance(model, include_penalty=True)


def input_token_capacity(model):
    """Plan intact documents against current baseline, NOT permission to send.

    Reservation still enforces temporary penalties, RPM/TPM and provider holds.
    Never raises a configured/observed ceiling or resets an existing ledger.
    """
    return _input_token_allowance(model, include_penalty=False)


def _input_token_allowance(model, *, include_penalty):
    from services import ai_rate_policy
    try:
        with _quota_locked():
            policy = _quota_policy()
            limits = ai_rate_policy.effective_limits(policy, model)
            path = _state_path() + '.minute.json'
            if limits is None:
                limits = ai_rate_policy.effective_limits({'state': 'reconfirmation_required'}, model)
            window = ai_rate_policy.read_window(path)
            penalty = window['penalties'].get(model, {})
            factor = penalty.get('token_factor', penalty.get('factor', 1)) if include_penalty else 1
            return max(1, int(limits['tpm'] * factor)) if limits['tpm'] else 0
    except (AiQuotaStateUnavailable, ValueError, TypeError, OSError):
        return 0


def input_request_budget(model):
    """Read-only batch planning in conservative app protection units.

    Use a complete minute's ceiling, not its momentary vacant slots: admission
    waits for those slots and rechecks provider holds before each dispatch.
    Prior ledger counts are preserved as units; no historical reset or claim
    about Google's actual batch-quota accounting is made here.
    """
    from services import ai_rate_policy
    try:
        with _quota_locked():
            _refresh_state_locked(preserve_corrupt=False)
            if (_DAILY_EXHAUSTED.get('*') == _STATE_DATE
                    or _DAILY_EXHAUSTED.get(model) == _STATE_DATE):
                return 0
            limits = ai_rate_policy.effective_limits(_quota_policy(), model)
            if limits is None:
                limits = ai_rate_policy.effective_limits({'state': 'reconfirmation_required'}, model)
            window = ai_rate_policy.read_window(_state_path() + '.minute.json')
            factor = window['penalties'].get(model, {}).get('factor', 1)
            rpm = max(1, int(limits['rpm'] * factor)) if limits['rpm'] else 0
            return max(0, min(rpm, _rpm_for_model(model)))
    except (AiQuotaStateUnavailable, ValueError, TypeError, OSError):
        return 0


def _checked_request_units(request_units):
    from services.ai_rate_policy import validate_request_units
    try:
        return validate_request_units(request_units)
    except ValueError:
        raise AiInputTokenBudgetExceeded('AI 요청 묶음의 항목 수를 확인할 수 없어 전송을 보류했습니다.') from None


def request_budget_config_lock():
    """Serialize quota-related config saves with usage reservations."""
    return _quota_locked()


def _refresh_external_busy() -> None:
    """다른 프로세스가 종료된 뒤 UI가 영구 busy로 남지 않도록 잠금을 재확인한다."""
    global _EXTERNAL_BUSY
    if not _EXTERNAL_BUSY or _JOB_LOCK.locked():
        return
    probe = _acquire_process_lock()
    if probe is not None:
        _release_process_lock(probe)
        _EXTERNAL_BUSY = False


def _rpm_for_model(model: str) -> int:
    if "embedding" in model.lower():
        return max(1, int(os.getenv("AI_GLOBAL_EMBED_RPM", "60")))
    return max(1, int(os.getenv("AI_GLOBAL_GENERATE_RPM", "10")))


def _period_seconds() -> float:
    return max(0.01, float(os.getenv("AI_GLOBAL_RATE_PERIOD_SEC", "60")))


def _model_lock(model: str) -> asyncio.Lock:
    lock = _MODEL_LOCKS.get(model)
    if lock is None:
        lock = asyncio.Lock()
    else:
        lock = _current_loop_lock(lock)
    _MODEL_LOCKS[model] = lock
    return lock


async def acquire_model_slot(model: str, *, input_tokens: Optional[int] = None,
                             request_units: int = 1) -> bool:
    """모델별 전역 RPM 슬롯을 얻는다. 대기했다면 True를 반환한다."""
    model = model or "unknown"
    _checked_request_units(request_units)
    period = _period_seconds()
    max_calls = _rpm_for_model(model)
    if request_units > max_calls:
        raise AiInputTokenBudgetExceeded('AI 요청 묶음이 분당 전송 보호값보다 큽니다. 묶음을 나눠주세요.')
    waited = False
    contention_started = None
    _MODEL_WAITERS[model] += 1
    try:
        while True:
            async with _model_lock(model):
                now = time.monotonic()
                calls = _MODEL_CALLS[model]
                while calls and now - calls[0] >= period:
                    calls.popleft()
                cooldown_wait = max(0.0, _MODEL_COOLDOWN_UNTIL[model] - now)
                can_admit = cooldown_wait <= 0 and len(calls) + request_units <= max_calls
                try:
                    # Recheck exhaustion even while the local RPM queue is
                    # full. A resumed waiter must not ignore a newly saved hold.
                    _reserve_daily_slot(model, reserve=can_admit, input_tokens=input_tokens,
                                        request_units=request_units)
                except _RateWindowWait as error:
                    wait_for = min(60, max(.01, error.seconds))
                except _QuotaStateBusy:
                    contention_started = contention_started or now
                    if now - contention_started >= 2:
                        raise AiQuotaStateUnavailable('AI 사용량 기록 잠금을 기다리다 중단했습니다. 잠시 후 다시 시도해주세요.')
                    wait_for = 0.05
                else:
                    contention_started = None
                    if can_admit:
                        calls.extend([time.monotonic()] * request_units)
                        return waited
                    rpm_wait = (period - (now - calls[0]) + 0.01
                                if calls and len(calls) + request_units > max_calls else 0)
                    wait_for = max(0.01, cooldown_wait, rpm_wait)
            waited = True
            await asyncio.sleep(wait_for)
    finally:
        _MODEL_WAITERS[model] = max(0, _MODEL_WAITERS[model] - 1)


def _reserve_daily_slot(model: str, *, reserve=True, input_tokens=None,
                        request_units: int = 1) -> None:
    global _STATE_WARNING
    _checked_request_units(request_units)
    try:
        with _quota_locked():
            changed = _refresh_state_locked()
            if changed:
                _save_state()
            _STATE_WARNING = None
            if _DAILY_EXHAUSTED.get('*') == _STATE_DATE or _DAILY_EXHAUSTED.get(model) == _STATE_DATE:
                raise AiDailyQuotaExhausted(
                    f'{model}의 일일 한도 또는 사용량 확인 문제로 요청을 보류했습니다. 다음 한도 초기화 후 다시 시도해주세요.')
            from services import ai_rate_policy
            policy = _quota_policy()
            limits = ai_rate_policy.effective_limits(policy, model)
            # Even a first unconfirmed single request needs shared safeguards;
            # removing an observation must not erase a recorded cooldown.
            if limits is None:
                limits = ai_rate_policy.effective_limits({'state': 'reconfirmation_required'}, model)
            if limits is not None:
                if input_tokens is None:
                    raise AiInputTokenBudgetExceeded('AI 입력 크기를 확인할 수 없어 전송을 보류했습니다.')
                window_path = _state_path() + '.minute.json'
                try:
                    window = ai_rate_policy.read_window(window_path)
                    admission = ai_rate_policy.window_admission(window, model, input_tokens, limits,
                                                                 request_units=request_units)
                except (ValueError, TypeError, OSError):
                    raise AiQuotaStateUnavailable('분당 AI 사용량 기록을 확인할 수 없어 전송을 보류했습니다.') from None
                if admission['too_large']:
                    raise AiInputTokenBudgetExceeded('이번 입력의 예상 크기 또는 묶음 항목 수가 분당 AI 전송 보호값보다 큽니다. '
                                                    '원문 범위, 입력 길이 또는 묶음 크기를 확인해주세요.')
                if admission.get('temporary_capacity'):
                    raise AiTemporaryRateLimit(admission['wait'])
                if admission['wait']:
                    raise _RateWindowWait(admission['wait'])
                if reserve:
                    # One entry per app protection unit, retaining the existing
                    # three-field schema and the exact total input estimate.
                    quotient, remainder = divmod(input_tokens, request_units)
                    stamp = time.time()
                    window['entries'].extend([model, stamp, quotient + (index < remainder)]
                                             for index in range(request_units))
                    # Write minute reservation first. If daily persistence fails,
                    # over-count briefly; never dispatch without both records.
                    _atomic_json(window_path, window)
            if not reserve:
                return
            before = _DAILY_CALLS.get(model, 0)
            _DAILY_CALLS[model] = before + request_units
            try:
                _save_state()
            except OSError:
                # Failed reservation is never permission to call the provider.
                _DAILY_CALLS[model] = before
                raise
    except (_RateWindowWait, AiTemporaryRateLimit):
        raise
    except AiQuotaStateUnavailable as error:
        _STATE_WARNING = str(error)
        raise


def reserve_sync_model_slot(model: str, *, input_tokens: Optional[int] = None,
                            request_units: int = 1) -> None:
    """Protect legacy SDK calls with the same durable usage and minute guards.

    Async adapters already reserve via acquire_model_slot and must not reserve
    twice. This admission does not pretend to measure provider/project TPM.
    Lock contention fails closed; no blocking sleep in a caller's event loop.
    """
    _reserve_daily_slot(model or 'unknown', input_tokens=input_tokens, request_units=request_units)


def set_model_cooldown(model: str, seconds: float) -> None:
    """429 retryDelay를 공유해 같은 모델의 새 호출도 함께 잠시 멈춘다."""
    if seconds <= 0:
        return
    _MODEL_COOLDOWN_UNTIL[model or "unknown"] = max(
        _MODEL_COOLDOWN_UNTIL[model or "unknown"], time.monotonic() + seconds
    )


def mark_daily_exhausted(model: str) -> None:
    """응답이 RPD 소진임을 명확히 알린 경우 해당 모델을 다음 태평양 날짜까지 차단."""
    global _STATE_WARNING
    model, day = model or 'unknown', _pacific_today()
    with _PENDING_LOCK:
        _PENDING_EXHAUSTED[model] = day
    try:
        # Durable outbox protects a scheduled process that exits while another
        # process still owns the ledger lock. Immutable intent files cannot
        # weaken a hold; ledger RMW and intent cleanup stay under lock.
        _persist_exhaustion_intent(model, day)
    except OSError:
        _STATE_WARNING = 'AI 한도 소진 기록을 저장할 수 없어 요청을 보류했습니다.'
    # Error handlers are synchronous: allow a short, finite handoff, not an
    # unbounded event-loop wait or an exception replacing the provider error.
    deadline = time.monotonic() + 0.2
    while True:
        try:
            with _quota_locked():
                _refresh_state_locked()
                _save_state()
                _STATE_WARNING = None
            return
        except _QuotaStateBusy as error:
            _STATE_WARNING = str(error)
            if time.monotonic() >= deadline:
                return  # Pending hold is merged before this process can call.
            time.sleep(0.01)
        except AiQuotaStateUnavailable as error:
            _STATE_WARNING = str(error)
            return  # Keep pending hold and original disk bytes for recovery.


def analyze_api_error(error: BaseException) -> dict:
    """SDK 버전에 덜 의존하도록 Gemini 오류를 한 곳에서 분류한다."""
    message = str(error)
    upper = message.upper()
    compact = re.sub(r'[^A-Z]', '', upper)
    raw_code = getattr(error, 'code', None) or getattr(error, 'status_code', None)
    raw_code = str(raw_code or '')
    code = int(raw_code) if len(raw_code) == 3 and raw_code.isascii() and raw_code.isdigit() else None
    # SDK status is authoritative. A token count or request ID containing 403,
    # 429 or 503 must not create an auth failure or a durable provider hold.
    text_codes = set(re.findall(r'(?<!\w)(?:401|403|429|503)(?!\w)', message)) if code is None else set()
    rate_limited = code == 429 or '429' in text_codes or "RESOURCE_EXHAUSTED" in upper
    daily = (
        isinstance(error, AiDailyQuotaExhausted)
        or rate_limited and (
            "REQUESTSPERDAY" in compact
            or re.search(r'\bRPD\b', upper) is not None
                and ("QUOTA" in upper or "EXHAUST" in upper))
    )
    unavailable = code == 503 or '503' in text_codes or "UNAVAILABLE" in upper
    auth_error = (code in (401, 403) or bool(text_codes & {'401', '403'})
                  or any(token in upper for token in ("API_KEY_INVALID", "INVALID API KEY")))
    match = re.search(r"retryDelay['\"\s:]+(\d+(?:\.\d+)?)s", message, re.IGNORECASE)
    request_minute = 'REQUESTSPERMINUTE' in compact
    token_minute = 'TOKENSPERMINUTE' in compact or 'INPUTTOKEN' in compact
    return {
        "local_budget_exhausted": isinstance(error, AiLocalBudgetExhausted),
        "daily_exhausted": daily,
        "rate_limited": rate_limited,
        "unavailable": unavailable,
        "auth_error": auth_error,
        "retry_after": float(match.group(1)) if match else None,
        "limit_kind": 'requests' if request_minute and not token_minute else
                      'tokens' if token_minute and not request_minute else 'unknown',
    }


def register_api_error(model: str, error: BaseException, default_cooldown: float = 30) -> dict:
    info = analyze_api_error(error)
    if info["local_budget_exhausted"]:
        return info  # Changing the app budget must not leave a provider hold.
    if info["daily_exhausted"]:
        mark_daily_exhausted(model)
    elif info["rate_limited"]:
        set_model_cooldown(model, (info["retry_after"] or default_cooldown) + 2)
        # Quotas may decrease after observation. A real 429 reduces admission
        # for an hour; expiry restores only the still-valid observed baseline.
        # Never edit Google's quota or raise a saved observation from an error.
        if info['rate_limited']:
            from services import ai_rate_policy
            try:
                with _quota_locked():
                    path = _state_path() + '.minute.json'
                    window = ai_rate_policy.read_window(path)
                    ai_rate_policy.lower_after_rate_error(window, model,
                        (info['retry_after'] or default_cooldown) + 2,
                        limit_kind=info['limit_kind'])
                    _atomic_json(path, window)
            except (AiQuotaStateUnavailable, ValueError, OSError):
                # Persist a conservative hold if cross-process throttling could
                # not be recorded; restarting must not clear a rate response.
                mark_daily_exhausted(model)
    return info


async def await_api(awaitable, timeout_seconds: Optional[float] = None):
    """모든 비동기 Gemini 요청에 공통 최대 응답 시간을 적용한다."""
    timeout_seconds = timeout_seconds or float(os.getenv("AI_REQUEST_TIMEOUT_SEC", "75"))
    try:
        async with asyncio.timeout(max(0.01, timeout_seconds)):
            return await awaitable
    except TimeoutError as exc:
        raise AiRequestTimeout(f"AI 서버가 {timeout_seconds:g}초 안에 응답하지 않았습니다") from exc


def _reset_for_tests() -> None:
    """테스트 격리 전용."""
    global _JOB_LOCK, _JOB_KIND, _JOB_LABEL, _JOB_STARTED_AT, _STATE_DATE, _STATE_LOADED
    global _PROCESS_LOCK_HANDLE, _EXTERNAL_BUSY, _STATE_WARNING
    _release_process_lock(_PROCESS_LOCK_HANDLE)
    _PROCESS_LOCK_HANDLE = None
    _EXTERNAL_BUSY = False
    _JOB_LOCK = asyncio.Lock()
    _JOB_KIND = None
    _JOB_LABEL = None
    _JOB_STARTED_AT = None
    _MODEL_LOCKS.clear()
    _MODEL_CALLS.clear()
    _MODEL_WAITERS.clear()
    _MODEL_COOLDOWN_UNTIL.clear()
    _DAILY_CALLS.clear()
    _DAILY_EXHAUSTED.clear()
    _STATE_DATE = None
    _STATE_LOADED = False
    _STATE_WARNING = None
    with _PENDING_LOCK:
        _PENDING_EXHAUSTED.clear()
