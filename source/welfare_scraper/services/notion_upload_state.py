"""Notion 생성 요청의 불확실한 결과를 재전송하지 않도록 로컬에 기록한다."""
import datetime
import database


def _prepare(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS notion_upload_attempts (
        link TEXT PRIMARY KEY, page_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')


def claim(item_id):
    with database.get_connection() as conn:
        _prepare(conn)
        item = conn.execute('SELECT link, notion_page_id FROM welfare_items WHERE id=?', (item_id,)).fetchone()
        if not item:
            return {'state': 'missing'}
        if item['notion_page_id']:
            return {'state': 'completed', 'page_id': item['notion_page_id']}
        previous = conn.execute('SELECT page_id FROM notion_upload_attempts WHERE link=?', (item['link'],)).fetchone()
        if previous:
            if previous['page_id']:
                conn.execute('UPDATE welfare_items SET notion_page_id=? WHERE id=?', (previous['page_id'], item_id))
                conn.commit()
            return {'state': 'completed' if previous['page_id'] else 'uncertain', 'page_id': previous['page_id']}
        claimed_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='microseconds')
        cursor = conn.execute('INSERT INTO notion_upload_attempts(link,created_at) VALUES (?,?)',
                              (item['link'], claimed_at))
        conn.commit()  # 원격 요청 전에 영속화. 중단/응답 유실 이후에도 재전송 금지.
        return {'state': 'claimed', 'link': item['link'],
                'attempt_rowid': cursor.lastrowid, 'claimed_at': claimed_at}


def release_unsubmitted(claimed):
    """Release only this fresh claim after proven local preflight failure.

    The caller holds the existing mutation gate. Never use this for transport
    failure, remote rejection, an old uncertain claim or a completed page.
    Row identity AND the exact creation timestamp prevent a stale claim from
    releasing a different subsequent attempt. No schema migration is needed.
    """
    if (claimed.get('state') != 'claimed' or not claimed.get('link')
            or type(claimed.get('attempt_rowid')) is not int or not claimed.get('claimed_at')):
        return False
    with database.get_connection() as conn:
        deleted = conn.execute('''DELETE FROM notion_upload_attempts
            WHERE rowid=? AND link=? AND created_at=? AND page_id IS NULL
            AND NOT EXISTS (SELECT 1 FROM welfare_items WHERE link=?
                AND notion_page_id IS NOT NULL AND notion_page_id!='')''',
            (claimed['attempt_rowid'], claimed['link'], claimed['claimed_at'], claimed['link']))
        conn.commit()
        return deleted.rowcount == 1


def complete(link, page_id):
    with database.get_connection() as conn:
        conn.execute('UPDATE notion_upload_attempts SET page_id=? WHERE link=?', (page_id, link))
        conn.execute('UPDATE welfare_items SET notion_page_id=? WHERE link=?', (page_id, link))
        conn.commit()


def allow_retry(ids):
    """사용자가 Notion에 생성되지 않았음을 직접 확인한 항목만 명시적으로 해제."""
    with database.get_connection() as conn:
        _prepare(conn)
        for item_id in set(ids):
            conn.execute('''DELETE FROM notion_upload_attempts WHERE page_id IS NULL AND link IN (
                SELECT link FROM welfare_items WHERE id=? AND (notion_page_id IS NULL OR notion_page_id='')
            )''', (item_id,))
        conn.commit()


def uncertain_links():
    with database.get_connection() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notion_upload_attempts'").fetchone():
            return set()
        return {row['link'] for row in conn.execute('SELECT link FROM notion_upload_attempts WHERE page_id IS NULL')}
