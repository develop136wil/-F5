"""Opt-in semantic scoring bridge; scheduler supplies the governed scorer.

Low scores remain reviewable, not deleted. No default threshold is assumed to
be calibrated for new populations. A scorer returns a cosine score or None.
"""
import asyncio
import json
import math
import weakref

from services import news_analysis_store as jobs
from services.news_profile_store import _atomic
from services.news_analysis_context import NewsAnalysisContext
from services.news_analysis_worker import db_call, _settle_db, RECIPE as LEGACY_RECIPE
from services.news_custom_analysis import RECIPE as CUSTOM_RECIPE
from services.news_publication import analysis_payload

_locks = weakref.WeakValueDictionary()


def _hold(conn, run_id, key, *, reason, analysis_model, scoring_version):
    """Persist an uncallable review record without admitting or caching AI work."""
    with _atomic(conn):
        _profile, payload, route, job_id = _read(conn, run_id, key)
        if route != 'semantic_candidate':
            # A preflight on an old scored record must not rewrite its history.
            return {'state': 'semantic_candidate', 'reason': reason}
        job_id = jobs.enqueue(conn, run_id, payload=analysis_payload(payload), model=analysis_model,
                              recipe='news-semantic-hold-v1:' + scoring_version)
        conn.execute("""UPDATE news_analysis_jobs SET state='failed',reason=?,retry_after=0
            WHERE job_id=? AND state='queued' AND attempts=0""", (reason, job_id))
        conn.execute("UPDATE news_run_candidates SET route='semantic_review',job_id=? WHERE run_id=? AND candidate_key=?",
                     (job_id, run_id, key))
        return {'state': 'semantic_review', 'job_id': job_id, 'reason': reason}


def _read(conn, run_id, key):
    row = conn.execute('SELECT payload_json,route,job_id FROM news_run_candidates WHERE run_id=? AND candidate_key=?',
                       (run_id, key)).fetchone()
    if row is None:
        raise KeyError(key)
    context = NewsAnalysisContext.from_run(conn, run_id)
    return context.profile, json.loads(row[0]), row[1], row[2]


def _finish(conn, run_id, key, *, score, threshold, analysis_model, scoring_version):
    with _atomic(conn):
        profile, payload, route, job_id = _read(conn, run_id, key)
        if route != 'semantic_candidate':
            return {'state': route, 'job_id': job_id}
        conn.execute('''CREATE TABLE IF NOT EXISTS news_semantic_decisions (
            run_id TEXT NOT NULL,candidate_key TEXT NOT NULL,score REAL NOT NULL,
            threshold REAL NOT NULL,scoring_version TEXT NOT NULL,
            PRIMARY KEY(run_id,candidate_key),
            FOREIGN KEY(run_id,candidate_key) REFERENCES news_run_candidates(run_id,candidate_key))''')
        route = 'semantic_review'
        if score >= threshold:
            recipe = CUSTOM_RECIPE if profile.general_mode == 'custom_v1' else LEGACY_RECIPE
            job_id = jobs.enqueue(conn, run_id, payload=analysis_payload(payload), model=analysis_model, recipe=recipe)
            route = 'semantic_matched'
        conn.execute('INSERT INTO news_semantic_decisions VALUES (?,?,?,?,?)',
                     (run_id, key, score, threshold, scoring_version))
        conn.execute('UPDATE news_run_candidates SET route=?,job_id=? WHERE run_id=? AND candidate_key=?',
                     (route, job_id, run_id, key))
        return {'state': route, 'job_id': job_id}


async def process_candidate(run_id, key, *, scorer, threshold, analysis_model, scoring_version, call_db=db_call):
    """scorer must use shared AI admission and a pinned embedding recipe.

Provider failures/cancellation leave the candidate pending; input/response holds
are saved for review and removed from automatic work. The per-loop lock avoids
duplicate calls in one desktop process; DB finalization is idempotent across
processes, but cross-process API admission remains the scheduler's duty.
"""
    if (type(threshold) not in (int, float) or not math.isfinite(threshold) or not -1 <= threshold <= 1
            or not isinstance(analysis_model, str) or not analysis_model.strip()
            or not isinstance(scoring_version, str) or not scoring_version.strip()):
        raise ValueError('Explicit valid scoring configuration required')
    lock_key = (asyncio.get_running_loop(), run_id, key)
    lock = _locks.setdefault(lock_key, asyncio.Lock())
    async with lock:
        profile, payload, route, job_id = await call_db(_read, run_id, key)
        if route not in ('semantic_candidate', 'semantic_matched', 'semantic_review'):
            return {'state': route, 'job_id': job_id}
        if route == 'semantic_review' and job_id:
            held = await call_db(jobs.get_job, job_id)
            if held['recipe'].startswith('news-semantic-hold-v1:'):
                return {'state': route, 'job_id': job_id, 'reason': held['reason']}
        reference = NewsAnalysisContext(profile).semantic_text
        if not reference.strip():
            return {'state': 'semantic_candidate', 'reason': 'missing_reference'}
        from services.ai_input_safety import AiInputReviewRequired, ensure_ai_text_safe
        from services.ai_generation_safety import AiResponseReviewRequired
        from services.embedding_input_limits import EmbeddingInputLimitExceeded, require_embedding_input
        from services.news_semantic_scorer import SemanticScorer
        from services.ai_coordinator import (
            AiLocalBudgetExhausted, AiDailyQuotaExhausted, AiInputTokenBudgetExceeded)
        async def hold(reason):
            result, cancelled = await _settle_db(call_db(_hold, run_id, key, reason=reason,
                analysis_model=analysis_model, scoring_version=scoring_version))
            if cancelled:
                raise asyncio.CancelledError
            return result
        try:
            # Existing v1 scores are compatible only for complete, safe inputs
            # that fit both the current byte policy and that recipe's max_chars.
            # Run this before the cached-route return, not only before new SDK
            # calls. Already scored job/decision/settings records stay intact;
            # new pending inputs may be moved into a separate review record.
            if isinstance(scorer, SemanticScorer):
                scorer.review_inputs(reference, payload['title'])
            else:
                ensure_ai_text_safe(reference, payload['title'])
                require_embedding_input(reference)
                require_embedding_input(payload['title'])
            if route != 'semantic_candidate':
                return {'state': route, 'job_id': job_id}
            score = await scorer(reference, payload['title'])
        except (EmbeddingInputLimitExceeded, AiInputTokenBudgetExceeded):
            return await hold('ai_input_size_review_required')
        except AiInputReviewRequired:
            return await hold('ai_input_review_required')
        except AiResponseReviewRequired:
            return await hold('ai_response_review_required')
        except AiLocalBudgetExhausted:
            return {'state': 'semantic_candidate', 'reason': 'local_budget_exhausted'}
        except AiDailyQuotaExhausted:
            return {'state': 'semantic_candidate', 'reason': 'daily_quota_exhausted'}
        except Exception:
            return {'state': 'semantic_candidate', 'reason': 'scoring_unavailable'}
        if type(score) not in (int, float) or not math.isfinite(score) or not -1 <= score <= 1:
            return {'state': 'semantic_candidate', 'reason': 'invalid_score'}
        result, cancelled = await _settle_db(call_db(_finish, run_id, key, score=score, threshold=threshold,
                             analysis_model=analysis_model, scoring_version=scoring_version))
        if cancelled:
            # Drain DB work before releasing the lock. A completed commit wins.
            raise asyncio.CancelledError
        return result
