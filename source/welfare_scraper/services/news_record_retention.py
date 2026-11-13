"""Opt-in cleanup of old, unadopted news records on a caller-owned connection.

No operational database is opened at import. Preview is read-only; deletion is
atomic and never touches welfare_items, Notion state, profiles or import receipts.
The runtime entry point also takes the workflow OS lease and maintenance lock.
"""
import asyncio
import datetime as dt
import json
import logging
import sqlite3
from contextlib import contextmanager


_UTC = dt.timezone.utc
_AUTO_TASK = None
_INACTIVE = frozenset({'complete', 'cancelled', 'failed', 'waiting'})
_COUNTS = ('deleted_records', 'deleted_jobs', 'deleted_candidates', 'deleted_official_results',
           'deleted_runs', 'deleted_cache', 'payload_bytes', 'skipped_active',
           'skipped_unknown_date', 'skipped_protected', 'skipped_resurrection')
_RUN_CHILDREN = ('news_execution_settings', 'news_bokjiro_pages', 'news_bokjiro_progress',
                 'news_collection_issues', 'news_collection_site_issues', 'news_profile_workflows')
_REFERENCES = {
    'news_analysis_jobs': {'news_job_events', 'news_run_candidates', 'news_result_imports'},
    'news_run_candidates': {'news_semantic_decisions'},
    'news_run_snapshots': set(_RUN_CHILDREN) | {'news_analysis_jobs', 'news_run_candidates',
                                              'news_official_results', 'news_official_source_state'},
}


def _days(days):
    if type(days) is not int or days not in (0, 7, 30):
        raise ValueError('Retention days must be 0, 7 or 30')
    return days


def configured_days():
    import utils
    try:
        value = utils.get_env_value('NEWS_RECORD_RETENTION_DAYS')
    except Exception:
        return 0
    return int(value) if type(value) is str and value in ('0', '7', '30') else 0


def _now(value):
    value = dt.datetime.now(_UTC) if value is None else value
    if not isinstance(value, dt.datetime):
        raise ValueError('Expected a datetime')
    return value.replace(tzinfo=_UTC) if value.tzinfo is None else value.astimezone(_UTC)


def _timestamp(value):
    if not isinstance(value, str) or len(value) < 19:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.replace(tzinfo=_UTC) if parsed.tzinfo is None else parsed.astimezone(_UTC)
    except (ValueError, OverflowError):
        return None


def _tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _rows(conn, sql, args=()):
    cursor = conn.execute(sql, args)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _empty(days, status='complete'):
    return dict(days=days, enabled=days > 0, status=status, **{key: 0 for key in _COUNTS})


def _json(value):
    try:
        value = json.loads(value)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _check_references(conn, tables):
    # A future ON DELETE CASCADE must not silently broaden retention's scope.
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        for foreign in conn.execute(f'PRAGMA foreign_key_list({quoted})'):
            if foreign[2] in _REFERENCES and table not in _REFERENCES[foreign[2]]:
                raise ValueError('Unknown news retention dependency')


def _protected_urls(conn, tables):
    from services.news_unified_feed import _url
    protected = set()
    for table in ('welfare_items', 'news_result_imports', 'notion_upload_attempts'):
        if table in tables:
            protected.update(_url(row[0]) for row in conn.execute(f'SELECT link FROM {table}'))
    protected.discard('')
    return protected


def _ready_records(conn, profiles):
    from services.news_unified_feed import _read_all
    ready, unreadable = set(), set()
    for profile in profiles:
        try:
            ready.update(row['record_id'] for row in _read_all(conn, profile)
                         if row['view'] == 'ready' and row.get('record_id'))
        except (ValueError, KeyError, TypeError):
            unreadable.add(profile)
    return ready, unreadable


def _historical_ready(conn, record):
    """Conservatively preserve usable results hidden by a newer URL record."""
    from services import news_analysis_store as jobs, news_result_import as imports
    if record['kind'] != 'job' or record['state'] != 'complete':
        return False
    try:
        source, facts, _fingerprint, _revision = imports._read(
            conn, record['profile_id'], record['job_id'], reviewed_job=jobs.get_job(conn, record['job_id']))
        return not imports._ended(source, facts)
    except (ValueError, KeyError, TypeError):
        return False


