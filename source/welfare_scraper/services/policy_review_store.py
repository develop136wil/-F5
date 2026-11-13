"""Latest-state source/extraction review ledger, never AI approval or raw text."""
import datetime
import hashlib
import json
import re
from urllib.parse import unquote, urlsplit, urlunsplit

import database
from services.ai_input_safety import inspect_ai_text

_NOTES = {
    'source_changed_unverified': '출처 원문이 변경됐지만 새 비교를 완료하지 못했습니다. 이전 제안은 보존하되 반영을 보류합니다. 정책 정보 점검으로 최신 원문을 다시 비교해주세요.',
    'source_input_budget_exceeded': '정책 원문과 필수 분석 지침이 현재 AI 입력 한도보다 큽니다. 입력 한도와 상세 출처를 확인한 뒤 다시 점검해주세요. 원문과 기존 내용은 유지됩니다.',
    'source_input_count_unavailable': 'AI 입력 크기를 확인하지 못해 분석을 잠시 보류했습니다. 연결·사용 한도를 확인하고 다시 점검해주세요. 원문과 기존 내용은 유지됩니다.',
    'source_input_rate_limited': 'AI 요청이 일시적으로 제한되어 분석을 잠시 보류했습니다. 잠시 후 다시 점검하면 완료하지 못한 정책을 재시도합니다. 기존 내용은 유지됩니다.',
    'source_read_incomplete': '원문이나 첨부파일 전체를 확인하지 못해 변경 제안을 보류했습니다. 상세 출처와 첨부 내용을 확인한 뒤 다시 점검해주세요. 기존 정책 내용은 유지됩니다.',
    'source_missing': '정책 원문을 확인하지 못했습니다. 출처를 확인한 뒤 다시 점검해주세요.',
    'source_too_long': '원문이 분석 범위보다 깁니다. 해당 정책의 상세 출처를 연결한 뒤 다시 점검해주세요.',
    'source_criteria_review_required': '소득·나이의 적용 대상과 기준일을 원문에서 확인해주세요. 모호하면 담당 기관에 문의해주세요. 같은 원문을 반복 점검해도 해결되지 않습니다.',
    'source_field_incomplete': 'AI 응답의 전용 항목에 원문 조건이 충분히 반영되지 않아 보류했습니다. 나이·소득·연락처·대상 분류 등 누락된 항목을 원문과 대조해주세요. 기존 내용은 유지됩니다.',
    'structured_evidence_missing': 'AI 응답의 지역·대상·나이·연락처 근거를 확인하지 못했습니다. 해당 조건을 원문과 대조해주세요. 기존 내용은 유지됩니다.',
    'source_schema_invalid': 'AI 응답의 항목 형식이나 선택지가 맞지 않아 보류했습니다. 연결된 정책표와 원문을 확인해주세요. 기존 내용은 유지됩니다.',
    'source_conditions_incomplete': 'AI 응답에 신청 방법·제외 조건·제출서류 등이 충분히 보존됐는지 확인하지 못했습니다. 원문 전체와 대조해주세요. 기존 내용은 유지됩니다.',
    'source_range_invalid': 'AI가 선택한 원문 구간의 형식을 확인하지 못했습니다. 원문과 해당 정책의 상세 출처를 확인해주세요. 기존 내용은 유지됩니다.',
    'source_fact_changed': 'AI 응답의 숫자·단위·대상별 수량이 인용 근거와 일치하지 않습니다. 원문과 대조해주세요. 기존 내용은 유지됩니다.',
    'source_evidence_mismatch': 'AI 응답의 인용 구간을 원문에서 찾지 못했습니다. 해당 정책의 원문을 직접 확인해주세요. 기존 내용은 유지됩니다.',
    'source_evidence_boundary_changed': 'AI 응답의 인용 경계나 제한 조건이 원문과 달라 보류했습니다. 앞뒤 조건을 함께 확인해주세요. 기존 내용은 유지됩니다.',
    'source_presentation_mismatch': 'AI 응답의 표시 문장과 조건이 인용 근거대로 보존됐는지 확인하지 못했습니다. 원문과 대조해주세요. 기존 내용은 유지됩니다.',
    'source_evidence_missing': 'AI 응답의 정책 내용이나 인용 근거가 충분하지 않아 보류했습니다. 원문 전체를 확인해주세요. 기존 내용은 유지됩니다.',
}
_FIELDS = frozenset({'support_detail', 'eligibility_detail', 'age_detail', 'target_income',
    'contact', 'category', 'life_cycle', 'target_characteristics', 'residence',
    'start_age_months', 'end_age_months'})
MAX_GROUPS_PER_POLICY = 100


