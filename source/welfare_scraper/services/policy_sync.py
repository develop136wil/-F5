"""정책 최신화 파이프라인 — 연결된 Notion 정책 보관함을 최신 상태로 유지하기 위한 변경 감지.

프로그램의 주 목적: 사용자가 연결한 Notion 정책 보관함을 최신 상태로 유지한다.
각 정책 페이지의 '관련홈페이지1·2·3'(출처 URL)을 감시원으로 삼는다.

핵심 = 2단계 AI 최소 호출:
  Stage 1 (AI 0회): 출처 URL 본문을 이전에 가져온 원문과 해시 비교(policy_watch 재활용).
                    원문이 안 바뀌면 여기서 끝 → AI 호출 없음.
  Stage 2 (변경분만): 바뀐 출처만 AI 재추출 → Notion 현재값과 필드별 비교.
                    핵심 필드(대상연령/자격/소득/금액·기간) 실질 변경만 🔴,
                    서술 재표현(코사인 높음)은 ⚪로 강등 → 오탐 제거.

자동 덮어쓰기 없음. 감지 결과를 제시만 하고 최종 반영은 사용자가 수동으로 한다.
"""
import os
import re
import json
import asyncio
import hashlib
import datetime
import difflib
import time
from urllib.parse import urlparse
from typing import Dict, Any, List, Optional, Tuple

from google.genai import types

import database
import utils
import ai_classifier
from services import notion_service, embedding_service
from services import policy_watch  # _normalize_text 재사용
from services.logger import setup_logger

logger = setup_logger(__name__)

# 동시 출처 fetch 제한 (사이트/Notion rate limit 안전선)
_FETCH_CONCURRENCY = 4
# 사업명 유사도 매칭 임계 (링크 불일치인데 이 이상이면 '유사정책 ⚠️')
_TITLE_SIM_THRESHOLD = float(utils.get_env_value("POLICY_TITLE_SIM") or 0.90)
# 서술 필드 재표현 판정 임계 (이 이상이면 실질변경 아님 → ⚪ 강등)
# 0.85: 최초 감사 실측(2026-07-02)에서 "무관"↔"소득 무관"(0.896) 등 동일 의미 표현이
# 0.92 문턱에 살짝 못 미쳐 오탐(🔴)으로 잡히는 사례가 다수 확인돼 완화.
_REPHRASE_COSINE = float(utils.get_env_value("POLICY_REPHRASE_COSINE") or 0.85)

# 진행 상태 (폴링용)
POLICY_STATUS: Dict[str, Any] = {
    "running": False, "phase": "", "checked": 0, "total": 0,
    "changed": 0, "candidates": 0, "message": "",
    "ai_required": 0, "ai_completed": 0, "ai_rate_waited": 0, "ai_failed": 0, "ai_preflight_held": 0,
    "review_count": 0, "review_notes": [],
}
# 동기화 전용 진행 상태 (점검과 별도 — 동시에 진행 상황을 각각 폴링)
SYNC_STATUS: Dict[str, Any] = {
    "running": False, "checked": 0, "total": 0, "message": "", "notion_rate_waited": 0,
}
# 최초 감사 전용 진행 상태 — Notion에 이미 적힌 내용과 "지금" 출처 원문을 1회 비교(베이스라인만 있고
# 아직 한 번도 Stage2 비교가 안 된 정책 대상). 이후 정기 점검(check_policies)은 원문 변경분만 비교.
AUDIT_STATUS: Dict[str, Any] = {
    "running": False, "checked": 0, "total": 0, "changed": 0, "message": "",
    "review_count": 0, "review_notes": [],
}
# (T3) 스타일 제안 변환 진행 상태
STYLE_STATUS: Dict[str, Any] = {
    "running": False, "checked": 0, "total": 0, "styled": 0, "message": "",
}
# (B1 2026-07-13) "한 번에 점검" 통합 액션 진행 상태 — 사용자의 실제 작업 단위(정책 정보가
# 최신인지 확인)에 맞춰 동기화→점검을 순서대로 실행한다. phase로 지금 어느 단계인지 표시.
# 최초 비교도 동일 출처 점검 안에서 처리하고, 필요한 제안 정리만 이어서 수행한다.
# 전역 AI 한도는 유지하며 Notion 쓰기는 사용자 승인 경로로만 수행한다.
ONE_CLICK_STATUS: Dict[str, Any] = {
    "running": False, "phase": "", "message": "",
    "ai_required": 0, "ai_completed": 0, "ai_rate_waited": 0, "ai_failed": 0, "ai_preflight_held": 0,
    "changed": 0,
    "review_count": 0, "review_notes": [],
}


def _record_extraction_review(status, title, review):
    """Bounded public diagnostics; never store source text or model output."""
    if not isinstance(review, dict) or not review.get('code'):
        return
    status['review_count'] = status.get('review_count', 0) + 1
    notes = status.setdefault('review_notes', [])
    if len(notes) < 20:
        note = review.get('note')
        if not isinstance(note, str) or not note.strip():
            from services import policy_review_store
            note = policy_review_store._NOTES.get(review['code'], '원문 확인이 필요합니다. 기존 내용은 유지됩니다.')
        entry = {'title': str(title)[:120], 'code': review['code'],
                 'note': note[:1500], 'fields': review.get('fields', [])}
        if isinstance(review.get('diagnostics'), dict):
            from services.policy_evidence import public_policy_diagnostics
            entry['diagnostics'] = public_policy_diagnostics(review['diagnostics'])
        notes.append(entry)


def _extraction_review_summary(status):
    count = status.get('review_count', 0)
    if not count:
        return ''
    codes = {item.get('code') for item in status.get('review_notes', [])}
    reasons = []
    if 'source_too_long' in codes:
        reasons.append('긴 원문 확인 필요')
    if 'ai_input_review_required' in codes:
        reasons.append('민감정보 전송 전 확인 필요')
    if 'source_conditions_incomplete' in codes:
        reasons.append('신청·제한 조건 확인 필요')
    if 'model_abstained' in codes:
        reasons.append('AI 추출 미확정')
    if 'source_criteria_review_required' in codes:
        reasons.append('소득·나이 적용 기준 확인 필요')
    comparison_codes = {'source_fact_changed', 'source_evidence_mismatch',
                        'source_evidence_boundary_changed', 'source_presentation_mismatch'}
    if codes & comparison_codes:
        reasons.append('표현·근거 대조 필요')
    if codes - {'source_too_long', 'ai_input_review_required', 'source_conditions_incomplete', 'model_abstained', 'source_criteria_review_required'} - comparison_codes:
        reasons.append('원문 근거 확인 필요')
    reason = '·'.join(reasons) or '원문 확인 필요'
    return f' · 보류 {count}건 ({reason}, 기존 내용 유지)'


async def _persist_policy_review(status, operation, *args):
    """A missing durable record is a visible failure, never apparent success."""
    from services import policy_review_store
    try:
        return await database.run_db(getattr(policy_review_store, operation), *args)
    except Exception as error:
        note = ('원문 보류 기록을 저장하지 못해 점검을 중단했습니다. 기존 정책 내용과 보류 기록은 유지됩니다. '
                '저장 공간과 DB 사용 상태를 확인한 뒤 다시 점검해주세요.'
                if operation == 'record_hold' else
                '정책 재점검 결과는 저장했지만 보류 목록 상태를 갱신하지 못했습니다. '
                '기존 보류 표시는 유지됩니다. 저장 공간과 DB 사용 상태를 확인한 뒤 다시 점검해주세요.')
        status['review_storage_error'] = note
        # Keep DB/URL/path/credential details out of the public background error.
        raise RuntimeError(note) from error

# (2026-08-28, GPT Codex 지적 ③+⑦) 정책 동기화·출처점검·통합점검·최초감사·진단재계산·
# 스타일재작성 + DB복원이 각자 독립된 *_STATUS["running"] 플래그만 확인해, (a) run_full_check()가
# sync_policies()/check_policies()를 직접 함수 호출해 그 엔드포인트 가드를 우회하고, (b) 거의
# 동시에 두 요청이 오면 "확인 후 백그라운드 등록" 사이에 await 지점이 있어 체크-후-설정이
# 원자적이지 않아 둘 다 통과할 수 있었다(코드로 확인). 이 모든 작업이 공유하는 유지보수 락
# 하나로 직렬화한다. 락 취득·해제는 API 엔드포인트 계층(api_server.py)에서만 하고, 이 서비스
# 함수들(sync_policies/check_policies/run_full_check 등) 자체는 그대로 둔다 — run_full_check가
# sync_policies/check_policies를 내부 호출할 때 락을 또 잡으면(재진입 불가) 데드락이 나기 때문.
_MAINTENANCE_LOCK = asyncio.Lock()
_MAINTENANCE_HOLDER: Optional[str] = None  # 사용자에게 "지금 뭐가 진행 중인지" 보여주기 위한 라벨
_MAINTENANCE_KIND: Optional[str] = None  # "exclusive"(장기 작업) | "mutation"(짧은 필드/상태 변경)


def _get_maintenance_lock() -> asyncio.Lock:
    """현재 이벤트 루프에서 사용할 유지보수 락.

    운영 서버는 단일 루프지만 IsolatedAsyncioTestCase와 개발 중 모듈 재로딩은 서로 다른
    루프에서 같은 모듈 전역을 재사용할 수 있다. 이전 루프에 바인딩된 *해제 상태* 락이면
    안전하게 새 락으로 교체한다. 잠긴 락을 다른 루프에서 교체하는 것은 금지한다.
    """
    global _MAINTENANCE_LOCK
    loop = asyncio.get_running_loop()
    bound_loop = getattr(_MAINTENANCE_LOCK, '_loop', None)
    if bound_loop is not None and bound_loop is not loop:
        if _MAINTENANCE_LOCK.locked():
            raise RuntimeError("유지보수 락이 다른 이벤트 루프에서 사용 중입니다")
        _MAINTENANCE_LOCK = asyncio.Lock()
    return _MAINTENANCE_LOCK


def maintenance_busy_message() -> Optional[str]:
    """유지보수 락이 잡혀 있으면 사용자에게 보여줄 메시지, 아니면 None."""
    if _MAINTENANCE_LOCK.locked():
        return f"다른 정책 작업({_MAINTENANCE_HOLDER or '진행 중'})이 끝난 후 다시 시도해주세요."
    return None


async def try_acquire_maintenance_lock(label: str) -> bool:
    """즉시 잠그거나(성공) 이미 잠겨 있으면 False. `locked()` 확인과 `acquire()` 사이에
    await 지점이 없어(잠기지 않은 락의 acquire()는 이벤트 루프에 양보하지 않고 바로 반환되므로)
    두 문장이 원자적으로 동작한다 — 예전의 "if STATUS['running']: raise 409" 방식은 이 확인과
    실제 running=True 설정 사이에 간극이 있어 거의 동시 요청이 둘 다 통과할 수 있었다."""
    global _MAINTENANCE_HOLDER, _MAINTENANCE_KIND
    from services import app_lifecycle
    if app_lifecycle.shutdown_requested:
        return False
    lock = _get_maintenance_lock()
    if lock.locked():
        return False
    await lock.acquire()
    _MAINTENANCE_HOLDER = label
    _MAINTENANCE_KIND = "exclusive"
    return True


async def acquire_db_mutation_lock(label: str) -> bool:
    """복원과 겹치면 안 되는 DB 변경 작업용 공유 게이트.

    장기 유지보수 작업이 잡고 있으면 즉시 False를 반환한다. 다른 짧은 변경이 잡고 있으면
    끝날 때까지 기다린 뒤 순차 실행한다. 이 락을 실제 점유하므로, 예전처럼 busy 상태를
    확인한 직후 유지보수가 끼어드는 TOCTOU 경합이 없다.
    """
    global _MAINTENANCE_HOLDER, _MAINTENANCE_KIND
    lock = _get_maintenance_lock()
    if lock.locked() and _MAINTENANCE_KIND != "mutation":
        return False
    await lock.acquire()
    _MAINTENANCE_HOLDER = label
    _MAINTENANCE_KIND = "mutation"
    return True


async def acquire_policy_mutation_lock(label: str) -> bool:
    """이전 호출부와 의미를 분명히 하기 위한 정책 변경 전용 별칭."""
    return await acquire_db_mutation_lock(label)


def release_maintenance_lock() -> None:
    global _MAINTENANCE_HOLDER, _MAINTENANCE_KIND
    _MAINTENANCE_HOLDER = None
    _MAINTENANCE_KIND = None
    if _MAINTENANCE_LOCK.locked():
        _MAINTENANCE_LOCK.release()


# ==================== 값 정규화·비교 ====================
def _norm_url(u: str) -> str:
    return (u or "").strip().rstrip("/").lower()


def policy_source_url_exists(url: str) -> bool:
    """현재 연결된 어느 정책 보관함에든 같은 출처 URL이 있으면 True.

    일반 수집 후보와 URL 직접 초안이 같은 중복 판정을 공유한다. 초안 생성 직전에도
    다시 호출해 미리보기 이후 다른 사용자가 같은 정책을 추가한 경우를 막는다.
    """
    norm = _norm_url(url)
    if not norm:
        return False
    active_keys = _active_configured_keys()
    if not active_keys:
        return False
    placeholders = ",".join("?" for _ in active_keys)
    rows = database.execute_query(
        f"SELECT source_urls FROM notion_policies WHERE db_key IN ({placeholders})",
        tuple(active_keys), raise_on_error=True) or []
    for row in rows:
        if any(_norm_url(source) == norm
               for source in utils.parse_json_safe(row["source_urls"], [])):
            return True
    return False


def _norm_text(v: str) -> str:
    """공백·문장부호 제거 후 비교용 문자열."""
    return re.sub(r"[\s,./·|]+", "", str(v or "")).strip()


def _norm_cat(v: str) -> str:
    """분류 정규화: 한글만 남김 ('의료/재활' == '의료재활')."""
    return re.sub(r"[^가-힣]", "", str(v or ""))


def _norm_set(v) -> set:
    """multi_select 값(list 또는 콤마문자열)을 집합으로."""
    if isinstance(v, list):
        items = v
    else:
        items = str(v or "").split(",")
    return {_norm_text(x) for x in items if _norm_text(x)}


_MONEY_RE = re.compile(r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)\s*(?:억\s*원?|만\s*원?|천\s*원|원)")
_DURATION_RE = re.compile(r"(?<!\d)\d+(?:\.\d+)?\s*(?:개월|개년|년간|년|개월간|주간|주|일간|일|시간|분|회|%)")
# 마지막 대안(\d{1,2}[-.\/]\d{1,2})은 전화번호(02-995-6800의 "02-99"/"5-68" 조각)를
# 날짜로 오인하는 문제가 실측에서 확인돼, 앞뒤로 숫자·하이픈이 더 이어지지 않는 경우만 인정.
_DATE_RE = re.compile(
    r"(\d{1,2})\s*월\s*(\d{1,2})\s*일|(\d{4})[-.\/](\d{1,2})[-.\/](\d{1,2})"
    r"|(?<![\d-])(\d{1,2})[-.\/](\d{1,2})(?![\d-])"
)
# 전화번호 패턴(02-995-6800, 010-1234-5678 등)은 날짜 토큰 추출 전에 미리 제거
_PHONE_RE = re.compile(r"\d{2,4}-\d{3,4}-\d{4}")
# 검색·목록 페이지는 정책 내용이 아니라 UI 텍스트라 AI 비교에 부적합 — 대상에서 제외
_LOW_VALUE_URL_RE = re.compile(r"search(_new)?\.asp|[?&]keyword=|board(list|List)|/notice/list|/board/list", re.I)
# (2026-07-07) 기관 포털의 루트/메인 페이지 — 특정 정책 1건이 아니라 기관 전체를 다루는
# 페이지라 실측(id 38/39: 무관한 다른 사업 내용 오염 / id 74/75: 빈 플레이스홀더로 채워짐)에서
# 다중 출처 비교시 AI 추출을 왜곡시킨 사례가 발견돼 추출 대상에서 제외한다.
_INDEXY_PATH_RE = re.compile(r"/(index|main|home)(\.\w+)?/?$", re.I)


def is_low_value_source(url: str) -> bool:
    """검색결과·게시판목록·기관 포털 루트/메인 페이지 등 정책 본문이 아닌 URL(비교에 부적합)."""
    url = url or ""
    if _LOW_VALUE_URL_RE.search(url):
        return True
    if _INDEXY_PATH_RE.search(url):
        return True
    path = urlparse(url).path.rstrip("/")
    if path == "":
        return True
    return False


_TITLE_STOPWORDS = {"지원", "사업", "프로그램", "서비스", "안내", "교실"}


def _pick_relevant_snippet(content: str, title: str, window: int = 400) -> str:
    """Select explicit policy sections; never crop at an arbitrary keyword.

    window is retained for caller compatibility, not used to truncate evidence.
    Unstructured pages still require subsequent evidence validation.
    """
    from services.policy_source_section import select_policy_section
    return select_policy_section(content, title)


def _policy_evidence_text(content: str, title: str, source_count: int = 1) -> str:
    """Keep cross-source boundaries: one page's heading cannot hide another.

    Several explicitly configured sources are one complete evidence set. Do
    not select a single heading from their concatenation; length/token/source
    checks still hold the whole request if too large, without cutting it.
    """
    return content if source_count > 1 else _pick_relevant_snippet(content, title)


def _money_date_tokens(text: str) -> set:
    """Amounts, dates and durations. Shared tokens never cancel changed facts."""
    t = _PHONE_RE.sub(" ", str(text or ""))
    toks = set()
    for m in _MONEY_RE.finditer(t):
        toks.add(m.group(0).replace(" ", "").replace(",", ""))
    for m in _DATE_RE.finditer(t):
        toks.add(m.group(0).replace(" ", ""))
    for m in _DURATION_RE.finditer(t):
        toks.add(m.group(0).replace(" ", ""))
    return toks


def _factual_text(text: str) -> str:
    """Remove proven presentation-only differences, not decimal/date punctuation."""
    return re.sub(r"\s+", "", _strip_markdown_symbols(str(text or "")))


def _fmt_age(fields: Dict[str, Any]) -> str:
    """대상연령 표시/비교 문자열. 월령·세부연령 모두 없으면 빈 문자열(placeholder 텍스트 금지 —
    "None~None|" 같은 값이 실제 정보처럼 보여 빈 값 비대칭 판정을 피해가는 버그가 있었음)."""
    s, e = fields.get("start_age_months"), fields.get("end_age_months")
    detail = _norm_text(fields.get("age_detail"))
    if s is None and e is None and not detail:
        return ""
    return f"{s}~{e}|{detail}"


# (2026-07-15) 대상연령↔생애주기 정합성 안내 — AI가 같은 원문에서 두 필드를 각각 독립
# 추출해 서로 모순되는 경우(예: "만 18세 이하"인데 생애주기엔 영아가 빠짐)를 자동으로
# 고치지 않고 사람에게 표시만 한다. 아래 어린이 구분과 동일한 태그를 쓰는 경우에만
# 비교한다. 노년이나 기관별 커스텀 생애주기를 이 구분에 억지로 맞추지 않는다.
_STAGE_AGE_BOUNDS = {"영아": (0, 35), "유아": (36, 83), "학령기": (84, 227)}


def _implied_stages(start_m, end_m) -> set:
    """개월수 범위가 걸치는 생애주기 단계 추정."""
    if start_m is None or end_m is None:
        return set()
    try:
        start_m, end_m = int(start_m), int(end_m)
    except (TypeError, ValueError):
        return set()
    return {stage for stage, (lo, hi) in _STAGE_AGE_BOUNDS.items() if start_m <= hi and end_m >= lo}


def _age_lifecycle_mismatch_note(fields: Dict[str, Any]) -> Optional[str]:
    """제안된 대상연령과 생애주기 태그가 어긋나면 안내 문구 반환 — 두 방향 모두 검사한다.

    (1) 누락: 연령상 있어야 할 단계가 생애주기에 없음.
    (2) 과잉 추가: 연령이 뒷받침하지 않는 단계가 생애주기에 들어있음.
    (2026-07-20 실측) 원래 (1)만 검사했는데, "만 8~13세 학령기" 프로그램에 AI가 원문의
    무관한 '전 연령' 문구를 보고 영아·유아까지 자동으로 붙인 사례를 놓쳤다 — 연령이
    뒷받침 안 하는데 '추가'된 경우는 검사 대상이 아니었기 때문. 과잉 추가도 함께 본다.
    """
    implied = _implied_stages(fields.get("start_age_months"), fields.get("end_age_months"))
    if not implied:
        return None
    lc = fields.get("life_cycle")
    tags = {_norm_text(x) for x in (lc if isinstance(lc, list) else [lc])}
    tags.discard("")
    # 다른 기관의 생애주기 체계는 이 어린이용 구분과 직접 비교할 수 없다.
    if tags - {_norm_text(s) for s in _STAGE_AGE_BOUNDS}:
        return None
    if not tags:
        return None  # 생애주기 추출 자체가 안 됐으면 비교 의미 없음
    missing = {s for s in implied if _norm_text(s) not in tags}
    child_stage_tags = {s for s in _STAGE_AGE_BOUNDS if _norm_text(s) in tags}
    extra = child_stage_tags - implied
    if missing and extra:
        return (f"대상연령상 '{', '.join(sorted(missing))}' 단계도 해당될 수 있으나 생애주기에 없고, "
                f"'{', '.join(sorted(extra))}' 단계는 대상연령이 뒷받침하지 않습니다")
    if missing:
        return f"대상연령상 '{', '.join(sorted(missing))}' 단계도 해당될 수 있으나 생애주기에 없습니다"
    if extra:
        return f"생애주기의 '{', '.join(sorted(extra))}' 단계는 대상연령이 뒷받침하지 않습니다"
    return None


def _embed_cosine(a: str, b: str, client, precomputed=None) -> Optional[float]:
    """두 텍스트의 임베딩 코사인. 실패 시 None.

    실제 정책 작업은 ``_diff_fields_with_embeddings``에서 필요한 문자열을 한 번에
    배치 임베딩한 뒤 precomputed를 넘긴다. 직접 호출 방식은 오래된 내부 테스트와
    호환하기 위한 폴백이며, client가 없으면 외부 호출을 시도하지 않는다.
    """
    if precomputed is not None:
        return precomputed.get((a, b))
    if client is None:
        return None
    try:
        import numpy as np
        va = embedding_service.embed_text(a, client=client)
        vb = embedding_service.embed_text(b, client=client)
        if not va or not vb:
            return None
        return embedding_service.cosine_sim(np.asarray(va), np.asarray(vb))
    except Exception:
        return None


def _overlap_diff(old_set: set, new_set: set, label: str, old_disp: str, new_disp: str,
                  tag_field: bool = False) -> Optional[Dict[str, Any]]:
    """다중값 집합 비교. 태그의 부분일치만 기존 사소 변경 규칙을 유지한다.

    금액·날짜·기간 비교는 교집합이 있어도 다른 값이 하나라도 있으면 주요 변경이다.

    tag_field=True(거주지·생애주기·대상특성)이고 새 제안이 기존의 순부분집합(빼기만 있고
    더하기 없음)이면 note='narrowing'을 붙인다 — 출처 1건만 본 AI가 태그를 좁게 뽑는
    전형적 패턴이라(실측: '막내 만18세 이하' 정책에서 영아 태그 탈락) 기존 값 유지를 권장.
    """
    if old_set == new_set:
        return None
    if not old_set and not new_set:
        return None
    # Overlap is a presentation heuristic for broad tags only. One unchanged
    # deadline must not hide a changed amount or shortened support duration.
    level = "low" if tag_field and (old_set & new_set) else "high"
    d = {"field": label, "old": old_disp, "new": new_disp, "level": level}
    if tag_field and new_set and new_set < old_set:
        d["note"] = "narrowing"
    return d


_ALL_CHILD_STAGES = {"영아", "유아", "학령기"}
_ALL_STAGE_LABELS = {"전생애", "전연령", "전연령대", "전체연령"}


def _life_cycle_equivalent(old_set: set, new_set: set) -> bool:
    """(T1) {영아,유아,학령기} 3개 선택 ≡ '전생애/전연령' 단일 태그 (사용자 작성 관례)."""
    if _ALL_CHILD_STAGES <= old_set and (new_set & _ALL_STAGE_LABELS):
        return True
    if _ALL_CHILD_STAGES <= new_set and (old_set & _ALL_STAGE_LABELS):
        return True
    return False


# (2026-07-07) 다중 태그 정규화 — AI가 원문을 보고 "전국"/"전생애"처럼 뭉뚱그려 뽑으면,
# 사용자가 실제로 관리하는 세부 태그(도봉구 주민 기준 포함 관계)로 환원한다. 이렇게 해두면
# 이 값이 그대로 diff의 "new"로 표시·저장·적용되므로, 실수로 "Notion에 적용"을 눌러도
# 기존보다 태그가 좁아지는(정보 손실) 사고가 원천적으로 안 생긴다.
def _residence_expansion_map() -> Dict[str, List[str]]:
    """(2026-07-13) 거주지 확장 규칙을 TARGET_REGION 설정에서 유도 — 하드코딩 제거.
    기본값(TARGET_REGION=도봉)이면 기존과 동일하게 전국→[전국,서울시,도봉구],
    서울시→[서울시,도봉구]가 나온다. 다른 서울 자치구를 설정하면 그 구 기준으로,
    서울 외 지역이면 광역 단계 없이 전국→[전국,{지역}]으로 자연스럽게 낮아진다."""
    from services.relevance_filter import _SEOUL_DISTRICTS
    target = (os.getenv("TARGET_REGION") or "도봉").strip()
    bare = target[:-1] if target.endswith("구") else target
    district = target if target.endswith(("구", "군", "시")) else f"{target}구"
    mapping: Dict[str, List[str]] = {}
    if bare in _SEOUL_DISTRICTS or bare == "도봉":
        mapping["서울시"] = ["서울시", district]
        mapping["서울특별시"] = ["서울시", district]
        mapping["전국"] = ["전국", "서울시", district]
    else:
        mapping["전국"] = ["전국", district]
    return mapping


def _expand_residence(vals, valid: Optional[List[str]] = None) -> List[str]:
    """거주지 태그를 포함관계로 환원(예: 전국→전국+서울시+{대상지역})."""
    expansion = _residence_expansion_map()
    items = vals if isinstance(vals, list) else [vals]
    out: List[str] = []
    for v in items:
        v = str(v or "").strip()
        if not v:
            continue
        expanded = expansion.get(v, [v])
        # 기관 DB가 TARGET_REGION과 전혀 다른 지역 체계이고 '전국' 자체 옵션도 없다면,
        # 전국 정책은 그 DB가 정의한 모든 거주지에 적용되는 것으로 안전하게 환원한다.
        if valid and v == "전국" and not (set(expanded) & set(valid)):
            expanded = list(valid)
        for x in expanded:
            if x not in out:
                out.append(x)
    return out


def _expand_life_cycle(vals, valid: Optional[List[str]] = None) -> List[str]:
    """전생애를 연결 DB의 실제 생애주기로 환원한다. 기존 DB는 영아·유아·학령기를 유지."""
    items = vals if isinstance(vals, list) else [vals]
    out: List[str] = []
    for v in items:
        v = str(v or "").strip()
        if not v:
            continue
        if v in _ALL_STAGE_LABELS:
            if valid is not None and v in valid:
                targets = [v]
            elif valid is not None:
                targets = list(valid)
            else:
                targets = ["영아", "유아", "학령기"]
            for x in targets:
                if x not in out:
                    out.append(x)
        elif v not in out:
            out.append(v)
    return out


