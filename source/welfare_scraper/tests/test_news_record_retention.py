"""Synthetic memory-only fixtures. No app import, operational DB or configuration."""
import asyncio
import ast
import contextlib
import datetime as dt
import json
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from services import news_record_retention as retention
from services import news_profile_store as profiles, news_analysis_store as jobs
from services.news_profile import NewsProfile


NOW = dt.datetime(2026, 9, 17, 12, tzinfo=dt.timezone.utc)
OLD = '2026-08-01 12:00:00'
RECENT = '2026-09-16 12:00:00'


@pytest.fixture
def db(monkeypatch):
    conn = sqlite3.connect(':memory:')
    conn.execute('PRAGMA foreign_keys=ON')
    profiles.ensure_schema(conn)
    jobs.ensure_schema(conn)
    conn.executescript('''
        CREATE TABLE welfare_items(id INTEGER PRIMARY KEY,link TEXT NOT NULL,title TEXT,
            is_archived INTEGER DEFAULT 0,notion_dismissed INTEGER DEFAULT 0,notion_page_id TEXT);
        CREATE TABLE notion_upload_attempts(link TEXT PRIMARY KEY,page_id TEXT);
        CREATE TABLE news_result_imports(job_id TEXT PRIMARY KEY,item_id INTEGER,link TEXT NOT NULL,
            outcome TEXT NOT NULL,fingerprint TEXT NOT NULL,created_at TEXT,
            FOREIGN KEY(job_id) REFERENCES news_analysis_jobs(job_id),
            FOREIGN KEY(item_id) REFERENCES welfare_items(id) ON DELETE SET NULL);
        CREATE TABLE news_semantic_decisions(run_id TEXT,candidate_key TEXT,score REAL,threshold REAL,scoring_version TEXT,
            PRIMARY KEY(run_id,candidate_key),
            FOREIGN KEY(run_id,candidate_key) REFERENCES news_run_candidates(run_id,candidate_key));
        CREATE TABLE news_execution_settings(run_id TEXT PRIMARY KEY,settings_json TEXT,
            FOREIGN KEY(run_id) REFERENCES news_run_snapshots(run_id));
        CREATE TABLE news_bokjiro_progress(run_id TEXT PRIMARY KEY,complete INTEGER,
            FOREIGN KEY(run_id) REFERENCES news_run_snapshots(run_id));
        CREATE TABLE news_bokjiro_pages(run_id TEXT,page INTEGER,fingerprint TEXT,
            FOREIGN KEY(run_id) REFERENCES news_run_snapshots(run_id));
        CREATE TABLE news_collection_issues(run_id TEXT,site_id TEXT,issue_key TEXT,
            FOREIGN KEY(run_id) REFERENCES news_run_snapshots(run_id) ON DELETE CASCADE);
        CREATE TABLE news_collection_site_issues(run_id TEXT,site_id TEXT,issue_code TEXT,
            FOREIGN KEY(run_id) REFERENCES news_run_snapshots(run_id) ON DELETE CASCADE);
        CREATE TABLE news_official_results(run_id TEXT,source_key TEXT,source_id TEXT,fingerprint TEXT,
            payload_json TEXT,result_json TEXT,change_state TEXT,created_at TEXT,
            PRIMARY KEY(run_id,source_key),FOREIGN KEY(run_id) REFERENCES news_run_snapshots(run_id));
        CREATE TABLE news_official_source_state(source_key TEXT PRIMARY KEY,source_id TEXT,latest_fingerprint TEXT,
            latest_run_id TEXT,first_seen_at TEXT,last_seen_at TEXT,
            FOREIGN KEY(latest_run_id) REFERENCES news_run_snapshots(run_id));
    ''')
    profiles.create_profile(conn, NewsProfile('p', 1, '시험 조건', '전국', site_ids=('1',)))
    # Grounding is tested in the existing feed suite. These boundaries explicitly
    # supply ready identities without importing the app or its configuration.
    monkeypatch.setattr(retention, '_ready_records', lambda _conn, _profiles: (set(), set()))
    monkeypatch.setattr(retention, '_historical_ready', lambda _conn, _record: False)
    yield conn
    conn.close()


