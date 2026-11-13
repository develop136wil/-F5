"""Read-only policy admission hints; dispatch reservations stay authoritative.

Daily remaining is unknown: the app does not impose a daily request cap and
cannot observe Google's account-wide remaining quota. Provider holds and minute
admission still determine whether the next request can proceed safely.
"""
import os

from services import ai_coordinator


def public_ai_budget(model=None, *, client_available=None):
    """Return unknown daily ``remaining`` and a verified workflow pause reason.

    The planning API uses a complete minute's ceiling, so occupied minute slots
    and cooldowns keep using the coordinator's normal waiting admission path.
    Reading this hint never reserves requests or resets/increases any budget.
    """
    result = {'remaining': None, 'reason': ''}
    try:
        if client_available is None:
            import ai_classifier
            client_available = ai_classifier.get_client() is not None
        if not client_available:
            result['reason'] = 'ai_connection_required'
            return result
    except Exception:
        result['reason'] = 'ai_connection_required'
        return result

    model = model if model is not None else os.getenv('CLASSIFY_MODEL', 'gemini-3.5-flash-lite')
    try:
        snapshot = ai_coordinator.status()
        if not isinstance(snapshot, dict):
            raise ValueError('AI budget status unavailable')
        if snapshot.get('state_warning'):
            result['reason'] = 'quota_state_unavailable'
            return result
        # Ignore obsolete local-budget fields in older status snapshots.
        # Saved counts and observed RPD are not proof of provider exhaustion.
        models = snapshot.get('models')
        if not isinstance(models, dict):
            raise ValueError('Model usage status unavailable')
        selected = models.get(model) or {}
        wildcard = models.get('*') or {}
        if not isinstance(selected, dict) or not isinstance(wildcard, dict):
            raise ValueError('Invalid model usage status')
        if selected.get('daily_exhausted') or wildcard.get('daily_exhausted'):
            result['reason'] = 'daily_limit'
            return result
        allowance = ai_coordinator.input_request_budget(model)
        if type(allowance) is not int or allowance <= 0:
            result['reason'] = 'quota_state_unavailable'
    except Exception:
        # Missing/corrupt ledger state is never permission to sweep remaining
        # queue items repeatedly invoking requests which cannot be admitted.
        result['reason'] = 'quota_state_unavailable'
    return result


def ai_pause_reason(model=None, *, client_available=None):
    """Return an empty string when work can attempt guarded AI admission."""
    return public_ai_budget(model, client_available=client_available)['reason']
