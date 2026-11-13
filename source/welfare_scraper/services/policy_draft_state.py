"""Durable create receipts keyed by source URL; no network or app DB access.

A missing receipt is the only state that permits another create request.
An unresolved receipt is not proof of failure. Only a definite rejection or an
explicit, attempt-specific user confirmation may release it.
"""
import json
import uuid
from services.news_profile_store import _atomic


def source_key(url):
    # Match policy_sync._norm_url, including its existing cross-policy scope.
    key = str(url or '').strip().rstrip('/').lower()
    if not key or len(key) > 10000:
        raise ValueError('A source URL is required')
    return key


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS policy_draft_attempts (
        source_key TEXT PRIMARY KEY, attempt_id TEXT NOT NULL,
        source_url TEXT NOT NULL, db_key TEXT NOT NULL, source_id TEXT NOT NULL,
        db_name TEXT NOT NULL DEFAULT '',
        fields_json TEXT NOT NULL, page_id TEXT,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    if 'db_name' not in {row[1] for row in conn.execute('PRAGMA table_info(policy_draft_attempts)')}:
        conn.execute("ALTER TABLE policy_draft_attempts ADD COLUMN db_name TEXT NOT NULL DEFAULT ''")


def lookup(conn, url):
    ensure_schema(conn)
    cursor = conn.execute('SELECT * FROM policy_draft_attempts WHERE source_key=?', (source_key(url),))
    row = cursor.fetchone()
    return dict(zip((col[0] for col in cursor.description), row)) if row else None


def claim(conn, url, db, fields):
    with _atomic(conn):
        previous = lookup(conn, url)
        if previous:
            return dict(previous, claimed=False)
        attempt_id = uuid.uuid4().hex
        conn.execute('''INSERT INTO policy_draft_attempts
            (source_key,attempt_id,source_url,db_key,source_id,db_name,fields_json)
            VALUES (?,?,?,?,?,?,?)''', (source_key(url), attempt_id, url, db['key'], db['data_source_id'],
                                     str(db.get('name') or db['key']),
                                     json.dumps(fields, ensure_ascii=False, allow_nan=False)))
        return dict(lookup(conn, url), claimed=True)


def complete(conn, url, attempt_id, page_id):
    if not isinstance(page_id, str) or not page_id.strip():
        raise ValueError('A confirmed Notion page ID is required')
    with _atomic(conn):
        changed = conn.execute('''UPDATE policy_draft_attempts SET page_id=?,updated_at=CURRENT_TIMESTAMP
            WHERE source_key=? AND attempt_id=?''',
            (page_id, source_key(url), attempt_id))
        if changed.rowcount != 1:
            raise ValueError('Draft attempt changed')


def release_rejected(conn, url, attempt_id):
    """Only for a provider-confirmed rejection or a request never dispatched."""
    with _atomic(conn):
        return conn.execute('''DELETE FROM policy_draft_attempts
            WHERE source_key=? AND attempt_id=? AND page_id IS NULL''',
            (source_key(url), attempt_id)).rowcount == 1


def allow_retry(conn, url, attempt_id, *, confirmed):
    """The user verified non-registration; never release a newer attempt."""
    if confirmed is not True:
        raise ValueError('Explicit non-registration confirmation is required')
    with _atomic(conn):
        changed = conn.execute('DELETE FROM policy_draft_attempts WHERE source_key=? AND attempt_id=?',
                               (source_key(url), attempt_id))
        if changed.rowcount != 1:
            raise ValueError('Draft attempt changed; check its status again')


async def find_remote(attempt, *, session, token, notion):
    """Read the original target, not the currently selected replacement table."""
    target = await notion.resolve_target(session, token, attempt['source_id'])
    if not target.get('ok'):
        return {'state': 'unavailable'}
    result = await notion.query_policy_source_pages(session, token, target)
    if not result.get('ok') or not isinstance(result.get('pages'), list):
        return {'state': 'unavailable'}
    if attempt.get('page_id'):
        page_key = attempt['page_id'].replace('-', '').lower()
        known = next((page for page in result['pages'] if isinstance(page, dict)
                      and str(page.get('page_id', '')).replace('-', '').lower() == page_key), None)
        if known:
            # Editing the source URL does not undo a successful creation.
            return {'state': 'completed', 'page_id': known['page_id'], 'fields': known}
        raw = await notion.get_page(attempt['page_id'])
        if not isinstance(raw, dict):
            return {'state': 'unavailable'}  # Missing access is not proof of deletion.
        if not (raw.get('archived') is True or raw.get('in_trash') is True):
            return {'state': 'completed', 'page_id': attempt['page_id'],
                    'fields': notion.extract_page_fields(raw)}
    pages = [page for page in result['pages'] if isinstance(page, dict)
             and isinstance(page.get('_source_urls'), list)
             and any(isinstance(url, str) and source_key(url) == attempt['source_key']
                     for url in page['_source_urls'] if url)]
    if len(pages) == 1 and isinstance(pages[0].get('page_id'), str) and pages[0]['page_id']:
        return {'state': 'completed', 'page_id': pages[0]['page_id'], 'fields': pages[0]}
    return {'state': 'multiple' if pages else 'not_found'}
