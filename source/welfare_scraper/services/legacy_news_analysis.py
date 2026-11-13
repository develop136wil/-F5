"""Failure metadata for legacy collection, without inventing classified facts.

Reading an old fallback is read-only. Only its exact historical shape is
recognized: an unclassified or undated but successful result is not a failure.
"""
import datetime
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_ATTEMPTS = 3
_OLD_FALLBACK = dict(is_relevant=True, item_type='program', category='미분류', target='일반',
    start_age_months=None, end_age_months=None, age_detail='', target_income='',
    eligibility_detail='', residence='', support_detail='', contact='', deadline=None,
    event_date=None, event_end_date=None, related_urls=[], source_site_name=None,
    source_site_url=None, reasoning='', evidence_snippet='')
_METADATA = {'source_input', 'source_page', 'source_attachments'}


def _aware_time(value):
    parsed = datetime.datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime.datetime):
        raise ValueError('Missing analysis time')
    # Old rows used local wall time. New retry metadata has an explicit offset.
    return parsed.astimezone(datetime.timezone.utc)


def _next_provider_reset(value):
    try:
        zone = ZoneInfo('America/Los_Angeles')
    except ZoneInfoNotFoundError:
        zone = datetime.timezone.utc  # Same fallback as the coordinator.
    current = _aware_time(value).astimezone(zone)
    next_day = current.date() + datetime.timedelta(days=1)
    return datetime.datetime.combine(next_day, datetime.time.min, tzinfo=zone).astimezone(datetime.timezone.utc)


def facts(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or '{}')
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def failure(value):
    data = facts(value)
    if data.get('analysis_status') == 'failed' and isinstance(data.get('analysis_failure'), dict):
        saved = data['analysis_failure']
        if type(saved.get('attempts')) is not int or not 0 <= saved['attempts'] <= MAX_ATTEMPTS:
            return dict(reason='invalid_saved_result', retryable=False,
                        attempts=MAX_ATTEMPTS, next_retry_at=None)
        return saved
    if (set(_OLD_FALLBACK) <= set(data) <= set(_OLD_FALLBACK) | _METADATA
            and all(type(data[key]) is type(expected) and data[key] == expected
                    for key, expected in _OLD_FALLBACK.items())):
        return dict(reason='legacy_analysis_failed', retryable=True, attempts=0, next_retry_at=None)
    return None


def failed_result(error=None, *, reason=None, attempts=1):
    from services.news_retry_policy import decide_retry
    decision = decide_retry(error, attempts=attempts, max_attempts=MAX_ATTEMPTS) if error else None
    code = reason or decision.reason
    retryable = bool(decision and not decision.terminal)
    retry_at = (datetime.datetime.now() + datetime.timedelta(seconds=max(60, decision.delay))).isoformat() if retryable else None
    if code == 'daily_quota_exhausted':
        # Provider exhaustion is not a bad input. Retry on a later collection
        # after the provider's day resets, without consuming content-error tries.
        retryable = True
        retry_at = _next_provider_reset(datetime.datetime.now(datetime.timezone.utc)).isoformat()
        attempts = max(0, attempts - 1)
    elif code == 'ai_usage_state_unavailable':
        # The early collection hint made no provider/content attempt. Do not
        # permanently exhaust an item's retries just by checking a locked ledger.
        retryable = True
        retry_at = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=60)).isoformat()
        attempts = max(0, attempts - 1)
    return dict(analysis_status='failed', analysis_failure=dict(
        reason=code, retryable=retryable, attempts=attempts, next_retry_at=retry_at))


def mark_failed(item, result):
    """Persist a failure separately from relevance; never use it as AI facts."""
    item['ai_data'] = json.dumps(result, ensure_ascii=False)
    item['ai_skip_reason'] = 'analysis_failed' if failure(result)['retryable'] else 'analysis_review_required'
    item['is_relevant'] = False
    return item


def backlog_due(item, *, now=None):
    if item.get('is_archived') or item.get('ai_skip_reason') == 'failed_permanent':
        return False
    outcome = failure(item.get('ai_data'))
    if outcome is None:
        return not facts(item.get('ai_data'))
    if outcome.get('reason') == 'daily_quota_exhausted':
        # Recover older terminal quota rows read-only, but only with a known
        # reset boundary. Actual admission still checks Google's durable hold.
        try:
            retry_at = outcome.get('next_retry_at')
            reset_at = _aware_time(retry_at) if retry_at else _next_provider_reset(item.get('ai_last_attempt_at'))
            return reset_at <= _aware_time(now or datetime.datetime.now(datetime.timezone.utc))
        except (ValueError, TypeError, OverflowError):
            return False
    if outcome.get('retryable') is not True or outcome.get('attempts', 0) >= MAX_ATTEMPTS:
        return False
    retry_at = outcome.get('next_retry_at')
    if not retry_at:
        return True
    try:
        return _aware_time(retry_at) <= _aware_time(now or datetime.datetime.now(datetime.timezone.utc))
    except (ValueError, TypeError):
        return False


# Broad SQL prefilter, followed by failure()/backlog_due() before admission.
# CASE protects malformed historical JSON. The category is not proof of failure.
BACKLOG_CANDIDATE_SQL = """COALESCE(is_archived, 0) = 0
    AND COALESCE(ai_skip_reason, '') <> 'failed_permanent'
    AND (ai_data IS NULL OR TRIM(ai_data) = '' OR ai_data = '{}'
        OR json_extract(CASE WHEN json_valid(ai_data) THEN ai_data ELSE '{}' END,
                        '$.analysis_status') = 'failed'
        OR json_extract(CASE WHEN json_valid(ai_data) THEN ai_data ELSE '{}' END,
                        '$.category') = '미분류')"""
