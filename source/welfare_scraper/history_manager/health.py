"""사이트별 헬스체크, 스크랩 실행 이력, 대시보드 집계."""
from typing import List, Dict, Any, Optional
from datetime import datetime
import database
from services.logger import setup_logger

logger = setup_logger(__name__)

import aiohttp
import asyncio

# 동시 헬스체크 요청 수 제한: 대량 호출 시 네트워크 과부하 방지
_health_check_semaphore = asyncio.Semaphore(10)

async def check_link_health(link: str, session: aiohttp.ClientSession = None) -> bool:
    """
    Checks if a link is accessible (health check).
    Returns True if status is 200-299, False otherwise.
    최대 10개 동시 요청으로 제한.
    session이 제공되면 재사용, 없으면 내부 생성.
    """
    if not link:
        return False

    async with _health_check_semaphore:
        try:
            if session:
                async with session.head(link, allow_redirects=True) as response:
                    if response.status < 400:
                        return True
                    else:
                        logger.warning(f"Link Health Check Failed: {link} (Status: {response.status})")
                        return False
            else:
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as _session:
                    async with _session.head(link, allow_redirects=True) as response:
                        if response.status < 400:
                            return True
                        else:
                            logger.warning(f"Link Health Check Failed: {link} (Status: {response.status})")
                            return False
        except Exception as e:
            logger.warning(f"Link Health Check Error: {link} ({e})")
            return False



def get_sites_last_scraped(site_names: List[str]) -> Dict[str, Optional[datetime]]:
    """여러 사이트의 마지막 스크래핑 시각을 한 번의 쿼리로 조회.

    증분 스크래핑에서 사이트마다 get_site_last_scraped를 호출하던 N+1 패턴을 제거.
    반환: {site_name: datetime 또는 None} (요청한 모든 이름 포함).
    """
    result: Dict[str, Optional[datetime]] = {name: None for name in site_names}
    if not site_names:
        return result
    try:
        placeholders = ','.join('?' for _ in site_names)
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT name, last_scraped_at FROM sites WHERE name IN ({placeholders})",
                tuple(site_names),
            )
            for row in cursor.fetchall():
                if row['last_scraped_at']:
                    try:
                        result[row['name']] = datetime.fromisoformat(row['last_scraped_at'])
                    except (ValueError, TypeError):
                        pass
    except Exception as e:
        logger.error(f"Error fetching last scraped times: {e}")
        raise
    return result



def record_site_scrape(site_name: str) -> None:
    """현재 시각을 사이트의 last_scraped_at으로 기록 (증분 스크래핑용)."""
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE sites SET last_scraped_at = ? WHERE name = ?",
                (datetime.now().isoformat(), site_name)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Error recording scrape time for '{site_name}': {e}")



def get_health_stats() -> List[Dict[str, Any]]:
    """
    Returns health status per site (last scrape time, total items).
    """
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT site_name, COUNT(*) as total, MAX(scraped_at) as last_activity 
                FROM welfare_items 
                GROUP BY site_name
            ''')
            rows = cursor.fetchall()
            
            stats = []
            for row in rows:
                last_act = row['last_activity']
                status = "good"
                if not last_act:
                    status = "unknown"
                else:
                    # Parse ISO format
                    try:
                        dt = datetime.fromisoformat(last_act)
                        if (datetime.now() - dt).days > 7:
                            status = "warning" # No activity for 7 days
                    except (ValueError, TypeError):
                        pass

                stats.append({
                    "site_name": row['site_name'],
                    "total_items": row['total'],
                    "last_activity": last_act,
                    "status": status
                })
            return stats
    except Exception as e:
        logger.error(f"Health Stats Error: {e}")
        raise


def record_scrape_run(site_id: int = None, site_name: str = '',
                      status: str = 'success', items_found: int = 0,
                      items_saved: int = 0, error_message: str = None,
                      duration_sec: float = None) -> None:
    """사이트 스크래핑 실행 결과를 scrape_runs 테이블에 기록."""
    try:
        database.execute_write(
            """INSERT INTO scrape_runs (site_id, site_name, status, items_found, items_saved, error_message, duration_sec)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (site_id, site_name, status, items_found, items_saved, error_message, duration_sec)
        )
    except Exception as e:
        logger.error(f"Record Scrape Run Error: {e}")



def get_site_health_dashboard() -> List[Dict[str, Any]]:
    """모든 사이트의 건강 상태 대시보드 데이터 반환. 단일 쿼리로 처리."""
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            # 사이트 목록 + 30일 실행 통계 + 최근 실행(서브쿼리)을 단일 쿼리로 처리
            cursor.execute("""
                SELECT
                    s.id as site_id,
                    s.name as site_name,
                    s.url,
                    s.last_scraped_at,
                    COUNT(sr.id) as total_runs,
                    SUM(CASE WHEN sr.status = 'success' THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN sr.status = 'error' THEN 1 ELSE 0 END) as error_count,
                    AVG(sr.duration_sec) as avg_duration,
                    SUM(sr.items_found) as total_items_found,
                    SUM(sr.items_saved) as total_items_saved,
                    lr.status as last_run_status,
                    lr.error_message as last_run_error,
                    lr.created_at as last_run_at
                FROM sites s
                LEFT JOIN scrape_runs sr ON sr.site_id = s.id AND sr.created_at >= date('now', '-30 days')
                LEFT JOIN (
                    SELECT site_id, status, error_message, created_at,
                           ROW_NUMBER() OVER (PARTITION BY site_id ORDER BY created_at DESC) as rn
                    FROM scrape_runs
                ) lr ON lr.site_id = s.id AND lr.rn = 1
                GROUP BY s.id
                ORDER BY s.name
            """)
            results = []
            for row in cursor.fetchall():
                r = dict(row)
                total = r['total_runs'] or 0
                errors = r['error_count'] or 0
                success_rate = round((r['success_count'] or 0) / total * 100, 1) if total > 0 else None

                # 상태 판정
                status = 'unknown'
                if total == 0:
                    status = 'no_data'
                elif errors > 0 and success_rate is not None and success_rate < 50:
                    status = 'critical'
                elif errors > 0 and success_rate is not None and success_rate < 80:
                    status = 'warning'
                elif total > 0:
                    status = 'healthy'

                results.append({
                    'site_id': r['site_id'],
                    'site_name': r['site_name'],
                    'url': r['url'],
                    'last_scraped_at': r['last_scraped_at'],
                    'total_runs_30d': total,
                    'success_count': r['success_count'] or 0,
                    'error_count': errors,
                    'success_rate': success_rate,
                    'avg_duration_sec': round(r['avg_duration'] or 0, 1),
                    'total_items_found': r['total_items_found'] or 0,
                    'total_items_saved': r['total_items_saved'] or 0,
                    'status': status,
                    'last_run_status': r['last_run_status'],
                    'last_run_error': r['last_run_error'],
                    'last_run_at': r['last_run_at'],
                })
            return results
    except Exception as e:
        logger.error(f"Site Health Dashboard Error: {e}")
        raise



def get_site_scrape_history(site_id: int, limit: int = 30) -> List[Dict[str, Any]]:
    """특정 사이트의 최근 스크래핑 실행 이력 반환."""
    try:
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT id, site_name, status, items_found, items_saved,
                          error_message, duration_sec, created_at
                   FROM scrape_runs WHERE site_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (site_id, limit)
            )
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Site Scrape History Error: {e}")
        raise


