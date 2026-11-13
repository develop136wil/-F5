"""Narrow, read-only admission for replacing an unadopted legacy failure.

Only a later validated result and an explicit final save may replace such a row.
This module never retries AI or mutates data; user decisions always win.
"""
from urllib.parse import urlsplit, urlunsplit

from services import legacy_news_analysis


def identity_url(value):
    """Identity only, never network permission or a URL-fetching validator."""
    if not isinstance(value, str) or len(value) > 10000 or any(ord(c) <= 32 for c in value):
        return ''
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
            return ''
        # Hash-router paths identify separate documents; ordinary #section
        # anchors still identify the same document. Preserve route spelling and
        # query exactly, without treating this identity rule as fetch permission.
        fragment = parsed.fragment if parsed.fragment.startswith(('/', '!/')) else ''
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or '/', parsed.query, fragment))
    except ValueError:
        return ''


def unadopted_failure(item, *, adopted=False, upload_recorded=False):
    return bool(item and not adopted and not upload_recorded
        and item.get('ai_skip_reason') != 'failed_permanent'
        and not any(item.get(key) for key in ('is_archived', 'notion_dismissed', 'notion_page_id', 'manual_news'))
        and (legacy_news_analysis.failure(item.get('ai_data')) is not None
             or (item.get('ai_skip_reason') == 'quota'
                 and not legacy_news_analysis.facts(item.get('ai_data')))))


def destination(conn, link):
    """Return an existing canonical URL row and whether explicit recovery is safe.

    Multiple historical spellings fail closed. Never guess which stored row a
    user intended to replace, and never treat absent facts as proof of failure.
    """
    key = identity_url(link)
    matches = [row[0] for row in conn.execute('SELECT id,link FROM welfare_items')
               if key and identity_url(row[1]) == key]
    if not matches:
        return None, False
    cursor = conn.execute('''SELECT id,link,ai_data,ai_skip_reason,is_archived,
        notion_dismissed,notion_page_id FROM welfare_items WHERE id=?''', (matches[0],))
    item = dict(zip((column[0] for column in cursor.description), cursor.fetchone()))
    adopted = any(identity_url(row[0]) == key for row in conn.execute('SELECT link FROM news_result_imports'))
    upload_recorded = False
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notion_upload_attempts'").fetchone():
        upload_recorded = any(identity_url(row[0]) == key for row in conn.execute('SELECT link FROM notion_upload_attempts'))
    return item, len(matches) == 1 and unadopted_failure(
        item, adopted=adopted, upload_recorded=upload_recorded)


def guidance(reason):
    lead = {
        'authentication_required': 'AI 연결을 확인해주세요.',
        'local_budget_exhausted': '이전에 분석하지 못한 원문은 보존되어 있습니다.',
        'daily_quota_exhausted': 'AI 제공업체 한도가 회복된 뒤 진행해주세요.',
        'ai_input_review_required': '원문과 추가 AI 요청 문구의 개인정보·비밀정보를 먼저 확인해주세요.',
        'ai_input_size_review_required': '원문 분량과 추가 AI 요청 문구를 먼저 확인해주세요.',
        'ai_response_review_required': '이전 분석 응답을 사용할 수 없습니다. 원문과 수집 조건을 확인해주세요.',
    }.get(reason, '원문과 AI 연결·수집 조건을 확인해주세요.')
    return (lead + ' 이 이전 기록에는 바로 재개할 연결 작업이 없습니다. 수집 조건을 저장한 뒤 '
            '‘새 소식 찾기’를 직접 실행할 수 있습니다. 새 수집에서 같은 주소를 다시 찾고 안전 검사를 '
            '통과해야 분석하며, 이전 기록은 보존됩니다.')
