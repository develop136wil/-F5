"""Notion 업로드 서비스 (REST API).

단발성 행사(event)를 '새 소식' 갤러리 DB에 페이지로 생성한다.
- 속성: 이름(title)/기관(text)/선택(select)/기간(date)
- 커버: 포스터 이미지(external)
- 본문: callout { 📌 홈페이지(bookmark) + 📝 관련 홍보물(포스터 image 또는 텍스트) }

커넥터 마크다운(NFM)으로는 북마크/이미지가 정확히 안 되므로 REST 블록으로 직접 구성한다.
토큰/DB ID는 config.json(NOTION_TOKEN, NOTION_DATABASE_ID)에서 읽는다.
"""
import os
import re
import json
import asyncio
import datetime
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import aiohttp

import utils
from services.logger import setup_logger

logger = setup_logger(__name__)

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# 사이트 → 기관 표시명 매핑 (Notion '기관' 속성용)
_SITE_ORG = [
    ("doccic", "도봉구 육아종합지원센터"),
    ("familynet", "도봉구 가족센터"),
    ("가족센터", "도봉구 가족센터"),
    ("seoul-i", "서울아이발달지원센터"),
    ("health.dobong", "도봉구보건소"),
    ("도봉보건소", "도봉구보건소"),
    ("dobong.go.kr", "도봉구"),
    ("dbwc2017", "도봉장애인종합복지관"),
    ("dbsw", "도봉서원노인종합복지관"),
    ("changdong21", "창동복지관"),
    ("sebumo", "서울특별시 발달장애인지원센터"),
    ("idolbom", "아이돌봄"),
    ("sen.go.kr", "서울시교육청"),
]


def get_credentials() -> Tuple[str, str]:
    """(token, database_id) 반환. 미설정이면 빈 문자열."""
    token = os.getenv("NOTION_TOKEN", "").strip()
    db_id = normalize_notion_id(os.getenv("NOTION_DATABASE_ID", ""))
    return token, db_id