# (2026-07-13) 도메인 클램프 — 생애주기·대상특성은 자유 태그가 아니라 각 사용자의 Notion
# DB에 정의된 select 옵션만 유효하다. AI 중립 추출은 원문을 있는 그대로 옮기므로(원문이
# "노인 대상 셔틀버스"라 쓰여 있으면 '노년'을 그대로 뽑음) 이 DB의 관례를 모른다.
# 실측(2026-07-13): 실제로 없는 옵션(노년/복지)이 제안·적용되는 사고로 이어짐.
#
# "유효 태그가 무엇인가"는 코드에 고정하지 않고 sync_policies()가 캐시해둔 Notion 실제
# 스키마(policy_db_schema 테이블)에서 읽는다 — 다른 지자체가 청소년·노년 옵션까지 포함한
# DB를 쓰더라도 코드 수정 없이 그대로 적응한다. 스키마가 아직 없으면(최초 동기화 전)
# 이 프로젝트가 실측한 기본 구성으로 안전하게 폴백한다.
_FALLBACK_LIFE_CYCLE = {"영아", "유아", "학령기"}
_FALLBACK_TARGET_CHARACTERISTICS = {
    "일반", "장애/발달지연", "저소득", "임산부", "보호자", "다문화", "다자녀", "한부모",
}


def _get_db_schema(db_key: Optional[str]) -> Dict[str, List[str]]:
    """캐시된 Notion 데이터소스 스키마(select/multi_select 옵션) 조회. 없으면 빈 dict."""
    if not db_key:
        return {}
    row = database.execute_query(
        "SELECT schema_json FROM policy_db_schema WHERE db_key=?", (db_key,), fetch_one=True)
    if not row or not row["schema_json"]:
        return {}
    return utils.parse_json_safe(row["schema_json"], {})


def record_policy_verification(items: List[Dict[str, Any]]) -> None:
    """연결 확인 결과를 보관함별로 저장한다. 비밀키와 Notion ID는 저장하지 않는다."""
    with database.get_connection() as conn:
        for item in items or []:
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            ok = bool(item.get("ok"))
            conn.execute(
                "INSERT INTO policy_db_status "
                "(db_key,title,last_verified_at,verification_ok,last_error,updated_at) "
                "VALUES (?,?,CURRENT_TIMESTAMP,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(db_key) DO UPDATE SET "
                "title=excluded.title,last_verified_at=CURRENT_TIMESTAMP,"
                "verification_ok=excluded.verification_ok,last_error=excluded.last_error,"
                "updated_at=CURRENT_TIMESTAMP",
                (key, item.get("title") or item.get("name") or key, int(ok),
                 None if ok else str(item.get("error") or "보관함 구성을 확인해주세요.")))
        conn.commit()


def record_policy_sync_result(db_key: str, title: str, ok: bool,
                              count: int = 0, error: str = "") -> None:
    """동기화 성공 시각·건수 또는 마지막 실패 원인을 저장한다."""
    if ok:
        database.execute_write(
            "INSERT INTO policy_db_status "
            "(db_key,title,last_sync_at,last_sync_count,last_error,updated_at) "
            "VALUES (?,?,CURRENT_TIMESTAMP,?,NULL,CURRENT_TIMESTAMP) "
            "ON CONFLICT(db_key) DO UPDATE SET title=excluded.title,"
            "last_sync_at=CURRENT_TIMESTAMP,last_sync_count=excluded.last_sync_count,"
            "last_error=NULL,updated_at=CURRENT_TIMESTAMP",
            (db_key, title or db_key, int(count or 0)), raise_on_error=True)
    else:
        database.execute_write(
            "INSERT INTO policy_db_status (db_key,title,last_error,updated_at) "
            "VALUES (?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(db_key) DO UPDATE SET title=excluded.title,"
            "last_error=excluded.last_error,updated_at=CURRENT_TIMESTAMP",
            (db_key, title or db_key, str(error or "동기화하지 못했습니다.")),
            raise_on_error=True)


def get_configured_policy_dbs() -> List[Dict[str, Any]]:
    """설정된 정책 DB 목록 — 이름은 동기화 때 캐시해둔 Notion 실제 제목을 우선 쓰고,
    아직 한 번도 동기화 안 했으면(캐시 없음) 설정 저장 시점의 기본 이름으로 대체한다.
    화면 이름이 특정 사용자의 Notion 구성에 하드코딩되지 않도록, 실제 어떤 이름의
    보관함을 몇 개 붙여뒀는지와 마지막 확인 상태를 함께 보여주기 위함."""
    out = []
    for d in notion_service.get_policy_dbs():
        schema = _get_db_schema(d.get("key"))
        status = database.execute_query(
            "SELECT title,last_verified_at,verification_ok,last_sync_at,last_sync_count,last_error "
            "FROM policy_db_status WHERE db_key=?", (d.get("key"),), fetch_one=True) or {}
        local = database.execute_query(
            "SELECT COUNT(*) AS count FROM notion_policies WHERE db_key=?",
            (d.get("key"),), fetch_one=True) or {"count": 0}
        out.append({
            "key": d.get("key"),
            "name": schema.get("_title") or status.get("title") or d.get("name") or d.get("key"),
            "category": d.get("category", ""),
            "last_verified_at": status.get("last_verified_at"),
            "verification_ok": None if status.get("verification_ok") is None else bool(status["verification_ok"]),
            "last_sync_at": status.get("last_sync_at"),
            "last_sync_count": int(status.get("last_sync_count") or 0),
            "last_error": status.get("last_error") or "",
            "local_count": int(local.get("count") or 0),
        })
    return out


def get_disconnected_policy_data() -> Dict[str, Any]:
    """현재 설정에 없는 보관함의 로컬 미러·스키마·상태를 삭제하지 않고 미리 보여준다."""
    active = set(_active_configured_keys())
    rows = database.execute_query(
        "SELECT db_key, COUNT(*) AS policy_count FROM notion_policies GROUP BY db_key") or []
    counts = {row["db_key"]: int(row.get("policy_count") or 0) for row in rows}
    schema_keys = {row["db_key"] for row in (database.execute_query(
        "SELECT db_key FROM policy_db_schema") or [])}
    statuses = {row["db_key"]: row for row in (database.execute_query(
        "SELECT db_key,title FROM policy_db_status") or [])}
    keys = sorted((set(counts) | schema_keys | set(statuses)) - active)
    items = [{"key": key, "name": statuses.get(key, {}).get("title") or key,
              "policy_count": counts.get(key, 0)} for key in keys]
    return {"count": len(items), "policy_count": sum(i["policy_count"] for i in items),
            "items": items}


def cleanup_disconnected_policy_data() -> Dict[str, Any]:
    """연결 해제된 보관함의 로컬 자료만 원자적으로 정리한다. Notion 원본에는 접근하지 않는다."""
    preview = get_disconnected_policy_data()
    keys = [item["key"] for item in preview["items"]]
    if not keys:
        return {**preview, "deleted": 0, "backup_created": False}
    if not database.auto_backup_db():
        raise RuntimeError("안전 백업을 만들지 못해 로컬 자료 정리를 중단했습니다.")
    placeholders = ",".join("?" for _ in keys)
    with database.get_connection() as conn:
        cursor = conn.execute(
            f"DELETE FROM notion_policies WHERE db_key IN ({placeholders})", tuple(keys))
        deleted = cursor.rowcount
        conn.execute(f"DELETE FROM policy_db_schema WHERE db_key IN ({placeholders})", tuple(keys))
        conn.execute(f"DELETE FROM policy_db_status WHERE db_key IN ({placeholders})", tuple(keys))
        conn.commit()
    return {**preview, "deleted": max(0, deleted), "backup_created": True}


def _active_configured_keys(requested: Optional[List[str]] = None) -> List[str]:
    """현재 설정에 남아 있는 보관함 키만 반환. 빈 요청은 '연결된 전체'를 뜻한다."""
    configured = [d.get("key") for d in notion_service.get_policy_dbs() if d.get("key")]
    if not requested:
        return configured
    return [key for key in requested if key in configured]


async def _db_query_strict(sql: str, params: tuple = (), fetch_one: bool = False):
    """정책 작업용 DB 조회. 실패를 빈 결과로 숨기지 않는다."""
    return await database.run_db(
        database.execute_query, sql, params, fetch_one, raise_on_error=True)


async def _db_write_strict(sql: str, params: tuple = ()):
    """정책 작업용 DB 저장. 실패를 완료 상태로 오인하지 않게 호출자까지 전파한다."""
    return await database.run_db(
        database.execute_write, sql, params, raise_on_error=True)


def _clamp_residence(vals: List[str], valid: Optional[List[str]] = None) -> List[str]:
    """거주지를 유효 옵션으로 제한 — 생애주기·대상특성과 같은 원리의 이중 방어.

    (2026-07-20 실측) AI가 원문의 "도봉 및 인접지역" 문구에서 '인접지역'을 지역 태그로
    추출해 그대로 제안됐다. Notion multi_select는 없는 옵션을 쓰면 새 태그를 자동 생성
    하므로, 적용 시 사용자 DB에 유령 태그가 실제로 생긴다 — 여기서 차단해야 한다.
    유효 목록은 동기화 때 캐시한 Notion 실제 스키마에서 읽고, 스키마가 아직 없으면
    TARGET_REGION 설정에서 유도되는 확장 집합(예: 전국·서울시·도봉구)으로 폴백한다."""
    if valid is not None:
        valid_set = set(valid)
    else:
        valid_set = {x for xs in _residence_expansion_map().values() for x in xs}
    return [v for v in vals if v in valid_set]


def _clamp_life_cycle(vals: List[str], valid: Optional[List[str]] = None) -> List[str]:
    """생애주기를 유효 옵션으로 제한 — 그 외 태그(스키마에 없는 노년 등)는 제거."""
    valid_set = set(valid) if valid is not None else _FALLBACK_LIFE_CYCLE
    return [v for v in vals if v in valid_set]


def _clamp_target_characteristics(vals: List[str], valid: Optional[List[str]] = None) -> List[str]:
    """대상특성을 유효 옵션으로 제한하고, '일반' 옵션이 존재하면(공통 적용 태그) 항상 포함."""
    valid_set = set(valid) if valid is not None else _FALLBACK_TARGET_CHARACTERISTICS
    out = [v for v in vals if v in valid_set]
    if "일반" not in out and "일반" in valid_set:
        out.insert(0, "일반")
    return out


def _category_for_db_key(db_key: str) -> Optional[str]:
    """레거시 연결에 분류값이 명시돼 있으면 사용한다. 새 연결은 빈 값이며 실제 Notion
    분류 선택지 안에서 AI 제안을 검증하므로 보관함 이름을 분류로 강제하지 않는다."""
    for d in notion_service.get_policy_dbs():
        if d.get("key") == db_key:
            return d.get("category") or d.get("name") or None
    return None


def _resolve_category_label(db_key: Optional[str], fallback: str) -> str:
    """(2026-07-20) 분류값을 실제 Notion select 옵션 철자로 맞춘다.

    config에 저장된 고정 이름(예: "가족지원")이 사용자 Notion DB의 실제 옵션 철자
    (예: "가족 지원")와 공백 하나만 달라도, 그대로 내보내면 Notion select 속성이 미존재
    값을 새 옵션으로 자동 생성해버린다 — multi_select(거주지 등)에서 이미 겪은 것과
    같은 유형의 사고. 캐시된 스키마에서 같은 값(공백·기호 제거 비교)을 찾아 실제
    철자로 치환하고, 스키마가 없거나 매칭 안 되면(아직 동기화 전 등) fallback 그대로 둔다.
    """
    schema = _get_db_schema(db_key)
    options = schema.get("category") or []
    target_norm = _norm_cat(fallback)
    for opt in options:
        if _norm_cat(opt) == target_norm:
            return opt
    return fallback


def _normalize_extracted_fields(fields: Dict[str, Any], db_key: Optional[str] = None) -> None:
    """AI 추출 직후(비교·저장 전) 값을 정규화 — 이후의 모든 비교·표시·Notion 적용이
    전부 이 정규화된 값을 쓰게 되어 축소 오적용·존재하지 않는 옵션 오적용을 원천 차단한다."""
    schema = _get_db_schema(db_key)
    if fields.get("residence"):
        fields["residence"] = _clamp_residence(
            _expand_residence(fields["residence"], schema.get("residence")), schema.get("residence"))
    if fields.get("life_cycle"):
        fields["life_cycle"] = _clamp_life_cycle(
            _expand_life_cycle(fields["life_cycle"], schema.get("life_cycle")), schema.get("life_cycle"))
    if fields.get("target_characteristics"):
        raw_characteristics = fields["target_characteristics"]
        if not isinstance(raw_characteristics, list):
            raw_characteristics = [raw_characteristics]
        fields["target_characteristics"] = _clamp_target_characteristics(
            raw_characteristics, schema.get("target_characteristics"))
    if fields.get("support_detail") and '_source_unit_selection' not in fields:
        # 수집 캐시 재활용분은 수집용 프롬프트의 '###' 스타일 그대로 들어옴 — 여기서 결정론 제거
        # Whole-unit extraction is already exact source text with verifiable
        # provenance. Keep that text byte-for-byte through persistence; legacy
        # formatting cleanup must not detach it from its source evidence.
        fields["support_detail"] = _strip_markdown_symbols(str(fields["support_detail"]))
    if db_key:
        fixed_cat = _category_for_db_key(db_key)
        category_options = schema.get("category") or []
        proposed = str(fields.get("category") or "").strip()
        # 레거시 연결은 설정된 분류를 우선해 품질을 보존한다. 기관이 전혀 다른 분류 체계를
        # 쓰는 DB라 설정값과 맞지 않으면, AI가 실제 Notion 옵션 중 선택한 값만 허용한다.
        fixed_match = _resolve_category_label(db_key, fixed_cat) if fixed_cat else ""
        if 'category' in schema and not category_options:
            fields['category'] = ''
        elif fixed_cat and (not category_options or fixed_match in category_options):
            fields["category"] = fixed_match
        elif category_options:
            proposed_match = next((opt for opt in category_options
                                   if _norm_cat(opt) == _norm_cat(proposed)), "")
            fields["category"] = proposed_match
        elif fixed_cat:
            fields["category"] = fixed_cat


def normalize_draft_fields(fields: Dict[str, Any], db_key: str,
                           schema: Dict[str, List[str]]) -> None:
    """신규 Notion 초안용 엄격 정규화.

    일반 정규화 뒤에도 실제 스키마에 없는 select/multi_select 값은 모두 제거한다.
    Notion이 쓰기 요청의 새 이름을 새 옵션으로 만들어버리는 부작용을 차단한다.
    """
    _normalize_extracted_fields(fields, db_key)
    if "category" in schema:
        allowed = schema.get("category") or []
        proposed = str(fields.get("category") or "").strip()
        fields["category"] = next(
            (option for option in allowed if _norm_cat(option) == _norm_cat(proposed)), "")
    for fkey in ("residence", "target_characteristics", "life_cycle"):
        if fkey not in schema:
            continue
        allowed = set(schema.get(fkey) or [])
        raw = fields.get(fkey) or []
        values = raw if isinstance(raw, list) else [raw]
        fields[fkey] = [value for value in values if value in allowed]


def _content_moved(value_norm: str, pool: str) -> bool:
    """(T1) 정규화된 값이 다른 필드들의 통합 텍스트(pool) 안에 사실상 존재하는지 —
    존재하면 '내용이 사라진 것'이 아니라 '필드 배치가 다른 것'이므로 오탐이 아니다."""
    if not value_norm or not pool:
        return False
    if value_norm in pool:
        return True
    match = difflib.SequenceMatcher(None, value_norm, pool).find_longest_match(0, len(value_norm), 0, len(pool))
    return match.size >= max(10, int(len(value_norm) * 0.5))


# (V1) 상세지원내용 섹션 미러링 — 사용자가 실제로 골라 쓴 섹션 구성(이모지/### 헤더)만
# 유지하도록, 기존 값에서 섹션 헤더 목록을 뽑아 스타일 변환 프롬프트에 지시로 넣는다.
# 리스트 항목 마커(하위 불릿) — 헤더가 아니라 본문 항목이므로 헤더 판정에서 제외
_LIST_MARKER_CHARS = set('•◦‣▪▫·-–—*○●')


def _is_emoji_start(ch: str) -> bool:
    """실제 이모지/픽토그램 범위만 좁게 판정. (버그 이력: U+2000 이상 전체를 이모지로
    취급했더니 한글(U+AC00~D7A3)도 걸려 '신청 당시...' 같은 일반 문장이 헤더로 오인됨.)"""
    cp = ord(ch)
    return (
        0x2600 <= cp <= 0x27BF or     # 기타 기호·딩뱃(✅⛔☎ 등)
        0x1F300 <= cp <= 0x1FAFF or   # 이모지 본진(📋📍💰📅📝 등)
        0x2190 <= cp <= 0x21FF or     # 화살표
        0x2B00 <= cp <= 0x2BFF or     # 별 등
        cp in (0x2757, 0x2753, 0x203C, 0x2049)  # ❗❓‼⁉
    )


def _is_header_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if s.startswith('#'):
        return len(s) <= 40
    first = s[0]
    if first in _LIST_MARKER_CHARS:
        return False
    return _is_emoji_start(first) and len(s) <= 25


def _extract_sections(text: str) -> List[Tuple[str, str]]:
    """텍스트를 섹션 헤더 기준으로 분리 → [(헤더, 본문)]. 헤더 없는 서두는 '(서두)'로 표시."""
    lines = [l.strip() for l in (text or '').splitlines() if l.strip()]
    sections: List[Tuple[str, List[str]]] = []
    header, body = None, []
    for line in lines:
        if _is_header_line(line):
            if header is not None or body:
                sections.append((header or '(서두)', body))
            header = re.sub(r'^#+\s*', '', line)
            body = []
        else:
            body.append(line)
    if header is not None or body:
        sections.append((header or '(서두)', body))
    return [(h, '\n'.join(b)) for h, b in sections]


# (2026-07-13) 프롬프트 지시(확률적)만으로는 '###' 금지·기존 섹션만 사용이 안 지켜지는
# 사례가 계속 나와(수집 캐시 재활용분은 수집용 프롬프트의 ### 스타일 그대로 유입되기도 함),
# AI 출력을 신뢰하지 않고 코드에서 결정론적으로 강제한다.
_MD_INLINE_HEADER_RE = re.compile(r"\s*#{2,}\s*")   # 줄 중간의 '###' → 줄바꿈으로 승격
_MD_HEADER_RE = re.compile(r"^\s*#{1,6}\s+", re.M)
_MD_BOLD_RE = re.compile(r"\*\*(.*?)\*\*")
# _is_emoji_start와 동일한 범위의 정규식 클래스 (인라인 섹션 분리용)
_EMOJI_CLASS = (
    "[←-⇿☀-➿⬀-⯿❗❓‼⁉"
    "\U0001F300-\U0001FAFF]"
)
_INLINE_EMOJI_SPLIT_RE = re.compile(rf"[ \t]+(?={_EMOJI_CLASS})")
# 이모지로 시작하는 줄에서 "헤더부 - 내용부"를 분리 (예: '📋 신청 자격 - 기저귀 지원대상: …')
_HEADER_WITH_BODY_RE = re.compile(rf"^({_EMOJI_CLASS}[^\n]{{0,20}}?)\s*[-–:]\s+(.+)$")


def _strip_markdown_symbols(text: str) -> str:
    """'###' 헤더 기호·'**' 볼드 기호를 제거 (내용은 보존). Notion 속성은 마크다운을
    렌더링하지 않아 이 기호들이 글자 그대로 보이는 문제의 결정론적 차단.
    줄 중간에 낀 '###'(수집 캐시 스타일)는 줄바꿈으로 바꿔 섹션 경계를 살린다."""
    if not text:
        return text
    out = _MD_INLINE_HEADER_RE.sub("\n", str(text))
    out = _MD_HEADER_RE.sub("", out)
    out = _MD_BOLD_RE.sub(r"\1", out)
    return out


def _clamp_sections_to_old(new_text: str, old_text: str) -> str:
    """제안값의 섹션을 기존 Notion 값에 실제로 있던 섹션으로만 제한.

    기존에 ✅지원 내용·💰지원 금액만 있는데 제안이 📋신청 자격·📅신청 기간까지 만들어내는
    사례(실측 2026-07-13) 차단 — 기존 헤더 집합에 없는 섹션은 통째로 버린다.
    기존 값에 섹션 헤더가 하나도 없으면(자유 서술) 제한하지 않는다.
    AI 출력은 '📋 신청 자격 - 내용…'처럼 헤더와 본문이 한 줄에 붙는 경우가 흔해,
    이모지 시작 지점으로 먼저 줄을 쪼갠 뒤 헤더부를 분리해 판정한다.
    """
    if not new_text or not old_text:
        return new_text
    old_headers = {_norm_text(h) for h, _ in _extract_sections(old_text) if h != '(서두)'}
    if not old_headers:
        return new_text

    # 인라인 이모지 헤더를 줄 단위로 승격
    pre = _INLINE_EMOJI_SPLIT_RE.sub("\n", new_text)
    kept: List[str] = []
    keep_mode = True  # 헤더 등장 전 서두는 유지
    for raw in pre.splitlines():
        line = raw.strip()
        if not line:
            if keep_mode:
                kept.append("")
            continue
        # (버그 이력 2026-07-14) "이모지헤더 - 짧은내용"이 25자 이하면 _is_header_line이
        # 줄 전체를 헤더로 오판해 본문이 헤더 이름에 섞여 기존 헤더와 매칭이 실패했다
        # (→ 아무 섹션도 안 남아 클램핑 자체가 무효화됨). 헤더+본문 분리를 먼저 시도한다.
        header, inline_body = None, None
        m = _HEADER_WITH_BODY_RE.match(line)
        if m and _is_emoji_start(m.group(1)[0]):
            header, inline_body = m.group(1).strip(), m.group(2).strip()
        elif _is_header_line(line):
            header = re.sub(r'^#+\s*', '', line)
        if header is not None:
            keep_mode = _norm_text(header) in old_headers
            if keep_mode:
                kept.append(header)
                if inline_body:
                    kept.append(inline_body)
        elif keep_mode:
            kept.append(line)
    result = "\n".join(kept).strip()
    result = re.sub(r"\n{3,}", "\n\n", result)
    # 전부 잘려나가면(헤더가 하나도 안 겹침) 원본 유지 — 정보를 없애기보다 사람 판단에 맡김
    return result if result else new_text


# (실측 2026-07-15) 섹션 클램핑은 '구조'만 맞추고 '정보량'은 못 맞춘다 — 기존에 없던
# 헤더 아래 내용을 통째로 버려 결과가 지나치게 부실해지는 사례(신청방법·비용 등 소실),
# 반대로 기존 값에 헤더가 아예 없어 클램핑이 걸리지 않아 카탈로그 페이지 원문이 그대로
# 들어와 지나치게 방대해지는 사례가 둘 다 확인됨. 어느 쪽도 자르는 대신, 기존 값 대비
# 상대 기준으로 '사람 확인이 필요하다'는 신호만 얹어 결정론 단계에서는 정보를 보존한다.
_LOSS_RATIO_MIN = 0.3   # 클램핑 후 원본의 이 비율 미만만 남으면 정보 손실로 보고 클램핑 포기
_BLOAT_RATIO_MAX = 3.0  # 기존 값의 이 배수를 넘으면 '과다' 신호 (Notion 적용 BLOAT 가드와 별개로 표시 단계에도 적용)


def _sanitize_support_detail(new_text: str, old_text: str = "", return_note: bool = False):
    """상세지원내용 제안값 최종 위생 처리: 마크다운 제거 + 기존 섹션으로 제한.

    return_note=True면 (표시용 텍스트, 주의 신호) 튜플을 반환한다.
    신호: 'sparse' — 클램핑이 정보를 과도하게 잘라내 정리를 포기하고 원문을 그대로 반환함
          'bloat'  — 기존 값 대비 너무 길어짐(섹션 제한이 안 걸린 자유서술 등)
    """
    stripped = _strip_markdown_symbols(new_text)
    out = stripped
    note = None
    if old_text:
        clamped = _clamp_sections_to_old(stripped, old_text)
        if clamped != stripped and len(stripped) > 20 and len(clamped) < len(stripped) * _LOSS_RATIO_MIN:
            note = 'sparse'  # 클램핑 포기 — out은 stripped(원문, 마크다운만 제거) 유지
        else:
            out = clamped
        if len(old_text) > 20 and len(out) > len(old_text) * _BLOAT_RATIO_MAX:
            note = note or 'bloat'
    if return_note:
        return out, note
    return out


def _support_detail_section_instructions(old_text: str) -> str:
    """(V1) 기존 상세지원내용의 섹션 구성을 스타일 변환 프롬프트용 지시문으로 변환."""
    headers = [h for h, _ in _extract_sections(old_text) if h != '(서두)']
    if not headers:
        return ""
    header_list = ", ".join(headers)

    # (V4) 기존 본문에서 실제 쓰인 불릿 기호를 감지해 그대로 쓰도록 지시 (하이픈 '-' 대신
    # 사용자 관례인 '•'/'◦' 등으로 통일 — 실측 사례: 상위 '•', 하위 상세 '◦').
    body_lines = [l.strip() for l in old_text.splitlines() if l.strip() and not _is_header_line(l.strip())]
    top_bullets = {l[0] for l in body_lines if l[0] in '•◦‣▪▫○●-*'}
    bullet_note = ""
    if top_bullets:
        symbols = ", ".join(sorted(top_bullets))
        bullet_note = (
            f"불릿 기호는 새로 만들지 말고 기존에 실제로 쓰인 것만 쓰세요: {symbols}. "
            "하이픈('-')은 쓰지 마세요.\n"
        )

    # (2026-07-13) 통합 안내 페이지(한 페이지에 여러 제도 소개) 대비 하드 상한 — 실측:
    # '장애인 의료비 공제'의 출처가 세금감면 전체 페이지라 자동차세·상속세 감면까지 통째로
    # 끌려 들어와 기존의 수십 배 분량이 제안됨. 분량 자체에 상한을 강제한다.
    max_chars = max(200, len(old_text) * 2)

    return (
        "\n[상세지원내용 섹션 규칙]\n"
        f"기존에 실제로 사용된 섹션은 다음과 같습니다: {header_list}\n"
        "이 섹션들만 사용하세요. 새 원문에 해당 섹션과 맞는 정보가 없으면 그 섹션은 통째로 생략하세요.\n"
        "기존에 없던 새 섹션을 만들지 마세요(원문에 중요한 새 정보가 있어도 가장 가까운 기존 섹션에 포함시키세요).\n"
        "각 섹션의 분량(문장 수·상세도)은 기존과 비슷한 수준으로 유지하세요 — "
        "기존이 한두 문장이면 새 값도 한두 문장으로 간결하게, 장황하게 늘리지 마세요.\n"
        f"결과 전체 길이는 {max_chars}자를 절대 넘기지 마세요 (기존 값의 최대 2배). "
        "출처 페이지가 여러 제도를 한꺼번에 소개하는 통합 안내문이라면, 이 정책의 사업명과 "
        "직접 관련된 내용만 남기고 함께 소개된 다른 제도·혜택은 전부 제외하세요.\n"
        f"{bullet_note}"
        "(V4) 절대 '#', '##', '###', '**' 같은 마크다운 기호를 쓰지 마세요 — Notion 속성은 마크다운을 "
        "렌더링하지 않아 그 기호가 글자 그대로 보입니다. 섹션 제목은 이모지로 시작하는 한 줄로만 쓰세요"
        "(예: '✅ 지원 내용'). 앱이 그 줄을 자동으로 볼드 처리해 Notion에 반영합니다.\n"
    )


# (B1) 비교 로직 버전 — 이 값을 올릴 때마다 이미 감지되어 대기 중인 진단은 낡은 판정 그대로
# 남는다(원문이 다시 안 바뀌면 재계산 안 됨). recompute_diffs()로 수동 갱신 가능.
_DIFF_LOGIC_VERSION = 5
from services.policy_evidence import POLICY_EVIDENCE_VERSION as _EXTRACTION_PROMPT_VERSION


