"""변경 이력 스냅샷 저장/조회, 항목 diff."""
import json
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import database
from services.logger import setup_logger
from .common import _parse_ai_data

logger = setup_logger(__name__)

def get_recently_changed_items(days: int = 14, limit: int = 100) -> List[Dict[str, Any]]:
    """최근 N일 내 변경(change_log 존재)된 항목 목록 — 'Notion 수정 필요' 검토용.

    마감일/제목/페이지수정일/내용 변경이 감지된 항목을 최신순으로 반환.
    각 항목에 last_change(가장 최근 변경 요약)와 content_changed 플래그 포함.
    """
    try:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT id, title, link, category, site_name, deadline,
                          last_updated, change_log
                   FROM welfare_items
                   WHERE change_log IS NOT NULL AND change_log != ''
                     AND last_updated >= ?
                     AND (is_archived IS NULL OR is_archived = 0)
                   ORDER BY last_updated DESC
                   LIMIT ?""",
                (cutoff, limit),
            )
            result = []
            for r in cursor.fetchall():
                d = dict(r)
                log = (d.get('change_log') or '').strip()
                d['last_change'] = log.split('\n')[-1] if log else ''
                d['content_changed'] = '내용 변경' in d['last_change']
                result.append(d)
            # 내용 변경(정책 변화)을 우선 노출, 그다음 최신순
            result.sort(key=lambda x: (not x['content_changed'], x.get('last_updated') or ''), reverse=False)
            return result
    except Exception as e:
        logger.error(f"get_recently_changed_items error: {e}")
        raise



def _save_snapshot(cursor, link: str, change_summary: str = None) -> None:
    """기존 항목의 현재 상태를 welfare_item_snapshots에 저장 (cursor 재사용)."""
    cursor.execute("SELECT * FROM welfare_items WHERE link = ?", (link,))
    row = cursor.fetchone()
    if not row:
        return
    snapshot = dict(row)
    # ai_data는 이미 JSON 문자열이므로 그대로 유지
    snapshot_json = json.dumps(snapshot, ensure_ascii=False, default=str)
    cursor.execute(
        "INSERT INTO welfare_item_snapshots (item_link, snapshot_data, change_summary) VALUES (?, ?, ?)",
        (link, snapshot_json, change_summary)
    )



def get_item_snapshots(item_link: str = None, item_id: int = None) -> List[Dict[str, Any]]:
    """항목의 변경 스냅샷 목록 반환 (최신순). item_id가 주어지면 link를 조회."""
    try:
        link = item_link
        if not link and item_id:
            row = database.execute_query("SELECT link FROM welfare_items WHERE id = ?", (item_id,), fetch_one=True, raise_on_error=True)
            if not row:
                return []
            link = row['link']
        if not link:
            return []

        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, item_link, change_summary, created_at FROM welfare_item_snapshots WHERE item_link = ? ORDER BY created_at DESC",
                (link,)
            )
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Get Snapshots Error: {e}")
        raise



def get_item_diff(item_id: int, snapshot_id: int) -> Optional[Dict[str, Any]]:
    """특정 스냅샷과 현재 상태를 비교하여 변경 내역(diff)을 반환."""
    try:
        from .items import get_item_by_id  # 지연 임포트 — items.py가 이 모듈의 _save_snapshot을 가져다 쓰므로 순환참조 방지
        # 현재 상태
        current = get_item_by_id(item_id)
        if not current:
            return None

        # 스냅샷 조회
        row = database.execute_query(
            "SELECT * FROM welfare_item_snapshots WHERE id = ? AND item_link = ?",
            (snapshot_id, current.get('link')),
            fetch_one=True, raise_on_error=True
        )
        if not row:
            return None

        try:
            old_data = json.loads(row['snapshot_data'])
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"Corrupted snapshot data for id={snapshot_id}")
            return None
        old_data['ai_data'] = _parse_ai_data(old_data.get('ai_data'))

        # 비교할 주요 필드
        diff_fields = [
            'title', 'category', 'target', 'deadline', 'support_detail',
            'eligibility_detail', 'contact', 'event_date', 'event_end_date',
            'residence', 'target_income', 'age_detail', 'page_modified_date'
        ]
        changes = []
        for field in diff_fields:
            old_val = old_data.get(field) or ''
            new_val = current.get(field) or ''
            if str(old_val) != str(new_val):
                changes.append({
                    'field': field,
                    'old': old_val,
                    'new': new_val
                })

        return {
            'snapshot_id': snapshot_id,
            'snapshot_date': row.get('created_at'),
            'change_summary': row.get('change_summary'),
            'changes': changes,
            'old_data': old_data,
            'current_data': current
        }
    except Exception as e:
        logger.error(f"Get Item Diff Error: {e}")
        raise