def _plan(conn, days, now):
    result = _empty(days)
    tables = _tables(conn)
    if 'news_run_snapshots' not in tables:
        return result, [], [], [], tables
    _check_references(conn, tables)
    cutoff = now - dt.timedelta(days=days)
    result['cutoff'] = cutoff.isoformat()
    runs = {row['run_id']: row for row in _rows(conn,
        'SELECT rowid AS run_order,run_id,profile_id,created_at FROM news_run_snapshots')}
    workflows = {row['run_id']: row for row in _rows(conn,
        'SELECT run_id,state,stage,error_code,updated_at FROM news_profile_workflows')} if 'news_profile_workflows' in tables else {}
    active = {run for run, row in workflows.items() if row['state'] not in _INACTIVE}
    from services.news_profile_workflow import can_resume_workflow
    for run, row in workflows.items():
        if row['state'] not in ('waiting', 'failed'):
            continue
        try:
            resumable = can_resume_workflow(conn, run, row['state'], row['stage'], row['error_code'])
        except (ValueError, KeyError, TypeError, sqlite3.DatabaseError):
            resumable = True  # Unreadable recovery state is not permission to delete it.
        if resumable:
            active.add(run)
    if 'news_analysis_jobs' in tables:
        active.update(row[0] for row in conn.execute(
            "SELECT DISTINCT run_id FROM news_analysis_jobs WHERE state='running' OR claim_token IS NOT NULL"))
    event_dates = {}
    if 'news_job_events' in tables:
        for run, value in conn.execute('''SELECT j.run_id,e.created_at FROM news_job_events e
                JOIN news_analysis_jobs j ON j.job_id=e.job_id'''):
            event_dates.setdefault(run, []).append(value)
    dates = {}
    for run, row in runs.items():
        values = [row['created_at']] + event_dates.get(run, [])
        if run in workflows:
            values.append(workflows[run]['updated_at'])
        parsed = [_timestamp(value) for value in values]
        # Future timestamps also fail closed instead of appearing old after arithmetic.
        dates[run] = max(parsed) if parsed and all(value is not None and value <= now for value in parsed) else None
    records = []
    valid_payload = "CASE WHEN json_valid({0}) THEN {0} ELSE '{{}}' END"
    if 'news_analysis_jobs' in tables:
        candidates_order = ('COALESCE((SELECT MAX(c.rowid) FROM news_run_candidates c WHERE c.job_id=j.job_id),0)'
                            if 'news_run_candidates' in tables else '0')
        for row in _rows(conn, f'''SELECT j.rowid AS record_order,j.job_id,j.run_id,j.state,j.cache_key,
                {candidates_order} AS item_order,
                json_extract({valid_payload.format('j.input_json')},'$.link') AS link,
                length(CAST(j.input_json AS BLOB))+COALESCE(length(CAST(j.result_json AS BLOB)),0) AS payload_bytes
                FROM news_analysis_jobs j'''):
            row.update(kind='job', identity='job:' + row['job_id'])
            records.append(row)
    if 'news_run_candidates' in tables:
        for row in _rows(conn, f'''SELECT rowid AS record_order,rowid AS item_order,run_id,candidate_key,
                job_id,route AS state,json_extract({valid_payload.format('payload_json')},'$.link') AS link,
                length(CAST(payload_json AS BLOB)) AS payload_bytes FROM news_run_candidates'''):
            row.update(kind='candidate', identity='candidate:' + row['run_id'] + ':' + row['candidate_key'])
            records.append(row)
    if 'news_official_results' in tables:
        for row in _rows(conn, f'''SELECT rowid AS record_order,rowid AS item_order,run_id,source_key,created_at,
                json_extract({valid_payload.format('payload_json')},'$.link') AS link,
                json_extract({valid_payload.format('result_json')},'$.target_status') AS target_status,
                length(CAST(payload_json AS BLOB))+length(CAST(result_json AS BLOB)) AS payload_bytes
                FROM news_official_results'''):
            row.update(kind='official', identity='official:' + row['run_id'] + ':' + row['source_key'],
                       state='complete', job_id=None)
            records.append(row)
    from services.news_unified_feed import _url
    protected_urls = _protected_urls(conn, tables)
    receipt_jobs = {row[0] for row in conn.execute('SELECT job_id FROM news_result_imports')} if 'news_result_imports' in tables else set()
    ready, unreadable = _ready_records(conn, {row['profile_id'] for row in runs.values()})
    removable, reasons = set(), {}
    for record in records:
        run = runs.get(record['run_id'])
        if run is None:
            raise ValueError('Orphan news record')
        record.update(profile_id=run['profile_id'], run_order=run['run_order'], url=_url(record['link']))
        date = dates[record['run_id']]
        own_date = _timestamp(record.get('created_at')) if record['kind'] == 'official' else None
        # Jobs/candidates have no creation column. A workflow update bounds later
        # additions/resumes; without it their individual age cannot be proved.
        unknown = (date is None or (record['kind'] != 'official' and record['run_id'] not in workflows)
                   or (record['kind'] == 'official' and (own_date is None or own_date > now)))
        if record['run_id'] in active:
            reasons[record['identity']] = 'skipped_active'
        elif unknown:
            reasons[record['identity']] = 'skipped_unknown_date'
        elif max(date, own_date or date) >= cutoff:
            reasons[record['identity']] = 'recent'
        elif (not record['url'] or record['url'] in protected_urls or record.get('job_id') in receipt_jobs
              or record['identity'] in ready or record['profile_id'] in unreadable
              or (record['kind'] == 'official' and record.get('target_status') != 'excluded')
              or _historical_ready(conn, record)):
            reasons[record['identity']] = 'skipped_protected'
        else:
            removable.add(record['identity'])
    # Linked candidate/job rows form one deletion unit, even for malformed old
    # cross-run references. Never remove a protected record through a dependency.
    components = {}
    for record in records:
        if record.get('job_id'):
            components.setdefault(record['job_id'], []).append(record['identity'])
    for members in components.values():
        if any(identity not in removable for identity in members):
            for identity in members:
                if identity in removable:
                    removable.remove(identity)
                    reasons[identity] = 'skipped_protected'
    # No new tombstones accumulate. Keep a newest suppression record whenever
    # removing it would expose any older retained version of the same URL.
    families = {}
    for record in records:
        if record['kind'] == 'candidate' and record.get('job_id'):
            continue
        family = (record['profile_id'], record['url'], 'official' if record['kind'] == 'official' else 'site')
        families.setdefault(family, []).append(record)
    for family in families.values():
        family.sort(key=lambda row: (row['run_order'], row['item_order'], row['record_order']), reverse=True)
        if family[0]['identity'] in removable and any(row['identity'] not in removable for row in family[1:]):
            newest = family[0]
            for identity in components.get(newest.get('job_id'), [newest['identity']]):
                removable.discard(identity)
                reasons[identity] = 'skipped_resurrection'
    selected = [record for record in records if record['identity'] in removable]
    for reason in reasons.values():
        if reason in result:
            result[reason] += 1
    for kind, name in [('job', 'deleted_jobs'), ('candidate', 'deleted_candidates'), ('official', 'deleted_official_results')]:
        result[name] = sum(record['kind'] == kind for record in selected)
    result['deleted_records'] = sum(record['kind'] != 'candidate' or not record.get('job_id') for record in selected)
    result['payload_bytes'] = sum(record['payload_bytes'] or 0 for record in selected)
    kept_runs = {record['run_id'] for record in records if record['identity'] not in removable}
    empty_runs = [run for run in runs if run not in kept_runs and run not in active
                  and dates[run] is not None and dates[run] < cutoff]
    result['deleted_runs'] = len(empty_runs)
    cache_keys = {record['cache_key'] for record in selected if record['kind'] == 'job'}
    kept_cache = {record['cache_key'] for record in records if record['kind'] == 'job' and record['identity'] not in removable}
    cache_keys -= kept_cache
    if 'news_analysis_cache' in tables:
        cached = {key: size for key, size in conn.execute('SELECT cache_key,length(CAST(result_json AS BLOB)) FROM news_analysis_cache')}
        cache_keys &= cached.keys()
        result['deleted_cache'] = len(cache_keys)
        result['payload_bytes'] += sum(cached[key] for key in cache_keys)
    else:
        cache_keys.clear()
    return result, selected, empty_runs, cache_keys, tables


