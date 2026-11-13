"""One bounded analysis pass; workflows continue pagination and preserve pauses."""
import asyncio
import json
import logging
import math
import re
import time
import uuid

from services import ai_coordinator
from services.news_profile_store import _atomic, get_run_snapshot
from services.news_analysis_worker import db_call, _settle_db, process_job
from services.news_semantic_scorer import SemanticScorer, process_with_embeddings
from services.news_direct_analysis import DirectAnalysis, queue_candidates
from services.news_analysis_store import get_job

RECOVERABLE_FAILURES = frozenset({'daily_quota_exhausted', 'local_budget_exhausted', 'authentication_required',
    'retry_limit_reached', 'analysis_error', 'service_unavailable', 'rate_limited'})
MANUAL_REVIEW_FAILURES = ('ai_input_review_required', 'ai_input_size_review_required',
                         'ai_response_review_required', 'ai_source_review_required')

logger = logging.getLogger(__name__)
_DIAGNOSTIC_REASONS = RECOVERABLE_FAILURES | frozenset(MANUAL_REVIEW_FAILURES) | {
    'invalid_analysis', 'invalid_request', 'analysis_failed', 'workflow_failed',
    'cancelled', 'busy', 'shutdown', 'waiting', 'more_pages', 'batch_limit',
    'scheduled_wait', 'scoring_unavailable', 'site_collection_failed', 'site_save_failed',
    'bokjiro_page_failed', 'analysis_settings_required', 'analysis_settings_changed',
    'ai_usage_state_unavailable', 'interrupted'}


def _diagnostic_id(value):
    return value if isinstance(value, str) and re.fullmatch('[0-9a-f]{32}', value) else 'unavailable'


def _diagnostic_reason(value):
    return value if isinstance(value, str) and value in _DIAGNOSTIC_REASONS else 'unspecified'


def _diagnostic_count(value):
    return value if type(value) is int and 0 <= value <= 10**9 else None


def _log_job_failure(job):
    """Only allowlisted labels, opaque IDs and a source length; never request text."""
    try:
        from services.ai_rate_policy import MODELS
        from services.news_custom_analysis import SUPPORTED_RECIPES
        from services.news_analysis_worker import RECIPE, PAGE_RECIPE, OLD_RECIPE, PREVIOUS_RECIPE
        model, recipe = job.get('model'), job.get('recipe')
        record = dict(run_id=_diagnostic_id(job.get('run_id')),
                      job_id=_diagnostic_id(job.get('job_id')),
                      reason=_diagnostic_reason(job.get('reason')),
                      model=model if model in MODELS else 'unrecognized',
                      recipe=recipe if recipe in (*SUPPORTED_RECIPES, RECIPE, PAGE_RECIPE,
                                                  OLD_RECIPE, PREVIOUS_RECIPE) else 'unrecognized')
        try:
            payload = json.loads(job.get('input_json') or '{}')
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get('content'), str):
            record['source_chars'] = len(payload['content'])
        logger.info('news_analysis_job_stopped %s', json.dumps(record, sort_keys=True))
    except Exception:
        # Diagnostics must not change the already durable job outcome.
        pass


def _prepare(conn, run_id, model, scoring_version, threshold):
    with _atomic(conn):
        get_run_snapshot(conn, run_id)
        conn.execute('''CREATE TABLE IF NOT EXISTS news_execution_settings (
            run_id TEXT PRIMARY KEY, settings_json TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES news_run_snapshots(run_id))''')
        encoded = json.dumps([model, scoring_version, threshold])
        existing = conn.execute('SELECT settings_json FROM news_execution_settings WHERE run_id=?', (run_id,)).fetchone()
        if existing and json.loads(existing[0]) != json.loads(encoded):
            raise ValueError('Run execution settings cannot change on resume')
        if conn.execute('SELECT 1 FROM news_analysis_jobs WHERE run_id=? AND model<>? LIMIT 1', (run_id, model)).fetchone():
            raise ValueError('Registered job model differs from run model')
        conn.execute('INSERT OR IGNORE INTO news_execution_settings VALUES (?,?)', (run_id, encoded))


def _pending_candidates(conn, run_id, limit):
    return [row[0] for row in conn.execute('''SELECT candidate_key FROM news_run_candidates
        WHERE run_id=? AND route='semantic_candidate' ORDER BY rowid LIMIT ?''', (run_id, limit))]


def _pending_jobs(conn, run_id, now, limit):
    return [row[0] for row in conn.execute('''SELECT job_id FROM news_analysis_jobs
        WHERE run_id=? AND state IN ('queued','waiting') AND retry_after<=?
        ORDER BY rowid LIMIT ?''', (run_id, now, limit))]


def _remaining(conn, run_id):
    semantic = conn.execute("SELECT COUNT(*) FROM news_run_candidates WHERE run_id=? AND route='semantic_candidate'", (run_id,)).fetchone()[0]
    analysis = conn.execute("SELECT COUNT(*) FROM news_analysis_jobs WHERE run_id=? AND state IN ('queued','waiting','running')", (run_id,)).fetchone()[0]
    return semantic + analysis


def _existing_failure(conn, run_id):
    reasons = [row[0] for row in conn.execute("SELECT reason FROM news_analysis_jobs WHERE run_id=? AND state='failed'", (run_id,))]
    # Item-level holds are terminal for that input, not for the whole run.
    reasons = [reason for reason in reasons if reason not in MANUAL_REVIEW_FAILURES]
    if not reasons:
        return None
    recoverable = all(reason in RECOVERABLE_FAILURES for reason in reasons)
    return {'state': 'pass_finished' if recoverable else 'failed',
            'semantic_processed':0, 'jobs_processed':0, 'failed_jobs':len(reasons),
            'remaining':_remaining(conn, run_id) + len(reasons),
            'work_remaining':recoverable, 'recovery_required':recoverable,
            'stop_reason':reasons[0] if recoverable else 'analysis_failed'}


