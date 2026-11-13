"""Tracked, resumable collection and analysis for manual and scheduled runs."""
import asyncio
import json
import logging
import uuid

from services import app_lifecycle
from services.news_analysis_worker import db_call, _settle_db
from services.news_profile_store import _atomic, get_run_snapshot
from services.news_site_collector import collect_registered_sites
from services.news_bokjiro_collector import collect_bokjiro_run
from services.news_run_executor import (
    execute_registered_run, RECOVERABLE_FAILURES, MANUAL_REVIEW_FAILURES, _review_summary,
    _diagnostic_id, _diagnostic_reason, _diagnostic_count)
from services.news_auto_sources import (
    retired_source_scope, SOURCE_SCOPE_CHANGED_REASON, SOURCE_SCOPE_CHANGED_MESSAGE)
from services.news_workflow_progress import initial_progress, analysis_progress, ProgressReporter

logger = logging.getLogger(__name__)


def _log_terminal(workflow, *, exception=False, state_persisted=True):
    """Log distinct job/candidate counts without source, settings or exception text."""
    try:
        state, stage = workflow.get('state'), workflow.get('stage')
        summary = workflow.get('summary') or {}
        analysis = summary.get('analysis') or {}
        progress = workflow.get('progress') or {}
        reviews = analysis.get('review_reasons') or {}
        review_counts = {reason: count for reason in MANUAL_REVIEW_FAILURES
                         if (count := _diagnostic_count(reviews.get(reason))) is not None and count > 0}
        held = _diagnostic_count(analysis.get('review_required_count'))
        record = dict(workflow_id=_diagnostic_id(workflow.get('workflow_id')),
                      run_id=_diagnostic_id(workflow.get('run_id')),
                      state=state if state in ('complete', 'waiting', 'failed', 'cancelled') else 'unknown',
                      stage=stage if stage in ('prepared', 'collecting', 'analyzing', 'finished',
                                              'failed', 'cancelled') else 'unknown',
                      error_code=_diagnostic_reason(workflow.get('error_code')),
                      exception=exception, state_persisted=state_persisted,
                      analysis_jobs_review_required=held if held is not None else sum(review_counts.values()),
                      analysis_job_review_reasons=review_counts)
        record['outcome'] = ('workflow_exception' if exception else 'complete_with_review'
                             if state == 'complete' and record['analysis_jobs_review_required'] else record['state'])
        failed = _diagnostic_count(analysis.get('failed_jobs'))
        if failed is not None:
            record['analysis_jobs_failed'] = failed
        for key in ('analysis_total', 'analysis_processed', 'analysis_remaining', 'analysis_complete',
                    'analysis_review_required', 'analysis_failed', 'analysis_running', 'saved_candidates'):
            count = _diagnostic_count(progress.get(key))
            if count is not None:
                record['candidate_' + key] = count
        log = logger.error if exception or state == 'failed' else logger.info
        log('news_workflow_terminal %s', json.dumps(record, sort_keys=True))
    except Exception:
        # A logging failure must never turn successful work into a failed run.
        pass


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS news_profile_workflows (
        workflow_id TEXT PRIMARY KEY,run_id TEXT NOT NULL UNIQUE,state TEXT NOT NULL,
        stage TEXT NOT NULL,error_code TEXT,summary_json TEXT NOT NULL DEFAULT '{}',
        settings_json TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(run_id) REFERENCES news_run_snapshots(run_id))''')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(news_profile_workflows)')}
    if 'settings_json' not in columns:
        conn.execute("ALTER TABLE news_profile_workflows ADD COLUMN settings_json TEXT NOT NULL DEFAULT '{}'")
    if 'progress_json' not in columns:
        conn.execute("ALTER TABLE news_profile_workflows ADD COLUMN progress_json TEXT NOT NULL DEFAULT '{}'")
    if 'history_dismissed' not in columns:
        conn.execute('ALTER TABLE news_profile_workflows ADD COLUMN history_dismissed INTEGER NOT NULL DEFAULT 0')


def create_workflow(conn, run_id, *, settings=None):
    with _atomic(conn):
        snapshot = get_run_snapshot(conn, run_id)
        ensure_schema(conn)
        workflow_id = uuid.uuid4().hex
        encoded = json.dumps(settings or {}, sort_keys=True, allow_nan=False)
        progress = json.dumps(initial_progress(len(snapshot['sites'])), sort_keys=True)
        conn.execute('''INSERT INTO news_profile_workflows
            (workflow_id,run_id,state,stage,settings_json,progress_json) VALUES (?,?,?,?,?,?)''',
                     (workflow_id, run_id, 'ready', 'prepared', encoded, progress))
        return workflow_id


def _transition(conn, workflow_id, state, stage, *, summary=None, error_code=None):
    allowed = {
        ('ready', 'running'), ('ready', 'cancelled'), ('waiting', 'running'),
        ('waiting', 'cancelled'), ('failed', 'waiting'),
        ('running', 'running'), ('running', 'complete'),
        ('running', 'waiting'), ('running', 'failed'), ('running', 'cancelled')}
    with _atomic(conn):
        row = conn.execute('SELECT state FROM news_profile_workflows WHERE workflow_id=?', (workflow_id,)).fetchone()
        if row is None:
            raise KeyError(workflow_id)
        if (row[0], state) not in allowed:
            raise ValueError('Invalid workflow transition')
        encoded = json.dumps(summary or {}, ensure_ascii=False, sort_keys=True, allow_nan=False)
        conn.execute('''UPDATE news_profile_workflows SET state=?,stage=?,error_code=?,summary_json=?,
            updated_at=CURRENT_TIMESTAMP WHERE workflow_id=?''',
                     (state, stage, error_code, encoded, workflow_id))


def cancel_if_ready(conn, workflow_id):
    """실행 중인 task가 없는 준비/대기 작업을 사용자가 명시적으로 종료한다."""
    with _atomic(conn):
        current = get_workflow(conn, workflow_id)
        if current['state'] in ('ready', 'waiting'):
            _transition(conn, workflow_id, 'cancelled', 'cancelled', error_code='cancelled')
            return True
        return False


def recover_interrupted_workflows(conn):
    """프로세스 재시작 뒤 소유 task가 사라진 ready/running 작업을 재개 가능하게 만든다.

    반드시 news_workflow_ownership OS lease를 보유한 admission에서만 호출한다.
    현재 프로세스의 task 목록이 비었다는 이유만으로 다른 GUI/예약 프로세스의
    running 분석 job을 회수해서는 안 된다.
    """
    from services.news_analysis_store import recover_interrupted_run
    ensure_schema(conn)
    rows = conn.execute("""SELECT workflow_id,run_id,state FROM news_profile_workflows
        WHERE state IN ('ready','running')""").fetchall()
    recovered = 0
    with _atomic(conn):
        for workflow_id, run_id, _state in rows:
            recover_interrupted_run(conn, run_id)
            conn.execute("""UPDATE news_profile_workflows
                SET state='waiting',error_code='interrupted',updated_at=CURRENT_TIMESTAMP
                WHERE workflow_id=? AND state IN ('ready','running')""", (workflow_id,))
            recovered += 1
    return recovered


def can_resume_workflow(conn, run_id, state, stage, error_code):
    """Read-only resume contract shared with retention; never creates schemas."""
    if state not in ('waiting', 'failed'):
        return False
    failures = [item[0] for item in conn.execute(
        "SELECT reason FROM news_analysis_jobs WHERE run_id=? AND state='failed'", (run_id,))
        if item[0] not in MANUAL_REVIEW_FAILURES]
    resumable_failures = not failures or all(reason in RECOVERABLE_FAILURES for reason in failures)
    resumable = (state == 'waiting' and (stage in ('prepared', 'collecting') or resumable_failures)) or (
        state == 'failed' and ((error_code in MANUAL_REVIEW_FAILURES and resumable_failures)
                              or (error_code == 'analysis_failed' and bool(_recoverable_failed_jobs(conn, run_id)))))
    return bool(resumable and not retired_source_scope(get_run_snapshot(conn, run_id)['profile']))


def get_workflow(conn, workflow_id):
    ensure_schema(conn)
    row = conn.execute('''SELECT workflow_id,run_id,state,stage,error_code,summary_json,updated_at,progress_json
        FROM news_profile_workflows WHERE workflow_id=?''', (workflow_id,)).fetchone()
    if row is None:
        raise KeyError(workflow_id)
    revision = conn.execute('SELECT revision FROM news_run_snapshots WHERE run_id=?', (row[1],)).fetchone()[0]
    can_resume = can_resume_workflow(conn, row[1], row[2], row[3], row[4])
    result = dict(workflow_id=row[0], run_id=row[1], state=row[2], stage=row[3], error_code=row[4],
                summary=json.loads(row[5]), updated_at=row[6], revision=revision, can_resume=can_resume)
    progress = json.loads(row[7])
    if progress:
        # Job receipts are already durable on each item, so status polling stays
        # accurate even during the reporter's short write-throttle window.
        # A completed run's final zero-remaining snapshot cannot be resumed.
        # Reuse it when polling history instead of re-aggregating every old run.
        # Missing/older snapshots and cancellation still read durable receipts.
        final_analysis = (row[2] == 'complete'
                          and type(progress.get('analysis_remaining')) is int
                          and progress['analysis_remaining'] == 0
                          and type(progress.get('analysis_running')) is int
                          and progress['analysis_running'] == 0)
        if progress.get('phase') == 'analysis' and not final_analysis:
            progress.update(analysis_progress(conn, row[1]))
        result['progress'] = progress
    if retired_source_scope(get_run_snapshot(conn, row[1])['profile']):
        result.update(can_resume=False, resume_block_reason=SOURCE_SCOPE_CHANGED_REASON,
                      resume_block_message=SOURCE_SCOPE_CHANGED_MESSAGE)
    result['can_clear_history'] = ((row[2] in ('complete', 'cancelled', 'failed')
        or (row[2] == 'waiting' and result.get('resume_block_reason') == SOURCE_SCOPE_CHANGED_REASON))
        and not result['can_resume'] and not conn.execute(
            "SELECT 1 FROM news_analysis_jobs WHERE run_id=? AND (state='running' OR claim_token IS NOT NULL) LIMIT 1",
            (row[1],)).fetchone())
    return result


def _recoverable_failed_jobs(conn, run_id):
    rows = conn.execute("SELECT job_id,attempts,reason FROM news_analysis_jobs WHERE run_id=? AND state='failed'",
                        (run_id,)).fetchall()
    rows = [row for row in rows if row[2] not in MANUAL_REVIEW_FAILURES]
    return rows if rows and all(row[2] in RECOVERABLE_FAILURES for row in rows) else []


def prepare_resume(conn, workflow_id, *, resume_analysis=True):
    """Collection resumes preserve jobs; analysis resumes require AI preflight."""
    from services.news_analysis_store import resume_job
    with _atomic(conn):
        current = get_workflow(conn, workflow_id)
        if not current['can_resume']:
            raise ValueError('This workflow cannot resume')
        if resume_analysis:
            for job_id, attempts, _reason in _recoverable_failed_jobs(conn, current['run_id']):
                resume_job(conn, job_id, expected_attempts=attempts)
        if current['state'] == 'failed':
            stage = 'analyzing' if isinstance(current['summary'].get('collection'), dict) else 'collecting'
            _transition(conn, workflow_id, 'waiting', stage, summary=current['summary'])


def get_workflow_settings(conn, workflow_id):
    ensure_schema(conn)
    row = conn.execute('SELECT settings_json FROM news_profile_workflows WHERE workflow_id=?', (workflow_id,)).fetchone()
    if row is None:
        raise KeyError(workflow_id)
    value = json.loads(row[0])
    if not isinstance(value, dict):
        raise ValueError('Invalid workflow settings')
    return value


def initialize_analysis_settings(conn, workflow_id, *, expected, settings):
    """One explicit first setup, never a recipe/model change after execution.

    Collection-only candidates contain the original snapshot/body but no jobs.
    Bind their first analysis recipe and settings in a single transaction.
    """
    from services.news_analysis_context import NewsAnalysisContext
    from services import news_analysis_store as jobs
    from services.news_custom_analysis import RECIPE
    with _atomic(conn):
        current = get_workflow(conn, workflow_id)
        saved = get_workflow_settings(conn, workflow_id)
        if (saved != expected or saved.get('mode') != 'analysis-setup-required-v1'
                or current['state'] != 'waiting' or current['stage'] != 'analyzing'
                or settings.get('mode') == 'analysis-setup-required-v1'
                or not isinstance(settings.get('analysis_model'), str)
                or not settings['analysis_model'].strip()):
            raise ValueError('Initial analysis setup is not available')
        run_id = current['run_id']
        if conn.execute('SELECT 1 FROM news_analysis_jobs WHERE run_id=? LIMIT 1', (run_id,)).fetchone():
            raise ValueError('Existing analysis jobs must retain their settings')
        for table in ('news_execution_settings', 'news_semantic_decisions'):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                if conn.execute('SELECT 1 FROM ' + table + ' WHERE run_id=? LIMIT 1', (run_id,)).fetchone():
                    raise ValueError('Existing analysis execution must retain its settings')
        context = NewsAnalysisContext.from_run(conn, run_id)
        if context.profile.general_mode != 'custom_v1':
            raise ValueError('Initial analysis setup requires custom rules')
        rows = conn.execute('SELECT candidate_key,payload_json,route,job_id FROM news_run_candidates WHERE run_id=?', (run_id,)).fetchall()
        if any(route not in ('excluded','analysis_unconfigured') or job_id is not None
               for _key, _payload, route, job_id in rows):
            raise ValueError('Previously routed candidates cannot change analysis setup')
        for key, encoded, route, _job_id in rows:
            if route == 'excluded':
                continue
            payload = json.loads(encoded)
            route, _ = context.route_candidate(payload['title'], payload['content'])
            from services.news_publication import analysis_payload
            job_id = jobs.enqueue(conn, run_id, payload=analysis_payload(payload), model=settings['analysis_model'], recipe=RECIPE) if route == 'matched' else None
            conn.execute('UPDATE news_run_candidates SET route=?,job_id=? WHERE run_id=? AND candidate_key=?',
                         (route, job_id, run_id, key))
        conn.execute('UPDATE news_profile_workflows SET settings_json=? WHERE workflow_id=?',
                     (json.dumps(settings, sort_keys=True, allow_nan=False), workflow_id))


def _pending_analysis_count(conn, run_id):
    candidates = conn.execute("SELECT COUNT(*) FROM news_run_candidates WHERE run_id=? AND route IN ('semantic_candidate','analysis_unconfigured')", (run_id,)).fetchone()[0]
    active_jobs = conn.execute("SELECT COUNT(*) FROM news_analysis_jobs WHERE run_id=? AND state IN ('queued','waiting','running')", (run_id,)).fetchone()[0]
    failed_jobs = sum(reason not in MANUAL_REVIEW_FAILURES for (reason,) in conn.execute(
        "SELECT reason FROM news_analysis_jobs WHERE run_id=? AND state='failed'", (run_id,)))
    return candidates + active_jobs + failed_jobs


def list_workflows(conn, profile_id, *, limit=20):
    from services.news_profile_store import get_profile
    get_profile(conn, profile_id)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Invalid limit')
    ensure_schema(conn)
    ids = [row[0] for row in conn.execute('''SELECT w.workflow_id FROM news_profile_workflows w
        JOIN news_run_snapshots r ON w.run_id=r.run_id WHERE r.profile_id=? AND w.history_dismissed=0
        ORDER BY w.rowid DESC LIMIT ?''', (profile_id, limit))]
    return [get_workflow(conn, workflow_id) for workflow_id in ids]


def _completed_source(previous_collection, source):
    """Return a persisted, completed source summary that is safe to reuse."""
    if not isinstance(previous_collection, dict):
        return None
    sources = previous_collection.get('sources')
    if not isinstance(sources, dict):
        return None
    summary = sources.get(source)
    if not isinstance(summary, dict) or summary.get('state') != 'finished':
        return None
    return dict(summary)


async def collect_profile_sources(run_id, *, analysis_model, headless=True, call_db=db_call,
                                  previous_collection=None,
                                  site_collector=collect_registered_sites,
                                  bokjiro_collector=collect_bokjiro_run, defer_analysis=False,
                                  progress=None):
    """Collect selected sources, reusing only sources that already finished."""
    snapshot = await call_db(get_run_snapshot, run_id)
    profile = snapshot['profile']
    if retired_source_scope(profile):
        raise ValueError(SOURCE_SCOPE_CHANGED_MESSAGE)
    sources = profile['sources']
    summaries = {}
    total_registered = 0
    if 'sites' in sources:
        site_summary = _completed_source(previous_collection, 'sites')
        if site_summary is None:
            site_summary = await site_collector(run_id, analysis_model=analysis_model,
                headless=headless, call_db=call_db,
                **({'progress': progress} if progress is not None else {}),
                **({'defer_analysis':True} if defer_analysis else {}))
        summaries['sites'] = site_summary
        if site_summary['state'] in ('busy', 'shutdown', 'waiting'):
            return {**site_summary, 'sources': summaries, 'analysis_required': True}
        total_registered += int(site_summary.get('registered', 0) or 0)
    if 'bokjiro' in sources:
        bokjiro_summary = _completed_source(previous_collection, 'bokjiro')
        if bokjiro_summary is None:
            bokjiro_summary = await bokjiro_collector(run_id, call_db=call_db)
        summaries['bokjiro'] = bokjiro_summary
        if bokjiro_summary['state'] in ('busy', 'shutdown', 'waiting', 'more_pages'):
            return {**bokjiro_summary, 'sources': summaries,
                    'analysis_required': 'sites' in sources}
        total_registered += int(bokjiro_summary.get('registered', 0) or 0)
    site_summary = summaries.get('sites', {})
    return {
        'state': 'finished', 'sources': summaries, 'registered': total_registered,
        'sites': int(site_summary.get('sites', 0) or 0),
        'content_failed': int(site_summary.get('content_failed', 0) or 0),
        'skipped_candidates': int(site_summary.get('skipped_candidates', 0) or 0),
        'vision_review_sites': int(site_summary.get('vision_review_sites', 0) or 0),
        'site_review_required': int(site_summary.get('site_review_required', 0) or 0),
        'site_failed': int(site_summary.get('site_failed', 0) or 0),
        'site_partial': int(site_summary.get('site_partial', 0) or 0),
        'site_issues': list(site_summary.get('site_issues') or []),
        'attachments_analyzed': int(site_summary.get('attachments_analyzed', 0) or 0),
        'attachment_review': int(site_summary.get('attachment_review', 0) or 0),
        'attachments_reused': int(site_summary.get('attachments_reused', 0) or 0),
        'analysis_required': 'sites' in sources,
    }


def _cancel_after_drain(conn, workflow_id):
    # Cancellation can race the final completion commit. Preserve a terminal
    # result instead of attempting an invalid complete -> cancelled transition.
    with _atomic(conn):
        current = get_workflow(conn, workflow_id)
        if current['state'] in ('ready', 'running', 'waiting'):
            _transition(conn, workflow_id, 'cancelled', 'cancelled', error_code='cancelled')


async def run_workflow(workflow_id, *, client, scorer, analysis_model, threshold,
                       headless=True, call_db=db_call, collector=collect_profile_sources,
                       executor=execute_registered_run, analysis_wait_reason=None,
                       defer_analysis=False):
    """Run one workflow exactly once; persisted waiting work can be resumed later."""
    raw_call_db = call_db
    async def settled_call_db(*args, **kwargs):
        result, cancelled = await _settle_db(raw_call_db(*args, **kwargs))
        if cancelled:
            raise asyncio.CancelledError
        return result
    # Includes stage transitions and collector/executor calls. Cancelling a
    # to_thread await must not release the workflow lease ahead of its commit.
    call_db = settled_call_db
    # Old source selections are immutable history, not permission to launch a
    # retired automatic source. Refuse before changing the saved workflow.
    current = await call_db(get_workflow, workflow_id)
    if current.get('resume_block_reason') == SOURCE_SCOPE_CHANGED_REASON:
        raise ValueError(SOURCE_SCOPE_CHANGED_MESSAGE)
    app_lifecycle.active_profile_jobs += 1
    progress = ProgressReporter(workflow_id, current['run_id'], current.get('progress') or {}, call_db)
    try:
        if current['state'] not in ('ready', 'waiting'):
            raise ValueError('Workflow is not ready to run')
        collection = current['summary'].get('collection') if current['stage'] == 'analyzing' else None
        previous_collection = (current['summary'].get('collection')
                               if current['stage'] == 'collecting' else None)
        while collection is None:
            collecting_summary = ({'collection': previous_collection}
                                  if previous_collection is not None else None)
            await call_db(_transition, workflow_id, 'running', 'collecting',
                          summary=collecting_summary)
            await progress({'phase': 'sites'}, force=True)
            collection = await collector(current['run_id'], analysis_model=analysis_model,
                headless=headless, call_db=call_db, previous_collection=previous_collection,
                progress=progress,
                **({'defer_analysis':True} if defer_analysis else {}))
            if collection['state'] == 'more_pages' and collection.get('pages_processed', 0) > 0:
                await call_db(_transition, workflow_id, 'running', 'collecting', summary={'collection': collection})
                previous_collection = collection
                collection = None
                await asyncio.sleep(0)
                continue
            if collection['state'] in ('busy', 'shutdown', 'waiting', 'more_pages'):
                await progress(force=True)
                await call_db(_transition, workflow_id, 'waiting', 'collecting', summary={'collection': collection},
                              error_code=collection.get('stop_reason') or collection['state'])
                result = await call_db(get_workflow, workflow_id)
                _log_terminal(result)
                return result
        await call_db(_transition, workflow_id, 'running', 'analyzing', summary={'collection': collection})
        await progress({'phase': 'analysis'}, force=True)
        if collection.get('analysis_required', True) and analysis_wait_reason:
            remaining = await call_db(_pending_analysis_count, current['run_id'])
            analysis = {'state':'pass_finished', 'jobs_processed':0, 'semantic_processed':0,
                        'remaining':remaining, 'work_remaining':bool(remaining),
                        'stop_reason':analysis_wait_reason, 'ai_called':False}
            analysis.update(await call_db(_review_summary, current['run_id']))
            if not remaining and analysis['review_required']:
                analysis['completion_status'] = 'complete_with_review'
        elif collection.get('analysis_required', True):
            while True:
                analysis = await executor(current['run_id'], client=client, scorer=scorer,
                    analysis_model=analysis_model, threshold=threshold, call_db=call_db, progress=progress)
                await call_db(_transition, workflow_id, 'running', 'analyzing',
                              summary={'collection': collection, 'analysis': analysis})
                if (analysis.get('stop_reason') != 'batch_limit' or not analysis.get('work_remaining')
                        or analysis.get('state') == 'failed'
                        or not (analysis.get('jobs_processed') or analysis.get('semantic_processed'))):
                    break
                # Internal pagination is not a user-visible stop. Cancellation
                # and provider limits are still observed on every bounded pass.
                await asyncio.sleep(0)
        else:
            analysis = {'state': 'not_required', 'ai_called': False,
                        'semantic_processed': 0, 'jobs_processed': 0}
        if analysis.get('state') == 'failed':
            final_state = 'failed'
        elif analysis['state'] == 'busy' or analysis.get('work_remaining'):
            final_state = 'waiting'
        else:
            final_state = 'complete'
        final_stage = 'finished' if final_state == 'complete' else ('failed' if final_state == 'failed' else 'analyzing')
        await progress(force=True)
        await call_db(_transition, workflow_id, final_state, final_stage,
                      summary={'collection': collection, 'analysis': analysis},
                      error_code=(analysis.get('stop_reason') or 'busy') if final_state != 'complete' else None)
        result = await call_db(get_workflow, workflow_id)
        _log_terminal(result)
        return result
    except asyncio.CancelledError:
        await _settle_db(raw_call_db(_cancel_after_drain, workflow_id))
        try:
            result, _ = await _settle_db(raw_call_db(get_workflow, workflow_id))
            _log_terminal(result)
        except Exception:
            pass
        raise
    except Exception:
        # Never persist raw provider, URL, DB, or instruction error text.
        state_persisted = False
        try:
            await call_db(_transition, workflow_id, 'failed', 'failed', error_code='workflow_failed')
            state_persisted = True
        except Exception:
            pass
        _log_terminal(dict(workflow_id=workflow_id, run_id=current['run_id'], state='failed',
                           stage='failed', error_code='workflow_failed', progress=progress.current),
                      exception=True, state_persisted=state_persisted)
        raise
    finally:
        app_lifecycle.active_profile_jobs = max(0, app_lifecycle.active_profile_jobs - 1)