def _extraction_meta(db_key: str) -> Dict[str, Any]:
    """변경 제안이 어떤 AI·프롬프트·기관 선택지로 생성됐는지 추적한다."""
    from services.policy_source_plan import POLICY_REQUEST_FORMAT
    schema = _get_db_schema(db_key) or {}
    canonical_schema = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {
        "model": os.getenv('CLASSIFY_MODEL', 'gemini-3.5-flash-lite'),
        "prompt_version": _EXTRACTION_PROMPT_VERSION,
        # Request transport is separate from compatibility: do not expire
        # unchanged v6 proposals or spend extra calls merely for a new format.
        "request_format": POLICY_REQUEST_FORMAT,
        "schema_fingerprint": hashlib.sha256(canonical_schema.encode('utf-8')).hexdigest()[:16],
    }

# (2026-07-20) diff 카드의 필드 제목 = 실제 Notion 속성명 (notion_service.get_policy_props()가
# 단일 소스). 예전엔 '대상특성'·'대상연령'·'자격요건'처럼 축약형이라 사용자가 Notion에서
# 어느 속성('대상 특성'·'세부 연령'·'추가 자격요건')을 고치라는 말인지 대조하기 어려웠다.
#
# (2026-07-30) 딕셔너리가 아니라 __getitem__마다 notion_service.get_policy_props()를 재조회하는
# 매핑 객체로 바꿨다 — 프로세스 내에서 오버라이드 값이 바뀌는 매 순간(예: 같은 실행 중 여러
# 값을 시험하는 테스트) 캐시되지 않고 즉시 반영되게 하기 위함(다른 지자체 Notion 워크스페이스로
# 속성명을 바꿔도 코드 수정·재빌드는 불필요). 기존 호출부(`_PROP_LABEL[fkey]`,
# `{_PROP_LABEL[k] for k in ...}`)는 전부 구독 접근이라 그대로 동작한다.
#
# 주의(2026-08-05, GPT 리뷰): "앱 재시작 불필요"는 config.json을 /api/config로 저장했을 때만
# 해당한다(`utils.set_env_value`가 os.environ도 함께 갱신하므로). config.json 파일을 앱 실행
# 중에 직접 편집하면, `utils.get_env_value`가 "이미 os.environ에 키가 있으면 파일을 다시 안
# 읽는" 방식이라(utils.py:206~208) 반영되지 않는다 — 이 경우는 앱을 재시작해야 한다. 전용
# Settings UI가 아직 없어 현실적으로는 사용자가 파일을 직접 편집할 가능성이 높으므로,
# 사용자 안내 문서에는 "설정 변경 후 앱 재시작 필요"라고 쓰는 것이 정확하다.
class _PropLabelMap:
    def __getitem__(self, fkey: str) -> str:
        for k, pname, _ in notion_service.get_policy_props():
            if k == fkey:
                return pname
        return fkey  # 매핑에 없는 키는 그대로 반환(방어적 fallback)


_PROP_LABEL = _PropLabelMap()


def _diff_fields(notion_fields: Dict[str, Any], new_fields: Dict[str, Any], client,
                 precomputed_cosines=None) -> List[Dict[str, Any]]:
    """Notion 현재값(old) vs AI 재추출값(new) 필드별 비교 → diff 목록.

    level: high(핵심 변경), info(확인이 필요한 보강·서술 변경), low(태그·표현).
    유사도나 일부 공통 숫자만으로 의미가 같다고 확정하지 않는다. 상세지원내용의
    서비스·제한 조건 변화는 info 이상으로 보존하며, 양적 사실 변화는 high로 남긴다.
    """
    diffs: List[Dict[str, Any]] = []

    # 1) 거주지/생애주기/대상특성 — 다중값 태그. 부분집합·겹침 있으면 사소.
    def cmp_multi(fkey, normfn):
        o, n = notion_fields.get(fkey), new_fields.get(fkey)
        so = {normfn(x) for x in (o if isinstance(o, list) else [o])}
        sn = {normfn(x) for x in (n if isinstance(n, list) else [n])}
        so.discard(""); sn.discard("")
        d = _overlap_diff(so, sn, _PROP_LABEL[fkey],
                          ", ".join(o) if isinstance(o, list) else str(o or ""),
                          ", ".join(n) if isinstance(n, list) else str(n or ""),
                          tag_field=True)
        if d:
            diffs.append(d)

    cmp_multi("residence", _norm_text)

    # (T1) 생애주기 등가 규칙 — 사용자 작성 관례: "전 연령"이면 영아·유아·학령기 3개를 모두
    # 체크(반면 AI는 원문의 '전 연령'/'전생애' 표현을 하나의 태그로만 뽑음). 이 둘은 본질적으로
    # 같은 뜻이므로 겹침 판정 전에 먼저 동등 처리한다.
    o_lc, n_lc = notion_fields.get("life_cycle"), new_fields.get("life_cycle")
    o_lc_set = {_norm_text(x) for x in (o_lc if isinstance(o_lc, list) else [o_lc])}
    n_lc_set = {_norm_text(x) for x in (n_lc if isinstance(n_lc, list) else [n_lc])}
    o_lc_set.discard(""); n_lc_set.discard("")
    if not _life_cycle_equivalent(o_lc_set, n_lc_set):
        cmp_multi("life_cycle", _norm_text)

    cmp_multi("target_characteristics", _norm_text)

    # 2) 분류 — 독립 재분류는 관점 차이일 뿐 실제 변경의 증거가 아니므로 항상 사소로만 표시
    oc, nc = _norm_cat(notion_fields.get("category")), _norm_cat(new_fields.get("category"))
    # 연결 DB에 없는 값을 AI가 반환하면 정규화 단계에서 새 값을 비운다. 그 경우 기존
    # 분류를 지우라는 변경안까지 만들지 않고 조용히 보류한다(유령 옵션·오삭제 이중 방어).
    if nc and oc != nc:
        diffs.append({"field": _PROP_LABEL["category"], "old": str(notion_fields.get("category") or ""),
                      "new": str(new_fields.get("category") or ""), "level": "low"})

    # 3) 대상연령 — 월령 숫자가 서로 일치하면 문구 차이는 사소. 숫자 없으면 유사도로 판정.
    o_age = notion_fields.get("age_detail") or _fmt_age(notion_fields)
    n_age = new_fields.get("age_detail") or _fmt_age(new_fields)
    if _norm_text(str(o_age)) != _norm_text(str(n_age)):
        os_, oe_ = notion_fields.get("start_age_months"), notion_fields.get("end_age_months")
        ns_, ne_ = new_fields.get("start_age_months"), new_fields.get("end_age_months")
        numeric_match = (os_ is not None and ns_ is not None and os_ == ns_
                         and oe_ is not None and ne_ is not None and oe_ == ne_)
        if numeric_match:
            level, cos = "low", None
        else:
            cos = _embed_cosine(str(o_age), str(n_age), client, precomputed_cosines) if (o_age and n_age) else 0.0
            level = "low" if (cos is not None and cos >= _REPHRASE_COSINE) else "high"
        age_diff = {"field": _PROP_LABEL["age_detail"], "old": str(o_age), "new": str(n_age), "level": level,
                    "cosine": round(cos, 3) if cos is not None else None}
        # (2026-08-07) id=13 실사고 재발 방지 — 진단 생성 시점에 미리 경고를 붙여, 사용자가
        # '적용' 버튼을 누르기 전 화면에서부터 의심스러운 값임을 알 수 있게 한다.
        # (2차 리뷰) 이번 추출의 대상특성 값도 함께 넘겨 교차 검사 — id=13은 정확히
        # "세부 연령에 대상 특성 값이 들어감"이었으므로 이게 가장 직접적인 방어선이다.
        n_tc = new_fields.get("target_characteristics")
        n_tc_display = ", ".join(n_tc) if isinstance(n_tc, list) else str(n_tc or "")
        age_warn = _validate_field_semantics("age_detail", str(n_age), n_tc_display)
        if age_warn:
            age_diff["validation_warning"] = age_warn
        diffs.append(age_diff)

    # 4) 텍스트 핵심 필드 (자격요건/대상소득) — 정규화 다르면, 코사인으로 재표현/실질 구분
    for fkey in ("eligibility_detail", "target_income"):
        o, n = str(notion_fields.get(fkey) or ""), str(new_fields.get(fkey) or "")
        if _factual_text(o) == _factual_text(n):
            continue
        cos = _embed_cosine(o, n, client, precomputed_cosines) if (o and n) else 0.0
        # Embeddings can be almost identical after a negation or threshold
        # changes. Keep uncertain prose visible; similarity is not proof of
        # unchanged eligibility. Exact quantity differences are always high.
        quantity_changed = _money_date_tokens(o) != _money_date_tokens(n)
        level = "info" if (not quantity_changed and cos is not None and cos >= _REPHRASE_COSINE) else "high"
        diffs.append({"field": _PROP_LABEL[fkey], "old": o, "new": n, "level": level,
                      "cosine": round(cos, 3) if cos is not None else None})

    # 5) 금액·날짜·기간은 일부 공통 값이 있어도 달라진 사실을 보존한다.
    to = _money_date_tokens(notion_fields.get("support_detail"))
    tn = _money_date_tokens(new_fields.get("support_detail"))
    d = _overlap_diff(to, tn, "지원금액·기간", ", ".join(sorted(to)) or "-", ", ".join(sorted(tn)) or "-")
    if d:
        diffs.append(d)

    # 6) 서비스 종류·지원 방식도 실제 변경이다. 숫자가 같다는 이유로 숨기지 않는다.
    # (V2) 잘림 방지: 코사인 계산용 입력만 2000자로 제한하고, 화면 표시/스타일변환용
    # old/new는 원문 그대로 보존한다 — 여기서 자르면 이후 어떤 단계도 복구할 수 없다.
    o, n = str(notion_fields.get("support_detail") or ""), str(new_fields.get("support_detail") or "")
    if _factual_text(o) != _factual_text(n) and (o or n):
        cos = _embed_cosine(o[:2000], n[:2000], client, precomputed_cosines) if (o and n) else 0.0
        # AI 재호출 없는 결정론적 위생 — 마크다운 기호 제거 + 기존 Notion 섹션 범위로 제한.
        # 여기서 처리해두면 최초 점검·진단 재계산(AI 없이 diff만 재생성) 양쪽 모두
        # 매번 최신 정리 로직을 통과한 화면 표시값을 갖게 된다(스타일 변환을 따로 안 돌려도).
        n_display, sd_note = _sanitize_support_detail(n, o, return_note=True)
        sd_diff = {"field": _PROP_LABEL["support_detail"], "old": o, "new": n_display,
                   "level": "high" if to != tn else "info",
                   "cosine": round(cos, 3) if cos is not None else None}
        if sd_note:
            sd_diff["note"] = sd_note
        sd_warn = _validate_field_semantics("support_detail", n_display)
        if sd_warn:
            sd_diff["validation_warning"] = sd_warn
        diffs.append(sd_diff)

    # 7) 빈 값 비대칭 강등 (P1②) — 한쪽만 비어있는 high는 "실질 변경"이 아니라
    #    "Notion에 없던 정보를 원문이 담고 있음"이므로 info(📝 보강 후보)로 재분류
    def _is_empty(v: str) -> bool:
        v = (v or "").strip()
        return not v or v == "-"

    for d in diffs:
        if d["level"] == "high" and _is_empty(d["old"]) != _is_empty(d["new"]):
            d["level"] = "info"

    # 8) (T1) 필드 경계 이동 강등 — 대상연령/자격요건/대상소득은 사용자 관례상 서로 넘나들며
    # 적히는 경우가 흔하다(예: 대상연령이 자격요건 문장 안에 서술됨). 내용이 '사라진' 게 아니라
    # 다른 필드로 '이동'했을 뿐이면 실질 변경이 아니므로 info로 강등한다.
    # (2026-07-15) 풀에 대상특성도 포함 — "대상연령: 만12세 이하 → 다문화자녀"처럼 AI가
    # 대상특성 값을 연령 필드에 잘못 옮겨 적는 오배치를 실질변경(🔴)으로 오탐하던 사례 차단.
    def _multi_text(fields: Dict[str, Any]) -> str:
        v = fields.get("target_characteristics")
        return ", ".join(v) if isinstance(v, list) else str(v or "")

    _pool_fields = ("age_detail", "eligibility_detail", "target_income")
    _pool_labels = {_PROP_LABEL[k] for k in _pool_fields}
    old_pool = _norm_text(" ".join(str(notion_fields.get(k) or "") for k in _pool_fields) + " " + _multi_text(notion_fields))
    new_pool = _norm_text(" ".join(str(new_fields.get(k) or "") for k in _pool_fields) + " " + _multi_text(new_fields))
    # (2026-07-20, 테스트 작성 중 발견) 위 old_pool/new_pool 비교는 "예전부터 있던 내용이
    # 다른 필드로 이동"만 잡는다 — 실제 신고 사례(대상연령: 만12세 이하 → 다문화자녀)는
    # AI가 이번 추출에서 같은 문구를 age_detail과 target_characteristics 양쪽에 동시에
    #새로 써넣은 "자기 자신 내 중복"이라 위 두 조건 다 못 잡았다(재현 테스트로 확인).
    # new_fields 안에서 대상특성과 겹치는지도 별도로 확인해 이 패턴을 잡는다.
    new_tc_only = _norm_text(_multi_text(new_fields))
    for d in diffs:
        if d["level"] != "high" or d["field"] not in _pool_labels:
            continue
        new_norm = _norm_text(d["new"])
        old_norm = _norm_text(d["old"])
        # new값이 old_pool 어딘가에 이미 있었거나, old값이 new_pool 어딘가에 여전히 있거나,
        # new값이 이번 추출의 대상특성과 겹치면(자기 자신 내 중복) '이동/오배치'로 본다.
        if (_content_moved(new_norm, old_pool) or _content_moved(old_norm, new_pool)
                or _content_moved(new_norm, new_tc_only)):
            d["level"] = "info"

    # 9) 대상연령↔생애주기 정합성 — 변경이 감지된(diffs에 이미 올라온) 두 필드에 한해서만
    # 안내를 붙인다. 변경 자체가 없으면(기존부터 있던 상태) 이번 점검의 관심사가 아니다.
    # (2026-07-20) 새 추출에 월령 숫자가 없으면 Notion 기존 월령으로 대체해 판정 — 실측
    # ('막내 만18세 이하' 정책)에서 새 추출이 월령 없이 생애주기만 좁게(영아 탈락) 내놓아
    # 월령 0~216 기준의 불일치를 잡지 못했다. 기존 월령은 사람이 검증한 값이라 더 믿을 만하다.
    merged_for_note = dict(new_fields)
    for mk in ("start_age_months", "end_age_months"):
        if merged_for_note.get(mk) is None and notion_fields.get(mk) is not None:
            merged_for_note[mk] = notion_fields[mk]
    age_lc_note = _age_lifecycle_mismatch_note(merged_for_note)
    if age_lc_note:
        for d in diffs:
            if d["field"] in (_PROP_LABEL["age_detail"], _PROP_LABEL["life_cycle"]) and not d.get("consistency_note"):
                d["consistency_note"] = age_lc_note

    return diffs


def _diff_embedding_pairs(notion_fields: Dict[str, Any], new_fields: Dict[str, Any]):
    """현재 비교 로직이 실제로 필요로 하는 임베딩 쌍만 결정론적으로 수집한다."""
    pairs = []
    o_age = notion_fields.get("age_detail") or _fmt_age(notion_fields)
    n_age = new_fields.get("age_detail") or _fmt_age(new_fields)
    if _norm_text(str(o_age)) != _norm_text(str(n_age)) and o_age and n_age:
        os_, oe_ = notion_fields.get("start_age_months"), notion_fields.get("end_age_months")
        ns_, ne_ = new_fields.get("start_age_months"), new_fields.get("end_age_months")
        numeric_match = (os_ is not None and ns_ is not None and os_ == ns_
                         and oe_ is not None and ne_ is not None and oe_ == ne_)
        if not numeric_match:
            pairs.append((str(o_age), str(n_age)))
    for fkey in ("eligibility_detail", "target_income"):
        old, new = str(notion_fields.get(fkey) or ""), str(new_fields.get(fkey) or "")
        if old and new and _norm_text(old) != _norm_text(new):
            pairs.append((old, new))
    old = str(notion_fields.get("support_detail") or "")
    new = str(new_fields.get("support_detail") or "")
    if old and new and _norm_text(old) != _norm_text(new):
        pairs.append((old[:2000], new[:2000]))
    return pairs


async def _diff_fields_with_embeddings(notion_fields: Dict[str, Any],
                                       new_fields: Dict[str, Any], client):
    """필드별 개별 호출 대신 정책 1건의 비교 문자열을 임베딩 요청 1회로 처리한다."""
    pairs = _diff_embedding_pairs(notion_fields, new_fields)
    if not pairs or client is None:
        return _diff_fields(notion_fields, new_fields, None, {})
    texts = list(dict.fromkeys(text for pair in pairs for text in pair))
    vectors = await embedding_service.embed_texts_batch_async(texts, client=client)
    vector_by_text = dict(zip(texts, vectors))
    cosines = {}
    for old, new in pairs:
        old_vector, new_vector = vector_by_text.get(old), vector_by_text.get(new)
        if old_vector and new_vector:
            cosines[(old, new)] = embedding_service.cosine_sim(old_vector, new_vector)
    return _diff_fields(notion_fields, new_fields, None, cosines)


def _is_failed_extraction(fields: Optional[Dict[str, Any]]) -> bool:
    """ai_classifier의 예외 폴백값(API 과부하 등) 감지 — 이 값을 진짜 추출로 오인해
    비교하면 대량 오탐이 발생하므로(2026-07-02 503 과부하 실측) 호출자가 건너뛰게 한다."""
    if not fields:
        return False
    return (fields.get("category") == "미분류" and fields.get("target") == "일반"
            and not fields.get("support_detail") and not fields.get("eligibility_detail"))


# ==================== 캐시 재활용 (Stage 2 비용 절감) ====================
def _get_cached_extraction(url: str, title: str = "") -> Optional[Dict[str, Any]]:
    """일반 수집 캐시는 정책 중립 추출의 출처·버전을 증명하지 못해 사용하지 않는다."""
    return None


# ==================== 출처 fetch (Stage 1 재료) ====================
def _relaxed_ssl_context():
    """Legacy function name; source reads require normal certificate/host checks.

    A public read still supplies evidence for a policy change. Invalid TLS must
    be a visible retrieval failure, never a silent unverified source fallback.
    """
    import ssl
    return ssl.create_default_context()


class SourceFetchResult(tuple):
    """Compatible three-tuple with acquisition provenance for stage 1 callers."""

    def __new__(cls, normalized, text, dead, *, source_review=None,
                attachment_manifest=(), fetch_failure_kind=''):
        result = super().__new__(cls, (normalized, text, dead))
        result.source_review = source_review or {}
        result.attachment_manifest = tuple(attachment_manifest)
        result.fetch_failure_kind = fetch_failure_kind
        return result


def _source_attachment_manifest(static):
    """Stable links only: no downloads, source text injection or duplicate order noise."""
    from urllib.parse import urldefrag
    values = static.get('attachments') or []
    return tuple(sorted({urldefrag(value)[0] for value in values
                         if isinstance(value, str) and value.startswith(('http://', 'https://'))}))


async def _fetch_source(session, scraper, url: str, sem) -> Tuple[str, str, bool]:
    """Fetch text plus hash-only attachment manifest and explicit coverage holds.

    The raw source remains unmodified for evidence matching. Attachments are
    NOT read by this path, so their presence cannot certify a complete policy.
    """
    async with sem:
        text = ""
        static = {}
        rendered_used = False
        failure_kind = ''
        try:
            from services.news_attachments import validate_public_attachment_url
            static = await ai_classifier.fetch_page_content_async(
                session, url, url_validator=validate_public_attachment_url)
            text = (static.get("policy_text") or static.get("text") or "")
            failure_kind = str(static.get('fetch_failure_kind') or '')
            # The browser may be configured to ignore HTTPS errors for other
            # tasks. Do not bypass a failed verified fetch with that context.
            if len(text) < 300 and scraper is not None and not failure_kind:
                rendered = await scraper.fetch_rendered_text(
                    url, url_validator=validate_public_attachment_url)
                if len(rendered) > len(text):
                    text = rendered
                    rendered_used = True
        except Exception as e:
            logger.warning(f"[정책 최신화] fetch 실패 {url}: {e}")
            failure_kind = 'fetch_failed'

        norm = policy_watch._normalize_text(text)
        manifest = _source_attachment_manifest(static)
        from services import page_coverage
        coverage = page_coverage.normalize(static.get('source_page'))
        reasons = list(coverage.get('reasons', []))
        if rendered_used:
            # Plain rendered text has no attachment manifest or coverage proof.
            reasons.append('rendered_capture_partial')
        if failure_kind:
            reasons.append('fetch_failed')
        if manifest:
            reasons.append('attachment_review')
        review = {}
        if reasons:
            coverage = page_coverage.make(*reasons)
            review = {'code': 'source_read_incomplete', 'note': coverage['note'],
                      'reasons': coverage['reasons']}
        if manifest and norm:
            norm += '\n[policy-attachment-manifest-v1]\n' + json.dumps(manifest, ensure_ascii=False, separators=(',', ':'))
        # The verified fetch already classified HTTP failures. Do not issue a
        # second unguarded GET after timeout/unsafe URL/TLS failure. Temporary
        # retrieval errors are review holds, not proof of a dead source link.
        dead = failure_kind == 'not_found'
        if not norm and not review:
            review = {'code': 'source_missing', 'note': '정책 원문을 확인하지 못했습니다.'}
        return SourceFetchResult(norm, text, dead, source_review=review,
                                 attachment_manifest=manifest, fetch_failure_kind=failure_kind)


# ==================== 동기화 (Notion → 로컬 미러) ====================
def _drop_manually_resolved_fields(policy_id: int, fresh_fields: Dict[str, Any]) -> None:
    """(B2) 동기화로 Notion 최신값을 받아올 때, 대기 중인 진단의 'old'와 지금 실제 Notion
    값이 달라진 필드는 그 사이 사용자가 Notion에서 직접 고친 것으로 보고 진단에서 뺀다.
    안 그러면 사용자가 막 고친 값을 낡은 진단 기준 '이전 값'으로 착각해 되돌릴 위험이 있다."""
    row = database.execute_query(
        "SELECT pending_diff FROM notion_policies WHERE id=?", (policy_id,), fetch_one=True,
        raise_on_error=True)
    if not row or not row["pending_diff"]:
        return
    pd = utils.parse_json_safe(row["pending_diff"], {})
    fields = pd.get("fields", [])
    if not fields:
        return
    kept = []
    for f in fields:
        if f.get('applied'):
            kept.append(f)  # Keep review/undo access until explicit card cleanup.
            continue
        fkey = _label_to_field(f["field"])
        if fkey:
            live_val = fresh_fields.get(fkey)
            live_display = ", ".join(live_val) if isinstance(live_val, list) else str(live_val or "")
            if _norm_text(live_display) != _norm_text(f.get("old") or ""):
                continue  # 그 사이 Notion에서 직접 수정됨 — 이 필드 진단은 폐기
        kept.append(f)
    if len(kept) == len(fields):
        return
    pd["fields"] = kept
    if kept:
        pd["intensity"] = _intensity_of(kept)
        database.execute_write(
            "UPDATE notion_policies SET pending_diff=? WHERE id=?",
            (json.dumps(pd, ensure_ascii=False), policy_id), raise_on_error=True)
    else:
        database.execute_write(
            "UPDATE notion_policies SET pending_diff=NULL, change_status='none' WHERE id=?",
            (policy_id,), raise_on_error=True)


def _upsert_policy(db_key: str, page: Dict[str, Any], title_blob: Optional[bytes]):
    now = datetime.datetime.now().isoformat()
    page_id = page["page_id"]
    fields = {k: v for k, v in page.items() if not k.startswith("_") and k != "page_id"}
    existing = database.execute_query(
        "SELECT id, title, db_key, notion_fields, source_urls, pending_diff FROM notion_policies WHERE page_id = ?", (page_id,), fetch_one=True,
        raise_on_error=True)
    nf = json.dumps(fields, ensure_ascii=False)
    su = json.dumps(page.get("_source_urls", []), ensure_ascii=False)
    new_title = page.get("title", "")
    if existing:
        # Invalidate BEFORE replacing the source/snapshot. Even if the later
        # mirror write fails, a proposal grounded in the former source must
        # not remain writable. Reordering identical URLs alone is harmless.
        previous = dict(existing)
        evidence_context_changed = (
            set(utils.parse_json_safe(previous.get('source_urls'), [])) != set(page.get('_source_urls', []))
            or previous.get('db_key') != db_key or previous.get('title') != new_title)
        if evidence_context_changed:
            # A different source/title is a new comparison context even when
            # the newly linked page has no previous hash or pending proposal.
            database.execute_write('UPDATE notion_policies SET audited_at=NULL WHERE id=?',
                                   (existing['id'],), raise_on_error=True)
        context_changed = (
            set(utils.parse_json_safe(previous.get('source_urls'), [])) != set(page.get('_source_urls', []))
            or previous.get('db_key') != db_key
            or utils.parse_json_safe(previous.get('notion_fields'), {}) != fields)
        pending = utils.parse_json_safe(previous.get('pending_diff'), {})
        if context_changed and isinstance(pending, dict):
            dirty = False
            for field in pending.get('fields', []):
                if isinstance(field, dict) and not field.get('applied'):
                    field['snapshot_current'] = False
                    dirty = True
            if dirty:
                # A free recomparison cannot certify a different source or
                # document context. Only fresh extraction restores this marker.
                pending['proposal_schema_version'] = None
                database.execute_write('UPDATE notion_policies SET pending_diff=? WHERE id=?',
                    (json.dumps(pending, ensure_ascii=False), existing['id']), raise_on_error=True)
        # 재동기화: Notion 필드·출처·임베딩만 갱신 (source_hashes 기준선은 보존)
        if title_blob is not None:
            database.execute_write(
                "UPDATE notion_policies SET db_key=?, title=?, notion_fields=?, source_urls=?, "
                "title_embedding=?, last_synced_at=? WHERE page_id=?",
                (db_key, new_title, nf, su, title_blob, now, page_id), raise_on_error=True)
        elif existing["title"] != new_title:
            # (2026-08-05, GPT Codex 3차 리뷰) 제목이 바뀌었는데 새 임베딩 생성에 실패한
            # 경우 — 이전 코드는 title_embedding을 SET절에서 아예 빼서 옛 제목의 벡터가
            # 새 제목 아래 그대로 남았다("영유아 발달지원 사업"의 벡터가 "장애위험 영유아
            # 조기개입 지원"이라는 새 제목에 붙어있는 식). 제목이 안 바뀐 일시적 실패라면
            # 벡터를 보존하는 게 맞지만(재사용 로직이 그 경우를 이미 걸러줌), 제목이
            # 실제로 바뀐 경우엔 아예 비워서 다음 동기화 때 재생성되게 한다 — 틀린 유사도
            # 경고를 잠깐이라도 보여주는 것보다 "이번엔 유사도 판정 없음"이 안전하다.
            database.execute_write(
                "UPDATE notion_policies SET db_key=?, title=?, notion_fields=?, source_urls=?, "
                "title_embedding=NULL, last_synced_at=? WHERE page_id=?",
                (db_key, new_title, nf, su, now, page_id), raise_on_error=True)
        else:
            database.execute_write(
                "UPDATE notion_policies SET db_key=?, title=?, notion_fields=?, source_urls=?, "
                "last_synced_at=? WHERE page_id=?",
                (db_key, new_title, nf, su, now, page_id), raise_on_error=True)
        _drop_manually_resolved_fields(existing["id"], fields)
    else:
        database.execute_write(
            "INSERT INTO notion_policies (db_key, page_id, title, notion_fields, source_urls, "
            "title_embedding, last_synced_at) VALUES (?,?,?,?,?,?,?)",
            (db_key, page_id, page.get("title", ""), nf, su, title_blob, now),
            raise_on_error=True)