def run(conn, *, date=OLD, state='complete', workflow=True):
    identity = profiles.prepare_run(conn, 'p', expected_revision=1,
        sites=[{'id':'1','name':'시험 기관','url':'https://example.invalid'}])
    conn.execute('UPDATE news_run_snapshots SET created_at=? WHERE run_id=?', (date, identity))
    if workflow:
        conn.execute('''INSERT INTO news_profile_workflows(workflow_id,run_id,state,stage,updated_at)
            VALUES (?,?,?,'analysis',?)''', ('workflow-' + identity, identity, state, date))
    conn.commit()
    return identity


def job(conn, run_id, *, link='https://example.invalid/notice', state='failed', candidate=True, marker='SYNTHETIC_BODY'):
    payload = {'title':'시험 소식','link':link,'site_name':'시험 기관','content':marker}
    identity = jobs.enqueue(conn, run_id, payload=payload, model='offline', recipe='retention-fixture')
    conn.execute('UPDATE news_analysis_jobs SET state=?,result_json=? WHERE job_id=?',
                 (state, json.dumps({'analysis':marker}), identity))
    if candidate:
        conn.execute('INSERT INTO news_run_candidates VALUES (?,?,?,?,?,?)',
            (run_id, identity, '1', json.dumps(payload), 'semantic_review', identity))
    conn.commit()
    return identity


def count(conn, table):
    return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


def test_disabled_default_and_invalid_options_never_scan_db(monkeypatch):
    class NoDb:
        def execute(self, *_args):
            raise AssertionError('Disabled cleanup touched the database')
    for action in (retention.preview, retention.cleanup):
        assert action(NoDb(), 0)['status'] == 'disabled'
        for invalid in (True, '7', 1, -1, 30.0, None):
            with pytest.raises(ValueError):
                action(NoDb(), invalid)
    for value, expected in [('', 0), (None, 0), ('broken', 0), ('7', 7), ('30', 30), ('0', 0), (' 7 ', 0), (7, 0)]:
        monkeypatch.setitem(sys.modules, 'utils', types.SimpleNamespace(get_env_value=lambda _key, v=value: v))
        assert retention.configured_days() == expected
    monkeypatch.setattr(retention, 'configured_days', lambda: 0)
    assert asyncio.run(retention.run_configured_cleanup())['status'] == 'disabled'


def test_record_payload_cache_events_and_empty_run_children_are_deleted_atomically(db):
    run_id = run(db)
    job_id = job(db, run_id, marker='OLD_LARGE_SOURCE_' * 10000)
    cache_key = db.execute('SELECT cache_key FROM news_analysis_jobs').fetchone()[0]
    db.execute('INSERT INTO news_analysis_cache VALUES (?,?)', (cache_key, 'OLD_AI_CACHE_' * 1000))
    db.execute("INSERT INTO news_job_events(job_id,event,attempts,created_at) VALUES (?,'failed',1,?)", (job_id, OLD))
    db.execute('INSERT INTO news_semantic_decisions VALUES (?,?,0.1,0.9,?)', (run_id, job_id, 'fixture'))
    for table, values in [('news_execution_settings',(run_id,'{}')), ('news_bokjiro_progress',(run_id,1)),
            ('news_bokjiro_pages',(run_id,1,'hash')), ('news_collection_issues',(run_id,'1','done')),
            ('news_collection_site_issues',(run_id,'1','failed'))]:
        db.execute(f'INSERT INTO {table} VALUES ({",".join("?" for _ in values)})', values)
    db.commit()
    before = '\n'.join(db.iterdump())
    preview = retention.preview(db, 30, now=NOW)
    assert '\n'.join(db.iterdump()) == before
    assert preview['deleted_records'] == 1
    assert preview['deleted_jobs'] == preview['deleted_candidates'] == preview['deleted_runs'] == 1
    assert preview['payload_bytes'] > 300000
    result = retention.cleanup(db, 30, now=NOW)
    assert result == preview
    for table in ('news_analysis_jobs','news_run_candidates','news_job_events','news_analysis_cache',
            'news_semantic_decisions','news_run_snapshots', *retention._RUN_CHILDREN):
        assert count(db, table) == 0
    assert 'OLD_LARGE_SOURCE_' not in '\n'.join(db.iterdump())
    assert 'OLD_AI_CACHE_' not in '\n'.join(db.iterdump())
    assert count(db, 'news_profiles') == count(db, 'news_profile_versions') == 1
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert retention.cleanup(db, 30, now=NOW)['deleted_jobs'] == 0


