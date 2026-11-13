"""Trusted, no-AI result boundary for Bokjiro's official service list.

This module accepts only rows produced by ``normalize_official_service``. It
does not fetch the network, write legacy welfare items, or claim eligibility.
"""
from contextlib import contextmanager
import json
import re
from urllib.parse import quote

from services.news_profile_store import _atomic, get_run_snapshot, get_profile
from services.news_profile_taxonomy import LIFE_CYCLES, HOUSEHOLD_SITUATIONS, INTEREST_TOPICS
from services.news_target_rules import evaluate_target


_FIELDS = {
    'source_type', 'source_id', 'source_key', 'title', 'summary', 'provider',
    'provider_region', 'eligibility_region', 'age_range_known', 'link',
    'life_cycles', 'household_situations', 'interest_topics',
}


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _fingerprint(value):
    import hashlib
    return hashlib.sha256(_encode(value).encode('utf-8')).hexdigest()


def ensure_schema(conn):
    with _atomic(conn):
        conn.execute('''CREATE TABLE IF NOT EXISTS news_official_source_state (
            source_key TEXT PRIMARY KEY, source_id TEXT NOT NULL,
            latest_fingerprint TEXT NOT NULL, latest_run_id TEXT NOT NULL,
            first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(latest_run_id) REFERENCES news_run_snapshots(run_id))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS news_official_results (
            run_id TEXT NOT NULL, source_key TEXT NOT NULL, source_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL, result_json TEXT NOT NULL,
            change_state TEXT NOT NULL CHECK(change_state IN ('new','unchanged','updated')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(run_id,source_key),
            FOREIGN KEY(run_id) REFERENCES news_run_snapshots(run_id))''')
        conn.execute('''CREATE INDEX IF NOT EXISTS idx_news_official_results_source
            ON news_official_results(source_key,created_at)''')


def _validate_item(item):
    from services.bokjiro_collector import DETAIL
    if not isinstance(item, dict) or set(item) != _FIELDS:
        raise ValueError('Expected an exact normalized Bokjiro service')
    if item['source_type'] != 'bokjiro_service' or item['age_range_known'] is not False:
        raise ValueError('Invalid Bokjiro source contract')
    source_id = item['source_id']
    if (not isinstance(source_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', source_id)
            or item['source_key'] != f'bokjiro:{source_id}'
            or item['link'] != DETAIL + quote(source_id, safe='')):
        raise ValueError('Invalid Bokjiro source identity')
    for key, maximum in (('title', 300), ('summary', 2000), ('provider', 200),
                         ('provider_region', 100), ('eligibility_region', 0), ('link', 1000)):
        value = item[key]
        if not isinstance(value, str) or len(value) > maximum or (key == 'title' and not value.strip()):
            raise ValueError('Invalid Bokjiro text field')
    for key, allowed in (('life_cycles', LIFE_CYCLES),
                         ('household_situations', HOUSEHOLD_SITUATIONS),
                         ('interest_topics', INTEREST_TOPICS)):
        values = item[key]
        if (not isinstance(values, list) or any(not isinstance(value, str) for value in values)
                or values != sorted(set(values)) or any(value not in allowed for value in values)):
            raise ValueError('Invalid Bokjiro official tags')


def _matches_filters(item, filters):
    for item_key, filter_key in (('life_cycles', 'life_cycles'),
                                 ('household_situations', 'characteristics'),
                                 ('interest_topics', 'interest_topics')):
        requested = set(filters[filter_key])
        if requested and not requested.intersection(item[item_key]):
            return False
    return True


def _result(profile, item):
    facts = {
        # Provider location is deliberately not promoted to residence eligibility.
        'residence': [], 'start_age_months': None, 'end_age_months': None,
        'life_cycle': item['life_cycles'],
        'target_characteristics': item['household_situations'],
        'interest_topics': item['interest_topics'],
    }
    decision = evaluate_target(profile, facts)
    if decision.status == 'excluded':
        raise ValueError('Official result does not match the run profile')
    labels = item['life_cycles'] + item['household_situations']
    return {
        'source_type': 'bokjiro_service', 'ai_called': False,
        'target_status': decision.status, 'checks': dict(decision.checks),
        'details': {
            'category': ', '.join(item['interest_topics']),
            'target': ', '.join(labels),
            'support_detail': item['summary'],
            'contact': item['provider'],
        },
        'input_note': (('선택한 세부 가구상황은 공식 목록 분류만으로 확인되지 않습니다. 원문 대상 확인이 필요합니다. '
                        if dict(decision.checks).get('characteristics') == 'review' else '')
                       + '복지로 공식 분류는 확인했습니다. 목록에 없는 거주지·나이·소득 등 '
                       '세부 자격은 원문에서 확인해주세요.'),
    }


@contextmanager
def _write_transaction(conn):
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute('BEGIN IMMEDIATE')
    try:
        with _atomic(conn):
            yield
        if owns_transaction:
            conn.commit()
    except BaseException:
        if owns_transaction:
            conn.rollback()
        raise


def register_official_results(conn, run_id, items):
    """Atomically save a bounded, idempotent batch and classify changes across runs."""
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= 100:
        raise ValueError('Expected 1..100 official services')
    for item in items:
        _validate_item(item)
    keys = [item['source_key'] for item in items]
    if len(keys) != len(set(keys)):
        raise ValueError('Duplicate official service in one batch')

    with _write_transaction(conn):
        ensure_schema(conn)
        snapshot = get_run_snapshot(conn, run_id)
        profile_data = snapshot['profile']
        profile = get_profile(conn, profile_data['profile_id'], profile_data['revision'])
        filters = profile.require_bokjiro_support()
        if not filters or filters.get('mode') != 'official_tags_v1':
            raise ValueError('Run is not an official Bokjiro collection')
        output = []
        for item in items:
            if not _matches_filters(item, filters):
                raise ValueError('Official result does not match the requested tags')
            fingerprint = _fingerprint(item)
            existing = conn.execute('''SELECT fingerprint,change_state FROM news_official_results
                WHERE run_id=? AND source_key=?''', (run_id, item['source_key'])).fetchone()
            if existing:
                if existing[0] != fingerprint:
                    raise ValueError('One run cannot contain conflicting versions of a service')
                output.append({'source_key': item['source_key'],
                               'change_state': existing[1], 'created': False})
                continue
            previous = conn.execute('''SELECT latest_fingerprint FROM news_official_source_state
                WHERE source_key=?''', (item['source_key'],)).fetchone()
            change_state = ('new' if previous is None else
                            'unchanged' if previous[0] == fingerprint else 'updated')
            result = _result(profile, item)
            result['change_state'] = change_state
            conn.execute('''INSERT INTO news_official_results
                (run_id,source_key,source_id,fingerprint,payload_json,result_json,change_state)
                VALUES (?,?,?,?,?,?,?)''',
                (run_id, item['source_key'], item['source_id'], fingerprint,
                 _encode(item), _encode(result), change_state))
            conn.execute('''INSERT INTO news_official_source_state
                (source_key,source_id,latest_fingerprint,latest_run_id) VALUES (?,?,?,?)
                ON CONFLICT(source_key) DO UPDATE SET
                    source_id=excluded.source_id,
                    latest_fingerprint=excluded.latest_fingerprint,
                    latest_run_id=excluded.latest_run_id,
                    last_seen_at=CURRENT_TIMESTAMP''',
                (item['source_key'], item['source_id'], fingerprint, run_id))
            output.append({'source_key': item['source_key'],
                           'change_state': change_state, 'created': True})
        return output