def record_created_policy(db_key: str, page_id: str, fields: Dict[str, Any]) -> None:
    """Notion 초안 생성 성공 직후 로컬 미러에도 최소 정보를 기록한다.

    다음 전체 동기화 전 같은 URL로 생성 버튼을 다시 눌러도 중복 검사가 즉시 작동하게
    하는 것이 목적이다. 임베딩은 다음 동기화에서 생성하므로 여기서는 비워 둔다.
    """
    page = {"page_id": page_id, **{
        key: fields.get(key) for key, _name, _type in notion_service.get_policy_props()
    }}
    page["title"] = str(fields.get("title") or "").strip()
    page["_source_urls"] = list(fields.get("_source_urls") or [])
    _upsert_policy(db_key, page, None)


# (2026-08-05, GPT Codex 리뷰) 배치 1건에 담을 제목 개수 — Gemini embed_content는 contents에
# 리스트를 넣으면 한 번의 API 요청으로 여러 텍스트를 처리하므로, 청크 크기만큼 API 요청
# 수 자체가 줄어 분당 한도(RPM) 위험이 크게 낮아진다(요청 수 기준 한도이지 항목 수 기준이
# 아니므로). 너무 크면 응답 지연·부분실패 시 되돌리는 범위가 커지므로 20으로 절충.
_EMBED_BATCH_CHUNK = 20


async def _embed_and_upsert_batch(db_key: str, pages: List[Dict[str, Any]], progress_cb=None, *, defer_embeddings=False) -> None:
    """사업명 임베딩을 청크 단위 배치 API 호출로 처리한 뒤 전부 로컬 미러에 upsert.

    (2026-07-28) 제목이 이전과 같고 기존 임베딩이 있으면 재사용 — "정책 정보 최신인지
    확인하기"를 누를 때마다 전체 정책(예: 155건)의 사업명을 매번 다시 임베딩하지 않는다.

    (2026-08-05) 재사용 불가한(신규·제목변경) 항목은 예전엔 페이지마다 개별 API 요청을
    Semaphore(4)로 "동시 개수"만 제한해 보냈는데, 이는 "분당 요청 수"는 전혀 막지 못해
    임베딩 캐시가 없는 새 설치·새 Notion DB에서 최대 155회의 개별 요청이 짧은 시간에
    몰릴 수 있었다(RPM 초과 위험). 대신 청크(기본 20개)로 묶어 embed_texts_batch_async
    한 번에 보낸다 — 요청 수 자체가 줄어든다. 청크 하나가 통째로 실패해도(네트워크 오류·
    그 청크 처리 중 일일 한도 소진 등) 그 청크만 임베딩 없이 저장되고, 이미 upsert된
    다른 청크나 재사용 항목에는 영향 없다(기존 임베딩을 지우지도 않음 — `_upsert_policy`가
    blob=None이면 title_embedding 컬럼을 SET절에서 아예 빼므로).
    """
    reuse_pages: List[Tuple[Dict[str, Any], Optional[bytes]]] = []
    need_embed_pages: List[Dict[str, Any]] = []

    for pg in pages:
        title = pg.get("title", "")
        page_id = pg.get("page_id")
        existing = await _db_query_strict(
            "SELECT title, title_embedding FROM notion_policies WHERE page_id = ?",
            (page_id,), True)
        if existing and existing["title"] == title and existing["title_embedding"]:
            reuse_pages.append((pg, existing["title_embedding"]))
        elif not defer_embeddings and title and title != "(제목없음)":
            need_embed_pages.append(pg)
        else:
            reuse_pages.append((pg, None))  # 제목 없음 — 임베딩 대상 아님

    for pg, blob in reuse_pages:
        await database.run_db(_upsert_policy, db_key, pg, blob)
        if progress_cb:
            progress_cb()

    for i in range(0, len(need_embed_pages), _EMBED_BATCH_CHUNK):
        chunk = need_embed_pages[i:i + _EMBED_BATCH_CHUNK]
        titles = [pg.get("title", "") for pg in chunk]
        try:
            vecs = await embedding_service.embed_texts_batch_async(titles)
        except Exception as e:
            logger.warning(f"[정책 최신화] 배치 임베딩 실패({len(chunk)}건): {e}")
            vecs = [None] * len(chunk)
        for pg, vec in zip(chunk, vecs):
            blob = embedding_service.to_blob(vec) if vec else None
            await database.run_db(_upsert_policy, db_key, pg, blob)
            if progress_cb:
                progress_cb()


async def sync_policies(db_keys: List[str], *, defer_embeddings=False) -> Dict[str, Any]:
    """선택한 정책 DB들의 Notion 페이지를 로컬 미러(notion_policies)에 동기화.

    사업명 임베딩은 청크(기본 20개) 단위 배치 API 호출로 처리해 요청 수 자체를 줄인다
    (`_embed_and_upsert_batch` 참조 — RPM 한도 보호).
    """
    import aiohttp
    token, _ = notion_service.get_credentials()
    if not token:
        return {"ok": False, "error": "Notion 토큰 미설정"}
    dbs = [d for d in notion_service.get_policy_dbs() if not db_keys or d["key"] in db_keys]
    if not dbs:
        return {"ok": False, "error": "설정된 정책 DB가 없습니다 (설정 → 정책 최신화 대상 DB)"}

    SYNC_STATUS.update(running=True, checked=0, total=0, message="Notion 페이지 조회 중…",
                       notion_rate_waited=0)
    synced = 0
    removed = 0
    reconciled = 0
    verification_pending = 0
    failures: List[Dict[str, str]] = []
    resolved_source_ids: Dict[str, str] = {}
    seen_by_db: Dict[str, set] = {}

    def _show_notion_wait(seconds: float) -> None:
        wait = max(1, int(round(seconds)))
        SYNC_STATUS["notion_rate_waited"] += 1
        SYNC_STATUS["message"] = f"Notion 요청이 몰려 잠시 대기 중 · {wait}초 후 자동 재시도"
        if ONE_CLICK_STATUS.get("running"):
            ONE_CLICK_STATUS["message"] = SYNC_STATUS["message"]

    async def _add_failure(db: Dict[str, Any], error: str) -> None:
        failures.append({"key": db["key"], "name": db["name"], "error": error})
        await database.run_db(
            record_policy_sync_result, db["key"], db.get("name") or db["key"], False, 0, error)

    try:
        async with aiohttp.ClientSession() as session:
            for d in dbs:
                target = await notion_service.resolve_target(
                    session, token, d["data_source_id"], _show_notion_wait)
                if not target.get("ok"):
                    error = target.get("error") or "보관함 ID를 해석하지 못했습니다."
                    await _add_failure(d, error)
                    logger.warning(f"[정책 최신화] {d['name']} 연결 실패: {error}")
                    continue
                canonical_id = notion_service.normalize_notion_id(str(target.get("id") or ""))
                if canonical_id in resolved_source_ids:
                    error = f"'{resolved_source_ids[canonical_id]}'과 같은 Notion 보관함이 중복 등록되어 있습니다."
                    await _add_failure(d, error)
                    logger.warning(f"[정책 최신화] {d['name']} 중복 연결: {error}")
                    continue
                resolved_source_ids[canonical_id] = d["name"]
                # (2026-07-13) 스키마 캐시 — 이 DB의 select/multi_select 허용 옵션을 조회해
                # 저장. 생애주기·대상특성 등 "유효 태그" 판정이 이 스키마를 근거로 삼는다.
                schema_result = await notion_service.get_policy_source_schema(
                    session, token, target, _show_notion_wait)
                if not schema_result.get("ok"):
                    error = schema_result.get("error") or "보관함 속성을 읽지 못했습니다."
                    await _add_failure(d, error)
                    logger.warning(f"[정책 최신화] {d['name']} 스키마 조회 실패: {error}")
                    continue
                missing = schema_result.get("missing_properties") or []
                mismatches = schema_result.get("type_mismatches") or []
                if missing or mismatches:
                    problems = []
                    if missing:
                        problems.append("필요한 속성 없음: " + ", ".join(str(name) for name in missing))
                    if mismatches:
                        problems.append("속성 종류 불일치: " + ", ".join(
                            str(item.get("name") or "") for item in mismatches))
                    error = "정책 보관함 " + " · ".join(problems) + ". 설정의 Notion 연결 확인에서 안내를 확인해주세요."
                    await _add_failure(d, error)
                    logger.warning(f"[정책 최신화] {d['name']} 스키마 확인 필요: {error}")
                    # An invalid table is not an empty table. Keep its schema,
                    # policy snapshots and pending work intact; failures also
                    # suppress the cross-database pruning below.
                    continue
                schema = schema_result["schema"]
                await _db_write_strict(
                    "INSERT INTO policy_db_schema (db_key, schema_json, updated_at) VALUES (?,?,CURRENT_TIMESTAMP) "
                    "ON CONFLICT(db_key) DO UPDATE SET schema_json=excluded.schema_json, updated_at=CURRENT_TIMESTAMP",
                    (d["key"], json.dumps(schema, ensure_ascii=False)))

                page_result = await notion_service.query_policy_source_pages(
                    session, token, target, _show_notion_wait)
                if not page_result.get("ok"):
                    error = page_result.get("error") or "보관함 페이지를 읽지 못했습니다."
                    await _add_failure(d, error)
                    logger.warning(f"[정책 최신화] {d['name']} 페이지 조회 실패: {error}")
                    continue
                pages = page_result["pages"]
                SYNC_STATUS["total"] += len(pages)
                SYNC_STATUS["message"] = f"{d['name']} 동기화 중… ({len(pages)}건)"
                # (버그 2026-07-16) check_policies와 동일한 문제 — "정책 정보 최신인지
                # 확인하기"가 내부적으로 이 동기화를 재사용하는데, 진행률이 SYNC_STATUS에만
                # 쓰여서 ONE_CLICK_STATUS만 폴링하는 프론트에는 전혀 안 보이고 멈춘 것처럼
                # 보였다. 통합 점검이 진행 중일 때만 같은 메시지를 그쪽에도 반영한다.
                if ONE_CLICK_STATUS.get("running"):
                    ONE_CLICK_STATUS["message"] = SYNC_STATUS["message"]
                _n_done = 0

                def _on_page_done():
                    nonlocal _n_done, synced
                    _n_done += 1
                    SYNC_STATUS["checked"] += 1
                    synced += 1
                    if ONE_CLICK_STATUS.get("running"):
                        ONE_CLICK_STATUS["message"] = f"{d['name']} 동기화 중… ({_n_done}/{len(pages)})"

                if defer_embeddings:
                    await _embed_and_upsert_batch(d["key"], pages, progress_cb=_on_page_done, defer_embeddings=True)
                else:
                    await _embed_and_upsert_batch(d["key"], pages, progress_cb=_on_page_done)
                logger.info(f"[정책 최신화] 동기화 {d['name']}: {len(pages)}건")

                # (S2) 미러 위생 — 이번 동기화에서 Notion에 더 이상 없는(삭제/이동된) 페이지를
                # 로컬 미러에서 정리. 방치하면 점검 때마다 죽은 정책의 출처를 계속 fetch하고
                # 신규후보 매칭에도 유령으로 남는다.
                seen_by_db[d['key']] = {pg['page_id'] for pg in pages}
                await database.run_db(
                    record_policy_sync_result, d["key"], schema.get("_title") or d["name"],
                    True, len(pages), "")
            # A page can move between selected databases. Finish every upsert
            # before pruning, so database iteration order cannot erase its ID,
            # pending review or undo history. Partial reads never prune.
            if not failures:
                all_seen = set().union(*seen_by_db.values()) if seen_by_db else set()
                for db_key in seen_by_db:
                    existing = await _db_query_strict(
                        'SELECT page_id FROM notion_policies WHERE db_key=?', (db_key,))
                    stale_ids = [row['page_id'] for row in existing if row['page_id'] not in all_seen]
                    if stale_ids:
                        q = ','.join('?' for _ in stale_ids)
                        await _db_write_strict(
                            f'DELETE FROM notion_policies WHERE db_key=? AND page_id IN ({q})',
                            (db_key, *stale_ids))
                        removed += len(stale_ids)
                # Explicit sync can safely re-read an uncertain write's exact
                # property value. This never sends or repeats a Notion write.
                from services import policy_undo
                for db_key in seen_by_db:
                    policies = await _db_query_strict('SELECT id FROM notion_policies WHERE db_key=?', (db_key,))
                    for policy in policies:
                        outcome = await policy_undo.reconcile(policy['id'])
                        reconciled += outcome['resolved']
                        verification_pending += outcome['pending']
    finally:
        if failures:
            names = ", ".join(item["name"] for item in failures)
            message = f"일부 보관함을 읽지 못해 중단 · {names} · 기존 로컬 자료 유지"
        else:
            message = f"동기화 완료 · {synced}건" + (f" (제거 {removed}건)" if removed else "")
            if verification_pending:
                message += f' · 이전 반영 결과 확인 필요 {verification_pending}건'
        SYNC_STATUS.update(running=False, message=message)
    return {"ok": not failures, "synced": synced, "removed": removed,
            "dbs": [d["key"] for d in dbs if d["key"] not in {f["key"] for f in failures}],
            "failures": failures,
            "error": failures[0]["error"] if failures else None,
            'reconciled': reconciled, 'verification_pending': verification_pending}


def _intensity_of(diffs: List[Dict[str, Any]]) -> str:
    """diff 목록 → 대표 강도. high가 하나라도 있으면 high, 없고 info가 있으면 info, 아니면 low."""
    if any(d["level"] == "high" for d in diffs):
        return "high"
    if any(d["level"] == "info" for d in diffs):
        return "info"
    return "low"


# "지원금액·기간"은 실제 Notion 속성이 없는 파생 비교 신호(상세지원내용에서 뽑은 금액/날짜
# 토큰 집합)라 "Notion에 적용" 버튼도 못 붙고, 원문 그대로 노출하면 토큰이 뒤섞여 보여
# 사용자가 혼란스러워한다(실측 2026-07-14). 판정에는 계속 반영하되(금액이 통째로 바뀌면
# 다른 필드 없이도 🔴로 잡아내는 유일한 신호이므로) 카드 목록에는 표시하지 않는다.
_NON_DISPLAY_FIELDS = {"지원금액·기간"}


