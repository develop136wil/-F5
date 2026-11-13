"""Small, persisted progress receipts, without remote data or extra AI calls.

Collection totals are scoped to the frozen site snapshot/current site's list.
Analysis totals count source candidates, not mutable per-pass job queue sizes.
"""
import asyncio
from datetime import datetime, timezone
import json
import time

from services.news_profile_store import _atomic


_MANUAL_REVIEW = frozenset({'ai_input_review_required', 'ai_input_size_review_required',
                          'ai_response_review_required', 'ai_source_review_required'})
_COUNTS = frozenset({'sites_total', 'sites_processed', 'sites_succeeded',
    'sites_review_required', 'site_index', 'site_documents_processed',
    'documents_processed', 'attachments_processed', 'attachments_review_required',
    'saved_candidates', 'analysis_total', 'analysis_processed', 'analysis_remaining',
    'analysis_complete', 'analysis_review_required', 'analysis_failed', 'analysis_running'})


def initial_progress(sites_total):
    return dict(schema_version=1, phase='sites', sites_total=sites_total,
                sites_processed=0, sites_succeeded=0, sites_review_required=0,
                site_index=0, site_name='', site_documents_total=None,
                site_documents_processed=0, documents_processed=0,
                attachments_processed=0, attachments_review_required=0,
                saved_candidates=0)


def analysis_progress(conn, run_id):
    """Read cumulative durable outcomes; failed retryable work is not processed.

This reads no document bodies or AI result JSON. A candidate can first need
semantic selection then a job, but remains exactly one analysis work item.
"""
    groups = list(conn.execute('''SELECT c.route,j.state,j.reason,COUNT(*)
        FROM news_run_candidates c LEFT JOIN news_analysis_jobs j ON j.job_id=c.job_id
        WHERE c.run_id=? AND c.route<>'excluded'
        GROUP BY c.route,j.state,j.reason''', (run_id,)))
    # Compatibility for durable jobs created before site candidate receipts.
    groups.extend(conn.execute('''SELECT 'job',j.state,j.reason,COUNT(*)
        FROM news_analysis_jobs j WHERE j.run_id=? AND j.job_id NOT IN (
            SELECT c.job_id FROM news_run_candidates c WHERE c.run_id=? AND c.job_id IS NOT NULL)
        GROUP BY j.state,j.reason''', (run_id, run_id)))
    total = complete = review = failed = running = 0
    for route, state, reason, count in groups:
        total += count
        if state == 'complete':
            complete += count
        elif (state == 'failed' and reason in _MANUAL_REVIEW) or (route == 'semantic_review' and state is None):
            review += count
        elif state == 'failed':
            failed += count
        elif state == 'running':
            running += count
    return dict(analysis_total=total, analysis_processed=complete + review,
                analysis_remaining=total - complete - review, analysis_complete=complete,
                analysis_review_required=review, analysis_failed=failed, analysis_running=running)


def save_progress(conn, workflow_id, progress):
    """Do not rewrite summary, state, errors, or any finalized workflow."""
    encoded = json.dumps(progress, ensure_ascii=False, sort_keys=True, allow_nan=False)
    with _atomic(conn):
        conn.execute('''UPDATE news_profile_workflows SET progress_json=?,updated_at=CURRENT_TIMESTAMP
            WHERE workflow_id=? AND state='running' ''', (encoded, workflow_id))


class ProgressReporter:
    """Throttle fast completions; force only stage/site/final boundaries.

Cancellation still drains through the workflow's settled call_db. Progress is
best-effort instrumentation and cannot turn a readable source into a failure.
"""
    def __init__(self, workflow_id, run_id, current, call_db, *, clock=time.monotonic):
        self.workflow_id, self.run_id = workflow_id, run_id
        self.current = dict(current, schema_version=1)
        self.call_db, self.clock = call_db, clock
        self.last_saved = float('-inf')

    async def __call__(self, changes=None, *, force=False):
        changes = changes or {}
        phase_changed = changes.get('phase', self.current.get('phase')) != self.current.get('phase')
        for key, value in changes.items():
            if key in _COUNTS and type(value) is int and value >= 0:
                self.current[key] = value
            elif key == 'site_documents_total' and (value is None or (type(value) is int and value >= 0)):
                self.current[key] = value
            elif key == 'phase' and value in ('sites', 'content', 'analysis'):
                self.current[key] = value
            elif key == 'site_name' and isinstance(value, str):
                self.current[key] = ' '.join(value.split())[:120]
        now = self.clock()
        if not force and not phase_changed and now - self.last_saved < .5:
            return
        try:
            if self.current.get('phase') == 'analysis':
                self.current.update(await self.call_db(analysis_progress, self.run_id))
            self.current['updated_at'] = datetime.now(timezone.utc).isoformat()
            await self.call_db(save_progress, self.workflow_id, self.current)
            self.last_saved = now
        except asyncio.CancelledError:
            raise
        except Exception:
            # A subsequent status request can still derive analysis counters.
            # Never expose private raw DB/provider exceptions in progress text.
            pass
