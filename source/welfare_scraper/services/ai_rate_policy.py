"""Dated, key-bound quota observations, not provider promises or billing limits.

The minute window is app-local and shared across this profile's processes.
Input tokens are deliberately conservative estimates, not measured token usage.
No request text, credentials or response content is persisted here.
"""
import datetime
import hashlib
import json
import math
import os
import time

POLICY_KEY = 'AI_QUOTA_POLICY'
POLICY_VERSION = 1
VALID_DAYS = 7
MODELS = ('gemini-3.5-flash-lite', 'gemini-embedding-001')


def today():
    return datetime.datetime.now(datetime.timezone.utc).date()


def key_binding(key):
    return hashlib.sha256(str(key or '').strip().encode()).hexdigest()


def parse_observation(value, *, key, current_date=None):
    if isinstance(value, str):
        value = json.loads(value)
    if (not isinstance(value, dict) or type(value.get('version')) is not int
            or value['version'] != POLICY_VERSION):
        raise ValueError('Invalid quota observation')
    observed = datetime.date.fromisoformat(value['observed_on'])
    age = ((current_date or today()) - observed).days
    if age < 0 or age >= VALID_DAYS:
        raise ValueError('Quota observation requires confirmation')
    if not isinstance(key, str) or not key.strip() or value.get('key_binding') != key_binding(key):
        raise ValueError('Quota observation belongs to another key')
    models = value.get('models')
    if not isinstance(models, dict) or set(models) != set(MODELS):
        raise ValueError('Explicit supported model observations required')
    clean = {}
    for model, raw in models.items():
        if not isinstance(raw, dict) or set(raw) != {'rpm', 'tpm', 'rpd'}:
            raise ValueError('RPM, TPM and RPD required')
        for field, maximum in (('rpm', 10000), ('tpm', 10000000), ('rpd', 1000000)):
            if type(raw[field]) is not int or not 0 <= raw[field] <= maximum:
                raise ValueError('Invalid quota value')
        clean[model] = dict(raw)
    return {'version': POLICY_VERSION, 'observed_on': observed.isoformat(),
            'key_binding': key_binding(key), 'models': clean}


def observe(models, *, key, current_date=None):
    return parse_observation({'version': POLICY_VERSION,
        'observed_on': (current_date or today()).isoformat(),
        'key_binding': key_binding(key), 'models': models}, key=key, current_date=current_date)


def load_policy(config_path):
    try:
        with open(config_path, encoding='utf-8-sig') as handle:
            config = json.load(handle)
        if not isinstance(config, dict):
            return {'state': 'invalid'}
        if POLICY_KEY not in config:
            return {'state': 'unconfirmed'}
        value = parse_observation(config[POLICY_KEY], key=config.get('GOOGLE_API_KEY'))
        return {'state': 'current', **value}
    except FileNotFoundError:
        return {'state': 'unconfirmed'}
    except (OSError, ValueError, TypeError, KeyError):
        # Expiry is not permission to raise an older, smaller minute limit.
        # Retain only structurally valid observations bound to the same key.
        try:
            raw = config[POLICY_KEY]
            raw = json.loads(raw) if isinstance(raw, str) else raw
            observed = datetime.date.fromisoformat(raw['observed_on'])
            if observed > today():
                raise ValueError('Future observation')
            previous = parse_observation(raw, key=config.get('GOOGLE_API_KEY'), current_date=observed)
            return {'state': 'reconfirmation_required', **previous}
        except (UnboundLocalError, OSError, ValueError, TypeError, KeyError):
            return {'state': 'reconfirmation_required'}


def effective_limits(policy, model):
    # RPD observations are diagnostic metadata only. Google, not an app-side
    # fraction of an old observation, decides whether the daily quota is spent.
    if policy.get('state') in ('invalid', 'reconfirmation_required'):
        # Provisional app safeguards, not a claim about Google's current quota.
        # A previous provider throttle remains in the durable minute ledger.
        if model not in MODELS:
            return {'rpm': 0, 'tpm': 0, 'rpd': None}
        conservative = {'rpm': 10 if 'embedding' in model else 2,
                        'tpm': 10000 if 'embedding' in model else 6000, 'rpd': None}
        if policy.get('models'):
            previous = effective_limits({'state': 'current', 'models': policy['models']}, model)
            return {'rpm': min(conservative['rpm'], previous['rpm']),
                    'tpm': min(conservative['tpm'], previous['tpm']), 'rpd': None}
        return conservative
    if policy.get('state') != 'current':
        return None
    raw = policy['models'].get(model)
    # An unobserved model must not inherit another model's generous quota.
    if raw is None:
        return {'rpm': 0, 'tpm': 0, 'rpd': None}
    def fraction(value, factor):
        return max(1, int(value * factor)) if value else 0
    return {'rpm': min(fraction(raw['rpm'], .7), 60 if 'embedding' in model else 10),
            'tpm': fraction(raw['tpm'], .6), 'rpd': None}


def public_status(policy):
    return {'state': policy['state'], 'valid_days': VALID_DAYS,
            'observed_on': policy.get('observed_on'),
            'observed': policy.get('models', {}),
            'effective': {model: effective_limits(policy, model) for model in MODELS}
                if policy['state'] == 'current' else {},
            'input_tokens_are_estimates': True}


