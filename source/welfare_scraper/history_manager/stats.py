"""요약/트렌드 통계, 키워드 추천, 마감 임박 항목."""
import re
from typing import List, Dict, Any
from datetime import date, datetime, timedelta
import database
from services.logger import setup_logger
from services import search_dates
from services.news_deadline_grounding import deadline_calendar_day
from .common import _parse_ai_data, _strip_blob

logger = setup_logger(__name__)

def get_summary_stats() -> Dict[str, Any]:
    """
    Returns aggregation stats for reports (including new_this_week and expiring_soon).
    """
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            
            stats = {}
            
            # Total count
            cursor.execute("SELECT COUNT(*) as count FROM welfare_items")
            row = cursor.fetchone()
            stats["total_items"] = row["count"] if row else 0

            # 최초 실행 안내의 자동 완료 판정용. welfare_items가 백업 복원/가져오기로
            # 존재하는 경우를 "직접 수집 완료"로 오인하지 않도록 성공 실행 이력을 따로 준다.
            cursor.execute("SELECT COUNT(*) as count FROM scrape_runs WHERE status = 'success'")
            row = cursor.fetchone()
            stats["successful_scrape_runs"] = row["count"] if row else 0
            
            # By Category
            cursor.execute("SELECT category, COUNT(*) as count FROM welfare_items GROUP BY category")
            stats["by_category"] = {row["category"]: row["count"] for row in cursor.fetchall()}
            
            # By Site
            cursor.execute("SELECT site_name, COUNT(*) as count FROM welfare_items GROUP BY site_name")
            stats["by_site"] = {row["site_name"]: row["count"] for row in cursor.fetchall()}
            
            # New this week
            today = datetime.now().strftime("%Y-%m-%d")
            week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            cursor.execute("SELECT COUNT(*) as count FROM welfare_items WHERE scraped_at >= ?", (week_ago,))
            row = cursor.fetchone()
            stats["new_this_week"] = row["count"] if row else 0

            # Previous week (for comparison)
            two_weeks_ago = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
            cursor.execute(
                "SELECT COUNT(*) as count FROM welfare_items WHERE scraped_at >= ? AND scraped_at < ?",
                (two_weeks_ago, week_ago)
            )
            row = cursor.fetchone()
            stats["new_prev_week"] = row["count"] if row else 0

            # New this week by category (for weekly category breakdown)
            cursor.execute(
                "SELECT category, COUNT(*) as count FROM welfare_items WHERE scraped_at >= ? GROUP BY category",
                (week_ago,)
            )
            stats["new_this_week_by_category"] = {row["category"]: row["count"] for row in cursor.fetchall()}

            # Active items count (deadline >= today OR deadline IS NULL)
            cursor.execute(
                "SELECT COUNT(*) as count FROM welfare_items WHERE (deadline >= ? OR deadline IS NULL) AND (is_archived IS NULL OR is_archived = 0)",
                (today,)
            )
            row = cursor.fetchone()
            stats["active_items"] = row["count"] if row else 0

            # Expiring soon (within 7 days)
            expiry_start, expiry_end = _expiring_window(7)
            cursor.execute(
                """SELECT deadline FROM welfare_items
                   WHERE deadline >= ? AND deadline < ?
                     AND (is_archived IS NULL OR is_archived = 0)""",
                (expiry_start, expiry_end)
            )
            stats["expiring_soon"] = sum(_valid_deadline(row['deadline']) for row in cursor)

            return stats
    except Exception as e:
        logger.error(f"DB Stats Error: {e}")
        raise


def get_trend_stats(days: int = 30) -> List[Dict[str, Any]]:
    """
    일별 수집 건수 트렌드를 반환 (최근 days일).
    반환 형식: [{"day": "2026-03-01", "count": 42}, ...]
    데이터가 없는 날짜는 포함되지 않음 (프론트엔드에서 0으로 채울 수 있음).
    """
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DATE(scraped_at) as day, COUNT(*) as count
                FROM welfare_items
                WHERE scraped_at >= date('now', ?)
                GROUP BY DATE(scraped_at)
                ORDER BY day ASC
            """, (f"-{days} days",))
            return [{"day": row["day"], "count": row["count"]} for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Trend Stats Error: {e}")
        raise



def get_keyword_suggestions(limit: int = 10) -> List[Dict[str, Any]]:
    """
    Analyzes titles of recent items to suggest new keywords.
    """
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            # Get last 200 titles
            cursor.execute("SELECT title, category FROM welfare_items ORDER BY scraped_at DESC LIMIT 200")
            rows = cursor.fetchall()
            
            from collections import Counter
            import re
            
            words = []
            stop_words = {"공고", "안내", "모집", "지원", "사업", "참여", "신청", "선발", "대상", "명단", "결과", "변경", "운영"}
            
            for row in rows:
                # Basic tokenization
                tokens = re.findall(r'\w+', row['title'])
                for token in tokens:
                    if len(token) > 1 and token not in stop_words and not token.isdigit():
                        words.append(token)
            
            # Count
            counter = Counter(words)
            
            # Filter out existing keywords? (Assuming frontend checks or we check here)
            # For now just return top freq
            most_common = counter.most_common(limit)
            return [{"keyword": w, "count": c} for w, c in most_common]
            
    except Exception as e:
        logger.error(f"Keyword Suggestion Error: {e}")
        raise


def _expiring_window(days):
    if type(days) is not int or days < 0:
        raise ValueError('Expiry days must be a nonnegative integer')
    today = date.fromisoformat(search_dates.today_kst())
    # Exclusive next-day boundary includes every supported time on day N,
    # unlike comparing a timestamp with the date-only inclusive upper bound.
    return today.isoformat(), (today + timedelta(days=days + 1)).isoformat()


def _valid_deadline(value):
    try:
        deadline_calendar_day(value)
    except ValueError:
        return False
    return True


def get_expiring_items(days: int = 7) -> List[Dict[str, Any]]:
    """한국 날짜 기준 오늘부터 days일째까지 마감되는 활성 항목. 저장값은 유지."""
    try:
        start, end = _expiring_window(days)
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM welfare_items
                WHERE deadline IS NOT NULL
                  AND deadline >= ?
                  AND deadline < ?
                  AND (is_archived IS NULL OR is_archived = 0)
                ORDER BY deadline ASC
            """, (start, end))
            items = []
            for row in cursor:
                if not _valid_deadline(row['deadline']):
                    continue  # Legacy free text/malformed dates are not facts.
                item = _strip_blob(dict(row))
                item['ai_data'] = _parse_ai_data(item.get('ai_data'))
                items.append(item)
            return items
    except Exception as e:
        logger.error(f"Get Expiring Items Error: {e}")
        raise
