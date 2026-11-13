"""보존기간 초과/만료 데이터 정리, 자동 유지보수 오케스트레이션."""
import os
from typing import Dict
from datetime import datetime, timedelta
import database
import utils
from services.logger import setup_logger

logger = setup_logger(__name__)

# 기존 DB에도 빈값/설명문/잘못된 날짜가 있을 수 있다. 문자열 대소 비교만으로
# 만료 판정을 하지 않는다. +0 days는 SQLite의 잘못된 월말 날짜를 정규화하므로
# 원문과 일치하는 실제 ISO 날짜만 자동 숨김/삭제 대상으로 삼는다.
_VALID_DEADLINE_SQL = """(
    length(deadline) = 10
    AND deadline GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
    AND substr(deadline, 1, 4) >= '0001'
    AND date(deadline, '+0 days') = deadline
)"""

def cleanup_data(days_old: int = 90) -> int:
    """
    Deletes items older than days_old OR items expired for more than 30 days.
    Also cleans up old snapshots and scrape_runs records.
    Returns deleted count.
    """
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()

            cutoff_date = (datetime.now() - timedelta(days=days_old)).isoformat()
            expired_cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

            cursor.execute(f'''
                DELETE FROM welfare_items
                WHERE scraped_at < ?
                   OR ({_VALID_DEADLINE_SQL} AND deadline < ?)
            ''', (cutoff_date, expired_cutoff))

            deleted_count = cursor.rowcount

            # 스냅샷 정리: 90일 이상 된 스냅샷 삭제 + 항목당 최대 20개 유지
            snapshot_cutoff = (datetime.now() - timedelta(days=days_old)).isoformat()
            cursor.execute("DELETE FROM welfare_item_snapshots WHERE created_at < ?", (snapshot_cutoff,))
            snap_deleted = cursor.rowcount
            # 항목당 20개 초과 스냅샷 삭제 (오래된 것부터)
            cursor.execute("""
                DELETE FROM welfare_item_snapshots WHERE id IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (PARTITION BY item_link ORDER BY created_at DESC) as rn
                        FROM welfare_item_snapshots
                    ) WHERE rn > 20
                )
            """)
            snap_deleted += cursor.rowcount

            # scrape_runs 정리: 90일 이상 된 기록 삭제
            cursor.execute("DELETE FROM scrape_runs WHERE created_at < ?", (snapshot_cutoff,))
            runs_deleted = cursor.rowcount

            conn.commit()
            logger.info(f"Cleaned up {deleted_count} items, {snap_deleted} snapshots, {runs_deleted} scrape runs.")
            return deleted_count
    except Exception as e:
        logger.error(f"Cleanup Error: {e}")
        raise



def run_maintenance(retention_days: int = None, force_cleanup: bool = False) -> Dict[str, int]:
    """스크래핑 종료 시 자동 호출되는 유지보수 루틴.

    1) 마감 지난 항목을 아카이브(숨김) — 매번 실행(가볍고 가역적).
    2) 보존기간(retention_days) 초과 데이터 정리 — 하루 1회만 실행(파괴적이라 빈도 제한).

    retention_days: 기본 환경변수 DATA_RETENTION_DAYS(기본 365). 0이면 정리 비활성화.
    무한 누적을 막되 최근 데이터는 보존한다. config.json의 LAST_AUTO_CLEANUP으로 빈도 추적.
    """
    if retention_days is None:
        retention_days = utils.safe_config_int('DATA_RETENTION_DAYS', 365, 0, 3650)

    result = {"archived": 0, "cleaned": 0}
    try:
        result["archived"] = archive_expired_items()
    except Exception as e:
        logger.warning(f"maintenance archive 실패: {e}")

    try:
        today = datetime.now().strftime('%Y-%m-%d')
        last = utils.get_env_value('LAST_AUTO_CLEANUP')
        if retention_days > 0 and (force_cleanup or last != today):
            result["cleaned"] = cleanup_data(retention_days)
            utils.set_env_value('LAST_AUTO_CLEANUP', today)
    except Exception as e:
        logger.warning(f"maintenance cleanup 실패: {e}")

    if result["archived"] or result["cleaned"]:
        logger.info(f"🧹 [유지보수] 만료 아카이브 {result['archived']}건, 보존기간 정리 {result['cleaned']}건")
    return result



def archive_expired_items() -> int:
    """마감일이 지난 항목을 is_archived=1로 표시. 완전 삭제 없이 숨김 처리."""
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                UPDATE welfare_items
                SET is_archived = 1
                WHERE {_VALID_DEADLINE_SQL}
                  AND deadline < date('now', 'localtime')
                  AND (is_archived IS NULL OR is_archived = 0)
            """)
            count = cursor.rowcount
            conn.commit()
            logger.info(f"Archived {count} expired items.")
            return count
    except Exception as e:
        logger.error(f"Archive Expired Error: {e}")
        raise