def supports_review(review):
    """Only known bounded input/validation holds, not arbitrary error text.

    Pure and safe before any DB access. Persisting a hold never retries a model
    call or makes the source/result approved. Named count/rate holds retain an
    actionable retry note; arbitrary provider failures are never persisted.
    """
    return (isinstance(review, dict) and isinstance(review.get('code'), str)
            and review['code'] in _NOTES)


def _safe_fields(value):
    # Field names only: never model values, excerpts, diagnostics or free text.
    if not isinstance(value, (list, tuple)):
        return []
    return sorted({field for field in value[:100] if isinstance(field, str) and field in _FIELDS})


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _source_ids(urls):
    if not isinstance(urls, (list, tuple)) or not 1 <= len(urls) <= 100:
        raise ValueError('Invalid policy review source group')
    if any(not isinstance(url, str) or not url or len(url) > 8192 for url in urls):
        raise ValueError('Invalid policy review source')
    return sorted({_digest(url) for url in urls})


def _safe_title(title, policy_id):
    value = re.sub(r'[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]', ' ', str(title or '')).strip()
    return value[:120] if value and inspect_ai_text(value).allowed else f'정책 #{policy_id}'


def _safe_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or len(value) > 2048 or not inspect_ai_text(unquote(value)).allowed
                or re.search(r'[\x00-\x20\x7f]', value)):
            return ''
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ''))
    except (ValueError, TypeError):
        return ''


