from typing import List, Optional
import database
from services.logger import setup_logger

logger = setup_logger(__name__)

# DB가 비어 있을 때 초기화에 쓰는 기본 키워드
DEFAULT_KEYWORDS: List[str] = [
    "영유아", "육아", "임신", "출산", "바우처", "신생아",
    "아동", "보육", "양육", "가족", "부모", "아이",
    "장난감", "건강", "예방접종", "난임", "산모", "돌봄"
]

DEFAULT_EXCLUDED_KEYWORDS: List[str] = [
    "[인사]", "인사발령", "승진", "전보",
    "[동정]", "동정",
    "[부고]", "부고", "별세",
    "[입찰결과]", "개찰결과", "낙찰자",
    "서버점검", "시스템점검", "작업공지",
    "행정처분", "고시공고", "반환", "회수",
    "결과발표", "선정결과",
    "수의계약", "계약체결", "설계공모"
]

# Keyword cache
_keyword_cache: Optional[List[str]] = None
# Excluded Keyword cache
_excluded_keyword_cache: Optional[List[str]] = None

def load_keywords() -> List[str]:
    """Load positive keywords from DB with caching."""
    global _keyword_cache
    if _keyword_cache is not None:
        return _keyword_cache

    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT keyword FROM keywords")
            rows = cursor.fetchall()

            if rows:
                _keyword_cache = [row['keyword'] for row in rows]
            else:
                # DB empty, initialize with defaults
                _keyword_cache = list(DEFAULT_KEYWORDS)
                save_keywords(_keyword_cache)

        return _keyword_cache
    except Exception as e:
        logger.warning(f"DB Load Keywords Error: {e}, using defaults")
        return list(DEFAULT_KEYWORDS)

def save_keywords(keywords: List[str]) -> None:
    """Save positive keywords to DB."""
    global _keyword_cache
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM keywords")
            for kw in keywords:
                cursor.execute("INSERT OR IGNORE INTO keywords (keyword) VALUES (?)", (kw,))
            conn.commit()
            _keyword_cache = keywords
    except Exception as e:
        logger.error(f"DB Save Keywords Error: {e}")
        raise

def load_excluded_keywords() -> List[str]:
    """Load excluded keywords from DB with caching."""
    global _excluded_keyword_cache
    if _excluded_keyword_cache is not None:
        return _excluded_keyword_cache
    
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT keyword FROM excluded_keywords")
            rows = cursor.fetchall()
        
            if rows:
                _excluded_keyword_cache = [row['keyword'] for row in rows]
            else:
                # DB empty, initialize with defaults
                _excluded_keyword_cache = list(DEFAULT_EXCLUDED_KEYWORDS)
                save_excluded_keywords(_excluded_keyword_cache)

        return _excluded_keyword_cache
    except Exception as e:
        logger.warning(f"DB Load Excluded Keywords Error: {e}, using defaults")
        return list(DEFAULT_EXCLUDED_KEYWORDS)

def save_excluded_keywords(keywords: List[str]) -> None:
    """Save excluded keywords to DB."""
    global _excluded_keyword_cache
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM excluded_keywords")
            for kw in keywords:
                cursor.execute("INSERT OR IGNORE INTO excluded_keywords (keyword) VALUES (?)", (kw,))
            conn.commit()
            _excluded_keyword_cache = keywords
    except Exception as e:
        logger.error(f"DB Save Excluded Keywords Error: {e}")
        raise

def add_excluded_keyword(keyword: str) -> None:
    """Add a single excluded keyword."""
    keywords = load_excluded_keywords()
    if keyword not in keywords:
        keywords.append(keyword)
        save_excluded_keywords(keywords)

def delete_excluded_keyword(keyword: str) -> None:
    """Delete a single excluded keyword."""
    keywords = load_excluded_keywords()
    if keyword in keywords:
        keywords.remove(keyword)
        save_excluded_keywords(keywords)

def clear_cache() -> None:
    """Clear keyword cache (call after external updates)."""
    global _keyword_cache, _excluded_keyword_cache
    _keyword_cache = None
    _excluded_keyword_cache = None

def is_relevant(text: str) -> bool:
    """Check if text contains any target keyword."""
    if not text:
        return False
    
    keywords = load_keywords()
    return any(keyword in text for keyword in keywords)

def get_matched_keywords(text: str, *, keywords=None) -> List[str]:
    """Return a list of keywords found in the text."""
    if not text:
        return []
    
    if keywords is None:
        keywords = load_keywords()
    matched = [keyword for keyword in keywords if keyword in text]
    return matched

def is_excluded(text: str, *, keywords=None) -> bool:
    """
    Check if text contains negative keywords (Strong noise filter).
    """
    if not text:
        return False
        
    negative_keywords = load_excluded_keywords() if keywords is None else keywords
    
    return any(keyword in text for keyword in negative_keywords)
