"""임베딩 서비스 — gemini-embedding-001 기반 벡터화.

용도:
  1) 의미 기반 검색 (키워드 FTS5를 보완하는 하이브리드 검색)
  2) 분류 전 관련성 보조 판정 및 정책 변경 중요도 비교 (사실 내용은 재활용하지 않음)

임베딩 모델의 실제 제공자 한도는 프로젝트별로 다르며 앱의 공통 요청 예산도 함께 적용된다.
벡터는 float32로 패킹하여 welfare_items.embedding (BLOB)에 저장한다.
"""
import os
import json
import asyncio
import datetime
import tempfile
import time
from collections import OrderedDict
from typing import List, Optional, Tuple

import numpy as np

import utils
from services.logger import setup_logger
from services import ai_coordinator
from services.ai_input_safety import AiInputReviewRequired, REVIEW_MESSAGE, ensure_ai_text_safe
from services.embedding_input_limits import embedding_input_fits, EMBEDDING_INPUT_LIMIT_MESSAGE

logger = setup_logger(__name__)

# 질의 임베딩 LRU 캐시: 같은 검색어 반복 시 재임베딩(API 호출) 방지.
# 검색(RETRIEVAL_QUERY)만 캐싱 — 문서 임베딩은 1회성이라 캐싱 이득이 없음.
_QUERY_CACHE_MAX = int(os.getenv('EMBED_QUERY_CACHE_SIZE', '256'))
_query_cache: "OrderedDict[str, List[float]]" = OrderedDict()

# 기본 수집의 의미 게이트 기준 문장은 항목 제목과 달리 여러 실행에서 반복된다.
# 설정/모델/차원이 같을 때만 재사용하여 무료 API 호출 1회를 매 수집마다 아낀다.
_SEMANTIC_PROFILE_CACHE_MAX = int(os.getenv('EMBED_PROFILE_CACHE_SIZE', '16'))
_semantic_profile_cache: "OrderedDict[tuple, List[float]]" = OrderedDict()
_ASYNC_QUOTA_RESERVED = object()  # Internal handoff, not a public budget bypass flag.


def _input_allows_embedding(*texts):
    try:
        ensure_ai_text_safe(*texts)
        return True
    except AiInputReviewRequired:
        logger.warning(REVIEW_MESSAGE)
        return False


def _log_embedding_admission_denied():
    logger.warning('AI 요청 한도 또는 사용량 기록 문제로 임베딩을 보류합니다. 단어 검색은 계속 사용할 수 있습니다.')

# gemini-embedding-001: 현행 Gemini API 임베딩 모델 (text-embedding-004는 이 키에서 폐기됨/404).
# output_dimensionality로 차원 축소 가능(기본 3072 → 768). 768 미만 차원은 비정규화 벡터라
# 코사인 비교 전 정규화 필요 → cosine_sim/rank_by_similarity가 이미 정규화하므로 안전.
EMBED_MODEL = os.getenv('EMBED_MODEL', 'gemini-embedding-001')
EMBED_DIM = int(os.getenv('EMBED_DIM', '768'))
# 의미 중복 판정 임계값 (코사인 유사도). 0.95 이상이면 사실상 동일 내용으로 간주.
DEDUP_THRESHOLD = float(os.getenv('EMBED_DEDUP_THRESHOLD', '0.95'))

# 검색 벡터는 생성 입력이 같아야 점수를 공정하게 비교할 수 있다. 과거 버전은
# 제목+본문/지원내용처럼 경로마다 재료가 달랐으므로, 현재 레시피를 DB에 함께 기록한다.
SEARCH_EMBEDDING_RECIPE = 'welfare-search-v2'
SOURCE_DEDUP_RECIPE = 'source-dedup-v1'