def _visible_diffs(diffs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """intensity 판정 후, 화면에 카드로 보여줄 필드만 남긴다."""
    return [d for d in diffs if d["field"] not in _NON_DISPLAY_FIELDS]


def _snapshot_diffs(diffs, notion_fields):
    """AI-to-AI detection is not the value that will be overwritten in Notion."""
    result = []
    for original in _visible_diffs(diffs):
        field = dict(original)
        key = _label_to_field(field['field'])
        if key is None:
            continue
        value = notion_fields.get(key)
        if key == 'age_detail':
            value = value or _fmt_age(notion_fields)
        display = ', '.join(str(item) for item in value) if isinstance(value, list) else str(value or '')
        if _factual_text(display) == _factual_text(str(field.get('new') or '')):
            continue
        field.update(old=display, snapshot_current=True)
        result.append(field)
    return result


def _proposal_schema_version():
    from services.policy_apply_coverage import POLICY_PROPOSAL_SCHEMA_VERSION
    return POLICY_PROPOSAL_SCHEMA_VERSION


def _proposal_source_context(title, sources):
    """Bind the whole analyzed source set, not just the preview's first URL."""
    return {'title': title, 'urls': list(sources), 'hashes': {
        url: hashlib.sha256(text.encode('utf-8')).hexdigest()
        for url, text in sources.items()}}


# ==================== 점검 (Stage 1 + Stage 2) ====================
async def watch_stage1_only(db_keys: List[str]) -> Dict[str, Any]:
    """(U2) 무료 자동 감시 — 해시만 비교하고 AI는 전혀 호출하지 않는다.

    스케줄러가 주기적으로 불러도 안전하도록 완전히 읽기 전용이다(DB에 아무것도 쓰지 않음).
    여기서 해시를 갱신해버리면 이후 사용자가 '선택 점검'을 눌러도 이미 최신 해시와
    비교하게 돼 Stage2(AI) 분석 기회를 조용히 놓치므로, 실제 갱신·분석은 항상 사용자가
    수동으로 누르는 선택 점검(check_policies)에게 맡긴다.
    """
    import aiohttp
    from scraper_engine import ScraperEngine

    db_keys = _active_configured_keys(db_keys)
    if not db_keys:
        return {"checked": 0, "changed": 0, "changed_titles": []}
    rows = await _db_query_strict(
        "SELECT id, title, source_urls, source_hashes FROM notion_policies "
        "WHERE db_key IN (%s)" % ",".join("?" * len(db_keys)), tuple(db_keys))
    rows = [dict(r) for r in rows]
    if not rows:
        return {"checked": 0, "changed": 0, "changed_titles": []}

    scraper = ScraperEngine(headless=True)
    changed_titles: List[str] = []
    failed_titles: List[str] = []
    try:
        await scraper.start()
        # Even read-only monitoring requires verified source acquisition.
        connector = aiohttp.TCPConnector(ssl=_relaxed_ssl_context())
        async with aiohttp.ClientSession(connector=connector) as session:
            sem = asyncio.Semaphore(_FETCH_CONCURRENCY)
            all_urls = set()
            for r in rows:
                for u in utils.parse_json_safe(r["source_urls"], []):
                    all_urls.add(u)
            all_urls = list(all_urls)
            fetch_tasks = [_fetch_source(session, scraper, u, sem) for u in all_urls]
            fetched = await asyncio.gather(*fetch_tasks, return_exceptions=True)
            url_map: Dict[str, Tuple[str, bool]] = {}
            for u, res in zip(all_urls, fetched):
                if isinstance(res, Exception):
                    url_map[u] = ("", True)
                else:
                    norm, _raw, dead = res
                    review = getattr(res, 'source_review', {})
                    reasons = review.get('reasons', [])
                    # It is possible to monitor attachment links without
                    # approving their unread contents. All other incompleteness
                    # remains an explicit monitoring failure, not unchanged.
                    incomplete = bool([reason for reason in reasons if reason != 'attachment_review']
                                      or (review and not reasons))
                    url_map[u] = (norm, bool(dead or incomplete or not norm))

            for r in rows:
                old_hashes = utils.parse_json_safe(r["source_hashes"], {})
                failed = False
                changed_here = False
                for u in utils.parse_json_safe(r["source_urls"], []):
                    if is_low_value_source(u):
                        continue
                    norm, unreadable = url_map.get(u, ("", True))
                    if unreadable or not norm:
                        failed = True
                        continue
                    h = hashlib.sha256(norm.encode("utf-8")).hexdigest()
                    if old_hashes.get(u) is not None and old_hashes.get(u) != h:
                        changed_here = True
                if failed:
                    failed_titles.append(r['title'])
                if changed_here:
                    changed_titles.append(r['title'])
    except Exception:
        failed_titles = [r['title'] for r in rows]
    finally:
        try:
            await scraper.close()
        except Exception:
            logger.warning('[정책 최신화-무료감시] 브라우저 정리 실패')
    logger.info(f"🔍 [정책 최신화-무료감시] 점검 {len(rows)}건 · 원문변경 의심 {len(changed_titles)}건 (AI 미사용)")
    failed = len(failed_titles)
    return {"checked": len(rows), "changed": len(changed_titles), "changed_titles": changed_titles,
            "failed": failed, "succeeded": len(rows) - failed, "failed_titles": failed_titles,
            "status": ('error' if failed == len(rows) else 'partial') if failed else 'completed'}


async def run_full_check(db_keys: List[str]) -> Dict[str, Any]:
    """Complete needed comparisons/preparation; Notion application stays manual."""
    from services.policy_full_check import run
    return await run(db_keys)


def _revert_hashes_for_unanalyzed_change(
        new_hashes: Dict[str, Any], new_texts: Dict[str, Any],
        old_hashes: Dict[str, Any], old_texts: Dict[str, Any],
        real_changes: List[Tuple[str, str, str]]) -> None:
    """(2026-08-05, GPT Codex 리뷰로 발견) AI 재추출(_extract)이 429 등으로 실패(None 반환)하면
    이 함수를 호출해, 실제 변경으로 감지됐던(real_changes) URL들의 해시·본문만 이전 값으로
    되돌린다(그 외 URL은 정상 갱신된 값 그대로 둠).

    되돌리지 않으면: 원문 변경 감지 → AI 429 실패 → 그래도 새 해시가 저장됨 → 다음 점검에서는
    이미 같은 해시라 "변경 없음"으로 간주 → 그 변경을 영원히 재분석하지 못하고 놓친다(실측
    확인된 데이터 정확성 결함). new_hashes/new_texts를 in-place로 수정한다."""
    for u, _prev, _new in real_changes:
        new_hashes[u] = old_hashes.get(u)
        new_texts[u] = old_texts.get(u, "")


def _policy_ai_pause_reason(*, client_available, stop_on_quota=False):
    """Provider/durable-state holds stop every policy entry, not just the main action.

    Individual source/privacy reviews are not global pauses. Standalone tools
    retain their existing missing-connection guidance without deriving a daily
    allowance from local counts or an old observation.
    """
    from services.policy_full_check import ai_pause_reason
    reason = ai_pause_reason(client_available=client_available)
    return reason if stop_on_quota or reason in {'daily_limit', 'quota_state_unavailable'} else ''


async def check_policies(db_keys: List[str], scraper=None, *, include_initial=False, stop_on_quota=False) -> Dict[str, Any]:
    """선택 DB 정책들의 출처를 점검. Stage1(해시) → 변경분만 Stage2(AI 재추출·비교).

    Stage2는 'Notion 현재값'이 아니라 **이전에 추출해둔 AI 결과(last_extracted_fields)**와
    비교한다(P2) — 같은 추출기를 양쪽에 써서 사람-AI 표현차 노이즈를 없앤다. 다만 최초
    비교(include_initial)와 재검증은 최신 Notion 값에 대비하여 미반영 제안을 보존한다.
    최초 비교와 변경 점검은 한 번의 추출을 공유하며 완료된 비교는 반복하지 않는다.
    """
    import aiohttp
    import difflib
    from services import policy_review_store
    from scraper_engine import ScraperEngine

    active_keys = _active_configured_keys(db_keys)
    if active_keys:
        rows = await _db_query_strict(
            "SELECT id, page_id, title, notion_fields, source_urls, source_hashes, source_texts, "
            "db_key, last_extracted_fields, pending_diff, change_status, audited_at FROM notion_policies WHERE db_key IN (%s)" %
            ",".join("?" * len(active_keys)), tuple(active_keys))
    else:
        rows = []
    rows = [dict(r) for r in rows]
    POLICY_STATUS.update(running=True, phase="fetch", checked=0, total=len(rows),
                         changed=0, ai_required=0, ai_completed=0,
                         ai_rate_waited=0, ai_failed=0, ai_preflight_held=0, message="출처 점검 중…",
                          review_count=0, review_notes=[], review_storage_error=None,
                          initial_compared=0, pause_reason='')
    if not rows:
        POLICY_STATUS.update(running=False, message="점검 대상 없음")
        return {"ok": True, "checked": 0, "changed": 0}

    unresolved_policies = await database.run_db(policy_review_store.unresolved_by_policy, active_keys)

    own = scraper is None
    if own:
        # (UX 2026-07-24) 브라우저 엔진 실행(최초엔 Chromium 자동 설치 포함)은 수 초~수 분 걸리는데
        # 그동안 "출처 점검 중…"만 떠 멈춘 것처럼 보였다. 준비 중임을 알리는 문구를 먼저 띄운다.
        _prep_msg = "브라우저 엔진을 준비하는 중이에요… (처음 실행할 땐 시간이 더 걸릴 수 있어요)"
        POLICY_STATUS["message"] = _prep_msg
        if ONE_CLICK_STATUS.get("running"):
            ONE_CLICK_STATUS["message"] = _prep_msg
        scraper = ScraperEngine(headless=True)
        try:
            await scraper.start()
        except BaseException:
            POLICY_STATUS.update(running=False, phase='error', message='브라우저 준비에 실패했습니다. 다시 점검해주세요.')
            try:
                await scraper.close()
            except Exception:
                logger.warning('[정책 최신화] 브라우저 준비 실패 후 정리 실패')
            raise

    ai_client = ai_classifier.get_client()
    changed = 0
    now = datetime.datetime.now().isoformat()
    # (url, title) 조합으로 캐싱 — 여러 정책이 같은 URL(예: 통합 안내 페이지)을 공유할 때
    # URL만으로 캐싱하면 먼저 처리된 정책의 추출값이 다른 정책에도 잘못 재사용되는 버그가
    # 실측(2026-07-03)에서 확인됨: "35세 이상 임산부..."와 "서울아기건강 첫걸음..."이 같은
    # health.dobong.go.kr 페이지를 참조하는데 후자에 전자의 대상연령이 섞여 들어감.
    run_cache: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    # 정책 점검은 일반 수집 리미터를 거치지 않는 독립 경로다. 같은 모델의 무료 RPM을
    # 갑자기 소진하지 않도록 이 점검 실행 안에서 슬라이딩 윈도우로 호출을 제한한다.
    ai_rpm = max(1, int(utils.get_env_value("POLICY_AI_RPM") or 10))
    ai_period = max(0.01, float(utils.get_env_value("POLICY_AI_RATE_PERIOD") or 60.0))
    ai_call_times: List[float] = []

    def _mirror_ai_status() -> None:
        if ONE_CLICK_STATUS.get("running"):
            for key in ("ai_required", "ai_completed", "ai_rate_waited", "ai_failed", "ai_preflight_held", "changed",
                        "review_count", "review_notes"):
                ONE_CLICK_STATUS[key] = POLICY_STATUS[key]

    async def _acquire_ai_slot() -> bool:
        waited = False
        while True:
            now_mono = time.monotonic()
            ai_call_times[:] = [t for t in ai_call_times if now_mono - t < ai_period]
            if len(ai_call_times) < ai_rpm:
                ai_call_times.append(now_mono)
                return waited
            waited = True
            wait_for = ai_period - (now_mono - ai_call_times[0]) + 0.01
            POLICY_STATUS["message"] = f"AI 호출 한도 보호를 위해 약 {max(1, round(wait_for))}초 대기 중…"
            if ONE_CLICK_STATUS.get("running"):
                ONE_CLICK_STATUS["message"] = POLICY_STATUS["message"]
            await asyncio.sleep(max(0.01, wait_for))

    async def _before_policy_send() -> None:
        # Input-only holds must not consume this workflow's RPM timestamps.
        # Actual attempts still retain both this limit and shared durable quota.
        if await _acquire_ai_slot():
            POLICY_STATUS['ai_rate_waited'] += 1

    async def _extract(url: str, title: str, db_key: str, text: str, *,
                       policy_id=None, source_urls=None, source_role='current') -> Optional[Dict[str, Any]]:
        cache_key = (db_key, url, title, hashlib.sha256(text.encode('utf-8')).hexdigest())
        if cache_key in run_cache:
            return run_cache[cache_key]
        # (2026-08-05, GPT Codex 3차 리뷰로 발견한 P0) 예전엔 여기서 _get_cached_extraction()로
        # 일반 수집(welfare_items)의 과거 ai_data를 재사용했는데, 그 캐시는 URL+제목만 보고
        # "지금 이 함수에 넘어온 새 원문(text)과 실제로 같은 내용인지"는 전혀 검증하지 않았다.
        # 이 함수는 check_policies의 Stage 2 — 즉 Stage 1이 해시 변경을 이미 확인해 "원문이
        # 바뀌었다"고 판단한 뒤에만 불린다. 그런데 그 URL이 예전에 일반 수집으로 분석된 적이
        # 있으면, 원문이 바뀌었다는 사실과 무관하게 그 옛날 캐시를 "새 원문 분석 결과"인 양
        # 그대로 반환해버려 실제 변경을 놓쳤다(실측: 운영 DB에서 정책 출처와 일반 수집 캐시가
        # 겹치는 URL 2건 확인됨 — 이론적 케이스가 아니었음). Stage 2는 "내용이 바뀐 게 확실한"
        # 소수의 URL만 도는 경로라 캐시로 아끼는 비용도 작으므로, 여기서는 캐시를 아예 쓰지
        # 않고 항상 새로 추출한다(일반 수집 쪽의 캐시 재사용은 그대로 유지 — 그쪽은 "내용이
        # 안 바뀌었을 가능성이 높은" 경로라 성격이 다르다).
        fields = None
        reason = POLICY_STATUS.get('pause_reason') or _policy_ai_pause_reason(
            client_available=ai_client is not None, stop_on_quota=stop_on_quota)
        if reason:
            POLICY_STATUS['pause_reason'] = reason
            return None
        if ai_client:
            # 중립 추출(B) — 수집 필터·CUSTOM_AI_PROMPT 편향 없이 필드값만 옮김
            # (2026-07-07) 다중 프로그램 나열 페이지 대비 제목 근접 발췌로 우선 좁힘
            snippet = _policy_evidence_text(text, title, len(source_urls or [url]))
            preflight = ai_classifier.policy_source_preflight_review(snippet)
            if preflight:
                _record_extraction_review(POLICY_STATUS, title, preflight)
                if policy_id is not None:
                    await _persist_policy_review(POLICY_STATUS, 'record_hold',
                        policy_id, title, source_urls or [url], snippet, preflight, source_role)
                POLICY_STATUS['ai_preflight_held'] += 1
                _mirror_ai_status()
                return None
            POLICY_STATUS["ai_required"] += 1
            _mirror_ai_status()
            review = {}
            fields = await ai_classifier.extract_policy_fields_async(
                ai_client, title, url, db_key, snippet, taxonomy=_get_db_schema(db_key), review=review,
                before_send=_before_policy_send)
            if fields is None:
                # The classifier may return None after a coordinator quota
                # rejection. Stop this pass, not every remaining policy.
                POLICY_STATUS['pause_reason'] = (
                    review['code'] if stop_on_quota and review.get('retryable') is True and review.get('code') in {
                        'source_input_count_unavailable', 'source_input_rate_limited'}
                    else _policy_ai_pause_reason(client_available=ai_client is not None,
                                                 stop_on_quota=stop_on_quota))
            _record_extraction_review(POLICY_STATUS, title, review)
            if fields is None and policy_id is not None and policy_review_store.supports_review(review):
                # Known validation holds survive a restart without saving model
                # output or making another request. Transient failures do not.
                await _persist_policy_review(POLICY_STATUS, 'record_hold',
                    policy_id, title, source_urls or [url], snippet, review, source_role)
            if fields and _is_failed_extraction(fields):
                fields = None  # API 과부하 등의 오류 폴백값을 진짜 추출로 오인하지 않음
            if fields:
                POLICY_STATUS["ai_completed"] += 1
            else:
                POLICY_STATUS["ai_failed"] += 1
            _mirror_ai_status()
        if fields:
            _normalize_extracted_fields(fields, db_key)
            run_cache[cache_key] = fields
        return fields

    try:
        # 검증된 HTTPS만 정책 근거로 사용한다. 인증서 오류는 보류로 안내하며
        # 검증 해제나 무검증 재요청으로 우회하지 않는다.
        connector = aiohttp.TCPConnector(ssl=_relaxed_ssl_context())
        async with aiohttp.ClientSession(connector=connector) as session:
            sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

            # 모든 출처 URL 수집 → URL 단위 dedup 후 병렬 fetch (①②)
            all_urls = set()
            for r in rows:
                for u in utils.parse_json_safe(r["source_urls"], []):
                    all_urls.add(u)
            all_urls = list(all_urls)
            POLICY_STATUS["total"] = len(all_urls)

            # (버그 2026-07-22) 이 fetch 단계는 URL이 많으면(백여 개) 동시성 제한 때문에
            # 수 분씩 걸릴 수 있는데, 여기서 진행 카운터를 전혀 갱신하지 않아 화면엔 고정
            # 문구만 떠서 멈춘 것처럼 보였다(실측: "변경된 정책 확인 중…"에서 무한정 정지된
            # 듯 보임). 완료되는 URL마다 카운터를 올려 실시간으로 반영한다.
            _fetch_done = 0

            async def _fetch_and_track(u: str):
                nonlocal _fetch_done
                result = await _fetch_source(session, scraper, u, sem)
                _fetch_done += 1
                POLICY_STATUS["checked"] = _fetch_done
                msg = f"출처 페이지 확인 중… ({_fetch_done}/{len(all_urls)})"
                POLICY_STATUS["message"] = msg
                if ONE_CLICK_STATUS.get("running"):
                    ONE_CLICK_STATUS["message"] = msg
                    ONE_CLICK_STATUS.update(checked=_fetch_done, total=len(all_urls))
                return result

            fetch_tasks = [_fetch_and_track(u) for u in all_urls]
            fetched = await asyncio.gather(*fetch_tasks, return_exceptions=True)
            url_map: Dict[str, Tuple[str, bool]] = {}
            for u, res in zip(all_urls, fetched):
                if isinstance(res, Exception):
                    url_map[u] = ("", False)  # Retrieval exception is not evidence of a dead link.
                else:
                    norm, _raw, dead = res
                    url_map[u] = (norm, dead)

            # 정책별 Stage 1 → Stage 2
            source_reviews = {url: getattr(result, 'source_review', {})
                              for url, result in zip(all_urls, fetched)
                              if not isinstance(result, Exception) and getattr(result, 'source_review', {})}
            for url, result in zip(all_urls, fetched):
                if isinstance(result, Exception) or result[2] or not result[0]:
                    source_reviews.setdefault(url, {'code': 'source_missing', 'fields': [],
                        'note': '정책 원문을 확인하지 못했습니다. 출처를 확인한 뒤 다시 점검해주세요.'})
            raw_by_url = {
                url: (result[1] or result[0])
                for url, result in zip(all_urls, fetched)
                if not isinstance(result, Exception)
            }
            # (표시 버그 2026-07-23) fetch 단계에서 total을 URL 개수로 바꿔놨는데, 비교
            # 단계는 정책(rows) 단위로 카운트하므로 total을 정책 수로 되돌려야 배너의
            # "(N/전체)"가 뒤로 점프하지 않고 100%까지 자연스럽게 찬다.
            POLICY_STATUS.update(phase="compare", total=len(rows))
            for i, r in enumerate(rows):
                POLICY_STATUS["checked"] = i + 1
                # (버그 2026-07-16) "정책 정보 최신인지 확인하기"(run_full_check)는 내부적으로
                # 이 check_policies를 그대로 재사용하는데, 진행률은 POLICY_STATUS에만 쓰여서
                # 프론트가 ONE_CLICK_STATUS만 폴링하는 동안엔 "(N/전체)" 숫자가 전혀 안 보이고
                # 막연히 멈춘 것처럼 보였다(실측: 사용자가 딴 화면 갔다 와야만 갱신됨). 진행 중인
                # 한번에 점검이 있을 때만 같은 숫자를 그쪽 상태에도 반영한다.
                if ONE_CLICK_STATUS.get("running"):
                    ONE_CLICK_STATUS["message"] = f"출처 점검 중… ({i + 1}/{len(rows)})"
                    ONE_CLICK_STATUS.update(checked=i + 1, total=len(rows))
                # (2026-08-05, GPT Codex 4차 리뷰) source_urls 저장 시점(extract_page_fields)의
                # 중복 제거는 다음 Notion 동기화부터만 적용된다. 이미 중복이 저장된 기존
                # 정책(실측: "희귀질환자 의료비 지원")은 다음 동기화 전까지 여기서도 중복을
                # 그대로 순회해 같은 원문이 두 번 병합될 수 있으므로, 읽는 시점에도 순서
                # 유지 중복 제거로 방어한다.
                urls = list(dict.fromkeys(utils.parse_json_safe(r["source_urls"], [])))
                old_hashes = utils.parse_json_safe(r["source_hashes"], {})
                old_texts = utils.parse_json_safe(r["source_texts"], {})
                new_hashes, new_texts, dead_links = {}, {}, []
                changed_items = []   # [(url, prev_text, new_text)] — 이번에 실제로 바뀐 출처 전부
                policy_review_start = POLICY_STATUS['review_count']
                held_urls = [u for u in urls if u in source_reviews and not is_low_value_source(u)]
                for u in held_urls:
                    review = source_reviews[u]
                    _record_extraction_review(POLICY_STATUS, r['title'], review)
                    await _persist_policy_review(POLICY_STATUS, 'record_hold',
                        r['id'], r['title'], [u], raw_by_url.get(u) or '', review, 'fetch')
                    POLICY_STATUS['ai_preflight_held'] += 1
                _mirror_ai_status()

                for u in urls:
                    norm, dead = url_map.get(u, ("", False))
                    if dead:
                        dead_links.append(u)
                    if not norm or u in held_urls:
                        new_hashes[u] = old_hashes.get(u)
                        new_texts[u] = old_texts.get(u, "")
                        continue
                    h = hashlib.sha256(norm.encode("utf-8")).hexdigest()
                    new_hashes[u] = h
                    structured = raw_by_url.get(u) or norm
                    new_texts[u] = structured
                    if old_hashes.get(u) is None:
                        pass  # 최초 기준선 — 변경 아님
                    elif old_hashes.get(u) != h and not is_low_value_source(u):
                        # 검색/목록 페이지의 해시 변동은 트리거로 쓰지 않음(④ 노이즈 제외)
                        changed_items.append((u, old_texts.get(u, ""), structured))

                # Only known formatting/volatile noise may suppress analysis.
                # 99% similarity can still hide a changed discount or negation.
                real_changes = []
                for u, prev, new in changed_items:
                    if (prev and policy_watch._normalize_text(prev) == policy_watch._normalize_text(new)
                            and old_hashes.get(u) == hashlib.sha256(policy_watch._normalize_text(prev).encode('utf-8')).hexdigest()
                            and not any(getattr(result, 'attachment_manifest', ())
                                        for source, result in zip(all_urls, fetched) if source == u)):
                        continue
                    real_changes.append((u, prev, new))

                # An explicit check also refreshes old pending evidence. Without
                # this, an unchanged source leaves a v2 proposal permanently
                # blocked by the apply gate. Never relabel its old AI output.
                old_pending = utils.parse_json_safe(r.get("pending_diff"), {})
                old_meta = old_pending.get("extraction_meta") if isinstance(old_pending, dict) else None
                old_pending_fields = old_pending.get("fields", []) if isinstance(old_pending, dict) else []
                changed_source_review = None
                if real_changes and isinstance(old_pending_fields, list) and any(
                        isinstance(field, dict) and not field.get('applied') for field in old_pending_fields):
                    # Persist before awaiting AI: a failure/cancellation must not
                    # leave the earlier proposal approved against a source we
                    # already observed changing. Retry baselines stay untouched.
                    # A separate role prevents a later input/model hold from
                    # erasing the requirement for a NEW fetch and comparison.
                    changed_source_review = {'code': 'source_changed_unverified', 'fields': []}
                    evidence_urls = [u for u in urls if not is_low_value_source(u)]
                    try:
                        await _persist_policy_review(POLICY_STATUS, 'record_hold',
                            r['id'], r['title'], evidence_urls,
                            '\n\n---\n\n'.join(new_texts.get(u, '') for u in evidence_urls),
                            changed_source_review, 'freshness')
                    except Exception:
                        if POLICY_STATUS['review_count'] == policy_review_start:
                            _record_extraction_review(POLICY_STATUS, r['title'], changed_source_review)
                            _mirror_ai_status()
                        raise
                revalidate_pending = bool(r.get("change_status") == "detected"
                    and isinstance(old_pending_fields, list)
                    and any(isinstance(field, dict) and not field.get("applied") for field in old_pending_fields)
                    and (not isinstance(old_meta, dict)
                         or old_meta.get("prompt_version") != _EXTRACTION_PROMPT_VERSION
                         or old_pending.get('proposal_schema_version') != _proposal_schema_version()
                         or (len([u for u in urls if not is_low_value_source(u)]) > 1
                             and not isinstance(old_pending.get('source_context'), dict))
                         or any(field.get('snapshot_current') is not True for field in old_pending_fields
                                if isinstance(field, dict) and not field.get('applied'))))
                initial_comparison = include_initial and r.get('audited_at') is None
                revalidate_pending = revalidate_pending or r['id'] in unresolved_policies or initial_comparison
                analysis_sources = real_changes
                if real_changes:
                    # A changed source is the trigger, not the complete evidence.
                    # Include unchanged companion sources so their conditions
                    # cannot disappear from a replacement proposal.
                    analysis_sources = [(u, old_texts.get(u, ''), new_texts.get(u, ''))
                                        for u in urls if not is_low_value_source(u)]
                # Current-version proposals can acquire a new source-only hold
                # without any source hash change. Surface it again on explicit
                # checks, but do not expire the proposal or spend an AI request.
                if (not revalidate_pending and not real_changes
                        and r.get('change_status') == 'detected'
                        and isinstance(old_pending_fields, list)
                        and any(isinstance(field, dict) and not field.get('applied') for field in old_pending_fields)):
                    for source_url in urls:
                        available, dead = url_map.get(source_url, ('', True))
                        if not available or dead or source_url in held_urls or is_low_value_source(source_url):
                            continue
                        snippet = _pick_relevant_snippet(new_texts[source_url], r['title'])
                        preflight = ai_classifier.policy_source_preflight_review(snippet)
                        if preflight:
                            _record_extraction_review(POLICY_STATUS, r['title'], preflight)
                            await _persist_policy_review(POLICY_STATUS, 'record_hold',
                                r['id'], r['title'], [source_url], snippet, preflight)
                            POLICY_STATUS['ai_preflight_held'] += 1
                    _mirror_ai_status()
                if revalidate_pending:
                    eligible_urls = [u for u in urls if not is_low_value_source(u)]
                    if not eligible_urls or held_urls or any(not url_map.get(u, ("", True))[0]
                            or url_map.get(u, ("", True))[1] for u in eligible_urls):
                        if not held_urls:
                            _record_extraction_review(POLICY_STATUS, r["title"], {
                                "code": "source_unavailable", "fields": [],
                                "note": "이전 변경 제안의 출처를 모두 확인하지 못했습니다. 출처 주소를 확인한 뒤 다시 점검해주세요. 기존 내용과 제안은 유지됩니다."})
                        analysis_sources = []
                        _revert_hashes_for_unanalyzed_change(
                            new_hashes, new_texts, old_hashes, old_texts, real_changes)
                    else:
                        analysis_sources = [(u, old_texts.get(u, ""), new_texts[u]) for u in eligible_urls]
                    _mirror_ai_status()

                # Never treat an available subset as complete evidence for a
                # multi-source policy. The next explicit check must retry it.
                if held_urls or any(url_map.get(u, ('', True))[1] or not url_map.get(u, ('', True))[0]
                                    for u in urls if not is_low_value_source(u)):
                    analysis_sources = []
                    _revert_hashes_for_unanalyzed_change(new_hashes, new_texts, old_hashes, old_texts, real_changes)

                # (S4) 출처가 여러 개(관련홈페이지1·2·3)인 정책은 동시에 여러 개가 바뀔 수 있다.
                # 이전엔 마지막 것만 분석해 나머지 변경을 놓쳤으므로, 실제로 바뀐 출처 전부를
                # 병합해 하나의 텍스트로 비교한다.
                changed_url, changed_text, prev_text_for_url = "", "", ""
                if analysis_sources:
                    changed_url = analysis_sources[0][0]   # 사이트 정보용 대표 URL
                    prev_text_for_url = "\n\n---\n\n".join(p for _, p, _ in analysis_sources if p)
                    changed_text = "\n\n---\n\n".join(n for _, _, n in analysis_sources)

                # Stage 2 — 원문이 실제 바뀐 경우만 AI 재추출·비교 (AI-vs-AI, P2)
                pending_diff = None
                status_val = "none"
                new_last_extracted = None
                revalidated_no_change = False
                if changed_url and changed_text:
                    POLICY_STATUS["phase"] = "ai"
                    new_fields = await _extract(changed_url, r["title"], r["db_key"], changed_text,
                        policy_id=r['id'], source_urls=[u for u, _, _ in analysis_sources])
                    if new_fields:
                        # The stale extraction may already equal the proposal.
                        # Recovery must compare fresh evidence with the user's
                        # current Notion content, not with that unsafe baseline.
                        baseline_fields = (utils.parse_json_safe(r["notion_fields"], {}) if revalidate_pending
                                           else utils.parse_json_safe(r["last_extracted_fields"], None))
                        # (2026-08-05, GPT Codex 2차 리뷰로 발견) 1차 추출(new_fields)만 실패를
                        # 처리하고 있었는데, last_extracted_fields가 없어(DB 실측 10건 확인) 이
                        # 2차(기준선) 추출을 시도하는 경우에도 429 등으로 실패할 수 있다. 그러면
                        # baseline_fields가 None이라 diff는 안 만들어지지만, 원래 코드는 그래도
                        # new_last_extracted를 무조건 확정해버려 이번 변경을 놓쳤다(같은 유실
                        # 패턴). prev_text_for_url이 있었는데(=비교할 이전 원문이 실제로 존재)
                        # 그 추출만 실패한 경우만 되돌리고, prev_text_for_url 자체가 없던
                        # "진짜 최초"(비교 대상이 원래 없음)는 실패가 아니므로 그대로 확정한다.
                        baseline_extraction_failed = False
                        if not baseline_fields and not revalidate_pending:
                            if prev_text_for_url:
                                previous_review = ai_classifier.policy_source_preflight_review(
                                    _policy_evidence_text(prev_text_for_url, r['title'], len(analysis_sources)))
                                if previous_review:
                                    # An unreadable historical source must not
                                    # force the same failed AI request forever.
                                    # Fresh, validated evidence is compared with
                                    # the actual Notion snapshot, never auto-applied.
                                    baseline_fields = utils.parse_json_safe(r['notion_fields'], {})
                                else:
                                    baseline_fields = await _extract(
                                        changed_url + "#prev", r["title"], r["db_key"], prev_text_for_url,
                                        policy_id=r['id'], source_urls=[u for u, _, _ in analysis_sources], source_role='baseline')
                                if not baseline_fields:
                                    baseline_extraction_failed = True
                        if baseline_fields or revalidate_pending:
                            # 저장된 레거시 기준선이나 방금 추출한 이전 원문에도 한 필드만
                            # 잘못 끼어들 수 있다. 사람이 관리한 Notion 값을 최후 fallback으로
                            # 삼아 기준선을 먼저 정제한 뒤, 새 추출은 그 기준선에 필드별로
                            # 대입한다. 빈값은 실제 삭제 신호일 수 있어 그대로 유지한다.
                            notion_fields = utils.parse_json_safe(r["notion_fields"], {})
                            _normalize_extracted_fields(baseline_fields, r["db_key"])
                            baseline_fields, baseline_rejected = _filter_low_quality_extracted_fields(
                                baseline_fields, notion_fields)
                            new_fields, new_rejected = _filter_low_quality_extracted_fields(
                                new_fields, baseline_fields)
                            rejected = {**baseline_rejected, **new_rejected}
                            if rejected:
                                logger.warning(
                                    "[정책 최신화] 저품질 추출 필드 제외(%s): %s",
                                    r["title"][:40], ", ".join(sorted(rejected)))
                            diffs = await _diff_fields_with_embeddings(baseline_fields, new_fields, ai_client)
                            # Preserve still-unapplied proposals across rolling
                            # AI baselines; refresh them from this same complete
                            # extraction instead of silently replacing the card.
                            prior_labels = {field.get('field') for field in old_pending_fields
                                            if isinstance(field, dict) and not field.get('applied')}
                            emitted = {field.get('field') for field in diffs}
                            if prior_labels and not revalidate_pending:
                                diffs += [field for field in _diff_fields(notion_fields, new_fields, None, {})
                                          if field['field'] in prior_labels and field['field'] not in emitted]
                            # Initial/revalidation comparisons use actual Notion
                            # values, including a style the user already accepted.
                            # Suppress only the exact saved current/extraction pair;
                            # changed facts, manual edits and uncertain receipts
                            # still need a new proposal. Keep all history below.
                            accepted_pending = {'fields': [field for field in old_pending_fields
                                if isinstance(field, dict) and not field.get('needs_verification')]}
                            diffs, _ = _preserve_accepted_fields(
                                diffs, accepted_pending, notion_fields, new_fields)
                            intensity = _intensity_of(diffs)
                            proposal_fields = _snapshot_diffs(diffs, notion_fields)
                            if intensity in ("high", "info") and proposal_fields:
                                # (C) 원문 diff 뷰용 — AI 판단을 못 믿을 때 확인할 ground truth
                                source_diff = {"url": changed_url,
                                              "old": prev_text_for_url[:3000],
                                              "new": changed_text[:3000]}
                                pending_diff = {"intensity": intensity, "fields": proposal_fields,
                                                "checked_at": now, "source_diff": source_diff,
                                                "logic_version": _DIFF_LOGIC_VERSION,
                                                "proposal_schema_version": _proposal_schema_version(),
                                                "extraction_meta": _extraction_meta(r["db_key"])}
                                pending_diff['source_context'] = _proposal_source_context(
                                    r['title'], {u: text for u, _old, text in analysis_sources})
                                status_val = "detected"
                                changed += 1
                                POLICY_STATUS["changed"] = changed
                                _mirror_ai_status()
                            elif revalidate_pending or old_pending_fields:
                                revalidated_no_change = True
                        if baseline_extraction_failed:
                            # 기준선 추출 실패 — 이번 변경을 다음 점검에서 다시 잡아내도록
                            # 해시·본문을 되돌리고, 이번엔 어떤 기준선도 확정하지 않는다.
                            _revert_hashes_for_unanalyzed_change(
                                new_hashes, new_texts, old_hashes, old_texts, real_changes)
                        else:
                            # 다음 비교를 위해 이번 추출을 새 기준선으로 저장 (굴려가기)
                            # 비교할 이전 원문이 아예 없는 진짜 최초 경로도 Notion 값을
                            # fallback으로 사용해 의심 필드가 기준선에 굳어지지 않게 한다.
                            if not baseline_fields:
                                notion_fields = utils.parse_json_safe(r["notion_fields"], {})
                                new_fields, rejected = _filter_low_quality_extracted_fields(
                                    new_fields, notion_fields)
                                if rejected:
                                    logger.warning(
                                        "[정책 최신화] 저품질 추출 필드 제외(%s): %s",
                                        r["title"][:40], ", ".join(sorted(rejected)))
                            new_last_extracted = json.dumps(new_fields, ensure_ascii=False)
                    else:
                        if revalidate_pending and not ai_client:
                            _record_extraction_review(POLICY_STATUS, r["title"], {
                                "code": "ai_connection_required", "fields": [],
                                "note": "이전 변경 제안을 재검증하려면 AI 연결이 필요합니다. 연결 후 다시 점검해주세요. 기존 내용과 제안은 유지됩니다."})
                            _mirror_ai_status()
                        # AI 추출 실패(429 등) — 이 변경을 다음 점검에서 다시 잡아내도록
                        # 해시·본문을 되돌린다(위 헬퍼 docstring 참조, 놓침 방지).
                        _revert_hashes_for_unanalyzed_change(
                            new_hashes, new_texts, old_hashes, old_texts, real_changes)

                # DB 갱신 (해시·본문은 항상 최신으로 — ③, 단 AI 실패로 되돌려진 URL은 예외)
                sets = "source_hashes=?, source_texts=?, dead_links=?, last_checked_at=?"
                params = [json.dumps(new_hashes, ensure_ascii=False),
                          json.dumps(new_texts, ensure_ascii=False),
                          json.dumps(dead_links, ensure_ascii=False), now]
                if new_last_extracted is not None:
                    sets += ", last_extracted_fields=?"
                    params.append(new_last_extracted)
                    if initial_comparison:
                        # Only a completed, fresh comparison establishes this
                        # marker. Missing AI, quota, source holds never do.
                        sets += ", audited_at=?"
                        params.append(now)
                if status_val == "detected":
                    pending_diff['fields'] += [field for field in old_pending_fields
                        if isinstance(field, dict) and field.get('applied')]
                    sets += ", change_status=?, pending_diff=?, last_changed_at=?"
                    params += ["detected", json.dumps(pending_diff, ensure_ascii=False), now]
                elif revalidated_no_change:
                    # Only a successful fresh comparison may retire the old
                    # suggestion; never alter the user's Notion content here.
                    sets += ", change_status=?, pending_diff=?"
                    accepted = [field for field in old_pending_fields
                                if isinstance(field, dict) and field.get('applied')]
                    params += (["detected", json.dumps({**old_pending, 'fields': accepted, 'completed': True}, ensure_ascii=False)]
                               if accepted else ["none", None])
                params.append(r["id"])
                await _db_write_strict(
                    f"UPDATE notion_policies SET {sets} WHERE id=?", tuple(params))
                if new_last_extracted is not None and initial_comparison:
                    POLICY_STATUS['initial_compared'] += 1
                    if ONE_CLICK_STATUS.get('running'):
                        ONE_CLICK_STATUS['initial_compared'] = POLICY_STATUS['initial_compared']
                # Neither a clear preflight alone nor a model/baseline failure
                # completes review. Resolve only after fresh analysis/comparison
                # and the existing policy save succeeded; unrelated URLs stay held.
                if new_last_extracted is not None and analysis_sources:
                    await _persist_policy_review(POLICY_STATUS, 'resolve_after_success',
                        r['id'], [u for u, _, _ in analysis_sources],
                        _policy_evidence_text(changed_text, r['title'], len(analysis_sources)), urls)
                elif changed_source_review and POLICY_STATUS['review_count'] == policy_review_start:
                    _record_extraction_review(POLICY_STATUS, r['title'], changed_source_review)
                    _mirror_ai_status()
                if POLICY_STATUS.get('pause_reason'):
                    break  # Earlier results are durable; unfinished rows remain retryable.
    finally:
        # Clear busy state even when browser teardown fails.
        POLICY_STATUS.update(running=False, changed=changed,
                              message=POLICY_STATUS.get('review_storage_error') or (
                                      ("점검 잠시 멈춤" if POLICY_STATUS.get('pause_reason') else "점검 완료")
                                      + f" · AI {POLICY_STATUS['ai_completed']}/"
                                      f"{POLICY_STATUS['ai_required']}건 · 실패 "
                                      f"{POLICY_STATUS['ai_failed']}건 · 실질변경 {changed}건"
                                      + _extraction_review_summary(POLICY_STATUS)))
        if own:
            await scraper.close()

    logger.info(f"🔍 [정책 최신화] 점검 {len(rows)}건 · 실질변경 {changed}건")
    return {"ok": True, "checked": POLICY_STATUS['checked'], "changed": changed,
            "ai_required": POLICY_STATUS["ai_required"],
            "ai_completed": POLICY_STATUS["ai_completed"],
            "ai_rate_waited": POLICY_STATUS["ai_rate_waited"],
            "ai_failed": POLICY_STATUS["ai_failed"],
            "ai_preflight_held": POLICY_STATUS["ai_preflight_held"],
            "review_count": POLICY_STATUS['review_count'], "review_notes": POLICY_STATUS['review_notes'],
            "initial_compared": POLICY_STATUS.get('initial_compared', 0),
            "pause_reason": POLICY_STATUS.get('pause_reason', '')}


async def run_initial_audit(db_keys: List[str]) -> Dict[str, Any]:
    """최초 감사: Notion에 이미 적힌 내용과 "지금" 출처 원문을 1회 비교.

    Stage 1(check_policies)은 원문이 '이전에 저장한 원문'과 달라야만 Stage2를 돌리므로,
    최초 점검은 그저 기준선만 세우고 끝난다 — Notion 작성 당시와 지금 원문이 이미 어긋나 있어도
    잡아내지 못한다. 이 함수는 그 최초 1회 어긋남을 잡기 위해, 이미 저장된 source_texts를
    재활용해(재요청 없음) audited_at이 비어있는 정책만 강제로 Stage2 비교한다. 멱등적 —
    한 번 감사된 정책은 다시 대상이 되지 않는다(이후는 check_policies의 변경분만 비교로 충분).
    """
    from services import policy_review_store

    active_keys = _active_configured_keys(db_keys)
    rows = await _db_query_strict(
        "SELECT id, page_id, title, notion_fields, source_urls, source_texts, db_key FROM notion_policies "
        "WHERE audited_at IS NULL AND db_key IN (%s)" % ",".join("?" * len(active_keys)),
        tuple(active_keys)) if active_keys else []
    rows = [dict(r) for r in rows]
    AUDIT_STATUS.update(running=True, checked=0, total=len(rows), changed=0, ai_failed=0,
                        message="최초 감사 중…", review_count=0, review_notes=[], review_storage_error=None,
                        pause_reason='')
    if not rows:
        AUDIT_STATUS.update(running=False, message="감사 대상 없음")
        return {"ok": True, "checked": 0, "changed": 0}

    ai_client = ai_classifier.get_client()
    changed = 0
    now = datetime.datetime.now().isoformat()

    # (url, title) 조합 캐시 — 여러 정책이 같은 URL(통합 안내 페이지)을 공유할 때 서로 다른
    # 정책에 같은 추출값이 잘못 재사용되는 것을 방지 (실측 2026-07-03 확인된 버그)
    run_cache: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    try:
        unresolved = await database.run_db(policy_review_store.unresolved_by_policy, active_keys)
        for i, r in enumerate(rows):
            pause_reason = _policy_ai_pause_reason(client_available=ai_client is not None)
            if pause_reason:
                AUDIT_STATUS['pause_reason'] = pause_reason
                break
            AUDIT_STATUS["checked"] = i + 1
            acquisition_holds = [item for item in unresolved.get(r['id'], [])
                                 if item.get('source_role') in {'fetch', 'freshness'}
                                 or item.get('code') in {'source_missing', 'source_read_incomplete',
                                                        'source_changed_unverified'}]
            if acquisition_holds:
                # A previous cached body is NOT a successful fresh read. Never
                # let initial audit clear an unavailable/partial-source hold by
                # analysing that older cache. Keep audited_at unset for retry.
                for hold in acquisition_holds:
                    _record_extraction_review(AUDIT_STATUS, r['title'], {
                        'code': hold.get('code', 'source_read_incomplete'),
                        'fields': [], 'note': '현재 출처를 완전히 확인하지 못했습니다. 정책 정보 점검으로 출처를 다시 확인한 뒤 진행해주세요.'})
                continue
            texts = utils.parse_json_safe(r["source_texts"], {})
            configured_urls = list(dict.fromkeys(utils.parse_json_safe(r.get('source_urls'), list(texts))))
            source_urls = [url for url in configured_urls if not is_low_value_source(url)]
            missing_urls = [url for url in source_urls if not isinstance(texts.get(url), str) or not texts[url].strip()]
            if not source_urls or missing_urls:
                review = {'code': 'source_missing', 'fields': [],
                          'note': '정책 출처의 저장된 원문이 부족합니다. 정책 정보 점검으로 모든 출처를 확인한 뒤 진행해주세요.'}
                _record_extraction_review(AUDIT_STATUS, r['title'], review)
                for url in missing_urls:
                    await _persist_policy_review(AUDIT_STATUS, 'record_hold',
                        r['id'], r['title'], [url], '', review, 'fetch')
                continue
            # All configured relevant sources form one policy's evidence. A
            # longer page must never displace a shorter exception/eligibility
            # source. One combined extraction retains the existing AI budget.
            best_url = source_urls[0]
            best_text = '\n\n---\n\n'.join(texts[url] for url in source_urls)

            notion_fields = utils.parse_json_safe(r["notion_fields"], {})
            cache_key = (r["db_key"], best_url, r["title"])
            # 일반 수집 ai_data는 필터·요약 목적이 다르고 원문/추출 버전의
            # 동일성을 증명할 수 없다. 정책 중립 추출의 회차 내 캐시만 공유한다.
            cache_key = (*cache_key, hashlib.sha256(best_text.encode('utf-8')).hexdigest())
            new_fields = run_cache.get(cache_key)
            if new_fields and _is_failed_extraction(new_fields):
                new_fields = None
            if not new_fields and ai_client:
                # 중립 추출(B) — 수집 필터·CUSTOM_AI_PROMPT 편향 없이 필드값만 옮김
                # (2026-07-07) 다중 프로그램 나열 페이지 대비 제목 근접 발췌로 우선 좁힘
                snippet = _policy_evidence_text(best_text, r["title"], len(source_urls))
                review = {}
                new_fields = await ai_classifier.extract_policy_fields_async(
                    ai_client, r["title"], best_url, r["db_key"], snippet,
                    taxonomy=_get_db_schema(r["db_key"]), review=review)
                _record_extraction_review(AUDIT_STATUS, r['title'], review)
                if new_fields is None and policy_review_store.supports_review(review):
                    await _persist_policy_review(AUDIT_STATUS, 'record_hold',
                        r['id'], r['title'], source_urls, snippet, review)
                if new_fields is None:
                    # 실패(과부하 등) — 이번 회차는 건너뛰고 audited_at을 남기지 않아 재시도되게 함
                    logger.warning(f"[정책 최신화] 추출 실패 — 감사 보류(재시도 예정): {r['title'][:30]}")
                    AUDIT_STATUS['ai_failed'] += 1
                    if review.get('retryable') is True and review.get('code') in {
                            'source_input_count_unavailable', 'source_input_rate_limited'}:
                        AUDIT_STATUS['pause_reason'] = review['code']
                        break
                    pause_reason = _policy_ai_pause_reason(client_available=ai_client is not None)
                    if pause_reason:
                        AUDIT_STATUS['pause_reason'] = pause_reason
                        break
                    continue
            if new_fields:
                _normalize_extracted_fields(new_fields, r["db_key"])
                new_fields, rejected = _filter_low_quality_extracted_fields(
                    new_fields, notion_fields)
                if rejected:
                    logger.warning(
                        "[정책 최신화] 최초 감사 저품질 추출 필드 제외(%s): %s",
                        r["title"][:40], ", ".join(sorted(rejected)))
                run_cache[cache_key] = new_fields

            if not new_fields:
                # No credential/client is not a completed first comparison.
                # Keep audited_at empty so the main action can retry later.
                _record_extraction_review(AUDIT_STATUS, r['title'], {
                    'code': 'ai_connection_required', 'note': 'AI 연결을 확인한 뒤 다시 점검해주세요.'})
                continue
            status_val, pending_diff, new_last_extracted = "none", None, None
            if new_fields:
                diffs = await _diff_fields_with_embeddings(notion_fields, new_fields, ai_client)
                intensity = _intensity_of(diffs)
                if intensity in ("high", "info"):
                    # (C) 최초 감사는 '이전 원문'이 없어 old=None — 프론트에서 원문 발췌만 표시
                    source_diff = {"url": best_url, "old": None, "new": best_text[:3000]}
                    pending_diff = {"intensity": intensity, "fields": _snapshot_diffs(diffs, notion_fields), "checked_at": now,
                                    "source_diff": source_diff, "logic_version": _DIFF_LOGIC_VERSION,
                                    "proposal_schema_version": _proposal_schema_version(),
                                    "extraction_meta": _extraction_meta(r["db_key"])}
                    pending_diff['source_context'] = _proposal_source_context(
                        r['title'], {url: texts[url] for url in source_urls})
                    status_val = "detected"
                    changed += 1
                # P2로 넘어갈 다리: 이번 추출을 last_extracted_fields로 저장해두면
                # 다음 정기 점검부터는 Notion 대신 이 AI 추출과 비교(AI-vs-AI)해 노이즈가 준다.
                new_last_extracted = json.dumps(new_fields, ensure_ascii=False)

            sets = "audited_at=?"
            params = [now]
            if new_last_extracted is not None:
                sets += ", last_extracted_fields=?"
                params.append(new_last_extracted)
            if status_val == "detected":
                sets += ", change_status=?, pending_diff=?, last_changed_at=?"
                params += ["detected", json.dumps(pending_diff, ensure_ascii=False), now]
            params.append(r["id"])
            await _db_write_strict(
                f"UPDATE notion_policies SET {sets} WHERE id=?", tuple(params))
            if new_last_extracted is not None:
                await _persist_policy_review(AUDIT_STATUS, 'resolve_after_success',
                    r['id'], source_urls, _policy_evidence_text(best_text, r['title'], len(source_urls)), configured_urls)
    finally:
        AUDIT_STATUS.update(running=False, changed=changed,
                            message=AUDIT_STATUS.get('review_storage_error') or (
                                ("최초 감사 잠시 멈춤" if AUDIT_STATUS['pause_reason'] else
                                 "최초 감사 일부 완료" if AUDIT_STATUS['ai_failed'] or AUDIT_STATUS['review_count'] else
                                 "최초 감사 완료") + f" · 변경 {changed}건"
                                + (f" · AI 분석 미완료 {AUDIT_STATUS['ai_failed']}건" if AUDIT_STATUS['ai_failed'] else '')
                                + _extraction_review_summary(AUDIT_STATUS)))

    logger.info(f"🔍 [정책 최신화] 최초 감사 {len(rows)}건 · 실질변경 {changed}건")
    return {"ok": True, "checked": AUDIT_STATUS['checked'], "changed": changed,
            'ai_failed': AUDIT_STATUS['ai_failed'], 'pause_reason': AUDIT_STATUS['pause_reason'],
            "review_count": AUDIT_STATUS['review_count'], "review_notes": AUDIT_STATUS['review_notes']}


RECOMPUTE_STATUS: Dict[str, Any] = {"running": False, "checked": 0, "total": 0, "message": ""}


def _automatic_recompute_ids(db_keys):
    """Only outdated comparison rules, never stale or incomplete evidence."""
    cards = get_changes(db_keys)
    eligible = {card['id'] for card in cards if card.get('apply_ready')
                and not any(item.get('needs_verification') for item in card.get('apply_history', []))}
    if not eligible:
        return set()
    rows = database.execute_query(
        'SELECT id,pending_diff FROM notion_policies WHERE id IN (%s)' % ','.join('?' for _ in eligible),
        tuple(eligible), raise_on_error=True)
    return {row['id'] for row in rows
            if utils.parse_json_safe(row['pending_diff'], {}).get('logic_version') != _DIFF_LOGIC_VERSION}


async def recompute_diffs(db_keys: List[str], *, only_needed=False) -> Dict[str, Any]:
    """(B1+C2) AI 재호출 없이, 저장된 last_extracted_fields로 대기 중인 진단을 현재(최신)
    비교 로직으로 다시 계산한다.

    배경: 비교 로직을 개선해도(A1 태그 정규화, T1 등가 규칙 등) 이미 감지되어 대기 중인
    진단은 원문이 다시 바뀌기 전까지 낡은 판정 그대로 남는다 — 실측(2026-07-07)으로 157건 중
    156건이 이 문제로 낡은 오탐(태그 축소 제안, 일부는 완전 공백 제안)을 갖고 있었다.
    이 함수는 그 낡은 진단들을 무료로 일괄 정리하는 수동 갱신 기능이다.
    """
    ai_client = ai_classifier.get_client()
    active_keys = _active_configured_keys(db_keys)
    if active_keys:
        placeholders = ",".join("?" * len(active_keys))
        rows = await _db_query_strict(
            "SELECT id, db_key, notion_fields, last_extracted_fields, pending_diff "
            "FROM notion_policies WHERE change_status = 'detected' "
            f"AND last_extracted_fields IS NOT NULL AND db_key IN ({placeholders})",
            tuple(active_keys))
    else:
        rows = []
    rows = [dict(r) for r in rows]
    if only_needed:
        eligible = await database.run_db(_automatic_recompute_ids, db_keys)
        rows = [row for row in rows if row['id'] in eligible]
    RECOMPUTE_STATUS.update(running=True, checked=0, total=len(rows), message="진단 재계산 중…")
    updated, resolved = 0, 0
    now = datetime.datetime.now().isoformat()
    try:
        for i, r in enumerate(rows):
            RECOMPUTE_STATUS["checked"] = i + 1
            if ONE_CLICK_STATUS.get('running'):
                ONE_CLICK_STATUS.update(checked=i + 1, total=len(rows), message=f'저장된 제안 재점검 중… ({i + 1}/{len(rows)})')
            saved_pending = utils.parse_json_safe(r['pending_diff'], {})
            if saved_pending.get('completed'):
                continue  # Completed cards are removed only by explicit cleanup.
            notion_fields = utils.parse_json_safe(r["notion_fields"], {})
            new_fields = utils.parse_json_safe(r["last_extracted_fields"], {})
            if not new_fields:
                continue
            _normalize_extracted_fields(new_fields, r["db_key"])
            new_fields, rejected = _filter_low_quality_extracted_fields(
                new_fields, notion_fields)
            if rejected:
                # 과거 버전에서 저장된 의심값도 이번 무료 재계산 때 정리해, 다음
                # 재계산에서 같은 오탐이 되살아나지 않게 기준선 자체를 교정한다.
                await _db_write_strict(
                    "UPDATE notion_policies SET last_extracted_fields=? WHERE id=?",
                    (json.dumps(new_fields, ensure_ascii=False), r["id"]))
                logger.warning(
                    "[정책 최신화] 저장된 저품질 추출 필드 정리(id=%s): %s",
                    r["id"], ", ".join(sorted(rejected)))
            # 이 기능은 안내대로 외부 AI를 호출하지 않는다. 저장된 값만 최신 비교
            # 규칙으로 다시 판정하며, 의미 유사도는 보수적으로 미확인(None) 처리한다.
            old_pd = utils.parse_json_safe(r["pending_diff"], {})
            diffs = _diff_fields(notion_fields, new_fields, None, {})
            diffs, accepted = _preserve_accepted_fields(diffs, old_pd, notion_fields, new_fields)
            intensity = _intensity_of(diffs)
            if intensity in ("high", "info"):
                pd = {"intensity": intensity, "fields": _snapshot_diffs(diffs, notion_fields) + accepted, "checked_at": now,
                      "logic_version": _DIFF_LOGIC_VERSION,
                      "proposal_schema_version": old_pd.get('proposal_schema_version'),
                      "source_diff": old_pd.get("source_diff"),
                      "source_context": old_pd.get("source_context"),
                      # Recomparison is not re-extraction. Never upgrade an
                      # old/unknown extraction to today's validation version.
                      "extraction_meta": old_pd.get("extraction_meta") or {}}
                await _db_write_strict(
                    "UPDATE notion_policies SET pending_diff=? WHERE id=?",
                    (json.dumps(pd, ensure_ascii=False), r["id"]))
                updated += 1
            elif accepted:
                retained = {**old_pd, 'fields': accepted, 'completed': True}
                await _db_write_strict(
                    "UPDATE notion_policies SET pending_diff=?, change_status='detected' WHERE id=?",
                    (json.dumps(retained, ensure_ascii=False), r['id']))
                resolved += 1
            else:
                await _db_write_strict(
                    "UPDATE notion_policies SET pending_diff=NULL, change_status='none' WHERE id=?",
                    (r["id"],))
                resolved += 1
    finally:
        RECOMPUTE_STATUS.update(running=False,
                                message=f"재계산 완료 · {updated}건 갱신, {resolved}건 해소")
    logger.info(f"[정책 최신화] 진단 재계산 {len(rows)}건 · 갱신 {updated}건 · 해소 {resolved}건")
    return {"ok": True, "checked": len(rows), "updated": updated, "resolved": resolved}


# ==================== (T2) 작성 스타일 가이드 자동 생성 ====================
def _collect_style_stats(db_keys: List[str]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """동기 DB 조회 — 필드별 값 분포 통계 + 대표 샘플 추출 (run_db로 오프로드해서 호출)."""
    from collections import Counter
    db_keys = _active_configured_keys(db_keys)
    where = "db_key IN (%s)" % ",".join("?" * len(db_keys)) if db_keys else "0=1"
    params: list = list(db_keys)
    rows = database.execute_query(
        f"SELECT db_key, notion_fields FROM notion_policies WHERE {where}", tuple(params),
        raise_on_error=True)
    all_fields = [utils.parse_json_safe(r["notion_fields"], {}) for r in rows]
    all_fields = [f for f in all_fields if f]

    life_cycle_combos = Counter()
    income_phrases = Counter()
    bullet_style = Counter()   # eligibility_detail 선두 기호
    heading_style = Counter()  # support_detail에 헤딩(###) 사용 여부
    for f in all_fields:
        lc = f.get("life_cycle")
        if isinstance(lc, list) and lc:
            life_cycle_combos[tuple(sorted(lc))] += 1
        income = str(f.get("target_income") or "").strip()
        if income:
            income_phrases[income] += 1
        elig = str(f.get("eligibility_detail") or "").strip()
        if elig:
            first = elig.lstrip()[:2]
            bullet_style[first] += 1
        sd = str(f.get("support_detail") or "")
        heading_style["###" if "###" in sd else "기타"] += 1

    stats = {
        "총건수": len(all_fields),
        "생애주기_조합_상위5": life_cycle_combos.most_common(5),
        "대상소득_문구_상위5": income_phrases.most_common(5),
        "자격요건_선두기호_상위5": bullet_style.most_common(5),
        "상세지원내용_헤딩스타일": dict(heading_style),
    }
    samples = all_fields[:10]
    return stats, samples


async def _verified_style_input(client, model, prompt):
    """Verify the exact complete style request under the shared unchanged quota."""
    from services import ai_coordinator
    from services.ai_verified_input import verify_input
    from services.ai_input_safety import ensure_ai_text_safe
    from services.policy_source_plan import MAX_COUNTED_POLICY_PROMPT_CHARS
    ensure_ai_text_safe(prompt)
    if len(prompt) > MAX_COUNTED_POLICY_PROMPT_CHARS:
        raise ai_coordinator.AiInputTokenBudgetExceeded('문장 정리 입력이 안전한 전체 문맥 크기를 넘었습니다. 가이드와 정책 내용을 확인해주세요. 원래 제안은 유지됩니다.')
    capacity = ai_coordinator.input_token_capacity(model)
    if capacity <= 0:
        raise ai_coordinator.AiQuotaStateUnavailable('AI 입력 한도를 확인하지 못했습니다. 잠시 후 다시 시도해주세요.')
    verified = await verify_input(client, prompt, None, model=model, input_token_budget=capacity)
    if verified.input_tokens > capacity:
        if not verified.is_provider_counted:
            raise ai_coordinator.AiQuotaStateUnavailable('문장 정리 입력 크기를 확인하지 못했습니다. 원래 제안을 보존하며 잠시 후 재시도할 수 있습니다.')
        raise ai_coordinator.AiInputTokenBudgetExceeded('문장 정리 입력이 AI 전송 보호값을 넘었습니다. 가이드와 정책 내용을 확인해주세요. 원래 제안은 유지됩니다.')
    return verified


async def generate_style_guide(db_keys: List[str]) -> Dict[str, Any]:
    """(T2) 기존 157건의 Notion 작성 내용을 분석해 '이 사용자의 작성 관례' 가이드를 1회 생성.

    통계(코드) + 대표 샘플을 AI에 주고 요약을 맡긴다. 결과는 config(POLICY_STYLE_GUIDE)에
    저장해 이후 비교·제안(T3)에서 재사용하며, Settings에서 사용자가 직접 교정할 수 있다.
    """
    from services.ai_generation_safety import (
        AiResponseReviewRequired, STYLE_GUIDE_OUTPUT_TOKENS, complete_response_text,
    )
    ai_client = ai_classifier.get_client()
    if not ai_client:
        return {"ok": False, "error": "AI 클라이언트 미설정"}
    stats, samples = await database.run_db(_collect_style_stats, db_keys)
    if stats["총건수"] == 0:
        return {"ok": False, "error": "분석할 정책이 없습니다 (먼저 동기화하세요)"}
    from services.ai_input_safety import ensure_ai_text_safe, AiInputReviewRequired
    try:
        ensure_ai_text_safe(json.dumps(stats, ensure_ascii=False), json.dumps(samples, ensure_ascii=False))
    except AiInputReviewRequired as error:
        # Do not log Notion samples or send part of a private document.
        return {"ok": False, "error": str(error), "code": error.code}

    prompt = f"""
    다음은 한 사용자가 Notion 복지정책 DB에 실제로 입력해 온 데이터의 통계와 샘플입니다.
    이 사용자의 "작성 관례"를 파악해, 앞으로 새 정책을 같은 스타일로 정리할 때 따라야 할
    간결한 한국어 가이드를 작성하세요.

    [통계]
    {json.dumps(stats, ensure_ascii=False, indent=2)}

    [샘플 {len(samples)}건]
    {json.dumps(samples, ensure_ascii=False, indent=2)}

    다음 항목을 짧은 불릿으로 정리하세요 (근거 없는 항목은 생략):
    - 생애주기 표기 관례 (예: 전 연령이면 어떻게 체크하는지)
    - 자격요건 문장의 서식(불릿 기호·이모지 사용 여부)
    - 대상소득 표준 문구
    - 지원금액/기간을 어느 필드에 적는지
    - 상세지원내용 헤딩 스타일
    - 기타 눈에 띄는 일관된 패턴

    JSON이 아닌 일반 텍스트(마크다운 불릿 목록)로만 답하세요.
    """
    try:
        model_name = os.getenv('CLASSIFY_MODEL', 'gemini-3.5-flash-lite')
        from services import ai_coordinator
        verified = await _verified_style_input(ai_client, model_name, prompt)
        await ai_coordinator.acquire_model_slot(model_name,
            input_tokens=verified.input_tokens)
        response = await ai_coordinator.await_api(ai_client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0,
                max_output_tokens=STYLE_GUIDE_OUTPUT_TOKENS, candidate_count=1),
        ))
        guide = complete_response_text(response)
        utils.set_env_value("POLICY_STYLE_GUIDE", guide)
        return {"ok": True, "guide": guide, "based_on": stats["총건수"]}
    except AiResponseReviewRequired as error:
        return {"ok": False, "error": str(error), "code": error.code}
    except Exception as e:
        ai_coordinator.register_api_error(model_name, e)
        logger.error(f"[정책 최신화] 스타일 가이드 생성 실패: {e}")
        return {"ok": False, "error": str(e)}