def preview(conn, days, now=None):
    days = _days(days)
    return _empty(days, 'disabled') if not days else _plan(conn, days, _now(now))[0]


@contextmanager
def _transaction(conn):
    owns = not conn.in_transaction
    if owns:
        conn.execute('BEGIN IMMEDIATE')
    from services.news_profile_store import _atomic
    try:
        with _atomic(conn):
            yield
        if owns:
            conn.commit()
    except BaseException:
        if owns:
            conn.rollback()
        raise


def cleanup(conn, days, now=None):
    days = _days(days)
    if not days:
        return _empty(days, 'disabled')
    now = _now(now)
    if conn.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise ValueError('News retention requires foreign key enforcement')
    with _transaction(conn):
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('News retention found invalid references')
        result, records, runs, cache_keys, tables = _plan(conn, days, now)
        for record in records:
            if record['kind'] == 'candidate':
                args = (record['run_id'], record['candidate_key'])
                if 'news_semantic_decisions' in tables:
                    conn.execute('DELETE FROM news_semantic_decisions WHERE run_id=? AND candidate_key=?', args)
                conn.execute('DELETE FROM news_run_candidates WHERE run_id=? AND candidate_key=?', args)
        for record in records:
            if record['kind'] == 'job':
                if 'news_job_events' in tables:
                    conn.execute('DELETE FROM news_job_events WHERE job_id=?', (record['job_id'],))
                conn.execute('DELETE FROM news_analysis_jobs WHERE job_id=?', (record['job_id'],))
            elif record['kind'] == 'official':
                conn.execute('DELETE FROM news_official_results WHERE run_id=? AND source_key=?', (record['run_id'], record['source_key']))
        for key in cache_keys:
            conn.execute('DELETE FROM news_analysis_cache WHERE cache_key=?', (key,))
        for run in runs:
            if 'news_official_source_state' in tables:
                for source_key, in conn.execute('SELECT source_key FROM news_official_source_state WHERE latest_run_id=?', (run,)).fetchall():
                    surviving = conn.execute('''SELECT o.run_id,o.fingerprint FROM news_official_results o
                        JOIN news_run_snapshots r ON r.run_id=o.run_id WHERE o.source_key=?
                        ORDER BY r.rowid DESC,o.rowid DESC LIMIT 1''', (source_key,)).fetchone()
                    if surviving:
                        conn.execute('UPDATE news_official_source_state SET latest_run_id=?,latest_fingerprint=? WHERE source_key=?', (*surviving, source_key))
                    else:
                        conn.execute('DELETE FROM news_official_source_state WHERE source_key=?', (source_key,))
            for table in _RUN_CHILDREN:
                if table in tables:
                    conn.execute(f'DELETE FROM {table} WHERE run_id=?', (run,))
            conn.execute('DELETE FROM news_run_snapshots WHERE run_id=?', (run,))
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('News retention reference check failed')
        conn.execute('''CREATE TABLE IF NOT EXISTS news_retention_state (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),last_cleanup_at TEXT NOT NULL)''')
        conn.execute('''INSERT INTO news_retention_state VALUES (1,?) ON CONFLICT(singleton)
            DO UPDATE SET last_cleanup_at=excluded.last_cleanup_at''', (now.isoformat(),))
        return result