def get_policy_dbs() -> List[Dict[str, str]]:
    """정책 최신화 대상 보관함 연결 목록 로드.

    config.json의 NOTION_POLICY_DBS(JSON 문자열)를 파싱.
    각 원소: {key, name, data_source_id, category}. 미설정이면 빈 리스트.
    """
    import json
    raw = os.getenv("NOTION_POLICY_DBS", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            out = []
            for d in data:
                if isinstance(d, dict) and d.get("data_source_id"):
                    out.append({**d, "data_source_id": normalize_notion_id(str(d["data_source_id"]))})
            return out
    except (json.JSONDecodeError, TypeError):
        logger.warning("[Notion] NOTION_POLICY_DBS JSON 파싱 실패")
    return []


def get_policy_db(db_key: str) -> Optional[Dict[str, str]]:
    """db_key로 정책 DB 설정 1건 조회."""
    for d in get_policy_dbs():
        if d.get("key") == db_key:
            return d
    return None


def is_configured() -> bool:
    token, db_id = get_credentials()
    return bool(token and db_id)


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def site_to_org(site_name: str, contact: str = "") -> str:
    """기관 표시명 결정: 사이트 매핑 우선, 없으면 contact(기관명스러운 경우), 없으면 사이트명."""
    s = site_name or ""
    for key, org in _SITE_ORG:
        if key in s:
            return org
    c = (contact or "").strip()
    if c and any(t in c for t in ("센터", "구청", "보건소", "복지관", "재단", "교육청", "주민센터")) \
            and not re.search(r"\d{2,}-\d", c):
        return c
    return s


def clean_title(title: str) -> str:
    """제목 정리: 선두 [분류] 접두사 제거 + 공백 정리."""
    t = (title or "").strip()
    t = re.sub(r"^\[[^\]]{1,20}\]\s*", "", t)   # '[가정양육지원/방학센터] ' 류 제거
    return t.strip()


def _norm_date(v: str) -> str:
    """YYYY-MM-DD 부분만 추출."""
    if not v:
        return ""
    m = re.search(r"(\d{4})[-.\/](\d{1,2})[-.\/](\d{1,2})", str(v))
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def compute_period(item: Dict[str, Any]) -> Tuple[str, str]:
    """Notion '기간' 표시용 날짜 = **마감일**(단일 날짜).

    마감일 > 행사종료일 > 행사시작일 순으로 사용. (end는 항상 '')
    """
    from services.news_timing import timing_review_note
    if timing_review_note(item):
        return "", ""
    d = (_norm_date(item.get("deadline"))
         or _norm_date(item.get("event_end_date"))
         or _norm_date(item.get("event_date")))
    return d, ""


def compute_status(item: Dict[str, Any]) -> str:
    """선택(상태) 계산: 진행 예정 / 진행 중 / 종료.

    표시 기준일(compute_period = 마감>종료>시작)이 오늘보다 과거면 '종료'.
    → 마감일이 없고 시작일만 과거인 옛 행사도 종료로 처리(후보에서 제외).
    """
    from services.news_timing import timing_review_note
    if timing_review_note(item):
        return "기간 확인 필요"
    today = datetime.date.today()
    today_s = today.isoformat()
    ref, _ = compute_period(item)
    if ref and ref < today_s:
        return "종료"
    # 날짜 파싱이 안 됐어도, 제목·날짜필드의 연도가 모두 작년 이하면 종료(옛 공고)
    blob = f"{item.get('title','')} {item.get('event_date','')} {item.get('deadline','')} {item.get('event_end_date','')}"
    years = [int(y) for y in re.findall(r'(20\d\d)', blob)]
    if years and max(years) < today.year:
        return "종료"
    start = _norm_date(item.get("event_date"))
    if start and start > today_s:
        return "진행 예정"
    return "진행 중"


_EVENT_TEXT_CHUNK_UNITS = 1900
_EVENT_ARRAY_LIMIT = 100
_EVENT_BLOCK_LIMIT = 1000
_EVENT_PAYLOAD_BYTE_LIMIT = 500_000  # Conservative decimal 500 KB, not 500 KiB.


class EventPayloadError(ValueError):
    """A local, definitive failure before a Notion create/update request."""
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _event_payload_too_large():
    return EventPayloadError(
        'NOTION_PAYLOAD_TOO_LARGE',
        '내용이 Notion 한 건의 저장 한도를 넘어 전송하지 않았습니다. '
        '내용을 자르지 않았으니 소식 모음 파일로 저장하거나 원문을 확인해주세요.')


def _utf16_units(text: str) -> int:
    try:
        return len(text.encode('utf-16-le')) // 2
    except (AttributeError, UnicodeEncodeError):
        raise EventPayloadError('NOTION_PAYLOAD_INVALID',
            '저장할 문자의 형식을 확인하지 못해 Notion에 전송하지 않았습니다.') from None


def _rt(content: str) -> List[Dict[str, Any]]:
    """Preserve all text, counting astral characters as two UTF-16 units."""
    _utf16_units(content)
    pieces, start, units = [], 0, 0
    for index, char in enumerate(content):
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > _EVENT_TEXT_CHUNK_UNITS:
            pieces.append(content[start:index])
            start, units = index, 0
        units += width
    pieces.append(content[start:])
    return [{"type": "text", "text": {"content": piece}} for piece in pieces]


def validate_event_payload(payload: Dict[str, Any]) -> None:
    """Validate a one-write payload locally; never trim or append remotely.

    aiohttp's default JSON serializer is json.dumps (ensure_ascii=True). Measure
    that exact serialization, including escaped Korean/astral characters.
    """
    block_count = 0

    def inspect(value, key=''):
        nonlocal block_count
        if key == 'url':
            if not isinstance(value, str):
                raise EventPayloadError('NOTION_PAYLOAD_INVALID',
                    '원문 또는 포스터 주소의 형식을 확인해주세요. Notion에는 전송하지 않았습니다.')
            if _utf16_units(value) > 2000:
                raise _event_payload_too_large()
            try:
                parsed = urlparse(value)
                valid = (parsed.scheme in ('http', 'https') and bool(parsed.hostname)
                         and parsed.username is None and parsed.password is None
                         and not any(char.isspace() or ord(char) < 32 for char in value))
                parsed.port  # Validate malformed/out-of-range ports locally too.
            except ValueError:
                valid = False
            if not valid:
                raise EventPayloadError('NOTION_PAYLOAD_INVALID',
                    '원문 또는 포스터 주소의 형식을 확인해주세요. Notion에는 전송하지 않았습니다.')
        if isinstance(value, list):
            if len(value) > _EVENT_ARRAY_LIMIT:
                raise _event_payload_too_large()
            if key == 'children':
                block_count += len(value)
                if block_count > _EVENT_BLOCK_LIMIT:
                    raise _event_payload_too_large()
            for child in value:
                inspect(child)
        elif isinstance(value, dict):
            for name, child in value.items():
                inspect(child, name)
        elif isinstance(value, str):
            units = _utf16_units(value)
            if key == 'content' and units > 2000:
                raise _event_payload_too_large()

    inspect(payload)
    try:
        encoded = json.dumps(payload, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise EventPayloadError('NOTION_PAYLOAD_INVALID',
            '저장할 내용의 형식을 확인하지 못해 Notion에 전송하지 않았습니다.') from None
    if len(encoded) > _EVENT_PAYLOAD_BYTE_LIMIT:
        raise _event_payload_too_large()


def _text_blocks(support_detail: str) -> List[Dict[str, Any]]:
    """support_detail(마크다운류)을 Notion 블록 리스트로 변환."""
    if support_detail is not None and not isinstance(support_detail, str):
        raise EventPayloadError('NOTION_PAYLOAD_INVALID',
            '저장할 본문의 형식을 확인하지 못해 Notion에 전송하지 않았습니다.')
    blocks = []
    for line in (support_detail or "").splitlines():
        s = line
        if s.startswith("### "):
            kind, content = 'heading_3', s[4:]
        elif s.startswith("## "):
            kind, content = 'heading_3', s[3:]
        elif s.startswith(("- ", "* ")):
            kind, content = 'bulleted_list_item', s[2:]
        else:
            kind, content = 'paragraph', s
        rich_text = _rt(content)
        for index in range(0, len(rich_text), _EVENT_ARRAY_LIMIT):
            blocks.append({'type': kind, kind: {
                'rich_text': rich_text[index:index + _EVENT_ARRAY_LIMIT]}})
            if len(blocks) > _EVENT_BLOCK_LIMIT:
                raise _event_payload_too_large()
    if not blocks:
        blocks.append({"type": "paragraph", "paragraph": {"rich_text": _rt("내용 없음")}})
    return blocks


def build_children(item: Dict[str, Any], poster_url: str) -> List[Dict[str, Any]]:
    """한 생성 요청 안의 callout들에 홈페이지와 홍보물을 빠짐없이 담는다."""
    inner: List[Dict[str, Any]] = []
    from services.news_display_projection import item_presentation
    display = item_presentation(item)
    # Explicit target-result imports keep review warnings in the exported page,
    # including the poster-only layout. Existing automatic gallery layout stays unchanged.
    ai = item.get('ai_data')
    if isinstance(ai, str):
        try:
            ai = json.loads(ai)
        except (ValueError, TypeError):
            ai = {}
    from services.news_timing import timing_review_note
    timing_note = timing_review_note(item)
    if (isinstance(ai, dict) and type(ai.get('news_import_review')) is bool) or timing_note:
        from services.page_coverage import input_note
        note = input_note(ai) if isinstance(ai, dict) else ''
        attachments = ai.get('source_attachments') if isinstance(ai, dict) else None
        if isinstance(attachments, dict) and attachments.get('status') == 'review_required':
            note += ' ' + str(attachments.get('note') or '첨부파일을 직접 확인해주세요.')[:1000]
        if timing_note:
            note += ' ' + timing_note
        elif not compute_period(item)[0]:
            note += ' 신청 가능 여부와 기간은 원문에서 확인해주세요.'
        if note.strip():
            inner.append({'type':'paragraph', 'paragraph':{'rich_text':_rt('원문 확인 필요: ' + note.strip())}})
    inner.append({"type": "paragraph",
                  "paragraph": {"rich_text": [{"type": "text", "text": {"content": "📌  홈페이지"},
                                               "annotations": {"bold": True}}]}})
    link = (item.get("link") or "").strip()
    if link.startswith("http"):
        inner.append({"type": "bookmark", "bookmark": {"url": link}})
    inner.append({"type": "paragraph",
                  "paragraph": {"rich_text": [{"type": "text", "text": {"content": "📝 관련 홍보물"},
                                               "annotations": {"bold": True}}]}})
    if poster_url:
        # 포스터가 있으면 포스터만 (사용자 규칙)
        inner.append({"type": "image", "image": {"type": "external", "external": {"url": poster_url}}})
    else:
        if item.get('support_detail') is not None and not isinstance(item['support_detail'], str):
            raise EventPayloadError('NOTION_PAYLOAD_INVALID',
                '저장할 본문의 형식을 확인하지 못해 Notion에 전송하지 않았습니다.')
        if display['source_note']:
            inner.append({'type': 'paragraph', 'paragraph': {'rich_text': _rt(display['source_note'])}})
        inner.extend(_text_blocks(display['support_detail'] or '자세한 내용은 원문에서 확인해주세요.'))
        # Only stored item fields, not free-form AI metadata. Keep repetitions:
        # the same condition in two sections can carry different context.
        for field, label in (('eligibility_detail', '참여 조건'), ('contact', '문의처')):
            value = item.get(field) or ''
            if value:
                inner.append({'type': 'heading_3', 'heading_3': {'rich_text': _rt(label)}})
                inner.extend(_text_blocks(value))

    # Multiple callouts are siblings in the SAME create request. No tail is
    # discarded and there is no additional append request / partial page.
    return [{'type': 'callout', 'callout': {
        'icon': {'type': 'emoji', 'emoji': '📢'}, 'rich_text': _rt(''),
        'children': inner[index:index + _EVENT_ARRAY_LIMIT],
    }} for index in range(0, len(inner), _EVENT_ARRAY_LIMIT)]


def build_payload(item: Dict[str, Any], poster_url: str, database_id: str) -> Dict[str, Any]:
    start, end = compute_period(item)
    status = compute_status(item)
    title = clean_title(item.get("title", ""))
    org = site_to_org(item.get("site_name", ""), item.get("contact", ""))

    props: Dict[str, Any] = {
        "이름": {"title": [{"text": {"content": title[:200]}}]},
        "기관": {"rich_text": [{"text": {"content": org[:200]}}]},
        "선택": {"select": {"name": status}},
    }
    ai = item.get('ai_data')
    if isinstance(ai, str):
        try:
            ai = json.loads(ai)
        except (ValueError, TypeError):
            ai = {}
    if status == '기간 확인 필요' or (
            not start and isinstance(ai, dict) and type(ai.get('news_import_review')) is bool):
        # Unknown timing is not evidence of an active application period.
        # Leave the existing select empty; do not create template options.
        props.pop('선택', None)
    if start:
        date_val: Dict[str, Any] = {"start": start}
        if end:
            date_val["end"] = end
        props["기간"] = {"date": date_val}

    payload: Dict[str, Any] = {
        "parent": {"database_id": database_id},
        "properties": props,
        "children": build_children(item, poster_url),
    }
    if poster_url:
        payload["cover"] = {"type": "external", "external": {"url": poster_url}}
    validate_event_payload(payload)
    return payload


_NOTION_RETRY_LIMIT = 1
_NOTION_RETRY_MAX_SECONDS = 30.0


def _retry_after_seconds(response, attempt: int) -> float:
    """Notion Retry-After(초)를 존중하되 데스크톱 작업이 무한 대기하지 않게 상한을 둔다."""
    raw = str(getattr(response, "headers", {}).get("Retry-After", "")).strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = min(2 ** (attempt + 1), 8)
    return max(0.1, min(value, _NOTION_RETRY_MAX_SECONDS))


def _notify_rate_wait(callback, seconds: float) -> None:
    if callback:
        try:
            callback(seconds)
        except Exception:
            logger.debug("[Notion] rate-limit 상태 콜백 실패", exc_info=True)


async def _get(session, url: str, token: str, version: str, on_rate_wait=None):
    hdr = dict(_headers(token)); hdr["Notion-Version"] = version
    for attempt in range(_NOTION_RETRY_LIMIT + 1):
        async with session.get(url, headers=hdr, timeout=aiohttp.ClientTimeout(total=20)) as r:
            status, data = r.status, await r.json()
            if status != 429 or attempt >= _NOTION_RETRY_LIMIT:
                return status, data
            delay = _retry_after_seconds(r, attempt)
        _notify_rate_wait(on_rate_wait, delay)
        logger.info(f"[Notion] 요청 한도 대기 {delay:g}초 후 재시도")
        await asyncio.sleep(delay)
    return 429, {"message": "Notion 요청 한도에 도달했습니다."}


def _title_of(data: dict) -> str:
    return "".join(t.get("plain_text", "") for t in data.get("title", [])) or "(제목없음)"


def normalize_notion_id(value: str) -> str:
    """Notion의 ID 또는 복사한 전체 URL에서 32자리 ID를 추출해 하이픈 없이 반환."""
    raw = (value or "").strip()
    pattern = r"(?i)([0-9a-f]{8}(?:-?[0-9a-f]{4}){3}-?[0-9a-f]{12})"
    # Notion 표 주소에는 `?v=<보기 ID>`가 흔히 붙는다. 전체 문자열의 마지막 UUID를
    # 택하면 표 ID 대신 보기 ID가 저장되므로, URL일 때는 경로의 ID를 최우선으로 쓴다.
    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        path_matches = re.findall(pattern, parsed.path)
        if path_matches:
            return path_matches[-1].replace("-", "")
        query = parse_qs(parsed.query)
        for key in ("data_source_id", "database_id", "page_id", "p"):
            for candidate in query.get(key, []):
                found = re.search(pattern, candidate)
                if found:
                    return found.group(1).replace("-", "")
    matches = re.findall(pattern, raw)
    return matches[0].replace("-", "") if matches else raw.replace("-", "")


async def resolve_target(session, token: str, raw_id: str, on_rate_wait=None) -> Dict[str, Any]:
    """입력 ID(database 또는 data_source)를 자동 인식해 생성용 부모 정보 반환.

    {ok, kind('database'|'data_source'), id, version, title} 또는 {ok:False, error}.
    신 데이터소스 모델(2026 워크스페이스)에서는 database_id를 줘도 data_source_id로 변환.
    """
    raw_id = normalize_notion_id(raw_id)
    last_err, last_status = "", 0

    # 1) 신 API database 조회를 먼저 시도해 canonical data_source_id를 얻는다.
    # 같은 표를 database URL과 data_source ID로 각각 등록해도 이후 중복 판정이 가능해진다.
    try:
        st, data = await _get(session, f"{API_BASE}/databases/{raw_id}", token, "2025-09-03", on_rate_wait)
        if st == 200:
            ds = data.get("data_sources") or []
            if ds and ds[0].get("id"):
                return {"ok": True, "kind": "data_source", "id": ds[0]["id"],
                        "version": "2025-09-03", "title": _title_of(data)}
        last_err, last_status = data.get("message", f"HTTP {st}"), st
        if st == 429:
            return {"ok": False, "status": 429,
                    "error": "Notion 요청이 몰려 잠시 멈췄습니다. 잠시 후 다시 시도해주세요."}
    except Exception as e:
        last_err = str(e)

    # 2) 구 API database 조회 (레거시 단일 소스 DB 호환)
    try:
        st, data = await _get(session, f"{API_BASE}/databases/{raw_id}", token, NOTION_VERSION, on_rate_wait)
        if st == 200:
            return {"ok": True, "kind": "database", "id": raw_id,
                    "version": NOTION_VERSION, "title": _title_of(data)}
        last_err, last_status = data.get("message", last_err), st
        if st == 429:
            return {"ok": False, "status": 429,
                    "error": "Notion 요청이 몰려 잠시 멈췄습니다. 잠시 후 다시 시도해주세요."}
    except Exception as e:
        last_err = str(e)

    # 3) 입력이 data_source ID 였던 경우 (신 API)
    try:
        st, data = await _get(session, f"{API_BASE}/data_sources/{raw_id}", token, "2025-09-03", on_rate_wait)
        if st == 200:
            return {"ok": True, "kind": "data_source", "id": raw_id,
                    "version": "2025-09-03", "title": _title_of(data)}
        last_err, last_status = data.get("message", last_err), st
    except Exception:
        pass

    hint = ""
    if last_status == 404:
        hint = " — DB ID가 맞는지, 통합(dbdb)이 해당 DB '연결'에 추가됐는지 확인하세요"
    elif last_status == 401:
        hint = " — 토큰이 올바른지 확인하세요"
    return {"ok": False, "error": f"{last_err}{hint}"}


EVENT_PROPS: List[Tuple[str, str]] = [
    ("이름", "title"),
    ("기관", "rich_text"),
    ("선택", "select"),
    ("기간", "date"),
]


async def get_event_source_schema(session, token: str, target: Dict[str, Any]) -> Dict[str, Any]:
    """새소식 갤러리 업로드에 필요한 네 속성의 이름과 종류를 확인한다."""
    kind = target.get("kind")
    target_id = target.get("id")
    if kind == "data_source":
        url, version = f"{API_BASE}/data_sources/{target_id}", "2025-09-03"
    else:
        url, version = f"{API_BASE}/databases/{target_id}", NOTION_VERSION
    try:
        status, data = await _get(session, url, token, version)
    except Exception as exc:
        return {"ok": False, "error": f"새 소식 보관함 구성을 확인하지 못했습니다: {exc}"}
    if status != 200:
        return {"ok": False, "error": data.get("message") or f"Notion 구성 조회 실패(HTTP {status})"}
    properties = data.get("properties") or {}
    missing = [name for name, _expected in EVENT_PROPS if name not in properties]
    mismatches = []
    for name, expected in EVENT_PROPS:
        actual = (properties.get(name) or {}).get("type")
        if actual and actual != expected:
            mismatches.append({"name": name, "expected": expected, "actual": actual})
    return {"ok": not missing and not mismatches,
            "missing_properties": missing, "type_mismatches": mismatches}


async def verify_connection() -> Dict[str, Any]:
    """토큰/DB 접근과 새소식 갤러리 필수 속성을 함께 확인한다."""
    token, db_id = get_credentials()
    if not token:
        return {"ok": False, "error": "토큰 미설정"}
    if not db_id:
        return {"ok": False, "error": "DB ID 미설정"}
    try:
        async with aiohttp.ClientSession() as s:
            res = await resolve_target(s, token, db_id)
            if res.get("ok"):
                schema = await get_event_source_schema(s, token, res)
                if not schema.get("ok"):
                    missing = schema.get("missing_properties") or []
                    mismatches = schema.get("type_mismatches") or []
                    parts = []
                    if missing:
                        parts.append("없는 속성: " + ", ".join(missing))
                    if mismatches:
                        parts.append("종류 확인: " + ", ".join(item["name"] for item in mismatches))
                    return {"ok": False, "status": "schema_error", "title": res["title"],
                            "error": "새 소식 보관함 구성을 확인해주세요. " + " · ".join(parts),
                            **schema}
                return {"ok": True, "title": res["title"], "kind": res["kind"], **schema}
            return {"ok": False, "error": res.get("error")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def verify_policy_databases() -> Dict[str, Any]:
    """설정된 정책 DB를 실제 Notion API로 열어 접근 권한과 필수 속성을 검사한다.

    AI 호출 없이 메타데이터만 조회한다. 결과에는 토큰이나 전체 ID를 포함하지 않는다.
    """
    token, _ = get_credentials()
    configured = {db.get("key"): db for db in get_policy_dbs() if db.get("key")}
    dbs = list(configured.values())
    if not token:
        return {"ok": False, "error": "Notion 통합 토큰이 설정되지 않았습니다.", "items": []}
    if not dbs:
        return {"ok": False, "error": "사용할 정책 보관함을 1개 이상 등록해주세요.", "items": []}
    expected = [(name, ptype) for _, name, ptype in get_policy_props()]
    expected += [(name, "url") for name in get_policy_source_props()]
    items: List[Dict[str, Any]] = []
    resolved_sources: Dict[str, str] = {}

    async with aiohttp.ClientSession() as session:
        for db in dbs:
            raw_id = str(db.get("data_source_id") or "").strip()
            base = {"key": db.get("key", ""), "name": db.get("name") or db.get("key") or "정책 DB"}
            if not raw_id:
                items.append({**base, "ok": False, "status": "missing_id", "error": "ID가 입력되지 않았습니다."})
                continue
            try:
                target = await resolve_target(session, token, raw_id)
                if not target.get("ok"):
                    items.append({**base, "ok": False, "status": "access_error",
                                  "error": target.get("error") or "DB에 접근할 수 없습니다."})
                    continue

                canonical_id = normalize_notion_id(str(target.get("id") or ""))
                if canonical_id in resolved_sources:
                    first_name = resolved_sources[canonical_id]
                    items.append({**base, "ok": False, "status": "duplicate_source",
                                  "error": f"'{first_name}'과 같은 Notion 보관함입니다. 중복 연결을 삭제해주세요."})
                    continue
                resolved_sources[canonical_id] = base["name"]

                kind = target["kind"]
                url = f"{API_BASE}/data_sources/{target['id']}" if kind == "data_source" \
                    else f"{API_BASE}/databases/{target['id']}"
                status, metadata = await _get(session, url, token, target["version"])
                if status != 200:
                    items.append({**base, "ok": False, "status": "access_error",
                                  "error": metadata.get("message") or f"Notion HTTP {status}"})
                    continue

                props = metadata.get("properties", {}) or {}
                missing = [name for name, _ in expected if name not in props]
                type_mismatches = []
                for name, expected_type in expected:
                    actual_type = (props.get(name) or {}).get("type")
                    if actual_type and actual_type != expected_type:
                        type_mismatches.append({"name": name, "expected": expected_type, "actual": actual_type})
                schema_ok = not missing and not type_mismatches
                option_counts = {}
                for fkey, pname, ptype in get_policy_props():
                    if ptype not in ("select", "multi_select"):
                        continue
                    pdef = props.get(pname) or {}
                    options = (pdef.get(ptype) or {}).get("options") or []
                    option_counts[fkey] = len([o for o in options if o.get("name")])
                items.append({
                    **base,
                    "ok": schema_ok,
                    "status": "connected" if schema_ok else "schema_error",
                    "title": target.get("title") or base["name"],
                    "missing_properties": missing,
                    "type_mismatches": type_mismatches,
                    "option_counts": option_counts,
                })
            except Exception as e:
                items.append({**base, "ok": False, "status": "network_error",
                              "error": f"연결 확인 중 오류가 발생했습니다: {e}"})

    return {"ok": bool(items) and all(item.get("ok") for item in items),
            "checked": len(items), "items": items}


async def _post_page(session, token: str, payload: dict, version: str = NOTION_VERSION):
    hdr = dict(_headers(token)); hdr["Notion-Version"] = version
    async with session.post(f"{API_BASE}/pages", headers=hdr, json=payload,
                            timeout=aiohttp.ClientTimeout(total=40)) as r:
        return r.status, await r.json()


async def create_event_page(item: Dict[str, Any], poster_url: str = "") -> Dict[str, Any]:
    """Create once after local preflight; never retry an uncertain remote write."""
    token, db_id = get_credentials()
    if not token or not db_id:
        return {"ok": False, "error": "Notion 미설정"}
    sent, status = False, None
    try:
        # Run before session/target lookup too: oversized local text needs no
        # network request at all. Recheck after replacing the canonical parent.
        payload = build_payload(item, poster_url, db_id)
        async with aiohttp.ClientSession() as s:
            target = await resolve_target(s, token, db_id)
            if not target.get("ok"):
                return {"ok": False, "error": target.get("error")}

            if target["kind"] == "data_source":
                payload["parent"] = {"type": "data_source_id", "data_source_id": target["id"]}
            else:
                payload["parent"] = {"database_id": target["id"]}
            version = target["version"]
            validate_event_payload(payload)
            # 커버 = 포스터 이미지 그대로(build_payload에서 external로 지정). 갤러리 통일감·시각적으로 유리.
            sent = True
            status, data = await _post_page(s, token, payload, version=version)
            page_id = data.get('id') if isinstance(data, dict) else None
            if status == 200 and isinstance(page_id, str) and page_id.strip():
                return {"ok": True, "page_id": page_id}
            rejected = 400 <= status < 500 and status != 408
            if rejected:
                return {'ok': False, 'code': 'NOTION_CREATE_REJECTED',
                        'error': f'Notion이 등록을 거절했습니다(HTTP {status}). 보관함과 포스터 주소를 확인해주세요.',
                        'http_status': status, 'request_sent': True, 'uncertain': False}
            return {'ok': False, 'code': 'NOTION_CREATE_UNCERTAIN',
                    'error': 'Notion 등록 여부를 확인하지 못했습니다. 중복 방지를 위해 Notion의 현재 내용을 먼저 확인해주세요.',
                    'http_status': status, 'request_sent': True, 'uncertain': True}
    except EventPayloadError as exc:
        # Only local validation failures use this code; the caller may release
        # its claim only when a create request provably was NOT attempted.
        return {'ok': False, 'code': exc.code, 'error': str(exc),
                'request_sent': sent, 'uncertain': sent}
    except Exception as e:
        logger.error(f"[Notion] 생성 응답 확인 실패: {type(e).__name__}")
        return {'ok': False, 'code': 'NOTION_CREATE_UNCERTAIN' if sent else 'NOTION_CREATE_NOT_SENT',
                'error': ('Notion 등록 여부를 확인하지 못했습니다. 중복 방지를 위해 Notion의 현재 내용을 먼저 확인해주세요.'
                          if sent else 'Notion 연결을 확인하지 못해 등록 요청을 보내지 않았습니다.'),
                'request_sent': sent, 'uncertain': sent}


# ==================== 정책 최신화 (연결된 정책 보관함) ====================
#
# A안: Notion 속성명을 상수로 분리. 실제 워크스페이스 속성명이 다르면 여기만 고치면 됨.
# 첫 sync 시 실제 발견된 속성명을 로그로 남겨(_log_props) 불일치를 바로 교정할 수 있게 한다.
DS_VERSION = "2025-09-03"

# (내부 필드키, Notion 속성명, 타입) — welfare_items / ai_data 필드와 1:1 매핑
# 실제 워크스페이스 속성명은 띄어쓰기 포함(2026-07-02 sync 로그로 확인). 월령 속성은 "(개월)" 접미사 포함.
POLICY_PROPS: List[Tuple[str, str, str]] = [
    ("title", "사업명", "title"),
    ("category", "분류", "select"),
    ("residence", "거주지", "multi_select"),
    ("target_characteristics", "대상 특성", "multi_select"),
    ("life_cycle", "생애주기", "multi_select"),
    ("start_age_months", "시작 월령(개월)", "number"),
    ("end_age_months", "종료 월령(개월)", "number"),
    ("age_detail", "세부 연령", "rich_text"),
    ("target_income", "대상 소득 수준", "rich_text"),
    ("eligibility_detail", "추가 자격요건", "rich_text"),
    ("support_detail", "상세 지원 내용", "rich_text"),
    ("contact", "문의처", "rich_text"),
]
# 출처 URL 속성 (관련 홈페이지 1·2·3) — 변경 감지의 출처가 되는 핵심
POLICY_SOURCE_PROPS = ["관련 홈페이지 1", "관련 홈페이지 2", "관련 홈페이지 3"]

# (2026-07-30) 위 두 상수는 이 워크스페이스(도봉구) 기준 기본값이다. 다른 지자체가 같은
# 기능을 쓰려면 자기 Notion의 속성명이 다를 수 있는데, 그때마다 소스를 고쳐 재빌드해야
# 했다. NOTION_POLICY_PROP_MAP(config.json, JSON 문자열)에 {내부fkey: 실제속성명} 오버라이드를
# 넣으면 재빌드 없이 대응 가능 — 없으면 기존 하드코딩 기본값 그대로 동작(하위호환).
# 출처 URL 3개는 "_source_1"~"_source_3" 키로 오버라이드한다.
def _load_policy_prop_override() -> Dict[str, str]:
    """NOTION_POLICY_PROP_MAP을 파싱한 dict를 반환. 형식이 안 맞으면(문법은 유효한 JSON이지만
    dict가 아닌 경우 포함 — 예: "[]", "\"문자열\"", "123") 빈 dict로 안전하게 처리한다.

    (2026-08-05, GPT 리뷰 2차) 최상위가 dict여도 각 값이 문자열이 아니면(예: "title": ["정책명"])
    이후 그 값을 Notion 속성 딕셔너리 키로 쓰는 곳(`props.get(pname)`)에서
    "unhashable type: list" 등으로 죽는다 — 실제 재현 확인. 문자열이 아니거나 빈/공백
    문자열인 항목은 건너뛰어 애초에 잘못된 값이 하위로 전달되지 않게 한다."""
    raw = utils.get_env_value('NOTION_POLICY_PROP_MAP')
    if not raw:
        return {}
    try:
        override = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(override, dict):
        return {}
    return {k: v.strip() for k, v in override.items() if isinstance(v, str) and v.strip()}


def get_policy_props() -> List[Tuple[str, str, str]]:
    """실제 적용할 (내부키, Notion 속성명, 타입) 매핑. config 오버라이드 우선, 없으면 기본값."""
    override = _load_policy_prop_override()
    if not override:
        return POLICY_PROPS
    return [(fkey, override.get(fkey) or pname, ptype) for fkey, pname, ptype in POLICY_PROPS]


def get_policy_source_props() -> List[str]:
    """실제 적용할 출처 URL 속성명 3개. config 오버라이드 우선, 없으면 기본값."""
    override = _load_policy_prop_override()
    if not override:
        return POLICY_SOURCE_PROPS
    return [override.get(f'_source_{i + 1}') or p for i, p in enumerate(POLICY_SOURCE_PROPS)]


_PROPS_LOGGED = set()   # ds_id별 속성명 1회만 로깅
# url 속성에 "라벨 텍스트\n\nhttps://..." 형태로 설명이 섞여 저장된 경우 실제 URL만 추출
_URL_IN_TEXT_RE = re.compile(r"https?://\S+")


def _extract_prop(prop: Dict[str, Any], ptype: str):
    """Notion 속성 객체 → 파이썬 값 추출."""
    if not prop:
        return "" if ptype in ("title", "select", "rich_text") else ([] if ptype == "multi_select" else None)
    if ptype == "title":
        return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    if ptype == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    if ptype == "select":
        sel = prop.get("select")
        return sel.get("name", "") if sel else ""
    if ptype == "multi_select":
        return [o.get("name", "") for o in prop.get("multi_select", [])]
    if ptype == "number":
        return prop.get("number")
    if ptype == "url":
        return prop.get("url") or ""
    return ""


def extract_page_fields(page: Dict[str, Any]) -> Dict[str, Any]:
    """Notion 페이지 → {필드키: 값, _source_urls:[...], page_id, title} 구조로 정규화."""
    props = page.get("properties", {}) or {}
    out: Dict[str, Any] = {"page_id": page.get("id", "")}
    for fkey, pname, ptype in get_policy_props():
        out[fkey] = _extract_prop(props.get(pname), ptype)
    urls = []
    for pname in get_policy_source_props():
        u = _extract_prop(props.get(pname), "url")
        if u:
            # "라벨 텍스트\n\nhttps://..." 형태로 URL 앞에 설명이 붙은 경우도 추출
            m = _URL_IN_TEXT_RE.search(u)
            if m:
                candidate = m.group(0).strip()
                # (2026-08-05, GPT Codex 4차 리뷰) 관련 홈페이지 1·2·3에 같은 URL이
                # 중복 입력된 경우(실측: "희귀질환자 의료비 지원" 등) 순서를 유지한 채
                # 중복만 제거 — 안 그러면 정책별 비교 단계에서 같은 원문이 두 번 병합돼
                # AI 입력에 "새 원문\n\n---\n\n새 원문"처럼 중복 전달되어 토큰 낭비·분석
                # 품질 저하로 이어질 수 있다.
                if candidate and candidate not in urls:
                    urls.append(candidate)
    out["_source_urls"] = urls
    out["title"] = out.get("title", "") or "(제목없음)"
    return out


def _log_props(ds_id: str, page: Dict[str, Any]):
    """첫 페이지의 실제 속성명·타입을 1회 로깅 (A안: 필드명 불일치 교정용)."""
    if ds_id in _PROPS_LOGGED:
        return
    _PROPS_LOGGED.add(ds_id)
    props = page.get("properties", {}) or {}
    found = ", ".join(f"{n}({p.get('type','?')})" for n, p in props.items())
    logger.info(f"[정책 최신화] DS={ds_id[:8]}… 발견된 속성: {found}")
    expected = [p for _, p, _ in get_policy_props()] + get_policy_source_props()
    missing = [e for e in expected if e not in props]
    if missing:
        logger.warning(f"[정책 최신화] 기대 속성 누락(POLICY_PROPS 상수 확인 필요): {missing}")


def _schema_from_metadata(data: Dict[str, Any]) -> Dict[str, Any]:
    """Notion database/data_source 메타데이터에서 앱이 쓰는 선택지 스키마를 만든다."""
    props = data.get("properties", {}) or {}
    pname_to_fkey = {pname: fkey for fkey, pname, _ in get_policy_props()}
    out: Dict[str, Any] = {}
    for pname, pdef in props.items():
        fkey = pname_to_fkey.get(pname)
        if not fkey:
            continue
        ptype = pdef.get("type")
        if ptype in ("select", "multi_select"):
            opts = pdef.get(ptype, {}).get("options", [])
            out[fkey] = [o.get("name") for o in opts if o.get("name")]
    # (2026-07-20) 데이터소스 실제 제목도 같은 응답에 이미 들어있어 추가 API 비용 없이
    # 캐시해둔다 — 화면에 하드코딩된 이름 대신, 사용자가 실제로
    # Notion에 지은 DB 이름을 그대로 보여주기 위함(다른 DB 구성으로 써도 코드 수정 불필요).
    title = "".join(t.get("plain_text", "") for t in (data.get("title") or []))
    if title:
        out["_title"] = title
    return out


async def get_policy_source_schema(session, token: str, target: Dict[str, Any], on_rate_wait=None) -> Dict[str, Any]:
    """resolve_target 결과로 스키마 조회. 실패·필수 속성 이상을 빈 스키마와 구분한다."""
    kind = target.get("kind")
    source_id = target.get("id")
    version = target.get("version") or (DS_VERSION if kind == "data_source" else NOTION_VERSION)
    if kind not in ("data_source", "database") or not source_id:
        return {"ok": False, "schema": {}, "error": "정책 보관함 ID를 해석하지 못했습니다."}
    resource = "data_sources" if kind == "data_source" else "databases"
    try:
        status, data = await _get(
            session, f"{API_BASE}/{resource}/{source_id}", token, version, on_rate_wait)
    except Exception as e:
        return {"ok": False, "schema": {}, "error": f"스키마 조회 중 오류: {e}"}
    if status != 200:
        return {"ok": False, "schema": {},
                "error": data.get("message") or f"Notion 스키마 조회 실패(HTTP {status})"}
    props = data.get("properties", {}) or {}
    expected = [(name, ptype) for _, name, ptype in get_policy_props()]
    expected += [(name, "url") for name in get_policy_source_props()]
    missing = [name for name, _ptype in expected if name not in props]
    type_mismatches = []
    for name, expected_type in expected:
        actual_type = (props.get(name) or {}).get("type")
        if actual_type and actual_type != expected_type:
            type_mismatches.append({
                "name": name, "expected": expected_type, "actual": actual_type,
            })
    return {
        "ok": True,
        "schema": _schema_from_metadata(data),
        "missing_properties": missing,
        "type_mismatches": type_mismatches,
    }


async def query_policy_source_pages(session, token: str, target: Dict[str, Any], on_rate_wait=None) -> Dict[str, Any]:
    """정책 보관함 전체 페이지 조회. 실패/부분 조회와 정상적인 빈 보관함을 구분한다."""
    kind = target.get("kind")
    source_id = target.get("id")
    version = target.get("version") or (DS_VERSION if kind == "data_source" else NOTION_VERSION)
    if kind not in ("data_source", "database") or not source_id:
        return {"ok": False, "pages": [], "error": "정책 보관함 ID를 해석하지 못했습니다."}
    resource = "data_sources" if kind == "data_source" else "databases"
    hdr = dict(_headers(token)); hdr["Notion-Version"] = version
    url = f"{API_BASE}/{resource}/{source_id}/query"
    results: List[Dict[str, Any]] = []
    cursor = None
    for page_index in range(50):   # 안전 상한(50*100=5000페이지)
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        try:
            for attempt in range(_NOTION_RETRY_LIMIT + 1):
                async with session.post(url, headers=hdr, json=body,
                                        timeout=aiohttp.ClientTimeout(total=30)) as r:
                    status = r.status
                    data = await r.json()
                    if status != 429 or attempt >= _NOTION_RETRY_LIMIT:
                        break
                    delay = _retry_after_seconds(r, attempt)
                _notify_rate_wait(on_rate_wait, delay)
                logger.info(f"[정책 최신화] Notion 요청 한도 대기 {delay:g}초 후 재시도")
                await asyncio.sleep(delay)
        except Exception as e:
            return {"ok": False, "pages": [], "error": f"페이지 조회 중 오류: {e}"}
        if status != 200:
            error = data.get("message") or f"HTTP {status}"
            logger.warning(f"[정책 최신화] 보관함 조회 실패 {status}: {error}")
            return {"ok": False, "pages": [], "error": error, "status": status}
        if (not isinstance(data, dict) or not isinstance(data.get('results'), list)
                or type(data.get('has_more')) is not bool
                or any(not isinstance(pg, dict) or not pg.get('id') or not isinstance(pg.get('properties'), dict)
                       for pg in data.get('results', []))):
            return {'ok': False, 'pages': [], 'error': '보관함 조회 응답을 확인하지 못했습니다. 기존 로컬 자료를 유지합니다.'}
        pages = data['results']
        if pages:
            _log_props(source_id, pages[0])
        for pg in pages:
            results.append(extract_page_fields(pg))
        if not data.get("has_more"):
            return {"ok": True, "pages": results}
        cursor = data.get("next_cursor")
        if not cursor:
            return {"ok": False, "pages": [], "error": "다음 페이지 위치가 없어 전체 조회를 완료하지 못했습니다."}
        if page_index == 49:
            return {"ok": False, "pages": [], "error": "정책이 5,000건을 넘어 안전 상한에서 중단했습니다."}
    return {"ok": False, "pages": [], "error": "전체 페이지 조회를 완료하지 못했습니다."}


def _chunk_rich_text(text: str, chunk_size: int = 1900) -> List[Dict[str, Any]]:
    """Preserve complete text in bounded UTF-16 runs, including astral emoji.

    The full payload still needs its array/byte preflight before a write.
    """
    text = str(text)
    if not text:
        return []
    if type(chunk_size) is not int or not 2 <= chunk_size <= 2000:
        raise ValueError('Invalid rich-text chunk size')
    _utf16_units(text)
    pieces, start, units = [], 0, 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > chunk_size:
            pieces.append({"text": {"content": text[start:index]}})
            start, units = index, 0
        units += width
    pieces.append({"text": {"content": text[start:]}})
    return pieces


# (V4) 상세지원내용의 섹션 헤더 감지 — policy_sync._is_header_line과 동일 기준(순환 import
# 방지를 위해 이 파일에 독립 정의). 리스트 마커(•◦ 등)는 헤더가 아니라 본문 항목이므로 제외.
_LIST_MARKER_CHARS = set('•◦‣▪▫·-–—*○●')


def _is_emoji_start(ch: str) -> bool:
    """실제 이모지/픽토그램 범위만 좁게 판정 (한글 등 일반 문장이 헤더로 오인되지 않도록)."""
    cp = ord(ch)
    return (
        0x2600 <= cp <= 0x27BF or
        0x1F300 <= cp <= 0x1FAFF or
        0x2190 <= cp <= 0x21FF or
        0x2B00 <= cp <= 0x2BFF or
        cp in (0x2757, 0x2753, 0x203C, 0x2049)
    )


def _is_bold_header_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if s.startswith('#'):
        return len(s) <= 40
    first = s[0]
    if first in _LIST_MARKER_CHARS:
        return False
    return _is_emoji_start(first) and len(s) <= 25


def _richtext_from_sections(text: str) -> List[Dict[str, Any]]:
    """(V4) 상세지원내용을 헤더 줄만 볼드(annotations.bold)로 표시하는 rich_text 런 배열로 변환.
    '#'/'##'/'###' 마크다운 기호는 Notion 속성에서 문자 그대로 보이므로 절대 남기지 않고,
    (과거 추출값에 남아있을 수 있는 것까지) 방어적으로 제거한다."""
    lines = str(text or "").splitlines()
    runs: List[Dict[str, Any]] = []
    for idx, raw in enumerate(lines):
        line = raw.strip()
        suffix = "\n" if idx < len(lines) - 1 else ""
        if not line:
            if suffix:
                runs.append({"text": {"content": "\n"}})
            continue
        is_header = _is_bold_header_line(line)
        clean = re.sub(r'^#+\s*', '', line)
        entry = {"text": {"content": clean + suffix}}
        if is_header:
            entry["annotations"] = {"bold": True}
        runs.append(entry)

    # Ordinary adjacent lines share the same annotation. Pack them before
    # splitting so hundreds of short lines do not consume hundreds of runs.
    # Never merge across a header/body boundary or drop a line to fit an array.
    packed: List[Dict[str, Any]] = []
    for run in runs:
        if packed and packed[-1].get('annotations') == run.get('annotations'):
            packed[-1]['text']['content'] += run['text']['content']
        else:
            packed.append(run)

    # UTF-16 safety split, keeping exactly the existing header annotations.
    chunked: List[Dict[str, Any]] = []
    for run in packed:
        content = run["text"]["content"]
        ann = run.get("annotations")
        for piece in _chunk_rich_text(content):
            if ann:
                piece["annotations"] = dict(ann)
            chunked.append(piece)
    return chunked or [{"text": {"content": ""}}]


def _build_prop_value(ptype: str, value, fkey: str = ""):
    """내부 값 → Notion 속성 객체 (create/update용). 빈 값은 None 반환(스킵)."""
    if ptype == "title":
        return {"title": [{"text": {"content": str(value)[:200]}}]} if value else None
    if ptype == "rich_text":
        if not value:
            return None
        if fkey == "support_detail":
            return {"rich_text": _richtext_from_sections(value)}
        return {"rich_text": _chunk_rich_text(value)}
    if ptype == "select":
        return {"select": {"name": str(value)[:100]}} if value else None
    if ptype == "multi_select":
        # (버그수정 2026-07-13) value가 None/빈 문자열이면 str(value)가 "None"이 되어
        # "None"이라는 실제 태그가 만들어지던 사고 — 다른 타입은 다 있던 빈값 가드가
        # 여기만 없었다. 단일 필드 적용(U1)이 build_policy_properties(전체 12속성 순회)를
        # 그대로 써서, 필드 1개만 적용해도 나머지 multi_select 3종이 "None" 태그로
        # PATCH에 끼어 들어가 기존 값을 덮어쓰는 사고로 이어졌다(실측 2026-07-13).
        if not value:
            return None
        vals = value if isinstance(value, list) else [v.strip() for v in str(value).split(",")]
        vals = [v for v in vals if v]
        return {"multi_select": [{"name": v[:100]} for v in vals]} if vals else None
    if ptype == "number":
        try:
            return {"number": float(value)} if value is not None and value != "" else None
        except (ValueError, TypeError):
            return None
    return None


def build_policy_properties(fields: Dict[str, Any]) -> Dict[str, Any]:
    """내부 필드 dict → Notion properties (초안 생성용). _source_urls는 관련홈페이지N에 매핑."""
    props: Dict[str, Any] = {}
    for fkey, pname, ptype in get_policy_props():
        pv = _build_prop_value(ptype, fields.get(fkey), fkey)
        if pv is not None:
            props[pname] = pv
    urls = fields.get("_source_urls") or []
    for i, pname in enumerate(get_policy_source_props()):
        if i < len(urls) and urls[i]:
            props[pname] = {"url": urls[i]}
    return props


async def get_page(page_id: str) -> Optional[Dict[str, Any]]:
    """(C1) 페이지 1건의 현재 속성을 실시간 조회. Notion에 적용하기 직전, 감지 당시 값과
    지금 값이 같은지 확인해 그 사이 사용자가 직접 고친 걸 덮어쓰지 않기 위해 쓴다."""
    token, _ = get_credentials()
    if not token:
        return None
    hdr = _headers(token); hdr["Notion-Version"] = DS_VERSION
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{API_BASE}/pages/{page_id}", headers=hdr,
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except Exception as e:
        logger.error(f"[정책 최신화] Notion 페이지 조회 예외: {e}")
        return None


def _policy_payload_rejection(error: EventPayloadError) -> Dict[str, Any]:
    message = ('정책 내용이 Notion 한 번의 저장 한도를 넘어 전송하지 않았습니다. '
               '내용은 자르지 않았으니 원문과 검토 내용을 확인해주세요.'
               if error.code == 'NOTION_PAYLOAD_TOO_LARGE' else
               '저장할 정책 내용·문자·주소의 형식을 확인하지 못해 Notion에 전송하지 않았습니다.')
    return {'ok': False, 'code': error.code, 'error': error.code, 'message': message,
            'request_sent': False, 'uncertain': False}


async def update_page_properties(page_id: str, properties: Dict[str, Any]) -> Dict[str, Any]:
    """(U1) 기존 Notion 페이지의 속성 일부를 PATCH로 업데이트. {ok, error}.

    build_policy_properties()로 만든 부분 속성(값 있는 필드만 포함)을 그대로 받아 반영한다.
    자동 실행이 아니라 사용자가 화면에서 명시적으로 "적용" 버튼을 눌렀을 때만 호출된다.
    """
    token, _ = get_credentials()
    if not token:
        return {"ok": False, "error": "Notion 토큰 미설정"}
    if not properties:
        return {"ok": False, "error": "반영할 속성이 없습니다"}
    payload = {"properties": properties}
    try:
        # A definite local rejection must not become UPDATE_UNCERTAIN or leave
        # a possible-write receipt. No session or PATCH has been attempted.
        validate_event_payload(payload)
    except EventPayloadError as exc:
        return _policy_payload_rejection(exc)
    hdr = _headers(token); hdr["Notion-Version"] = DS_VERSION
    status = None

    def uncertain():
        result = {"ok": False, "error": "UPDATE_UNCERTAIN",
                  "message": "Notion 반영 여부를 확인하지 못했습니다. 추가 반영을 멈췄으니 Notion의 현재 내용을 먼저 확인해주세요."}
        if status is not None:
            result["http_status"] = status
        return result

    try:
        async with aiohttp.ClientSession() as s:
            async with s.patch(f"{API_BASE}/pages/{page_id}", headers=hdr,
                               json=payload,
                               timeout=aiohttp.ClientTimeout(total=30)) as r:
                status = r.status
                try:
                    data = await r.json()
                except Exception:
                    # A received 4xx rejection (except request timeout) is
                    # definitive even when its error body is not valid JSON.
                    if 400 <= status < 500 and status != 408:
                        return {"ok": False, "error": f"HTTP {status}", "http_status": status}
                    return uncertain()
        if status == 408 or status >= 500:
            return uncertain()
        if status == 200:
            return {"ok": True} if isinstance(data, dict) else uncertain()
        message = data.get("message") if isinstance(data, dict) else None
        return {"ok": False, "error": str(message or f"HTTP {status}"), "http_status": status}
    except Exception as e:
        logger.error(f"[정책 최신화] Notion 업데이트 응답 확인 실패: {type(e).__name__}")
        return uncertain()


async def create_policy_page(fields: Dict[str, Any], source_id: str) -> Dict[str, Any]:
    """정책 초안 페이지 1건 생성. database/data_source ID 또는 전체 주소를 모두 받는다."""
    token, _ = get_credentials()
    if not token:
        return {"ok": False, "error": "Notion 토큰 미설정", "request_sent": False, "uncertain": False}
    sent = False
    try:
        # Fail locally before even resolving a remote target. Revalidate the
        # exact parent below, without creating an incomplete page or appending.
        payload = {"parent": {"database_id": normalize_notion_id(source_id)},
                   "properties": build_policy_properties(fields)}
        validate_event_payload(payload)
        async with aiohttp.ClientSession() as s:
            target = await resolve_target(s, token, source_id)
            if not target.get("ok"):
                return {"ok": False, "error": target.get("error") or "정책 보관함을 찾지 못했습니다.",
                        "request_sent": False, "uncertain": False}
            kind = target["kind"]
            parent_key = "data_source_id" if kind == "data_source" else "database_id"
            payload["parent"] = {"type": parent_key, parent_key: target["id"]}
            validate_event_payload(payload)
            sent = True
            status, data = await _post_page(s, token, payload, version=target["version"])
            if status == 200 and isinstance(data.get("id"), str) and data["id"]:
                return {"ok": True, "page_id": data.get("id", "")}
            return {"ok": False, "error": str(data.get("message", f"HTTP {status}")),
                    "status": status, "request_sent": True,
                    "uncertain": status not in (400, 401, 403, 404, 405, 422, 429)}
    except EventPayloadError as exc:
        return _policy_payload_rejection(exc)
    except Exception as e:
        logger.error(f"[정책 최신화] 초안 생성 예외: {e}")
        return {"ok": False, "error": str(e), "request_sent": sent, "uncertain": sent}


def page_deeplink(page_id: str) -> str:
    """Notion 페이지 딥링크 URL."""
    pid = (page_id or "").replace("-", "")
    return f"https://www.notion.so/{pid}" if pid else ""
