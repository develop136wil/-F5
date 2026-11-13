"""제목 정규화/유사도 비교, ai_data 파싱 등 다른 서브모듈이 공유하는 헬퍼."""
import difflib
import json
import re

# 재공고·수정·마감연장 등 반복성 접두사 패턴
_TITLE_PREFIX_RE = re.compile(
    r'^[\[\(【「『\s]*(?:재공고|재모집|수정공고|변경공고|정정공고|수정|변경|정정|추가모집|추가공고|기간연장|마감연장|공고|안내|알림|긴급)\s*[\]\)】」』\s]*',
    re.IGNORECASE
)

def _normalize_title(title: str) -> str:
    """재공고·수정 등의 접두사를 제거하여 제목을 정규화."""
    if not title:
        return title
    return _TITLE_PREFIX_RE.sub('', title).strip() or title


def _is_similar_title(title1: str, title2: str, threshold: float = 0.90) -> bool:
    """두 제목의 유사도가 threshold(기본 90%) 이상인지 확인."""
    if not title1 or not title2:
        return False
    return difflib.SequenceMatcher(None, title1, title2).ratio() >= threshold


def _parse_ai_data(value) -> dict:
    """ai_data 필드의 JSON 문자열을 안전하게 파싱. 이미 dict이면 그대로 반환."""
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}


def _strip_blob(item: dict) -> dict:
    """(버그 2026-07-21) welfare_items.embedding은 float32 BLOB(bytes)인데, `SELECT *`로
    읽은 dict를 API가 그대로 JSON 응답하면 FastAPI의 인코더가 bytes를 직렬화하지 못해
    500 Internal Server Error가 난다. 임베딩이 채워진 항목이 늘어난 이후(이번 세션의
    수집·백필 작업) 처음 실측됨 — /api/data, /api/data/{id}, /api/search가 전부 이 경로로
    깨져 있었다. API로 나가는 항목 dict에서는 항상 이 필드를 제거한다(내부 코사인 비교는
    별도 함수(get_embedding_index 등)가 전용 쿼리로 직접 읽으므로 영향 없음)."""
    item.pop('embedding', None)
    return item