def build_search_embedding_text(item: dict) -> str:
    """검색용 문서 벡터 입력을 한 곳에서 만든다.

    사용자가 실제로 찾는 대상·거주지·생애주기·자격 조건을 포함하고, 비어 있는 값과
    동일한 반복 값은 제외한다. 수집 직후와 기존 데이터 재생성이 이 함수를 함께 쓴다.
    """
    fields = [
        ('제목', item.get('title')),
        ('분류', item.get('category')),
        ('지원 대상', item.get('target')),
        ('대상 특성', item.get('target_characteristics')),
        ('거주지', item.get('residence')),
        ('생애주기', item.get('life_cycle')),
        ('나이', item.get('age_detail')),
        ('소득 기준', item.get('target_income')),
        ('자격 조건', item.get('eligibility_detail')),
        ('지원 내용', item.get('support_detail')),
    ]
    lines = []
    seen = set()
    for label, raw in fields:
        if isinstance(raw, (list, tuple, set)):
            raw = ', '.join(str(v).strip() for v in raw if str(v).strip())
        value = ' '.join(str(raw or '').split())
        if not value:
            continue
        normalized = value.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        lines.append(f'{label}: {value}')
    return '\n'.join(lines)

_DTYPE = np.float32

# (2026-07-28, 2026-08-05 정확도 보강) 일일 임베딩 한도(무료 등급 1000건/일) 소진 감지 시
# 조기 차단. embed_text/embed_texts_batch 모두 재시도·레이트리미터가 전혀 없어, 한도 소진
# 후에도 남은 항목마다 계속 API를 호출해 매번 429를 받고 로그만 쌓였다(라이브 시연 중 실제
# 관찰: 정책 최신화 점검 한 번에 수백 줄의 "임베딩 생성 실패" 로그). 하루 한도(분당 한도와
# 달리 대기해도 안 풀림)로 확인되면, 그날 남은 호출을 API 왕복 없이 즉시 건너뛴다.
#
# (2026-08-05, GPT Codex 리뷰로 발견한 문제 2건 수정)
# ① Google의 RPD 한도는 태평양 시간 자정에 초기화되는데, 초기 구현은 한국 로컬 날짜
#    (datetime.date.today())로 판정했다. 한국은 태평양보다 16~17시간 빨라, 태평양 리셋보다
#    한참 먼저(최악의 경우 자정 직후) 차단이 풀려버려 아직 안 풀린 실제 한도에 재시도하고
#    또 429를 받는 문제가 있었다. zoneinfo로 실제 태평양 날짜를 기준으로 판정하도록 수정.
# ② 차단 상태가 프로세스 메모리에만 있어 앱을 재시작하면(또는 사이드카가 재기동되면) 바로
#    풀렸다. 작은 상태 파일(quota_state.json)에 영속화해 재시작 후에도 유지되게 했다 —
#    다만 디스크 I/O를 매 호출마다 하지 않도록, 같은 프로세스 안에서는 최초 1회만 파일을
#    읽고 이후엔 메모리 캐시를 쓴다(소진 표시 시점엔 메모리+파일 둘 다 즉시 갱신).
try:
    from zoneinfo import ZoneInfo
    _PACIFIC_TZ = ZoneInfo('America/Los_Angeles')
except Exception:
    _PACIFIC_TZ = None  # tzdata 없는 극단적 환경 대비 — 이 경우 UTC 날짜로 폴백(아래 사용처)

_daily_exhausted_date: Optional[str] = None   # 메모리 캐시 (프로세스 내 재조회 방지)
_state_loaded = False                          # 디스크에서 1회 로드했는지 여부


def _quota_state_path() -> str:
    return os.path.join(utils.get_app_data_dir(), 'quota_state.json')