def estimate_input_tokens(contents, *, media_bytes=0):
    """UTF-8 byte-based planning allowance + overhead; never claims exact TPM."""
    texts = [contents] if isinstance(contents, str) else contents
    if not isinstance(texts, (list, tuple)) or not all(isinstance(x, str) for x in texts):
        raise ValueError('Explicit text input required for quota estimate')
    if type(media_bytes) is not int or media_bytes < 0:
        raise ValueError('Invalid media size')
    return 256 + sum(128 + len(text.encode('utf-8')) for text in texts) + math.ceil(media_bytes / 1024) * 256


def read_window(path, *, now=None):
    now = time.time() if now is None else now
    try:
        with open(path, encoding='utf-8') as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return {'version': 1, 'entries': [], 'penalties': {}}
    if (not isinstance(value, dict) or type(value.get('version')) is not int
            or value['version'] != 1):
        raise ValueError('Invalid AI minute ledger')
    entries, penalties = value.get('entries'), value.get('penalties')
    if not isinstance(entries, list) or len(entries) > 10000 or not isinstance(penalties, dict):
        raise ValueError('Invalid AI minute ledger')
    kept = []
    for row in entries:
        if (not isinstance(row, list) or len(row) != 3 or not isinstance(row[0], str)
                or type(row[1]) not in (float, int) or not math.isfinite(row[1])
                or type(row[2]) is not int or row[2] < 0 or row[1] > now + 1):
            raise ValueError('Invalid AI minute reservation')
        if row[1] > now - 60:
            kept.append(row)
    clean = {}
    for model, penalty in penalties.items():
        if (not isinstance(model, str) or not isinstance(penalty, dict)
                or set(penalty) not in ({'until', 'cooldown_until', 'factor'},
                                       {'until', 'cooldown_until', 'factor', 'token_factor'})
                or any(type(penalty[k]) not in (int, float) or not math.isfinite(penalty[k])
                       for k in penalty)
                or not 0 < penalty['factor'] <= 1
                or not 0 < penalty.get('token_factor', penalty['factor']) <= 1):
            raise ValueError('Invalid AI minute penalty')
        if penalty['until'] > now:
            clean[model] = penalty
    return {'version': 1, 'entries': kept, 'penalties': clean}


def validate_request_units(request_units):
    """App protection units; provider batch-quota counting is not assumed."""
    if type(request_units) is not int or not 1 <= request_units <= 1000:
        raise ValueError('Request units must be an integer from 1 to 1000')
    return request_units


def window_admission(window, model, tokens, limits, *, now=None, request_units=1):
    now = time.time() if now is None else now
    validate_request_units(request_units)
    if type(tokens) is not int or tokens < 0:
        raise ValueError('Input estimate required')
    penalty = window['penalties'].get(model, {})
    factor = penalty.get('factor', 1) if penalty.get('until', 0) > now else 1
    token_factor = penalty.get('token_factor', factor) if penalty.get('until', 0) > now else 1
    rpm = max(1, math.floor(limits['rpm'] * factor)) if limits['rpm'] else 0
    tpm = max(1, math.floor(limits['tpm'] * token_factor)) if limits['tpm'] else 0
    if (rpm == 0 or tpm == 0 or tokens > limits['tpm']
            or request_units > limits['rpm']):
        return {'too_large': True, 'wait': 0}
    if tokens > tpm or request_units > rpm:
        # A request fitting the unchanged baseline is not an invalid document.
        # Keep the reduced admission ceiling until its durable expiry.
        return {'too_large': False,
                'wait': max(.05, penalty.get('until', now) - now + .05,
                            penalty.get('cooldown_until', now) - now),
                'temporary_capacity': True}
    rows = sorted((row for row in window['entries'] if row[0] == model), key=lambda x: x[1])
    wait = max(0, penalty.get('cooldown_until', 0) - now)
    count, total = len(rows), sum(row[2] for row in rows)
    for row in rows:
        if count + request_units <= rpm and total + tokens <= tpm:
            break
        wait = max(wait, row[1] + 60.05 - now)
        count -= 1
        total -= row[2]
    return {'too_large': False, 'wait': max(0, wait)}


def lower_after_rate_error(window, model, retry_after, *, now=None, limit_kind='unknown'):
    now = time.time() if now is None else now
    previous = window['penalties'].get(model, {})
    if previous.get('until', 0) <= now:
        previous = {}
    old_tokens = previous.get('token_factor', previous.get('factor', 1))
    window['penalties'][model] = {
        'until': max(previous.get('until', 0), now + max(3600, retry_after)),
        'cooldown_until': max(previous.get('cooldown_until', 0), now + max(2, retry_after)),
        'factor': max(.125, previous.get('factor', 1) / 2),
        # Explicit request-rate errors must not shrink document capacity.
        # Unknown/TPM errors remain conservative; old ledgers retain both caps.
        'token_factor': old_tokens if limit_kind == 'requests'
                        else max(.125, old_tokens / 2)}