# ==================== (T3) 제안값 스타일 변환 ====================
def _needs_policy_restyle(field: Dict[str, Any]) -> bool:
    tags = {_PROP_LABEL[k] for k in
            ("residence", "life_cycle", "target_characteristics", "category")}
    if field.get("applied") or field.get("field") in tags:
        return False
    value = field.get("new")
    if not isinstance(value, str) or value.strip().casefold() in {"", "none", "null", "undefined", "nan", "-", "(없음)"}:
        return False
    styled = field.get("styled")
    valid_styled = isinstance(styled, str) and styled.strip().casefold() not in {"", "none", "null", "undefined", "nan"}
    return (field.get("level") in ("high", "info") or field.get("field") == _PROP_LABEL["support_detail"]) and (
        not valid_styled or field.get("note") == "sparse")


def _policy_style_fingerprint(field: Dict[str, Any], guide: str) -> str:
    """Hash the original proposal, not generated formatting or check timestamps."""
    value = {key: field.get(key) for key in ("field", "old", "new")}
    value["guide"] = guide
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _needs_automatic_policy_restyle(field: Dict[str, Any], guide: str) -> bool:
    if not isinstance(field, dict) or not _needs_policy_restyle(field):
        return False
    styled = field.get("styled")
    if isinstance(styled, str) and styled.strip().casefold() not in {
            "", "none", "null", "undefined", "nan", "-", "(없음)"}:
        return False
    attempt = field.get("_auto_style")
    return not (isinstance(attempt, dict)
                and attempt.get("fingerprint") == _policy_style_fingerprint(field, guide)
                and attempt.get("status") in {"styled", "rejected"})


def _automatic_policy_style_targets(db_keys: List[str], guide: str) -> Dict[int, set]:
    # get_changes shares the apply-ready gate: source holds, old snapshots,
    # old extraction versions and uncertain/matching receipts must all wait.
    targets = {}
    for card in get_changes(db_keys):
        if (not card.get("apply_ready")
                or any(record.get("needs_verification") for record in card.get("apply_history", []))):
            continue
        fingerprints = {_policy_style_fingerprint(field, guide)
                        for field in card.get("fields", [])
                        if _needs_automatic_policy_restyle(field, guide)}
        if fingerprints:
            targets[card["id"]] = fingerprints
    return targets


def preview_style_work(db_keys: List[str]) -> Dict[str, Any]:
    """Read-only estimate of new automatic styling work (one call per policy)."""
    guide = utils.get_env_value("POLICY_STYLE_GUIDE")
    if not isinstance(guide, str) or not guide.strip():
        return {"configured": False, "eligible_count": 0, "eligible_fields": 0}
    targets = _automatic_policy_style_targets(db_keys, guide)
    return {"configured": True, "eligible_count": len(targets),
            "eligible_fields": sum(len(fields) for fields in targets.values())}


def _mirror_policy_style_status() -> None:
    if ONE_CLICK_STATUS.get("running") and ONE_CLICK_STATUS.get("phase") == "style":
        ONE_CLICK_STATUS.update(
            checked=STYLE_STATUS["checked"], total=STYLE_STATUS["total"],
            styled=STYLE_STATUS.get("styled", 0),
            style_failed=STYLE_STATUS.get("failed", 0), message=STYLE_STATUS["message"])