def _pacific_today_str() -> str:
    """오늘 날짜를 태평양 시간 기준 ISO 문자열로. tzdata 문제로 zoneinfo를 못 쓰면 UTC로 폴백
    (한국 로컬시간보다는 훨씬 태평양에 가까워 그래도 개선)."""
    if _PACIFIC_TZ is not None:
        return datetime.datetime.now(_PACIFIC_TZ).date().isoformat()
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _load_persisted_exhausted_date() -> Optional[str]:
    try:
        with open(_quota_state_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError('잘못된 임베딩 한도 상태')
        value = data.get('embedding_daily_exhausted_date_pacific')
        if value is not None:
            datetime.date.fromisoformat(value)
        return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        logger.warning('임베딩 사용량 기록을 읽지 못해 오늘의 요청을 보류합니다.')
        today = _pacific_today_str()
        try:
            import shutil
            shutil.copy2(_quota_state_path(), _quota_state_path() + f'.corrupt.{time.time_ns()}')
            _save_persisted_exhausted_date(today)
        except OSError:
            pass
        return today


def _save_persisted_exhausted_date(date_str: str) -> None:
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix='embedding_quota_', suffix='.tmp', dir=os.path.dirname(_quota_state_path()))
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'embedding_daily_exhausted_date_pacific': date_str}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, _quota_state_path())
        tmp_path = None
    except OSError as e:
        logger.warning(f"임베딩 한도 상태 저장 실패(무시 가능): {e}")
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _is_daily_quota_error(exc: Exception) -> bool:
    msg = str(exc)
    return 'RESOURCE_EXHAUSTED' in msg and 'PerDay' in msg


def _daily_quota_exhausted_today() -> bool:
    global _daily_exhausted_date, _state_loaded
    if not _state_loaded:
        _daily_exhausted_date = _load_persisted_exhausted_date()
        _state_loaded = True
    return _daily_exhausted_date == _pacific_today_str()


def _mark_daily_quota_exhausted():
    global _daily_exhausted_date, _state_loaded
    _daily_exhausted_date = _pacific_today_str()
    _state_loaded = True
    _save_persisted_exhausted_date(_daily_exhausted_date)


def get_unavailable_reason() -> Optional[str]:
    """(2026-08-05, GPT Codex 3차 리뷰) 임베딩을 지금 시도하면 실패가 뻔한 상태인지
    호출 전에 미리 판정. API 엔드포인트가 "API 키를 확인하세요"라는 오해의 소지가 있는
    문구를 일일 한도 소진 때도 그대로 보여주고 있었다(영속화 이후로는 태평양 자정까지
    이 메시지가 계속 뜨게 되어 더 눈에 띄는 문제가 됨) — 두 원인을 구분할 수 있게 한다.

    반환값: None(정상 시도 가능) | 'missing_api_key' | 'daily_quota_exhausted'
    (분당 한도 초과·네트워크 오류처럼 호출 전엔 알 수 없는 원인은 호출자가 실제 실패
    이후 일반 오류로 처리해야 한다 — 여기서 예측하지 않는다).
    """
    if _get_client() is None:
        return 'missing_api_key'
    if _daily_quota_exhausted_today():
        return 'daily_quota_exhausted'
    return None


def _get_client():
    """genai 클라이언트 반환. 키 없으면 None."""
    key = os.getenv('GOOGLE_API_KEY')
    if not key:
        return None
    from google import genai
    from services import ai_coordinator
    return ai_coordinator.create_client(key)


def _input_size_allows_embedding(text: str) -> bool:
    if embedding_input_fits(text):
        return True
    logger.warning(EMBEDDING_INPUT_LIMIT_MESSAGE)
    return False


def embed_text(text: str, task_type: str = 'RETRIEVAL_DOCUMENT',
               client=None, *, _quota_reservation=None) -> Optional[List[float]]:
    """단일 텍스트를 임베딩 벡터(list[float])로 변환. 실패 시 None.

    동기 함수 — async 컨텍스트에서는 database.run_db()로 감싸 호출한다.
    task_type: 저장은 'RETRIEVAL_DOCUMENT', 검색 질의는 'RETRIEVAL_QUERY'.
    """
    if not _input_allows_embedding(text) or not _input_size_allows_embedding(text):
        return None
    if not text.strip():
        return None

    is_query = task_type == 'RETRIEVAL_QUERY'
    if is_query and text in _query_cache:
        _query_cache.move_to_end(text)  # LRU 갱신
        return _query_cache[text]

    if _daily_quota_exhausted_today():
        return None

    client = client or _get_client()
    if client is None:
        return None
    try:
        from google.genai import types
        if _quota_reservation is not _ASYNC_QUOTA_RESERVED:
            ai_coordinator.reserve_sync_model_slot(EMBED_MODEL,
                input_tokens=ai_coordinator.estimate_input_tokens(text))
        resp = client.models.embed_content(
            model=EMBED_MODEL,
            contents=text,
            config=types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=EMBED_DIM,
            ),
        )
        vec = resp.embeddings[0].values
        result = list(vec) if vec else None
        if is_query and result is not None:
            _query_cache[text] = result
            _query_cache.move_to_end(text)
            while len(_query_cache) > _QUERY_CACHE_MAX:
                _query_cache.popitem(last=False)  # 가장 오래된 항목 제거
        return result
    except (ai_coordinator.AiDailyQuotaExhausted, ai_coordinator.AiQuotaStateUnavailable):
        _log_embedding_admission_denied()
        return None
    except Exception as e:
        ai_coordinator.register_api_error(EMBED_MODEL, e)
        if _is_daily_quota_error(e):
            _mark_daily_quota_exhausted()
            logger.warning(f"임베딩 일일 한도 소진 감지 — 오늘 남은 임베딩 호출을 생략합니다: {e}")
        else:
            logger.warning(f"임베딩 생성 실패: {e}")
        return None