@pytest.mark.parametrize('state', ['ready','running','unknown-state'])
def test_active_or_unknown_workflow_protects_every_run_record(db, state):
    identity = run(db, state=state)
    job(db, identity)
    assert retention.cleanup(db, 7, now=NOW)['skipped_active'] == 2
    assert count(db, 'news_analysis_jobs') == count(db, 'news_run_candidates') == 1


@pytest.mark.parametrize('state', ['failed','cancelled','complete'])
def test_old_inactive_pending_records_are_eligible(db, state):
    identity = run(db, state=state)
    job(db, identity, state='waiting')
    assert retention.cleanup(db, 7, now=NOW)['deleted_jobs'] == 1


def test_old_waiting_workflow_keeps_pending_records_without_writing_on_preview(db):
    identity = run(db, state='waiting')
    job(db, identity, state='waiting')
    before = '\n'.join(db.iterdump())
    result = retention.preview(db, 7, now=NOW)
    assert result['deleted_jobs'] == result['deleted_runs'] == 0
    assert result['skipped_active'] == 2
    assert '\n'.join(db.iterdump()) == before
    assert retention.cleanup(db, 7, now=NOW)['deleted_jobs'] == 0


@pytest.mark.parametrize('running,claim', [(True,None),(False,'live-claim')])
def test_running_or_claimed_job_protects_the_whole_run(db, running, claim):
    identity = run(db)
    first = job(db, identity, state='running' if running else 'failed')
    job(db, identity, link='https://example.invalid/other')
    db.execute('UPDATE news_analysis_jobs SET claim_token=? WHERE job_id=?', (claim, first))
    db.commit()
    assert retention.cleanup(db, 7, now=NOW)['skipped_active'] == 4
    assert count(db, 'news_analysis_jobs') == 2


@pytest.mark.parametrize('bad', ['',None,'not-a-date','2026-09-99 12:00:00','2035-01-01 00:00:00'])
def test_unknown_malformed_or_future_workflow_dates_fail_closed(db, bad):
    identity = run(db)
    job(db, identity)
    if bad is None:
        db.execute('DELETE FROM news_profile_workflows WHERE run_id=?', (identity,))
    else:
        db.execute('UPDATE news_profile_workflows SET updated_at=? WHERE run_id=?', (bad, identity))
    db.commit()
    assert retention.cleanup(db, 7, now=NOW)['skipped_unknown_date'] == 2
    assert count(db, 'news_analysis_jobs') == 1


def test_recent_event_or_workflow_and_cutoff_boundary_prevent_deletion(db):
    boundary = (NOW - dt.timedelta(days=7)).isoformat()
    for index, latest in enumerate([RECENT, boundary]):
        identity = run(db)
        created = job(db, identity, link=f'https://example.invalid/{index}')
        db.execute("INSERT INTO news_job_events(job_id,event,attempts,created_at) VALUES (?,'resumed',1,?)", (created, latest))
    db.commit()
    assert retention.cleanup(db, 7, now=NOW)['deleted_jobs'] == 0


@pytest.mark.parametrize('kind', ['stored','archived','dismissed','uploaded','receipt','deleted-receipt','uncertain-notion'])
def test_saved_items_and_notions_and_import_receipts_are_never_removed(db, kind):
    identity = run(db)
    created = job(db, identity)
    link = 'https://example.invalid/notice'
    if kind == 'uncertain-notion':
        db.execute('INSERT INTO notion_upload_attempts VALUES (?,NULL)', (link,))
    elif kind == 'deleted-receipt':
        db.execute('INSERT INTO news_result_imports VALUES (?,NULL,?,?,?,?)', (created,link,'created','fingerprint',OLD))
    else:
        db.execute('INSERT INTO welfare_items VALUES (1,?,?, ?,?,?)', (link,'saved',int(kind=='archived'),int(kind=='dismissed'),'page' if kind=='uploaded' else None))
        if kind == 'receipt':
            db.execute('INSERT INTO news_result_imports VALUES (?,1,?,?,?,?)', (created,link,'created','fingerprint',OLD))
    db.commit()
    before = {table:db.execute(f'SELECT * FROM {table}').fetchall() for table in ('welfare_items','news_result_imports','notion_upload_attempts')}
    assert retention.cleanup(db, 7, now=NOW)['deleted_jobs'] == 0
    assert all(db.execute(f'SELECT * FROM {table}').fetchall() == rows for table, rows in before.items())