async def restyle_pending_diffs(db_keys: List[str], *, only_needed: bool = False) -> Dict[str, Any]:
    """감지된 변경(🔴·📝)의 '새 값'을 사용자의 기존 작성 스타일로 다듬어 제안값으로 저장.

    사실은 원문 추출값 그대로 유지하고 표현·서식만 다듬는다(건당 AI 1회 — 필드별X). 감지 로직
    자체는 중립 추출을 그대로 쓰고(앵커 편향 방지), 여기서 확정된 변경에 한해서만 후처리한다.
    """
    guide = utils.get_env_value("POLICY_STYLE_GUIDE")
    if not guide or (only_needed and (not isinstance(guide, str) or not guide.strip())):
        if only_needed:
            return {"ok": True, "styled": 0, "rejected_fields": 0, "failed": 0,
                    "paused": False, "pause_reason": "", "skipped": 0}
        return {"ok": False, "error": "스타일 가이드가 없습니다 (Settings → 정책 작성 스타일 가이드에서 먼저 생성하세요)"}
    automatic_targets = (await database.run_db(_automatic_policy_style_targets, db_keys, guide)
                         if only_needed else None)
    if only_needed and not automatic_targets:
        return {"ok": True, "styled": 0, "rejected_fields": 0, "failed": 0,
                "paused": False, "pause_reason": "", "skipped": 0}
    ai_client = ai_classifier.get_client()
    if not ai_client:
        if only_needed:
            return {"ok": True, "styled": 0, "rejected_fields": 0, "failed": 0,
                    "paused": True, "pause_reason": "ai_connection_required", "skipped": 0}
        return {"ok": False, "error": "AI 클라이언트 미설정"}

    active_keys = _active_configured_keys(db_keys)
    if active_keys:
        placeholders = ",".join("?" * len(active_keys))
        rows = await _db_query_strict(
            "SELECT id, title, pending_diff FROM notion_policies "
            "WHERE change_status = 'detected' "
            f"AND db_key IN ({placeholders})", tuple(active_keys))
    else:
        rows = []
    rows = [dict(r) for r in rows]

    # (V1) 상세지원내용은 등급과 무관하게 항상 대상 — 사용자 지적대로 여기서 무분별한
    # 스크랩(전체 원문 통짜 이식)이 가장 흔히 발생하므로 섹션 미러링을 항상 적용한다.
    # (2026-07-13) 태그 필드(거주지·생애주기·대상특성·분류)는 스타일 변환에서 제외 —
    # 고정 태그 목록이지 산문이 아니라서, AI 재작성이 "전국, 서울시, 도봉구"를 "전국"으로
    # 뭉뚱그리는 등 정규화를 되돌리는 사고가 실측(2026-07-13)됨.
    _TAG_FIELD_LABELS = {_PROP_LABEL[k] for k in
                         ("residence", "life_cycle", "target_characteristics", "category")}
    _SUPPORT_LABEL = _PROP_LABEL["support_detail"]

    def _needs_styling(field, policy_id):
        if not isinstance(field, dict):
            return False
        if not only_needed:
            return _needs_policy_restyle(field)
        return (_policy_style_fingerprint(field, guide) in automatic_targets.get(policy_id, set())
                and _needs_automatic_policy_restyle(field, guide))

    targets = []
    for r in rows:
        pd = utils.parse_json_safe(r["pending_diff"], {})
        fields = pd.get("fields", [])
        if any(_needs_styling(f, r["id"]) for f in fields):
            targets.append((r, pd))

    STYLE_STATUS.update(running=True, checked=0, total=len(targets), styled=0, rejected_fields=0,
                        failed=0, paused=False, pause_reason="", message="스타일 제안 생성 중…")
    _mirror_policy_style_status()
    styled_count = 0
    rejected_fields = 0
    failed_count = 0
    pause_reason = ""
    try:
        for i, (r, pd) in enumerate(targets):
            if only_needed:
                from services.policy_full_check import ai_pause_reason
                pause_reason = ai_pause_reason(client_available=bool(ai_client))
                if pause_reason:
                    break
            STYLE_STATUS["checked"] = i + 1
            _mirror_policy_style_status()
            actionable = [f for f in pd["fields"] if _needs_styling(f, r["id"])]
            if not actionable:
                continue

            # Retain every selected old/new field. The complete prompt is
            # privacy-checked and counted; an over-limit request is held whole.
            line_parts = []
            extra_instructions = ""
            for f in actionable:
                old_v, new_v = f.get("old") or "", f.get("new") or ""
                if f["field"] == _SUPPORT_LABEL:
                    extra_instructions = _support_detail_section_instructions(old_v)
                    line_parts.append(f'- {f["field"]}: 기존="{old_v}" 새값="{new_v}"')
                else:
                    line_parts.append(f'- {f["field"]}: 기존="{old_v}" 새값="{new_v}"')
            lines = "\n".join(line_parts)

            prompt = f"""
            아래 [작성 스타일 가이드]를 참고해, 각 필드의 "새값"에 담긴 사실 정보는 그대로 유지하면서
            사용자의 기존 문체(불릿 기호·어투·표기 관례)에 맞게 다시 쓰세요.
            사실을 추가하거나 삭제하지 마세요 — 표현과 서식만 다듬으세요.
            {extra_instructions}
            [작성 스타일 가이드]
            {guide}

            [정책명] {r['title']}
            [변경 필드]
            {lines}

            JSON으로만 응답하세요: {{"필드명": "스타일 적용된 새 값", ...}}
            """
            try:
                from services import ai_coordinator
                model_name = os.getenv('CLASSIFY_MODEL', 'gemini-3.5-flash-lite')
                verified = await _verified_style_input(ai_client, model_name, prompt)
                result = await ai_classifier._call_gemini_json(
                    ai_client, prompt, r["title"], model_override=model_name,
                    verified_input=verified, **({"request_attempts": 1} if only_needed else {}))
            except Exception as e:
                logger.warning(f"[정책 최신화] 스타일 변환 실패 {r['title'][:30]}: {e}")
                if only_needed:
                    pause_reason = ('' if isinstance(e, ai_coordinator.AiInputTokenBudgetExceeded)
                                    else 'source_input_rate_limited' if isinstance(e, ai_coordinator.AiTemporaryRateLimit)
                                    else 'source_input_count_unavailable' if isinstance(e, ai_coordinator.AiQuotaStateUnavailable)
                                    else ai_pause_reason(client_available=bool(ai_client)))
                    if pause_reason:
                        break
                    for field in actionable:
                        field["_auto_style"] = {
                            "fingerprint": _policy_style_fingerprint(field, guide), "status": "failed"}
                    await _db_write_strict(
                        "UPDATE notion_policies SET pending_diff=? WHERE id=?",
                        (json.dumps(pd, ensure_ascii=False), r["id"]))
                failed_count += 1
                STYLE_STATUS["failed"] = failed_count
                _mirror_policy_style_status()
                continue
            if not isinstance(result, dict):
                if not only_needed:
                    continue
                pause_reason = ai_pause_reason(client_available=bool(ai_client))
                if pause_reason:
                    break
                result = {}
            accepted = False
            if only_needed:
                # One bounded receipt per original field; rejected or missing
                # output is not sent again by the next main-button click.
                for field in actionable:
                    field["_auto_style"] = {
                        "fingerprint": _policy_style_fingerprint(field, guide), "status": "rejected"}
            for f in pd["fields"]:
                if f in actionable and f["field"] in result:
                    styled_val = result[f["field"]]
                    if not isinstance(styled_val, str) or styled_val.strip().casefold() in {"", "none", "null", "undefined", "nan", "-", "(없음)"}:
                        rejected_fields += 1
                        continue
                    new_note = None
                    # (2026-07-13) AI 출력 위생 강제 — 프롬프트 지시를 안 지켜도
                    # 마크다운 제거·기존 섹션 제한을 코드에서 결정론적으로 적용
                    if f["field"] == _SUPPORT_LABEL:
                        # (버그 2026-07-20) note='sparse'는 diff 감지 시점에 한 번만 계산돼
                        # pending_diff에 박히는데, 스타일 변환이 바로 그 문제(섹션 불일치)를
                        # 고치라고 안내하는 용도였다 — 변환 후에도 note를 갱신 안 하면 이미
                        # 정리된 카드에 "스타일 제안 생성하세요" 안내가 그대로 남는다. 변환된
                        # 값 기준으로 note를 다시 판정해 해소됐으면 지운다.
                        styled_val, new_note = _sanitize_support_detail(
                            styled_val, f.get("old") or "", return_note=True)
                    # Validate the final sanitized output, not just the AI reply.
                    # Restyling must not remove/change amounts already extracted.
                    from services.policy_evidence import validate_policy_evidence
                    source_value = f.get("new") or ""
                    if not validate_policy_evidence({
                        'support_detail': styled_val,
                        '_source_evidence': {'support_detail': source_value},
                    }, source_value):
                        rejected_fields += 1
                        continue
                    if f["field"] == _SUPPORT_LABEL:
                        if new_note:
                            f["note"] = new_note
                        else:
                            f.pop("note", None)
                    f["styled"] = styled_val
                    if only_needed:
                        f["_auto_style"]["status"] = "styled"
                    accepted = True
                elif only_needed and f in actionable:
                    rejected_fields += 1
            if not accepted and not only_needed:
                continue
            await _db_write_strict(
                "UPDATE notion_policies SET pending_diff=? WHERE id=?",
                (json.dumps(pd, ensure_ascii=False), r["id"]))
            if accepted:
                styled_count += 1
            STYLE_STATUS.update(styled=styled_count, rejected_fields=rejected_fields)
            _mirror_policy_style_status()
    finally:
        STYLE_STATUS.update(running=False, styled=styled_count, rejected_fields=rejected_fields,
                            failed=failed_count, paused=bool(pause_reason), pause_reason=pause_reason,
                            message=(f"문장 다듬기 {'일시 중지' if pause_reason else '완료'} · "
                                     f"정책 {styled_count}건 · 검증 보류 {rejected_fields}개 항목"))
        _mirror_policy_style_status()
    return {"ok": True, "styled": styled_count, "rejected_fields": rejected_fields,
            "failed": failed_count, "paused": bool(pause_reason), "pause_reason": pause_reason,
            "skipped": len(rows) - len(targets)}


def get_health_summary(db_keys: List[str]) -> Dict[str, Any]:
    """(S3) 변경 감지 여부와 무관하게 항상 노출할 상태 요약 — 깨진 출처 링크, 출처 URL 없음.

    실측(2026-07-02)에서 깨진 링크가 변경 미감지 정책에 있으면 화면 어디에도 안 보이는
    사각지대가 확인돼, 변경감지 카드에 종속시키지 않고 별도로 항상 노출한다.
    """
    active_keys = _active_configured_keys(db_keys)
    if not active_keys:
        return {"dead_links": [], "no_source": []}
    placeholders = ",".join("?" * len(active_keys))
    rows = database.execute_query(
        f"SELECT id, db_key, page_id, title, source_urls, dead_links "
        f"FROM notion_policies WHERE db_key IN ({placeholders})", tuple(active_keys))
    dead, no_source = [], []
    for r in rows:
        dl = utils.parse_json_safe(r["dead_links"], [])
        if dl:
            dead.append({"id": r["id"], "title": r["title"], "db_key": r["db_key"],
                        "urls": dl, "deeplink": notion_service.page_deeplink(r["page_id"])})
        urls = utils.parse_json_safe(r["source_urls"], [])
        if not urls:
            no_source.append({"id": r["id"], "title": r["title"], "db_key": r["db_key"],
                              "deeplink": notion_service.page_deeplink(r["page_id"])})
    return {"dead_links": dead, "no_source": no_source}


# ==================== 조회 (변경감지 / 신규후보) ====================
def get_source_reviews(db_keys, limit=100, offset=0, exclude_policy_ids=None):
    from services import policy_review_store
    return policy_review_store.list_unresolved(_active_configured_keys(db_keys), limit, offset, exclude_policy_ids)


def requires_individual_review(field_label: str) -> bool:
    # Grounding/quantity checks do not verify omitted or inverted conditions.
    # No legacy or AI-provided confidence flag is allowed to bypass this gate.
    return _label_to_field(field_label) != "category"


def get_changes(db_keys: List[str]) -> List[Dict[str, Any]]:
    """변경 감지된 정책 목록 (중요순 정렬)."""
    from services import policy_review_store, policy_undo
    active_keys = _active_configured_keys(db_keys)
    if not active_keys:
        return []
    placeholders = ",".join("?" * len(active_keys))
    rows = database.execute_query(
        f"SELECT id, db_key, page_id, title, pending_diff, dead_links, last_changed_at, source_urls, change_status "
        f"FROM notion_policies WHERE db_key IN ({placeholders})", tuple(active_keys), raise_on_error=True)
    reviews = policy_review_store.unresolved_by_policy(active_keys)
    receipts = {}
    for receipt in policy_undo.applied_records():
        receipts.setdefault(receipt['page_id'], []).append(receipt)
    # One request-local snapshot: preserve live setting changes without reloading
    # the whole configuration for every field/property lookup.
    label_keys = {label: key for key, label, _ in notion_service.get_policy_props()
                  if key in _APPLYABLE_FKEYS}
    out = []
    for r in rows:
        pd = utils.parse_json_safe(r["pending_diff"], {})
        history = [{**record, 'undo_id': record['id'], 'old': record['before_value'],
                    'new': record['after_value'], 'applied': True, 'level': 'low'}
                   for record in receipts.get(r['page_id'], [])]
        if r['change_status'] != 'detected' and not history:
            continue
        receipt_ids = {entry['undo_id'] for entry in history}
        fields = [field for field in pd.get('fields', [])
                  if isinstance(field, dict) and field.get('undo_id') not in receipt_ids]
        pending = [field for field in fields if not field.get('applied')]
        # Remote PATCH may have succeeded while the local card save failed.
        # The durable receipt, not this old card, is authoritative. Do not
        # offer the exact same write again before a fresh Notion snapshot.
        for field in pending:
            for record in history:
                if (record['field'] == field.get('field') and
                        field.get('old') == record['old'] and
                        (field.get('styled') or field.get('new')) == record['new']):
                    field['snapshot_current'] = False
        notes = reviews.get(r['id'], [])
        if any(record.get('needs_verification') for record in history):
            notes = [*notes, {'code': 'apply_result_uncertain',
                'note': '이전 반영 또는 취소 결과가 확정되지 않았습니다. Notion의 실제 내용을 먼저 확인해주세요. 같은 변경을 다시 전송하지 않습니다.'}]
        fresh = (pd.get('proposal_schema_version') == _proposal_schema_version()
                 and (pd.get('extraction_meta') or {}).get('prompt_version') == _EXTRACTION_PROMPT_VERSION
                 and all(field.get('snapshot_current') is True for field in pending))
        state = ('source_review' if notes else 'completed' if not pending
                 else 'proposal' if fresh else 'needs_revalidation')
        out.append({
            "id": r["id"], "db_key": r["db_key"], "page_id": r["page_id"], "title": r["title"],
            "intensity": pd.get("intensity", "low"), "fields": [
                {**field, "requires_individual_review": (
                    label_keys.get(field.get("field", "")) or
                    _LEGACY_LABEL_ALIASES.get(field.get("field", ""))
                ) != "category"}
                for field in fields],
            'apply_history': history, 'completed': not pending,
            'review_notes': notes, 'review_state': state,
            'apply_ready': bool(pending and fresh and not notes),
            "dead_links": utils.parse_json_safe(r["dead_links"], []),
            "deeplink": notion_service.page_deeplink(r["page_id"]),
            "source_urls": utils.parse_json_safe(r["source_urls"], []),
            "last_changed_at": r["last_changed_at"],
            "source_diff": pd.get("source_diff"),
        })
    # 🔴 중요 → 📝 보강 후보 → ⚪ 사소 순
    _order = {"high": 0, "info": 1, "low": 2}
    out.sort(key=lambda x: (_order.get(x["intensity"], 2), x["title"]))
    return out


def get_new_candidates(db_keys: List[str]) -> List[Dict[str, Any]]:
    """수집된 program 항목 중 어느 정책 DB에도 없는 신규 후보 (유사정책 ⚠️ 포함)."""
    import numpy as np
    # 현재 연결된 보관함만 중복·유사도 판정에 사용한다. 연결 해제한 보관함의 로컬 미러는
    # 복구 가능성을 위해 보존하되 신규 후보를 영구히 가로막는 유령 데이터가 되면 안 된다.
    active_keys = _active_configured_keys(db_keys)
    if not active_keys:
        return []
    placeholders = ",".join("?" * len(active_keys))
    prows = database.execute_query(
        f"SELECT page_id, title, source_urls, title_embedding, db_key FROM notion_policies "
        f"WHERE db_key IN ({placeholders})", tuple(active_keys))
    src_urls = set()
    pol_vecs: List[Tuple[dict, np.ndarray]] = []
    for p in prows:
        for u in utils.parse_json_safe(p["source_urls"], []):
            src_urls.add(_norm_url(u))
        vec = embedding_service.from_blob(p["title_embedding"])
        if vec is not None:
            pol_vecs.append(({"title": p["title"], "db_key": p["db_key"]}, vec))

    items = database.execute_query(
        "SELECT id, title, link, site_name, category, ai_data, embedding "
        "FROM welfare_items WHERE is_archived = 0 AND notion_dismissed = 0")
    out = []
    for it in items:
        ai = utils.parse_json_safe(it["ai_data"], {})
        if ai.get("item_type") != "program":
            continue
        if _norm_url(it["link"]) in src_urls:
            continue  # 이미 정책 DB에 등재된 출처
        # 사업명 유사도 (링크 불일치인데 유사 → ⚠️)
        similar = None
        vec = embedding_service.from_blob(it["embedding"])
        if vec is not None and pol_vecs:
            ranked = embedding_service.rank_by_similarity(vec, pol_vecs, limit=1,
                                                          min_score=_TITLE_SIM_THRESHOLD)
            if ranked:
                payload, score = ranked[0]
                similar = {"title": payload["title"], "db_key": payload["db_key"],
                           "score": round(score, 3)}
        out.append({
            "id": it["id"], "title": it["title"], "link": it["link"],
            "site_name": it["site_name"], "category": it["category"],
            "target": ai.get("target", ""),
            "life_cycle": ai.get("life_cycle", []),
            "similar": similar,
        })
    return out


# (U1) 필드 라벨 → Notion 속성 반영용 내부 키. 값 없으면 자동 반영 불가(직접 수정 안내).
# "지원금액·기간"은 support_detail에서 파생된 비교 신호일 뿐 실제 Notion 속성이 없어 제외.
# (2026-07-20) 라벨은 실제 Notion 속성명(_PROP_LABEL)에서 파생. 라벨 개편 전에 저장된
# pending_diff(구 축약 라벨)도 재계산 없이 바로 적용되도록 구 라벨 별칭을 함께 받는다.
_APPLYABLE_FKEYS = ("residence", "life_cycle", "target_characteristics", "category",
                    "age_detail", "eligibility_detail", "target_income", "support_detail")
# 구 축약 라벨(라벨 개편 전 저장된 pending_diff 호환용) — 속성명 오버라이드와 무관하게 고정.
_LEGACY_LABEL_ALIASES = {
    "대상특성": "target_characteristics",
    "대상연령": "age_detail",
    "자격요건": "eligibility_detail",
    "대상소득": "target_income",
    "상세지원내용": "support_detail",
}


def _label_to_field(field_label: str) -> Optional[str]:
    """라벨 → 내부 fkey 역조회. (2026-07-30) 매번 notion_service.get_policy_props()를 재조회해
    NOTION_POLICY_PROP_MAP 오버라이드가 바뀐 뒤 생성된 새 라벨도 즉시 해석 가능하게 한다.

    알려진 제약(2026-08-05): pending_diff에는 diff 생성 시점의 라벨 문자열이 그대로 저장된다.
    오버라이드를 바꾼 뒤에도 예전 매핑으로 이미 생성된 pending_diff가 남아있으면, 그 라벨은
    새 매핑에서 못 찾아 "자동 반영 불가"로 처리된다(구 축약 라벨처럼 _LEGACY_LABEL_ALIASES에
    등록돼 있지 않는 한). 데이터 손상이나 크래시는 없고 단지 그 건은 재적용이 막힐 뿐이므로,
    매핑을 바꾼 뒤에는 "정책 정보 최신인지 확인하기"로 변경 후보를 다시 계산해야 한다."""
    live_map = {_PROP_LABEL[k]: k for k in _APPLYABLE_FKEYS}
    return live_map.get(field_label) or _LEGACY_LABEL_ALIASES.get(field_label)


_MULTI_FIELD_KEYS = ("residence", "life_cycle", "target_characteristics")


# ==================== 필드 의미 검증 (2026-08-07) ====================
# 실제 사고 사례(id=13 "도봉구가족센터 다문화자녀 언어발달서비스"): AI 재추출이 대상특성
# 값("다문화")을 세부 연령 필드에 잘못 옮겨 적어("다문화자녀") 실제 운영 Notion에 그대로
# 반영됐다. 기존 "필드 오배치" 감지(_content_moved, 위 8번)는 최소 10자 매칭을 요구해
# "다문화자녀"처럼 짧은 태그성 오배치는 못 잡는 사각지대가 있었다(재현 확인). 값 자체를
# 고치지 않고 "적용" 직전에 의심스러우면 강한 확인을 요구하는 방식으로 대응한다 — 오탐이
# 있어도 force=True로 사용자가 직접 확인 후 넘어갈 수 있어야 하므로 완전 차단이 아니다.
_AGE_UNIT_RE = re.compile(r'\d+\s*(?:세|개월|살|년생)|만\s*\d|생후\s*\d|\d+\s*[~\-]\s*\d+\s*(?:세|개월)?')
# (2026-08-07, 2차 GPT Codex 리뷰) 처음엔 "숫자가 하나라도 있으면 통과"·"'세'가 어디든
# 포함되면 통과"였는데, 실제로 "다문화 2자녀"·"3인 가구"·"2회 지원"·"세계 아동 지원"(→'세계'의
# '세') 같은 값이 전부 오통과됨을 재현으로 확인했다. 숫자는 반드시 나이 단위(세/개월/살)와
# 붙어 있어야 하고, 비숫자 표현은 통짜 토큰으로만 인정한다(부분 문자열 매칭 금지).
_AGE_WHOLE_WORD_MARKERS = {
    '연령', '영아', '유아', '아동', '청소년', '성인', '임산부', '초등학생', '미취학',
    '학령기', '전연령', '전 연령', '영유아', '청년', '노인', '고령', '미취학아동',
}
_TARGET_CHARACTERISTIC_VOCAB = {
    '다문화', '다문화자녀', '다문화가정', '일반', '장애인', '장애', '장애/발달지연',
    '저소득', '한부모', '한부모가정', '다자녀', '새터민', '북한이탈주민', '조손가정',
    '보호자', '차상위', '기초생활수급자',
}


def _looks_like_age_expression(value: str) -> bool:
    """숫자+연령단위 조합, 또는 독립된 생애주기 단어 토큰이 있으면 통과."""
    if not value:
        return False
    if _AGE_UNIT_RE.search(value):
        return True
    tokens = re.split(r'[\s,·/~\-]+', value)
    return any(t in _AGE_WHOLE_WORD_MARKERS for t in tokens if t)


# 실제 사고 사례의 support_detail: 헤더(✅ 지원 내용 등)마다 본문이 헤더명 재진술 placeholder만
# 있고 실제 지원 내용(금액·방법·서비스명 등 구체적 사실)이 없었다.
_SUPPORT_DETAIL_PLACEHOLDER_PHRASES = (
    '대상자 및 자격 요건', '방법 및 장소', '금액, 바우처, 서비스 내용', '접수기간', '기타 안내',
    '신청 자격', '신청 방법', '지원 금액', '신청 기간', '참고 사항', '지원 내용',
)