async def embed_text_async(text: str, task_type: str = 'RETRIEVAL_DOCUMENT',
                           client=None) -> Optional[List[float]]:
    """embed_text의 async 래퍼 (스레드풀에서 동기 호출 실행)."""
    if not _input_allows_embedding(text) or not _input_size_allows_embedding(text):
        return None
    prepared = text
    is_cached_query = task_type == 'RETRIEVAL_QUERY' and prepared in _query_cache
    can_call = bool(prepared.strip()) and not is_cached_query and not _daily_quota_exhausted_today()
    can_call = can_call and (client is not None or _get_client() is not None)
    if can_call:
        try:
            await ai_coordinator.acquire_model_slot(EMBED_MODEL,
                input_tokens=ai_coordinator.estimate_input_tokens(prepared))
        except (ai_coordinator.AiDailyQuotaExhausted, ai_coordinator.AiQuotaStateUnavailable):
            _log_embedding_admission_denied()
            return None
    return await asyncio.to_thread(embed_text, text, task_type, client,
        _quota_reservation=_ASYNC_QUOTA_RESERVED if can_call else None)


async def embed_semantic_profile_async(text: str, client=None) -> Optional[List[float]]:
    """의미 게이트의 반복 기준 문장을 모델·차원·문장별로 메모리 캐시한다."""
    if not _input_allows_embedding(text) or not _input_size_allows_embedding(text):
        return None
    prepared = text
    if not prepared.strip():
        return None
    key = (EMBED_MODEL, EMBED_DIM, prepared)
    cached = _semantic_profile_cache.get(key)
    if cached is not None:
        _semantic_profile_cache.move_to_end(key)
        return cached
    result = await embed_text_async(text, task_type='SEMANTIC_SIMILARITY', client=client)
    if result is not None:
        _semantic_profile_cache[key] = result
        _semantic_profile_cache.move_to_end(key)
        while len(_semantic_profile_cache) > _SEMANTIC_PROFILE_CACHE_MAX:
            _semantic_profile_cache.popitem(last=False)
    return result


def _batch_input_budget():
    """Planning only. Every actual chunk still needs its own durable reservation."""
    try:
        budget = ai_coordinator.input_token_budget(EMBED_MODEL)
        if budget is None or type(budget) is int and budget >= 0:
            return budget
    except Exception:
        pass
    return 0


def _batch_request_budget():
    """Logical embedding items, not HTTP calls; unavailable budgets fail closed."""
    try:
        budget = ai_coordinator.input_request_budget(EMBED_MODEL)
        if budget is None or type(budget) is int and budget >= 0:
            return budget
    except Exception:
        pass
    return 0


def _next_embedding_chunk(prepared, start, budget, request_budget=None):
    """Largest ordered prefix within token and item budgets, not smaller texts.

    Empty and individually oversized inputs keep their original None positions.
    Re-read the budget for the next request so a new provider penalty is honored.
    """
    indices, contents = [], []
    skipped_large = False
    while start < len(prepared):
        if request_budget is not None and len(contents) >= request_budget:
            break
        text = prepared[start]
        if not embedding_input_fits(text):
            skipped_large = True
            start += 1
            continue
        if not text.strip():
            start += 1
            continue
        if budget is not None:
            if ai_coordinator.estimate_input_tokens([text]) > budget:
                skipped_large = True
                start += 1
                continue
            if contents and ai_coordinator.estimate_input_tokens([*contents, text]) > budget:
                break
        indices.append(start)
        contents.append(text)
        start += 1
    return start, indices, contents, skipped_large