def _review_summary(conn, run_id):
    counts = dict(conn.execute('''SELECT reason,COUNT(*) FROM news_analysis_jobs
        WHERE run_id=? AND state='failed' GROUP BY reason''', (run_id,)))
    reviews = {reason: counts[reason] for reason in MANUAL_REVIEW_FAILURES if counts.get(reason)}
    return {'failed_jobs': sum(counts.values()), 'review_required': bool(reviews),
            'review_required_count': sum(reviews.values()), 'review_reasons': reviews}


async def execute_registered_run(run_id, *, client, scorer, analysis_model, threshold,
                                 limit=50, call_db=db_call, progress=None):
    """Run one bounded pass. Waiting work is persisted, not automatically looped.

Both maintenance and global AI admission are held until DB cancellation drains.
The caller remains responsible for lifecycle task tracking and continuation.
"""
    direct = isinstance(scorer, DirectAnalysis)
    if (not isinstance(scorer, (SemanticScorer, DirectAnalysis)) or client is None
            or not isinstance(analysis_model, str) or not analysis_model.strip()
            or type(limit) is not int or not 1 <= limit <= 100
            or (direct and threshold is not None)
            or (not direct and (type(threshold) not in (int, float) or not math.isfinite(threshold) or not -1 <= threshold <= 1))):
        raise ValueError('Invalid execution settings')
    # 시작 시 중단 작업을 점검하는 경로도 이 모듈을 읽는다. Google SDK를
    # 사용하는 정책 모듈은 실제 분석 실행에서만 로드하되 공유 잠금은 유지한다.
    from services import policy_sync

    label = '새소식 프로필 분석'
    if not await policy_sync.try_acquire_maintenance_lock(label):
        return {'state': 'busy'}
    kind = 'news-profile-' + uuid.uuid4().hex
    admitted = False
    try:
        admitted = await ai_coordinator.try_start_job(kind, label)
        if not admitted:
            return {'state': 'busy'}
        _, cancelled = await _settle_db(call_db(_prepare, run_id, analysis_model, scorer.scoring_version, threshold))
        if cancelled:
            raise asyncio.CancelledError
        async def emit_progress(*, force=False):
            if progress is not None:
                await progress({'phase': 'analysis'}, force=force)
        await emit_progress(force=True)
        failure = await call_db(_existing_failure, run_id)
        if failure:
            failure.update(await call_db(_review_summary, run_id))
            return failure
        summary = {'state': 'pass_finished', 'semantic_processed': 0, 'jobs_processed': 0}
        if direct:
            queued, cancelled = await _settle_db(call_db(queue_candidates, run_id, analysis_model, limit))
            if cancelled:
                raise asyncio.CancelledError
            summary.update(analysis_mode='direct-body-bounded-v1', direct_queued=queued, batch_limit=limit)
        for key in ([] if direct else await call_db(_pending_candidates, run_id, limit)):
            result = await process_with_embeddings(run_id, key, scorer=scorer, threshold=threshold,
                analysis_model=analysis_model, call_db=call_db)
            summary['semantic_processed'] += 1
            await emit_progress()
            if result.get('reason') in MANUAL_REVIEW_FAILURES:
                # The gate has durably moved this input to a review route.
                continue
            if result['state'] == 'semantic_candidate':
                # Stop this phase on unavailable scoring; do not flood the API.
                summary.update(work_remaining=True, stop_reason=result.get('reason', 'scoring_unavailable'))
                break
        pending_jobs = ([] if summary.get('stop_reason') else
                        await call_db(_pending_jobs, run_id, time.time(), limit))
        for job_id in pending_jobs:
            state = await process_job(job_id, client, call_db=call_db)
            summary['jobs_processed'] += 1
            await emit_progress()
            if state == 'failed':
                job = await call_db(get_job, job_id)
                _log_job_failure(job)
                reason = job.get('reason')
                if reason in MANUAL_REVIEW_FAILURES:
                    continue
                if reason in RECOVERABLE_FAILURES:
                    # Retain the failed job until an explicit/scheduled resume;
                    # quota/auth failures must not erase the continuation path.
                    summary.update(work_remaining=True, stop_reason=reason, recovery_required=True)
                else:
                    summary.update(state='failed', work_remaining=False, stop_reason='analysis_failed')
                break
            if state == 'waiting':
                # Conservative batch stop. Saved reason explains manual resume.
                summary.update(work_remaining=True, stop_reason=state)
                break
        remaining = await call_db(_remaining, run_id)
        summary['remaining'] = remaining + int(bool(summary.get('recovery_required')))
        summary.update(await call_db(_review_summary, run_id))
        if remaining:
            summary.setdefault('work_remaining', True)
            summary.setdefault('stop_reason', 'batch_limit' if (summary['jobs_processed'] >= limit
                or summary['semantic_processed'] >= limit) else 'scheduled_wait')
        else:
            summary.setdefault('work_remaining', False)
        if (summary['review_required'] and not summary.get('work_remaining')
                and summary['state'] != 'failed'):
            summary['completion_status'] = 'complete_with_review'
        await emit_progress(force=True)
        return summary
    finally:
        if admitted:
            ai_coordinator.finish_job(kind)
        policy_sync.release_maintenance_lock()
