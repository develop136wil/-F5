"""Opt-in job execution; not yet scheduled by the desktop collection API.

Uses the existing classifier/coordinator for shared model admission and retries.
Recipe version pins prompt code and input truncation; bump on either change.
"""
import asyncio
import json
import time

from services import news_analysis_store as store
from services.news_analysis_context import NewsAnalysisContext
from services.news_retry_policy import decide_retry
from services import news_custom_analysis
from services.news_classification_result import validate_result

OLD_RECIPE = 'news-legacy-v1-content6000'
PREVIOUS_RECIPE = 'news-legacy-v2-evidence6000'
PAGE_RECIPE = 'news-legacy-v3-page6000'
RECIPE = 'news-legacy-v4-office-contact-minimization-page6000'


async def db_call(fn, *args, **kwargs):
    import database
    def execute():
        with database.get_connection() as conn:
            return fn(conn, *args, **kwargs)
    return await database.run_db(execute)


async def _settle_db(awaitable):
    """Drain a DB worker before propagating cancellation; never orphan a claim."""
    task = asyncio.create_task(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            return result, cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True


async def process_job(job_id, client, *, call_db=db_call, clock=time.time):
    token, cancelled = await _settle_db(call_db(store.claim, job_id, now=clock()))
    if token is None:
        if cancelled:
            raise asyncio.CancelledError
        return 'not_claimed'  # includes a cache hit; inspect job state for UI

    async def release(reason, failed=False, delay=60, unspent_attempt=False):
        return await _settle_db(call_db(store.defer, job_id, token,
                                       retry_after=clock() + (0 if failed else delay),
                                       reason=reason, failed=failed, unspent_attempt=unspent_attempt))
    job = None
    try:
        if cancelled:
            raise asyncio.CancelledError
        job = await call_db(store.get_job, job_id)
        if job['cancel_requested']:
            await release('cancelled', failed=True)
            return 'cancelled'
        if job['recipe'] not in (RECIPE, PAGE_RECIPE, OLD_RECIPE, PREVIOUS_RECIPE,
                                 *news_custom_analysis.SUPPORTED_RECIPES):
            raise ValueError('Unsupported analysis recipe')
        context = await call_db(NewsAnalysisContext.from_run, job['run_id'])
        payload = json.loads(job['input_json'])
        if job['recipe'] in news_custom_analysis.SUPPORTED_RECIPES:
            options = news_custom_analysis.recipe_options(job['recipe'])
            if options['complete_document_rules']:
                from services.news_complete_analysis import classify_complete
                async def ensure_active():
                    if not await call_db(store.active_claim, job_id, token):
                        raise ValueError('Analysis claim cancelled or stale')
                async def save_checkpoint(value):
                    _, interrupted = await _settle_db(call_db(store.save_checkpoint, job_id, token, value))
                    if interrupted:
                        raise asyncio.CancelledError
                result = await classify_complete(client, context.profile, payload, job['model'],
                    checkpoint=json.loads(job['checkpoint_json']) if job.get('checkpoint_json') else None,
                    save_checkpoint=save_checkpoint, ensure_active=ensure_active, recipe=job['recipe'])
            else:
                result = await news_custom_analysis.classify(client, context.profile, payload, job['model'], request_attempts=1,
                    **{key: options[key] for key in ('legacy_input', 'evidence_rules', 'applicant_rules', 'structured_output', 'grounding_rules', 'complete_grounding_rules', 'explicit_facts_rules', 'source_span_rules', 'source_unit_rules', 'whole_document_rules', 'budgeted_request', 'compact_schema_rules')})
            validate_result(result)
            news_custom_analysis.validate_facts(
                result, require_interest_topics=options['require_interest_topics'])
            if options['applicant_rules']:
                news_custom_analysis.applicant_status(result, payload['content'])
            # The local safety contract also protects old queued recipes. Their
            # model prompt/cache identities remain pinned; unsupported old facts
            # simply require review instead of inheriting former false matches.
            from services.news_analysis_safety import assess
            from services.news_source_selection import eligible_payload
            output = assess(context.profile, result, payload['content'],
                require_source_selection=options['source_unit_rules'] and eligible_payload(
                    payload, extended=options['whole_document_rules'], complete_document=options['complete_document_rules']),
                whole_document_rules=options['whole_document_rules'],
                complete_document_rules=options['complete_document_rules'],
                source_payload=payload if options['whole_document_rules'] else None)
        else:
            context._require_legacy()
            import ai_classifier
            result = await ai_classifier.classify_post_async(
                client, **payload, custom_prompt=context.profile.ai_prompt,
                strict=True, model_override=job['model'], content_max_chars=6000,
                input_strategy='prefix' if job['recipe'] == OLD_RECIPE else 'balanced',
                evidence_rules=job['recipe'] in (RECIPE, PAGE_RECIPE), request_attempts=1)
            validate_result(result)
            enriched, passed, reason = context.post_filter(result)
            output = {'analysis': enriched,
                      'accepted': result['is_relevant'] and passed, 'filter_reason': reason}
        completed, cancelled = await _settle_db(call_db(store.complete, job_id, token, output))
    except asyncio.CancelledError:
        await release('cancelled')
        raise
    except ValueError:
        _, cancelled = await release('invalid_analysis', failed=True)
        if cancelled:
            raise asyncio.CancelledError
        return 'failed'
    except Exception as error:
        # Do not persist raw provider errors: may contain sensitive request data.
        decision = decide_retry(error, attempts=(job['attempts'] - job['retry_start_attempt']) if job else 1)
        from services.ai_coordinator import AiTemporaryRateLimit
        _, cancelled = await release(decision.reason, failed=decision.terminal, delay=decision.delay,
                                    unspent_attempt=isinstance(error, AiTemporaryRateLimit))
        if cancelled:
            raise asyncio.CancelledError
        return 'failed' if decision.terminal else 'waiting'
    if cancelled:
        # Completed DB commit wins; no stale defer after successful completion.
        raise asyncio.CancelledError
    return 'complete' if completed else 'cancelled'
