"""Bounded retry decisions; never log or persist raw provider error messages."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RetryDecision:
    reason: str
    delay: float = 0
    terminal: bool = False


def decide_retry(error, *, attempts, max_attempts=5):
    from services.ai_coordinator import AiTemporaryRateLimit
    if isinstance(error, AiTemporaryRateLimit):
        # No provider request occurred. Do not consume failure attempts merely
        # because a temporary capacity penalty has not expired yet.
        return RetryDecision('rate_limited', max(1.0, error.seconds))
    from services.ai_coordinator import analyze_api_error, AiInputTokenBudgetExceeded
    from services.ai_input_safety import AiInputReviewRequired
    from services.ai_generation_safety import AiResponseReviewRequired
    from services.news_source_selection import SourceInputReviewRequired
    if isinstance(error, SourceInputReviewRequired):
        return RetryDecision('ai_source_review_required', terminal=True)
    if isinstance(error, AiResponseReviewRequired):
        return RetryDecision('ai_response_review_required', terminal=True)
    if isinstance(error, AiInputReviewRequired):
        return RetryDecision('ai_input_review_required', terminal=True)
    if isinstance(error, AiInputTokenBudgetExceeded):
        return RetryDecision('ai_input_size_review_required', terminal=True)
    info = analyze_api_error(error)
    if info.get('local_budget_exhausted'):
        return RetryDecision('local_budget_exhausted', terminal=True)
    # No automatic key/model switching. Daily exhaustion needs explicit resumption
    # after reset until the run scheduler implements reset-aware wakeups.
    if info['auth_error']:
        return RetryDecision('authentication_required', terminal=True)
    if info['daily_exhausted']:
        return RetryDecision('daily_quota_exhausted', terminal=True)
    code = getattr(error, 'code', None) or getattr(error, 'status_code', None)
    if code in (400, 404, 405, 422):
        return RetryDecision('invalid_request', terminal=True)
    if code in (401, 403):
        return RetryDecision('authentication_required', terminal=True)
    if attempts >= max_attempts:
        return RetryDecision('retry_limit_reached', terminal=True)
    reason = ('rate_limited' if info['rate_limited'] or code == 429 else
              'service_unavailable' if info['unavailable'] or code in (500, 502, 503, 504) else
              'analysis_error')
    delay = min(900, 60 * 2 ** max(0, attempts - 1))
    provider_delay = info['retry_after']
    if provider_delay is not None and math.isfinite(provider_delay):
        # Never retry earlier than the provider requested.
        delay = max(delay, provider_delay + 2)
    return RetryDecision(reason, delay)