def _batch_inputs_unchanged(texts, snapshot):
    # Async admission may yield while a caller mutates the original list. Never
    # send new input under the old estimate, including private text in later rows.
    current = tuple(texts)
    if current == snapshot:
        return True  # Immutable strings already passed the full-batch preflight.
    _input_allows_embedding(*current)
    return False


def _embed_batch_chunk(contents, task_type, client, *, texts, snapshot,
                       _quota_reservation=None):
    """Exactly one SDK request. Returns (vectors, stop); never re-chunks/retries."""
    if not _batch_inputs_unchanged(texts, snapshot) or _daily_quota_exhausted_today():
        return None, True
    try:
        from google.genai import types
        if _quota_reservation is not _ASYNC_QUOTA_RESERVED:
            ai_coordinator.reserve_sync_model_slot(EMBED_MODEL,
                input_tokens=ai_coordinator.estimate_input_tokens(contents),
                request_units=len(contents))
        if not _batch_inputs_unchanged(texts, snapshot):
            return None, True
        resp = client.models.embed_content(
            model=EMBED_MODEL,
            contents=contents,
            config=types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=EMBED_DIM,
            ),
        )
        # Missing/extra response rows must not shift vectors onto another input.
        if not isinstance(resp.embeddings, (list, tuple)) or len(resp.embeddings) != len(contents):
            logger.warning('배치 임베딩 응답 개수가 달라 이번 묶음을 보류합니다.')
            return None, True
        return [list(emb.values) if emb.values else None for emb in resp.embeddings], False
    except (ai_coordinator.AiDailyQuotaExhausted, ai_coordinator.AiQuotaStateUnavailable):
        _log_embedding_admission_denied()
        return None, True
    except Exception as e:
        ai_coordinator.register_api_error(EMBED_MODEL, e)
        if _is_daily_quota_error(e):
            _mark_daily_quota_exhausted()
            logger.warning('임베딩 일일 한도 소진 감지 — 오늘 남은 임베딩 호출을 생략합니다.')
        else:
            logger.warning('배치 임베딩 요청을 마치지 못해 남은 묶음을 보류합니다.')
        return None, True


def embed_texts_batch(texts: List[str], task_type: str = 'RETRIEVAL_DOCUMENT',
                      client=None, *, _quota_reservation=None) -> List[Optional[List[float]]]:
    """입력 보호값 안에서 최대한 묶어 임베딩하고, 완료한 결과는 보존한다.

    원문을 자르지 않는다. 빈 입력·단독으로도 너무 큰 입력·중단 뒤 남은 입력은
    같은 자리에 None을 남겨 상위 백필/재개 경로가 다시 처리할 수 있게 한다.
    """
    snapshot = tuple(texts)
    results: List[Optional[List[float]]] = [None] * len(snapshot)
    if not snapshot or not _input_allows_embedding(*snapshot) or _daily_quota_exhausted_today():
        return results
    prepared = snapshot
    if not any(text.strip() for text in prepared):
        return results
    client = client or _get_client()
    if client is None:
        return results
    start, warned_large = 0, False
    while start < len(prepared):
        budget = _batch_input_budget()
        request_budget = _batch_request_budget()
        if budget == 0 or request_budget == 0:
            _log_embedding_admission_denied()
            break
        start, indices, contents, oversized = _next_embedding_chunk(prepared, start, budget, request_budget)
        if oversized and not warned_large:
            logger.warning('단일 입력이 AI 전송 보호값보다 커 해당 임베딩은 보류합니다. 다른 자료는 계속 처리합니다.')
            warned_large = True
        if not contents:
            continue
        # Public batch calls always reserve each chunk. The old private handoff
        # argument cannot share one reservation across multiple SDK requests.
        vectors, stop = _embed_batch_chunk(contents, task_type, client, texts=texts, snapshot=snapshot)
        if vectors is not None:
            for index, vector in zip(indices, vectors):
                results[index] = vector
        if stop:
            break
    return results