def test_ready_job_protects_its_candidate_but_old_unrelated_record_in_same_run_is_removed(db, monkeypatch):
    identity = run(db)
    ready = job(db, identity, state='complete')
    job(db, identity, link='https://example.invalid/rejected')
    monkeypatch.setattr(retention, '_ready_records', lambda _conn,_profiles: ({'job:' + ready},set()))
    result = retention.cleanup(db, 7, now=NOW)
    assert result['deleted_jobs'] == result['deleted_candidates'] == 1
    assert result['deleted_runs'] == 0
    assert db.execute('SELECT job_id FROM news_analysis_jobs').fetchone()[0] == ready


def test_latest_nonready_record_does_not_resurrect_hidden_protected_ready(db, monkeypatch):
    first = job(db, run(db), state='complete')
    old_duplicate = job(db, run(db))
    latest = job(db, run(db))
    monkeypatch.setattr(retention, '_historical_ready', lambda _conn, record: record.get('job_id') == first)
    result = retention.cleanup(db, 7, now=NOW)
    assert result['skipped_resurrection'] == 2
    assert {row[0] for row in db.execute('SELECT job_id FROM news_analysis_jobs')} == {first, latest}
    assert db.execute('SELECT 1 FROM news_analysis_jobs WHERE job_id=?', (old_duplicate,)).fetchone() is None
    assert not any('tombstone' in name for name in retention._tables(db))


def test_complete_url_history_is_removed_when_no_record_needs_protection(db):
    for _index in range(4):
        job(db, run(db))
    result = retention.cleanup(db, 7, now=NOW)
    assert result['deleted_jobs'] == result['deleted_candidates'] == result['deleted_runs'] == 4
    assert result['skipped_resurrection'] == 0


def test_shared_cache_survives_for_retained_job(db):
    job(db, run(db))
    kept = job(db, run(db, date=RECENT))
    cache_key = db.execute('SELECT cache_key FROM news_analysis_jobs WHERE job_id=?', (kept,)).fetchone()[0]
    db.execute('INSERT INTO news_analysis_cache VALUES (?,?)', (cache_key,'SAVED_CACHE'))
    db.commit()
    result = retention.cleanup(db, 7, now=NOW)
    assert result['deleted_jobs'] == 1
    assert result['deleted_cache'] == 0
    assert count(db, 'news_analysis_cache') == 1


def test_official_result_protection_and_source_state_fk_cleanup(db):
    for index, status in enumerate(['match','review','excluded']):
        identity = run(db)
        key = f'bokjiro:{index}'
        db.execute('INSERT INTO news_official_results VALUES (?,?,?,?,?,?,?,?)', (identity,key,str(index),'hash',json.dumps({'link':f'https://example.invalid/official/{index}'}),json.dumps({'target_status':status}),'new',OLD))
        db.execute('INSERT INTO news_official_source_state VALUES (?,?,?,?,?,?)', (key,str(index),'hash',identity,OLD,OLD))
    db.commit()
    result = retention.cleanup(db, 7, now=NOW)
    assert result['deleted_official_results'] == result['deleted_runs'] == 1
    assert count(db, 'news_official_results') == count(db, 'news_official_source_state') == 2
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_unknown_cascade_dependency_and_disabled_foreign_keys_fail_closed(db):
    created = job(db, run(db))
    db.execute('''CREATE TABLE future_user_notes(job_id TEXT,notes TEXT,
        FOREIGN KEY(job_id) REFERENCES news_analysis_jobs(job_id) ON DELETE CASCADE)''')
    db.execute('INSERT INTO future_user_notes VALUES (?,?)', (created,'saved notes'))
    db.commit()
    with pytest.raises(ValueError, match='Unknown news retention dependency'):
        retention.cleanup(db, 7, now=NOW)
    assert count(db, 'news_analysis_jobs') == count(db, 'future_user_notes') == 1
    db.execute('DROP TABLE future_user_notes')
    db.execute('PRAGMA foreign_keys=OFF')
    with pytest.raises(ValueError, match='foreign key enforcement'):
        retention.cleanup(db, 7, now=NOW)