def _exists(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='policy_source_reviews'").fetchone())


def _ensure(conn):
    # Only first write creates this metadata table. Existing policy data and
    # schema are untouched; deleting an existing policy cleans only its metadata.
    conn.execute('''CREATE TABLE IF NOT EXISTS policy_source_reviews (
        policy_id INTEGER NOT NULL REFERENCES notion_policies(id) ON DELETE CASCADE,
        source_key TEXT NOT NULL, id TEXT NOT NULL, source_ids TEXT NOT NULL,
        source_hash TEXT NOT NULL, title TEXT NOT NULL, source_url TEXT NOT NULL,
        source_count INTEGER NOT NULL, source_role TEXT NOT NULL,
        code TEXT NOT NULL, fields TEXT NOT NULL,
        first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('unresolved', 'resolved')),
        resolved_at TEXT, resolved_source_hash TEXT, PRIMARY KEY(policy_id, source_key)
    )''')


def record_hold(policy_id, title, source_urls, source, review, source_role='current'):
    """One row per policy/source group, not an unbounded log per source version."""
    if type(policy_id) is not int or policy_id <= 0 or not isinstance(source, str):
        raise ValueError('Invalid policy review identity')
    if not supports_review(review):
        return False
    code = review['code']
    if source_role not in {'current', 'baseline', 'fetch', 'freshness'}:
        raise ValueError('Invalid policy review source role')
    source_ids = _source_ids(source_urls)
    encoded = json.dumps(source_ids, separators=(',', ':'))
    source_key, source_hash = _digest(encoded + ':' + source_role), _digest(source)
    stable_id = _digest(f'{policy_id}:{source_key}:{source_hash}:{code}')
    fields = _safe_fields(review.get('fields'))
    now = _now()
    with database.get_connection() as conn:
        if not conn.execute('SELECT 1 FROM notion_policies WHERE id=?', (policy_id,)).fetchone():
            return False
        _ensure(conn)
        existing = conn.execute('SELECT 1 FROM policy_source_reviews WHERE policy_id=? AND source_key=?', (policy_id, source_key)).fetchone()
        if not existing:
            count = conn.execute('SELECT COUNT(*) FROM policy_source_reviews WHERE policy_id=?', (policy_id,)).fetchone()[0]
            if count >= MAX_GROUPS_PER_POLICY:
                # Retain unresolved work. Bound old, resolved source-group
                # metadata too when a user repeatedly changes source addresses.
                conn.execute("DELETE FROM policy_source_reviews WHERE policy_id=? AND source_key IN (SELECT source_key FROM policy_source_reviews WHERE policy_id=? AND status='resolved' ORDER BY resolved_at LIMIT ?)",
                             (policy_id, policy_id, count - MAX_GROUPS_PER_POLICY + 1))
                count = conn.execute('SELECT COUNT(*) FROM policy_source_reviews WHERE policy_id=?', (policy_id,)).fetchone()[0]
                if count >= MAX_GROUPS_PER_POLICY:
                    raise RuntimeError('미해결 정책 보류 출처가 너무 많아 기록과 점검을 중단했습니다. 기존 보류와 정책 내용은 유지됩니다.')
        conn.execute('''INSERT INTO policy_source_reviews
            (policy_id, source_key, id, source_ids, source_hash, title, source_url,
             source_count, source_role, code, fields, first_seen_at, last_seen_at, status, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unresolved', NULL)
            ON CONFLICT(policy_id, source_key) DO UPDATE SET
                id=excluded.id, source_hash=excluded.source_hash, title=excluded.title,
                source_url=excluded.source_url, source_count=excluded.source_count,
                code=excluded.code, fields=excluded.fields,
                first_seen_at=CASE WHEN policy_source_reviews.status='unresolved'
                    THEN policy_source_reviews.first_seen_at ELSE excluded.first_seen_at END,
                last_seen_at=excluded.last_seen_at, status='unresolved', resolved_at=NULL, resolved_source_hash=NULL''',
            (policy_id, source_key, stable_id, encoded, source_hash, _safe_title(title, policy_id),
             _safe_url(source_urls[0]), len(source_ids), source_role, code, json.dumps(fields), now, now))
        conn.commit()
    return True


def resolve_after_success(policy_id, source_urls, validated_source, current_source_urls=None):
    """Only call after fresh source analysis AND policy comparison were saved.

    Failed fetch/AI/baseline/comparison, viewing, or a subset of a source group
    cannot resolve the outstanding review. No source or model prose is stored.
    """
    source_ids = set(_source_ids(source_urls))
    current_ids = set(_source_ids(current_source_urls)) if current_source_urls else source_ids
    if not isinstance(validated_source, str) or not validated_source.strip():
        raise ValueError('Missing validated policy source')
    resolved_hash = _digest(validated_source)
    with database.get_connection() as conn:
        if not _exists(conn):
            return 0
        rows = conn.execute("SELECT source_key, source_ids FROM policy_source_reviews WHERE policy_id=? AND status='unresolved'", (policy_id,)).fetchall()
        keys = [row['source_key'] for row in rows
                if (set(json.loads(row['source_ids'])) & current_ids
                    if current_source_urls is not None else set(json.loads(row['source_ids']))) <= source_ids]
        for key in keys:
            conn.execute("UPDATE policy_source_reviews SET status='resolved', resolved_at=?, resolved_source_hash=? WHERE policy_id=? AND source_key=?", (_now(), resolved_hash, policy_id, key))
        conn.commit()
    return len(keys)


def list_unresolved(db_keys, limit=100, offset=0, exclude_policy_ids=None):
    """Read-only, paginated and scoped to currently connected policy databases."""
    if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
        raise ValueError('Invalid policy review pagination')
    empty = {'items': [], 'total': 0, 'truncated': False}
    if not db_keys:
        return empty
    with database.get_connection() as conn:
        if not _exists(conn):
            return empty
        marks = ','.join('?' for _ in db_keys)
        where = f" FROM policy_source_reviews r JOIN notion_policies p ON p.id=r.policy_id WHERE r.status='unresolved' AND p.db_key IN ({marks})"
        params = list(db_keys)
        excluded = sorted({value for value in (exclude_policy_ids or []) if type(value) is int and value > 0})
        if excluded:
            where += ' AND r.policy_id NOT IN (' + ','.join('?' for _ in excluded) + ')'
            params.extend(excluded)
        total = conn.execute('SELECT COUNT(*)' + where, tuple(params)).fetchone()[0]
        rows = conn.execute('SELECT r.*' + where + ' ORDER BY r.last_seen_at DESC, r.policy_id, r.source_key LIMIT ? OFFSET ?', (*params, limit, offset)).fetchall()
    items = []
    for row in rows:
        item = {key: row[key] for key in ('id', 'policy_id', 'source_hash', 'source_count', 'source_role', 'code', 'first_seen_at', 'last_seen_at', 'status')}
        item.update(title=_safe_title(row['title'], row['policy_id']), source_url=_safe_url(row['source_url']),
                    note=_NOTES.get(row['code'], '원문을 확인한 뒤 다시 점검해주세요.'),
                    fields=_safe_fields(json.loads(row['fields'])))
        items.append(item)
        if row['source_role'] == 'baseline':
            item['note'] = '이전 비교 원문의 확인이 필요합니다. ' + item['note']
    return {'items': items, 'total': total, 'truncated': total > offset + len(items)}


def unresolved_by_policy(db_keys):
    """One read-only snapshot shared by cards and explicit revalidation."""
    grouped, offset = {}, 0
    while True:
        page = list_unresolved(db_keys, 200, offset)
        for item in page['items']:
            grouped.setdefault(item['policy_id'], []).append(item)
        if not page['truncated']:
            return grouped
        offset += len(page['items'])