def _cleanup_if_due(conn, days, now):
    if 'news_retention_state' in _tables(conn):
        row = conn.execute('SELECT last_cleanup_at FROM news_retention_state WHERE singleton=1').fetchone()
        if row:
            previous = _timestamp(row[0])
            if previous is None or now - previous < dt.timedelta(days=1):
                return _empty(days, 'not_due')
    return cleanup(conn, days, now=now)


async def run_configured_cleanup():
    """Best effort once per day, with the same ownership order as collection."""
    days = configured_days()
    if not days:
        return _empty(0, 'disabled')
    import database
    from services import app_lifecycle, news_workflow_ownership, policy_sync
    from services.news_analysis_worker import _settle_db
    if app_lifecycle.shutdown_requested:
        return _empty(days, 'shutdown')
    lease = news_workflow_ownership.acquire()
    if lease is None:
        return _empty(days, 'busy')
    locked = False
    try:
        locked = await policy_sync.try_acquire_maintenance_lock('오래된 뉴스 수집 기록 정리')
        if not locked:
            return _empty(days, 'busy')
        app_lifecycle.active_profile_jobs += 1
        def work():
            with database.get_connection() as conn:
                return _cleanup_if_due(conn, days, _now(None))
        result, cancelled = await _settle_db(database.run_db(work))
        if cancelled:
            raise asyncio.CancelledError
        return result
    finally:
        if locked:
            app_lifecycle.active_profile_jobs -= 1
            policy_sync.release_maintenance_lock()
        lease.close()


def schedule_configured_cleanup():
    """Track one post-workflow task; configuration defaults to disabled."""
    global _AUTO_TASK
    if _AUTO_TASK is not None and not _AUTO_TASK.done():
        return
    if not configured_days():
        return
    async def perform():
        try:
            await run_configured_cleanup()
        except asyncio.CancelledError:
            pass
        except Exception:
            logging.getLogger(__name__).warning('오래된 뉴스 기록 정리를 완료하지 못했습니다. 기존 기록은 유지됩니다.')
    _AUTO_TASK = asyncio.create_task(perform())