def test_delete_failure_rolls_back_payload_and_dependencies(db):
    identity = run(db)
    created = job(db, identity)
    db.execute('INSERT INTO news_semantic_decisions VALUES (?,?,0.1,0.9,?)', (identity,created,'fixture'))
    db.execute("CREATE TRIGGER reject_retention BEFORE DELETE ON news_analysis_jobs BEGIN SELECT RAISE(ABORT,'fixture rejection'); END")
    db.commit()
    before = '\n'.join(db.iterdump())
    with pytest.raises(sqlite3.IntegrityError):
        retention.cleanup(db, 7, now=NOW)
    assert '\n'.join(db.iterdump()) == before


def test_caller_can_roll_back_cleanup_and_daily_marker(db):
    job(db, run(db))
    db.execute('BEGIN IMMEDIATE')
    assert retention.cleanup(db, 7, now=NOW)['deleted_jobs'] == 1
    db.rollback()
    assert count(db, 'news_analysis_jobs') == 1
    assert 'news_retention_state' not in retention._tables(db)


def test_successful_manual_cleanup_throttles_automatic_runs_for_a_day(db, monkeypatch):
    retention.cleanup(db, 7, now=NOW)
    cleanup = Mock(wraps=retention.cleanup)
    monkeypatch.setattr(retention, 'cleanup', cleanup)
    assert retention._cleanup_if_due(db, 7, NOW + dt.timedelta(hours=23))['status'] == 'not_due'
    cleanup.assert_not_called()
    retention._cleanup_if_due(db, 7, NOW + dt.timedelta(days=1))
    cleanup.assert_called_once()


def test_runtime_obeys_ownership_order_and_releases_after_drained_cancellation(db, monkeypatch):
    import services
    events = []
    monkeypatch.setattr(retention, 'configured_days', lambda: 7)
    lifecycle = types.SimpleNamespace(shutdown_requested=False,active_profile_jobs=0)
    lease = types.SimpleNamespace(close=lambda:events.append('lease-close'))
    ownership = types.SimpleNamespace(acquire=lambda:events.append('lease-acquire') or lease)
    async def acquire(_label):
        events.append('maintenance-acquire')
        return True
    policy = types.SimpleNamespace(try_acquire_maintenance_lock=acquire,
        release_maintenance_lock=lambda:events.append('maintenance-release'))
    async def call_db(work):
        events.append('db-work')
        assert lifecycle.active_profile_jobs == 1
        return work()
    async def settle(awaitable):
        result = await awaitable
        events.append('db-settled')
        return result, True
    modules = {'database':types.SimpleNamespace(run_db=call_db,get_connection=lambda:contextlib.nullcontext(db)),
        'services.app_lifecycle':lifecycle,'services.news_workflow_ownership':ownership,
        'services.policy_sync':policy,'services.news_analysis_worker':types.SimpleNamespace(_settle_db=settle)}
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
        if name.startswith('services.'):
            monkeypatch.setattr(services,name.split('.')[-1],module,raising=False)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(retention.run_configured_cleanup())
    assert events == ['lease-acquire','maintenance-acquire','db-work','db-settled','maintenance-release','lease-close']
    assert lifecycle.active_profile_jobs == 0


@pytest.mark.parametrize('completed', [False, True])
def test_waiting_workflow_schedules_cleanup_only_after_completed_lease_pin_exits(monkeypatch, completed):
    # Extract the narrow hook without importing the app or its operational DB.
    source = Path(__file__).parents[1] / 'services' / 'news_profiles_api.py'
    node = next(node for node in ast.parse(source.read_text(encoding='utf-8')).body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == '_with_workflow_ownership')
    events = []
    owners = {}
    class Ownership:
        transferred = False
        @contextlib.contextmanager
        def pin(self):
            events.append('pin-enter')
            try:
                yield
            finally:
                events.append('pin-exit')
        def close(self):
            events.append('close')
    ownership = Ownership()
    namespace = {'_acquire_workflow_ownership': AsyncMock(return_value=ownership),
                 '_WORKFLOW_OWNERS': owners}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    monkeypatch.setattr(retention, 'schedule_configured_cleanup', lambda: events.append('schedule'))
    async def action(current):
        current.transferred = True
        owners['fixture'] = current
        if completed:
            owners.pop('fixture')
            current.close()  # done callback requests close while the caller is pinned
        events.append('final-read')
        return 'result'
    assert asyncio.run(namespace['_with_workflow_ownership'](action)) == 'result'
    assert events == (['pin-enter', 'close', 'final-read', 'pin-exit', 'schedule'] if completed
                      else ['pin-enter', 'final-read', 'pin-exit'])