async def embed_texts_batch_async(texts: List[str], task_type: str = 'RETRIEVAL_DOCUMENT',
                                  client=None) -> List[Optional[List[float]]]:
    """묶음마다 비동기 슬롯을 예약하고 정확히 한 동기 요청에만 넘긴다."""
    snapshot = tuple(texts)
    results: List[Optional[List[float]]] = [None] * len(snapshot)
    if not snapshot or not _input_allows_embedding(*snapshot) or _daily_quota_exhausted_today():
        return results
    prepared = snapshot
    if not any(text.strip() for text in prepared):
        return results
    client = client or _get_client()
    if client is None:
        return results
    start, warned_large = 0, False
    while start < len(prepared):
        budget = _batch_input_budget()
        request_budget = _batch_request_budget()
        if budget == 0 or request_budget == 0:
            _log_embedding_admission_denied()
            break
        start, indices, contents, oversized = _next_embedding_chunk(prepared, start, budget, request_budget)
        if oversized and not warned_large:
            logger.warning('단일 입력이 AI 전송 보호값보다 커 해당 임베딩은 보류합니다. 다른 자료는 계속 처리합니다.')
            warned_large = True
        if not contents:
            continue
        if not _batch_inputs_unchanged(texts, snapshot):
            break
        try:
            await ai_coordinator.acquire_model_slot(EMBED_MODEL,
                input_tokens=ai_coordinator.estimate_input_tokens(contents),
                request_units=len(contents))
        except (ai_coordinator.AiDailyQuotaExhausted, ai_coordinator.AiQuotaStateUnavailable):
            _log_embedding_admission_denied()
            break
        vectors, stop = await asyncio.to_thread(_embed_batch_chunk, contents, task_type, client,
            texts=texts, snapshot=snapshot, _quota_reservation=_ASYNC_QUOTA_RESERVED)
        if vectors is not None:
            for index, vector in zip(indices, vectors):
                results[index] = vector
        if stop:
            break
    return results


# ── 직렬화 ────────────────────────────────────────────────────────────────────
def to_blob(vec: Optional[List[float]]) -> Optional[bytes]:
    """벡터를 float32 바이트로 패킹 (DB BLOB 저장용)."""
    if vec is None:
        return None
    arr = np.asarray(vec, dtype=_DTYPE)
    return arr.tobytes()


def from_blob(blob: Optional[bytes]) -> Optional[np.ndarray]:
    """BLOB을 정규화된 float32 벡터로 복원. 길이 불일치 시 None."""
    if not blob:
        return None
    try:
        arr = np.frombuffer(blob, dtype=_DTYPE)
        if arr.size == 0:
            return None
        return arr
    except Exception:
        return None


# ── 유사도 ────────────────────────────────────────────────────────────────────
def _normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    """두 벡터의 코사인 유사도."""
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def rank_by_similarity(
    query: np.ndarray,
    candidates: List[Tuple[object, np.ndarray]],
    limit: int = 20,
    min_score: float = 0.0,
) -> List[Tuple[object, float]]:
    """query에 대한 후보들의 코사인 유사도를 계산해 상위 limit개 반환.

    candidates: [(payload, vector), ...]. payload는 호출자가 식별에 쓰는 임의 값.
    반환: [(payload, score), ...] (score 내림차순).
    벡터화 연산으로 수천 건도 빠르게 처리.
    """
    if query is None or not candidates:
        return []
    valid = [(p, v) for p, v in candidates if v is not None and v.size == query.size]
    if not valid:
        return []
    mat = np.vstack([v for _, v in valid])
    q = query / (np.linalg.norm(query) or 1.0)
    mat_n = _normalize(mat)
    scores = mat_n @ q
    order = np.argsort(-scores)
    out: List[Tuple[object, float]] = []
    for idx in order[:limit]:
        score = float(scores[idx])
        if score < min_score:
            break
        out.append((valid[idx][0], score))
    return out