def _looks_like_template_placeholder(value: str) -> bool:
    """섹션 헤더 아래 본문이 대부분 헤더명 재진술뿐이면(구체적 내용 없음) True."""
    if not value:
        return False
    lines = [l.strip(' \t-•●▪‣·✅📋📍💰📅📝#') .strip() for l in value.split('\n')]
    lines = [l for l in lines if l]
    if len(lines) < 3:
        return False
    placeholder_hits = sum(1 for l in lines if l in _SUPPORT_DETAIL_PLACEHOLDER_PHRASES)
    return placeholder_hits >= max(2, len(lines) // 2)


def _validate_field_semantics(fkey: str, value: str,
                               target_characteristics_display: str = "") -> Optional[str]:
    """의미상 이상해 보이는 제안값이면 경고 문구 반환, 정상이면 None.

    target_characteristics_display: (2026-08-07, 2차 리뷰) 같은 진단의 '대상 특성' 필드
    값(있으면) — id=13 사고가 정확히 "세부 연령에 대상 특성 값이 들어감"이었으므로, 교차
    비교가 가장 직접적인 방어선이다. 정적 어휘 목록(다문화·일반·장애인 등)은 이 필드가
    비어있을 때의 최소 방어선으로만 쓴다."""
    if fkey == "age_detail":
        if not value:
            return None
        norm = _norm_text(value)
        if target_characteristics_display and norm and norm in _norm_text(target_characteristics_display):
            return ("연령 정보로 판단하기 어려운 값입니다. 이번 진단의 대상 특성 값과 "
                    "겹쳐 필드가 뒤바뀌었을 수 있으므로 원문을 확인해주세요.")
        if norm in {_norm_text(v) for v in _TARGET_CHARACTERISTIC_VOCAB}:
            return ("연령 정보로 판단하기 어려운 값입니다. 대상 특성 용어로 보이므로 "
                    "필드가 잘못 들어갔을 수 있습니다 — 원문을 확인해주세요.")
        if not _looks_like_age_expression(value):
            return ("연령 정보로 판단하기 어려운 값입니다. 대상 특성 내용이 잘못 들어갔을 "
                    "수 있으므로 원문을 확인해주세요.")
    elif fkey == "support_detail":
        if _looks_like_template_placeholder(value):
            return ("실제 지원 내용보다 문서의 항목명만 추출된 것으로 보입니다. "
                    "자동 적용하지 않고 원문 확인이 필요합니다.")
    return None


def _filter_low_quality_extracted_fields(
        fields: Dict[str, Any], fallback_fields: Optional[Dict[str, Any]] = None
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """확실히 잘못 추출된 필드만 직전 정상값으로 되돌린다.

    빈 문자열·빈 목록은 원문에서 정보가 실제로 빠진 결과일 수 있으므로 저품질로 단정하지
    않는다. 현재는 운영 사고와 회귀 테스트로 판정 근거가 확보된 ``age_detail``의 필드
    오배치와 ``support_detail``의 템플릿 placeholder만 걸러낸다. 한 필드가 걸려도 나머지
    새 추출값은 그대로 보존해 정상 변경까지 통째로 버리는 일을 막는다.

    반환값은 ``(정제된 복사본, 제외 사유 {필드키: 안내문})``이다. 대체할 직전 값이 없으면
    해당 키만 제거한다. 이후 비교 로직이 존재하지 않는 값을 빈 값으로 다루므로, 호출부는
    가능한 한 이전 AI 기준선 또는 사람이 관리한 Notion 값을 fallback으로 넘겨야 한다.
    """
    cleaned = dict(fields or {})
    fallback = fallback_fields or {}
    rejected: Dict[str, str] = {}

    tc = cleaned.get("target_characteristics")
    tc_display = ", ".join(str(v) for v in tc) if isinstance(tc, list) else str(tc or "")
    for fkey in ("age_detail", "support_detail"):
        if fkey not in cleaned:
            continue
        value = str(cleaned.get(fkey) or "")
        warning = _validate_field_semantics(
            fkey, value, tc_display if fkey == "age_detail" else "")
        if not warning:
            continue
        rejected[fkey] = warning
        if fkey in fallback:
            cleaned[fkey] = fallback[fkey]
        else:
            cleaned.pop(fkey, None)
        if fkey == "age_detail":
            # 세부 연령 문구와 시작/종료 개월은 Notion의 한 논리 필드를 구성한다.
            # 문구만 이전 값으로 돌리고 개월 수를 새 추출값으로 남기면 기준선 내부가
            # 모순되므로, 의심 판정 시 이 두 내부 값도 같은 기준선으로 맞춘다.
            for companion in ("start_age_months", "end_age_months"):
                if companion in fallback:
                    cleaned[companion] = fallback[companion]
                else:
                    cleaned.pop(companion, None)

    return cleaned, rejected


def _structured_apply_hold(fkey, py_value, source, extracted):
    """Recheck only the exact outgoing tags against their saved source.

    A cached version marker or a user's force confirmation is not evidence.
    Numeric companions are used only to preserve an already exact child range;
    they are revalidated locally, never guessed, written or used as a fallback
    for missing source. Unrelated bad cached fields must not block a sound tag.
    """
    if fkey not in _MULTI_FIELD_KEYS:
        return None
    if not isinstance(source, str) or not source.strip():
        return ('제안의 저장된 원문을 확인할 수 없어 태그 반영을 보류했습니다. '
                '원문을 다시 점검해주세요. 기존 Notion 내용과 검토 대기 제안은 유지됩니다.')
    from services.policy_source_plan import policy_source_max_chars
    max_chars = policy_source_max_chars()
    if max_chars <= 0 or len(source) > max_chars:
        return ('저장된 정책 원문이 검증 길이 한도를 넘어 태그 반영을 보류했습니다. '
                '원문을 자르지 않았습니다. 정책별 상세 출처와 분석 길이 설정을 확인해주세요. '
                '기존 Notion 내용과 검토 대기 제안은 유지됩니다.')
    from services.policy_evidence import structured_policy_issues
    try:
        facts = {fkey: py_value}
        issues = structured_policy_issues(facts, source)
        if issues and fkey == 'life_cycle' and isinstance(extracted, dict):
            bounds = {key: extracted.get(key)
                      for key in ('start_age_months', 'end_age_months')}
            if all(type(value) is int for value in bounds.values()):
                # The existing guard requires the one complete recipient range
                # to match both bounds before accepting overlapping child tags.
                issues = structured_policy_issues(dict(facts, **bounds), source)
        if not issues:
            return None
    except Exception:
        # Local validation failure is a hold, not permission to patch Notion.
        # Do not log source text, user values or a model's untrusted response.
        logger.warning('[정책 최신화] 적용 전 구조화 원문 근거 검증 실패')
    return ('태그가 실제 지원 대상에 해당하는지 원문 근거를 확인하지 못해 반영을 보류했습니다. '
            '제외 대상·제공인력·참고자료 또는 연령 범위를 확인해주세요. '
            '기존 Notion 내용과 검토 대기 제안은 유지됩니다.')


def _apply_field_sync(policy_id: int, field_label: str, force: bool = False,
                       force_semantic: bool = False) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[Dict[str, Any]]]:
    """DB 조회+필드 매칭(동기 부분). 반환 (신청정보|None, error|None, pending_diff dict).

    신청정보 dict: {page_id, properties, fkey, py_value, old_display} — properties는 Notion
    PATCH용, 나머지는 (C1 실시간 대조 / B4 로컬 미러 갱신)에 필요한 부가 정보.

    (2026-08-07, 2차 GPT Codex 리뷰) force와 force_semantic을 분리한다 — 예전엔 force
    하나가 범위축소(NARROWING)·팽창(BLOAT)·의미검증(SUSPICIOUS_VALUE)을 전부 한꺼번에
    우회했다. 프론트의 "그래도 적용" 확인창 하나로 세 가지 서로 무관한 안전장치가 동시에
    풀리는 건 위험하므로, 의미검증 우회는 별도 플래그로 명시적으로만 가능하게 한다.
    """
    row = database.execute_query(
        "SELECT page_id, db_key, pending_diff, notion_fields, source_urls, source_texts, title, last_extracted_fields "
        "FROM notion_policies WHERE id=?",
        (policy_id,), fetch_one=True, raise_on_error=True)
    if not row:
        return None, "정책을 찾을 수 없습니다", None
    pd = utils.parse_json_safe(row["pending_diff"], {})
    fields = pd.get("fields", [])
    target = next((f for f in fields if f["field"] == field_label and not f.get("applied")), None)
    if not target:
        return None, "해당 필드를 찾을 수 없습니다(이미 적용됨)", None
    fkey = _label_to_field(field_label)
    if not fkey:
        return None, f"'{field_label}'은 자동 반영을 지원하지 않는 필드입니다 (직접 Notion에서 수정해주세요)", None

    # This endpoint applies generated policy-update suggestions, not a user's
    # normal policy registration/edit. Older suggestions cannot be revalidated
    # from their truncated source preview, even with a force override. Preserve
    # both the pending suggestion and the existing user content for re-check.
    meta = pd.get('extraction_meta')
    if not isinstance(meta, dict) or meta.get('prompt_version') != _EXTRACTION_PROMPT_VERSION:
        return None, ('이전 검증 버전의 최신화 제안입니다. 원문을 다시 점검한 뒤 적용해주세요. '
                      '기존 Notion 내용과 검토 대기 제안은 유지됩니다.'), pd
    from services.policy_apply_coverage import POLICY_PROPOSAL_SCHEMA_VERSION
    if (pd.get('proposal_schema_version') != POLICY_PROPOSAL_SCHEMA_VERSION
            or target.get('snapshot_current') is not True):
        return None, ('현재 Notion 원본과 대조하기 전의 제안입니다. 최신 내용을 다시 '
                      '불러와 점검한 뒤 적용해주세요. 기존 내용은 변경하지 않았습니다.'), pd

    from services import policy_review_store, policy_undo
    if policy_review_store.unresolved_by_policy([row['db_key']]).get(policy_id):
        return None, ('NEEDS_SOURCE_REVIEW:현재 출처에 해결되지 않은 확인 사항이 있어 '
                      '반영을 보류했습니다. 원문을 다시 점검한 뒤 적용해주세요. '
                      '기존 Notion 내용은 변경하지 않았습니다.'), pd
    if any(record.get('needs_verification') for record in
           policy_undo.applied_records(page_id=row['page_id'])):
        return None, ('APPLY_RESULT_UNCERTAIN:이전 반영 또는 취소 결과가 확정되지 않아 '
                      '새 요청을 보내지 않았습니다. Notion의 실제 내용과 반영 기록을 '
                      '먼저 확인해주세요.'), pd

    # Recheck known source-criteria risks in previously saved suggestions too.
    # Prefer the full source for this proposal, never an unrelated source URL.
    # If it is unavailable, the preview/proposed prose can reveal a risk but
    # cannot establish completeness. No mutation, API call or force bypass.
    from services.policy_source_review import source_criteria_issues, source_criteria_note
    saved_row = dict(row)
    source_texts = utils.parse_json_safe(saved_row.get('source_texts'), {})
    configured_urls = list(dict.fromkeys(utils.parse_json_safe(saved_row.get('source_urls'), [])))
    evidence_urls = [url for url in configured_urls if not is_low_value_source(url)]
    source_diff = pd.get('source_diff') if isinstance(pd.get('source_diff'), dict) else {}
    context = pd.get('source_context')
    full_source = None
    if isinstance(context, dict):
        urls, hashes = context.get('urls'), context.get('hashes')
        valid = (context.get('title') == saved_row.get('title') and isinstance(urls, list)
                 and bool(urls) and all(isinstance(url, str) for url in urls)
                 and len(urls) == len(set(urls)) and set(urls) == set(evidence_urls)
                 and isinstance(hashes, dict) and isinstance(source_texts, dict))
        if valid:
            valid = all(isinstance(source_texts.get(url), str) and source_texts[url].strip()
                        and hashlib.sha256(source_texts[url].encode('utf-8')).hexdigest() == hashes.get(url)
                        for url in urls)
        if not valid:
            return None, 'NEEDS_SOURCE_REVIEW:제안의 전체 출처 근거가 변경되었습니다. 정책을 다시 점검해주세요.', pd
        full_source = '\n\n---\n\n'.join(source_texts[url] for url in urls)
    elif len(evidence_urls) > 1:
        return None, 'NEEDS_SOURCE_REVIEW:여러 출처의 전체 근거를 다시 확인해야 합니다. 정책을 다시 점검해주세요.', pd
    else:
        full_source = source_texts.get(source_diff.get('url')) if isinstance(source_texts, dict) else None
    if not full_source and not source_diff.get('url') and isinstance(source_texts, dict) and len(source_texts) == 1:
        full_source = next(iter(source_texts.values()))
    if isinstance(full_source, str) and full_source.strip():
        review_source = _policy_evidence_text(full_source, saved_row.get('title') or '', len(evidence_urls))
    else:
        review_source = '\n'.join(value for value in [
            source_diff.get('new'), *(field.get('new') for field in fields if isinstance(field, dict))
        ] if isinstance(value, str))
    criteria_review = source_criteria_issues(review_source)
    if criteria_review:
        return None, source_criteria_note(criteria_review), pd

    value = target.get("styled") or target.get("new") or ""
    # Unknown output must never become a destructive empty/tag update, even
    # when a caller explicitly overrides semantic or size warnings.
    if (not isinstance(value, str) or
            value.strip().casefold() in {"", "none", "null", "undefined", "nan", "-", "(없음)",
                                       "unknown", "미상", "알 수 없음", "알수없음"}):
        return None, "제안값을 확인할 수 없어 반영하지 않았습니다. 기존 Notion 내용은 유지됩니다.", pd
    if fkey in _MULTI_FIELD_KEYS:
        py_value = [v.strip() for v in value.split(",") if v.strip()]
        # (2026-07-13) 적용 직전 최종 정규화 — 과거 스타일 변환이 만든 뭉뚱그린 styled
        # 값("전국" 단독 등)이 DB에 남아 있어도, 나가는 값은 항상 확장·클램프를 거친다.
        schema = _get_db_schema(row["db_key"])
        if fkey == "residence":
            py_value = _clamp_residence(
                _expand_residence(py_value, schema.get("residence")), schema.get("residence"))
        elif fkey == "life_cycle":
            py_value = _clamp_life_cycle(
                _expand_life_cycle(py_value, schema.get("life_cycle")), schema.get("life_cycle"))
        elif fkey == "target_characteristics":
            selected_tags = set(py_value)
            py_value = _clamp_target_characteristics(py_value, schema.get("target_characteristics"))
            if not selected_tags.intersection(py_value):
                return None, ('연결된 선택지와 일치하는 제안 태그가 없어 반영을 보류했습니다. '
                              '일반 태그로 대신 바꾸지 않습니다. 기존 Notion 내용과 검토 대기 제안은 유지됩니다.'), pd
    elif fkey == "support_detail":
        # 과거 생성된 styled/추출값에 마크다운·초과 섹션이 남아 있어도 나가기 전 최종 위생
        py_value = _sanitize_support_detail(value, target.get("old") or "")
    else:
        py_value = value

    if not py_value:
        return None, "유효한 제안값이 없어 반영하지 않았습니다. 기존 Notion 내용은 유지됩니다.", pd

    # A preview or proposed prose may reveal a criteria risk above, but cannot
    # establish structural evidence. Guard the final clamped/styled value, not
    # the original extraction, before either user-overridable warning branch.
    source_for_tags = (review_source if isinstance(full_source, str) and full_source.strip() else None)
    extracted = utils.parse_json_safe(saved_row.get('last_extracted_fields'), {})
    structured_hold = _structured_apply_hold(fkey, py_value, source_for_tags, extracted)
    if structured_hold:
        return None, structured_hold, pd

    from services.policy_apply_coverage import standalone_apply_hold
    current_fields = utils.parse_json_safe(row['notion_fields'], {})
    # Legacy test/data rows can lack a local mirror, but the displayed before
    # value still cannot be silently discarded. Live Notion is checked again.
    current_fields.setdefault(fkey, target.get('old') or '')

    # (2026-08-07) 의미 검증 — id=13 실사고 재발 방지. 값 자체는 안 고치고, 의심스러우면
    # force_semantic 없이는 Notion에 안 나가게만 막는다(범위축소/팽창 우회용 force와 분리).
    if not force_semantic:
        warn_value = ", ".join(py_value) if isinstance(py_value, list) else str(py_value)
        # 교차 검사용 대상특성 값 — 같은 진단에서 함께 바뀌는 중이면 그 새 값을,
        # 아니면 현재 Notion의 대상특성 값을 사용한다.
        tc_sibling = next((f for f in fields if f["field"] == _PROP_LABEL["target_characteristics"]), None)
        if tc_sibling:
            tc_display = tc_sibling.get("new") or ""
        else:
            nf = utils.parse_json_safe(row["notion_fields"], {}) if row else {}
            tc_val = nf.get("target_characteristics")
            tc_display = ", ".join(tc_val) if isinstance(tc_val, list) else str(tc_val or "")
        warn = _validate_field_semantics(fkey, warn_value, tc_display)
        if warn:
            return None, "SUSPICIOUS_VALUE:" + warn, pd

    coverage_hold = standalone_apply_hold(fkey, py_value, current_fields, source_for_tags)
    if coverage_hold:
        return None, 'NEEDS_LINKED_REVIEW:' + coverage_hold, pd

    # (B3) 정보 축소 차단 — 다중값 필드가 기존보다 태그 수가 줄면(=정보 손실 가능) 확인 없이는 막는다.
    if not force and fkey in _MULTI_FIELD_KEYS:
        old_display = target.get("old") or ""
        old_tags = {t.strip() for t in old_display.split(",") if t.strip()}
        new_tags = {t.strip() for t in py_value}
        if old_tags and not old_tags <= new_tags and len(new_tags) < len(old_tags):
            return None, "NARROWING", pd

    # (2026-07-13) 팽창 차단 — 새 값이 기존의 3배를 넘으면 출처가 여러 제도를 포함한 통합
    # 페이지에서 무관한 내용까지 끌려 들어왔을 가능성이 높다(실측: 장애인 의료비 공제에
    # 자동차세·상속세 감면 전체가 제안됨). 확인 없이는 막는다.
    if not force and fkey == "support_detail":
        old_len = len(target.get("old") or "")
        if old_len >= 50 and len(str(py_value)) > old_len * 3:
            return None, "BLOAT", pd

    props = notion_service.build_policy_properties({fkey: py_value})
    if not props:
        return None, "반영할 값이 비어 있습니다", None
    # (이중 안전장치 2026-07-13) 단일 필드 적용인데 PATCH 본문에 속성이 2개 이상 담기면
    # 무조건 중단 — multi_select None 누락 사고처럼, 의도치 않은 속성이 끼어드는 어떤
    # 버그가 다시 생겨도 여기서 걸러진다(요청 자체를 보내지 않음).
    if len(props) != 1:
        logger.error(f"[정책 최신화] 단일 필드 적용 검증 실패 — 예상 1개, 실제 {len(props)}개: {list(props.keys())}")
        return None, "내부 검증 실패로 반영을 중단했습니다 (의도치 않은 속성 포함 감지)", None
    return {"page_id": row["page_id"], "properties": props, "fkey": fkey,
            "py_value": py_value, "old_display": target.get("old") or "",
            "coverage_source": source_for_tags,
            'snapshot_context': {'title': saved_row.get('title'), 'source_urls': configured_urls}}, None, pd


def _preserve_accepted_fields(diffs, pending, current, extracted):
    """Suppress only an exact, already accepted extraction/current-value pair.

    Do not use fuzzy matching: a changed amount or a manual Notion edit must
    reopen review. Missing legacy receipts are deliberately not inferred.
    """
    accepted = []
    for field in pending.get("fields", []):
        receipt = field.get("acceptance")
        if not field.get("applied") or not isinstance(receipt, dict):
            continue
        key = receipt.get("key")
        if (key in current and key in extracted and
                current[key] == receipt.get("written") and
                extracted[key] == receipt.get("extracted")):
            accepted.append(field)
    labels = {field["field"] for field in accepted}
    return [field for field in diffs if field["field"] not in labels], accepted


def _finalize_apply(policy_id: int, field_label: str, pd: Dict[str, Any], fkey: str, py_value) -> int:
    """PATCH 성공 후: (B4) 로컬 미러 notion_fields를 방금 반영한 값으로 갱신해 다음 점검 때
    같은 필드가 또 '변경'으로 잡히는 것을 막는다.

    (UX 2026-07-13) 적용된 필드를 목록에서 삭제하지 않고 applied=True로 표시만 한다 —
    삭제하면 방금 무엇을 눌렀는지, 다른 필드는 어떤 상태인지 화면에서 사라져 보기 어렵다는
    피드백 반영. 적용 안 된(active) 필드가 하나도 안 남으면 confirmed로 전환.

    반환값: 이번 적용 후 남은(미적용) 필드 수 — (2026-08-07, GPT Codex 지적) 호출자가 이걸
    몰라서 필드가 몇 개 남았든 "전체 완료"로만 표시하던 버그의 근본 수정."""
    # Notion PATCH는 이미 성공한 뒤이므로 로컬 미러/스냅샷은 반드시 한 트랜잭션으로
    # 끝내거나 전부 롤백한다. 실패를 숨겨 성공 응답을 보내면 다음 점검 때 같은 변경이
    # 되살아나므로 예외는 호출자까지 전달한다.
    with database.get_connection() as conn:
        row = conn.execute(
            "SELECT page_id, notion_fields, last_extracted_fields FROM notion_policies WHERE id=?", (policy_id,)
        ).fetchone()
        if not row:
            raise LookupError(f"정책을 찾을 수 없습니다: {policy_id}")

        notion_fields = utils.parse_json_safe(row["notion_fields"], {})
        notion_fields[fkey] = py_value
        nf_json = json.dumps(notion_fields, ensure_ascii=False)
        extracted = utils.parse_json_safe(row["last_extracted_fields"], {}) or {}

        for f in pd.get("fields", []):
            if f["field"] == field_label:
                f["applied"] = True
                if fkey in extracted:
                    f["acceptance"] = {"key": fkey, "written": py_value, "extracted": extracted[fkey]}
        active = [f for f in pd.get("fields", []) if not f.get("applied")
                  and isinstance(f.get('styled') or f.get('new'), str)
                  and (f.get('styled') or f.get('new')).strip().casefold()
                  not in {'', 'none', 'null', 'undefined', 'nan', '-', '(없음)'}]
        pd['completed'] = not bool(active)
        # Preserve the card's original priority/order until explicit cleanup.
        if active:
            cursor = conn.execute(
                "UPDATE notion_policies SET pending_diff=?, notion_fields=? WHERE id=?",
                (json.dumps(pd, ensure_ascii=False), nf_json, policy_id))
        else:
            conn.execute(
                "INSERT INTO welfare_item_snapshots (item_link, snapshot_data, change_summary) VALUES (?, ?, ?)",
                (f"notion:{row['page_id']}", row["notion_fields"] or "{}", "정책 변경 자동 반영(전체 완료)"))
            cursor = conn.execute(
                "UPDATE notion_policies SET pending_diff=?, notion_fields=?, change_status='detected' WHERE id=?",
                (json.dumps(pd, ensure_ascii=False), nf_json, policy_id))
        if cursor.rowcount != 1:
            raise RuntimeError(f"정책 로컬 상태를 갱신하지 못했습니다: {policy_id}")
        conn.commit()
        return len(active)


# (2026-08-28, GPT Codex 지적 ①) 같은 정책 카드의 서로 다른 필드를 거의 동시에 "적용"하면,
# 두 요청이 각자 오래된 pending_diff를 읽어 나중에 저장한 쪽이 먼저 적용된 필드의
# applied=True 표시를 덮어쓸 수 있었다(Notion엔 둘 다 반영됐는데 로컬 DB엔 하나만 남는 불일치).
# 정책 ID별 락으로 "조회→Notion조회→PATCH→로컬갱신" 전체 구간을 직렬화해 근본 차단한다.
# 정책 수가 유한(수백 건)해 dict가 무한정 커질 위험은 낮음 — 프로세스 재시작 시 자연 소멸.
_POLICY_APPLY_LOCKS: Dict[int, asyncio.Lock] = {}


def _get_policy_apply_lock(policy_id: int) -> asyncio.Lock:
    lock = _POLICY_APPLY_LOCKS.get(policy_id)
    if lock is None:
        lock = asyncio.Lock()
        _POLICY_APPLY_LOCKS[policy_id] = lock
    return lock


async def apply_field_to_notion(policy_id: int, field_label: str, force: bool = False,
                                 force_semantic: bool = False) -> Dict[str, Any]:
    """(U1) 감지된 변경 중 필드 1개를 Notion 페이지에 직접 반영(PATCH).

    자동 실행이 아니라 사용자가 화면에서 필드별 '적용' 버튼을 눌렀을 때만 호출된다.
    해당 필드가 사라진(전부 반영된) 정책은 confirmed로 전환하고 스냅샷을 남긴다(⑥).

    (C1) PATCH 직전 Notion 현재값을 실시간 조회해 감지 당시(old)와 다르면 중단한다 —
    동기화 이후 사용자가 Notion에서 직접 그 필드를 고쳤다면, 낡은 진단으로 덮어쓰지 않기 위함.

    force: 범위축소(NARROWING)·팽창(BLOAT) 우회. force_semantic: 의미검증(SUSPICIOUS_VALUE)
    우회 — (2026-08-07, 2차 리뷰) 서로 무관한 안전장치라 별도 플래그로 분리했다.

    (2026-08-28) 같은 policy_id에 대한 동시 호출은 이 락으로 직렬화된다 — 서로 다른
    policy_id는 병렬로 진행 가능(불필요한 대기 방지).

    (2026-08-28, ②) 정책 동기화·점검·통합점검·최초감사·재계산·스타일재작성·DB복원 등
    유지보수 작업이 진행 중이면 필드 적용도 거부한다 — 동기화가 notion_fields/pending_diff를
    갱신하는 도중에 필드 적용이 끼어들면 서로 다른 시점의 스냅샷을 섞어 쓸 위험이 있다.
    """
    if not await acquire_policy_mutation_lock(f"정책 필드 적용 #{policy_id}"):
        return {"ok": False, "error": "MAINTENANCE_BUSY",
                "message": maintenance_busy_message()}
    try:
        async with _get_policy_apply_lock(policy_id):
            return await _apply_field_to_notion_locked(policy_id, field_label, force, force_semantic)
    finally:
        release_maintenance_lock()


def _live_field_matches_snapshot(fkey: str, live_value, old_display: str) -> bool:
    """Compare a write precondition, not presentation similarity.

    A decimal point, range separator or slash can change a policy's meaning.
    Keep them all; only normalize repeated whitespace. Multi-select order is
    not semantic, and its saved display joins individual tags with commas.
    """
    def normalize(value):
        return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()

    if fkey in _MULTI_FIELD_KEYS:
        if not isinstance(live_value, list):
            return False
        current = {normalize(tag) for tag in live_value if normalize(tag)}
        expected = {normalize(tag) for tag in old_display.split(",") if normalize(tag)}
        return current == expected
    return normalize(live_value) == normalize(old_display)


async def _apply_field_to_notion_locked(policy_id: int, field_label: str, force: bool,
                                         force_semantic: bool) -> Dict[str, Any]:
    target, err, pd = await database.run_db(
        _apply_field_sync, policy_id, field_label, force, force_semantic)
    if err and err.startswith('NEEDS_LINKED_REVIEW:'):
        return {'ok': False, 'error': 'NEEDS_LINKED_REVIEW', 'message': err.split(':', 1)[1]}
    if err and err.startswith(('NEEDS_SOURCE_REVIEW:', 'APPLY_RESULT_UNCERTAIN:')):
        code, message = err.split(':', 1)
        return {'ok': False, 'error': code, 'message': message}
    if err and err.startswith("SUSPICIOUS_VALUE:"):
        return {"ok": False, "error": "SUSPICIOUS_VALUE", "message": err.split(":", 1)[1]}
    if err == "NARROWING":
        return {"ok": False, "error": "NARROWING",
                "message": "기존보다 태그가 줄어드는 변경입니다. 정말 적용하시려면 다시 시도해 확인해주세요."}
    if err == "BLOAT":
        return {"ok": False, "error": "BLOAT",
                "message": "새 값이 기존의 3배가 넘습니다 — 출처 페이지가 여러 제도를 함께 소개하는 통합 안내문이라 무관한 내용이 섞였을 수 있어요. 내용을 확인하셨다면 다시 시도해 적용할 수 있습니다."}
    if err:
        return {"ok": False, "error": err}

    page = await notion_service.get_page(target["page_id"])
    if not page:
        return {"ok": False, "error": "LIVE_READ_FAILED",
                "message": "Notion의 최신 내용을 확인하지 못해 적용하지 않았습니다. 연결 상태를 확인한 뒤 다시 시도해주세요."}
    live_fields = notion_service.extract_page_fields(page)
    context = target.get('snapshot_context')
    if isinstance(context, dict) and (
            live_fields.get('title') != context.get('title')
            or set(live_fields.get('_source_urls') or []) != set(context.get('source_urls') or [])):
        return {'ok': False, 'error': 'STALE_SOURCE',
                'message': 'Notion의 정책명이나 출처 주소가 변경되어 반영하지 않았습니다. 정책 정보를 다시 불러와 점검해주세요.'}
    fkey = target["fkey"]
    live_val = live_fields.get(fkey)
    if not _live_field_matches_snapshot(fkey, live_val, target["old_display"]):
        return {"ok": False, "error": "STALE",
                "message": "Notion에서 이미 이 필드가 수정된 것 같습니다 — 최신 상태를 확인 후 다시 시도해주세요."}

    from services.policy_apply_coverage import standalone_apply_hold
    coverage_hold = standalone_apply_hold(fkey, target['py_value'], live_fields,
                                          target.get('coverage_source'))
    if coverage_hold:
        return {'ok': False, 'error': 'NEEDS_LINKED_REVIEW', 'message': coverage_hold}

    from services import policy_undo
    try:
        receipt = await database.run_db(policy_undo.prepare, policy_id, field_label, target, page, live_val)
    except Exception:
        logger.exception('정책 적용 전 복원 기록 저장 실패')
        return {"ok": False, "error": "UNDO_RECORD_FAILED", "message": "적용 전 원본을 안전하게 저장하지 못해 Notion을 변경하지 않았습니다."}
    res = await notion_service.update_page_properties(target["page_id"], target["properties"])
    if not res.get("ok"):
        await database.run_db(policy_undo.mark, receipt, 'uncertain' if res.get('error') == 'UPDATE_UNCERTAIN' else 'failed')
        return res
    try:
        for field in pd.get('fields', []):
            if field.get('field') == field_label:
                field['undo_id'] = receipt
                field['undo_before'] = ', '.join(live_val) if isinstance(live_val, list) else str(live_val if live_val is not None else '')
        await database.run_db(policy_undo.mark, receipt, 'applied')
        remaining = await database.run_db(
            _finalize_apply, policy_id, field_label, pd, target["fkey"], target["py_value"])
    except Exception as e:
        logger.exception(f"[정책 최신화] Notion 반영 후 로컬 상태 저장 실패 #{policy_id}: {e}")
        return {
            "ok": False,
            "error": "LOCAL_SAVE_FAILED",
            "message": "Notion 반영은 완료됐지만 앱의 확인 기록을 저장하지 못했습니다. 다시 적용하지 말고 반영 이력과 Notion의 현재 내용을 확인해주세요.",
            "receipt": receipt,
        }
    return {"ok": True, "remaining": remaining, "completed": remaining == 0}


async def undo_policy_field(policy_id: int, field: str, receipt: str) -> Dict[str, Any]:
    if not await acquire_policy_mutation_lock(f"정책 적용 취소 #{policy_id}"):
        return {"ok": False, "error": "MAINTENANCE_BUSY", "message": maintenance_busy_message()}
    try:
        async with _get_policy_apply_lock(policy_id):
            from services import policy_undo
            try:
                return await policy_undo.undo(policy_id, field, receipt)
            except Exception:
                logger.exception('정책 적용 취소 실패')
                return {"ok": False, "error": "LOCAL_SAVE_FAILED", "message": "취소 결과를 확인하지 못했습니다. 재시도하지 말고 Notion 내용을 확인해주세요."}
    finally:
        release_maintenance_lock()


async def set_status_locked(policy_id: int, status: str) -> Dict[str, Any]:
    """(2026-08-07, GPT Codex 지적) set_status()가 필드 적용과 같은 정책별 락을 안 타서,
    "필드 적용 진행 중에 처리 완료를 누르면 그 처리 완료가 pending_diff=NULL로 만든 걸
    적용 작업이 자기가 예전에 읽은 pending_diff로 다시 덮어써 되살릴 수 있는" 경합이
    남아있었다. apply_field_to_notion()과 동일한 정책별 락을 사용해 상호 배제한다.

    (2026-08-28, ②) 유지보수 작업(동기화/점검/복원 등) 진행 중에도 거부한다 — 이유는
    apply_field_to_notion()과 동일(동기화가 갱신 중인 notion_fields/pending_diff와 충돌 위험)."""
    if not await acquire_policy_mutation_lock(f"정책 상태 변경 #{policy_id}"):
        return {"ok": False, "error": "MAINTENANCE_BUSY",
                "message": maintenance_busy_message()}
    try:
        async with _get_policy_apply_lock(policy_id):
            try:
                await database.run_db(set_status, policy_id, status)
            except LookupError as e:
                return {"ok": False, "error": "NOT_FOUND", "message": str(e)}
            except Exception as e:
                logger.exception(f"[정책 최신화] 상태 저장 실패 #{policy_id}: {e}")
                return {"ok": False, "error": "LOCAL_SAVE_FAILED",
                        "message": "처리 상태를 저장하지 못했습니다. 잠시 후 다시 시도해주세요."}
        return {"ok": True}
    finally:
        release_maintenance_lock()


def set_status(policy_id: int, status: str) -> None:
    """변경 확인(confirmed)/무시(dismissed) 처리. confirm 시 스냅샷 보존(⑥).

    직접 호출하지 말 것 — 정책별 락 없이 apply_field_to_notion()과 경합할 수 있다.
    API 엔드포인트에서는 항상 set_status_locked()를 거칠 것."""
    with database.get_connection() as conn:
        row = conn.execute(
            "SELECT page_id, notion_fields, pending_diff FROM notion_policies WHERE id=?",
            (policy_id,)).fetchone()
        if not row:
            raise LookupError(f"정책을 찾을 수 없습니다: {policy_id}")
        if status == "confirmed":
            # 이력과 상태 변경은 한 트랜잭션이다. 둘 중 하나라도 실패하면 성공 처리하지 않는다.
            conn.execute(
                "INSERT INTO welfare_item_snapshots (item_link, snapshot_data, change_summary) "
                "VALUES (?, ?, ?)",
                (f"notion:{row['page_id']}", row["notion_fields"] or "{}", "정책 변경 확인"))
        cursor = conn.execute(
            "UPDATE notion_policies SET change_status=?, pending_diff=NULL WHERE id=?",
            (status, policy_id))
        if cursor.rowcount != 1:
            raise RuntimeError(f"정책 상태를 저장하지 못했습니다: {policy_id}")
        conn.commit()
