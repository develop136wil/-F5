"""Opt-in tokenizer preflight bound to an exact generation request.

Callers MUST privacy-check the entire original article, attachments/preferences
before splitting. countTokens transmits to Google too. This module additionally
inspects the exact outgoing prompt/schema, but cannot inspect omitted source.

google-genai 1.62 Developer API countTokens does not support generation_config.
We count the exact prompt and add the FULL schema UTF-8 allowance and headroom.
This is conservative planning, not an exact billing-token claim. Existing quota
policy and generation RPM/TPM/RPD reservations remain unchanged and authoritative.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import logging
import math
import secrets
import threading
import time

from services import ai_coordinator
from services.ai_input_safety import ensure_ai_text_safe

logger = logging.getLogger(__name__)
_SEAL = object()
_RECEIPT_KEY = secrets.token_bytes(32)
_CACHE = OrderedDict()
_LOCK = threading.Lock()
_STARTS = deque()
_COUNT_ACTIVE = False
_BLOCKED_UNTIL = 0.0
_CACHE_TTL = 300.0
_CACHE_CAPACITY = 256
_COUNT_TIMEOUT = 12.0
_COUNT_INTERVAL = 2.0
_COUNT_PER_MINUTE = 20
_FAILURE_COOLDOWN = 30.0
_MAX_COUNT_BYTES = 1024 * 1024


@dataclass(frozen=True, repr=False)
class _VerifiedInput:
    _fingerprint: str
    input_tokens: int
    method: str
    _seal: object = field(repr=False, compare=False)
    _signature: str = field(repr=False, compare=False)

    @property
    def is_provider_counted(self):
        return self.method in ('provider_prompt_plus_schema_bytes', 'provider_media_plus_schema_bytes')

    def __repr__(self):
        return f'VerifiedInput(input_tokens={self.input_tokens}, method={self.method!r})'


def _schema_text(schema):
    return (json.dumps(schema, ensure_ascii=False, separators=(',', ':'))
            if schema is not None else '')


def _fingerprint(model, prompt, schema_text):
    data = json.dumps([model, prompt, schema_text], ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def _receipt(fingerprint, tokens, method):
    return _VerifiedInput(fingerprint, tokens, method, _SEAL, _signature(fingerprint, tokens, method))


def _signature(fingerprint, tokens, method):
    payload = json.dumps([fingerprint, tokens, method], separators=(',', ':')).encode('utf-8')
    return hmac.new(_RECEIPT_KEY, payload, hashlib.sha256).hexdigest()


def input_tokens_for(receipt, *, model, prompt, response_json_schema=None):
    """Reject receipts for a different model, prompt or response schema."""
    if (type(receipt) is not _VerifiedInput or receipt._seal is not _SEAL
            or type(receipt.input_tokens) is not int or receipt.input_tokens <= 0
            or not hmac.compare_digest(receipt._signature,
                _signature(receipt._fingerprint, receipt.input_tokens, receipt.method))
            or receipt._fingerprint != _fingerprint(model, prompt, _schema_text(response_json_schema))):
        raise ValueError('Verified AI input does not match the current request')
    return receipt.input_tokens


def _cached(fingerprint):
    now = time.monotonic()
    with _LOCK:
        value = _CACHE.get(fingerprint)
        if value is not None and now - value[0] <= _CACHE_TTL:
            _CACHE.move_to_end(fingerprint)
            return value[1]
        _CACHE.pop(fingerprint, None)
    return None


async def _take_count_slot():
    """Process-local auxiliary-call pacing, not a claim about provider quotas."""
    global _COUNT_ACTIVE
    while True:
        now = time.monotonic()
        with _LOCK:
            if now < _BLOCKED_UNTIL:
                return False
            while _STARTS and _STARTS[0] <= now - 60.0:
                _STARTS.popleft()
            delay = max(0.0, _STARTS[-1] + _COUNT_INTERVAL - now) if _STARTS else 0.0
            if len(_STARTS) >= _COUNT_PER_MINUTE:
                delay = max(delay, _STARTS[0] + 60.0 - now)
            if _COUNT_ACTIVE:
                delay = max(delay, 0.05)
            if delay <= 0:
                _STARTS.append(now)
                _COUNT_ACTIVE = True
                return True
        await asyncio.sleep(delay)


def _release_count_slot():
    global _COUNT_ACTIVE
    with _LOCK:
        _COUNT_ACTIVE = False


def _block_failed_count():
    global _BLOCKED_UNTIL
    with _LOCK:
        _BLOCKED_UNTIL = max(_BLOCKED_UNTIL, time.monotonic() + _FAILURE_COOLDOWN)


async def verify_input(client, prompt, response_json_schema=None, *, model, input_token_budget,
                       before_dispatch=None):
    """Return a bound estimate without truncation or rewriting.

    ``is_provider_counted`` distinguishes a tokenizer result (possibly too large)
    from ``fallback_*`` failures. Count-service unavailability is NOT proof that
    an article is too long. Cancellation propagates; fallbacks never mean zero.
    A caller's claim/cancel guard runs after pacing, immediately before sending;
    guard exceptions propagate unchanged rather than becoming count failures.
    """
    if not isinstance(model, str) or not model or not isinstance(prompt, str):
        raise ValueError('AI model and text prompt are required')
    schema_text = _schema_text(response_json_schema)
    ensure_ai_text_safe(prompt, schema_text)
    fingerprint = _fingerprint(model, prompt, schema_text)
    estimate = ai_coordinator.estimate_generation_input_tokens(prompt, response_json_schema)
    if type(input_token_budget) is not int or input_token_budget <= 0:
        return _receipt(fingerprint, estimate, 'fallback_invalid_budget')
    if estimate <= input_token_budget:
        return _receipt(fingerprint, estimate, 'local_bytes')
    cached = _cached(fingerprint)
    if cached is not None:
        return _receipt(fingerprint, cached, 'provider_prompt_plus_schema_bytes')
    if len(prompt.encode('utf-8')) + len(schema_text.encode('utf-8')) > _MAX_COUNT_BYTES:
        return _receipt(fingerprint, estimate, 'fallback_request_too_large')
    if not await _take_count_slot():
        return _receipt(fingerprint, estimate, 'fallback_count_cooldown')
    try:
        # Another waiter may have completed this exact count while we waited.
        cached = _cached(fingerprint)
        if cached is not None:
            return _receipt(fingerprint, cached, 'provider_prompt_plus_schema_bytes')
        if before_dispatch is not None:
            await before_dispatch()
        return await _provider_receipt(client, prompt, model, schema_text, fingerprint, estimate)
    finally:
        _release_count_slot()


async def _provider_receipt(client, prompt, model, schema_text, fingerprint, estimate,
                            *, contents=None, method='provider_prompt_plus_schema_bytes'):
    """Catch provider failures only; a caller's ownership guard is outside."""
    try:
        from google.genai import types
        response = await asyncio.wait_for(client.aio.models.count_tokens(
            model=model, contents=prompt if contents is None else contents,
            config=types.CountTokensConfig(http_options=types.HttpOptions(
                timeout=int(_COUNT_TIMEOUT * 1000),
                retry_options=types.HttpRetryOptions(attempts=1)))),
            timeout=_COUNT_TIMEOUT)
        count = getattr(response, 'total_tokens', None)
        if type(count) is not int or count <= 0:
            _block_failed_count()
            return _receipt(fingerprint, estimate, 'fallback_count_invalid')
        tokens = count + 256 + math.ceil(count * 0.1)
        if schema_text:
            tokens += len(schema_text.encode('utf-8')) + 128
        with _LOCK:
            _CACHE[fingerprint] = (time.monotonic(), tokens)
            _CACHE.move_to_end(fingerprint)
            while len(_CACHE) > _CACHE_CAPACITY:
                _CACHE.popitem(last=False)
        return _receipt(fingerprint, tokens, method)
    except TimeoutError:
        method = 'fallback_count_timeout'
    except Exception:
        # Provider errors can include credentials, URLs or source text.
        method = 'fallback_count_unavailable'
    _block_failed_count()
    logger.info('AI token preflight unavailable; conservative estimate retained (%s)', method)
    return _receipt(fingerprint, estimate, method)


def _media_fingerprint(model, prompt, schema_text, media_bytes, mime_type):
    if (not isinstance(model, str) or not model or not isinstance(prompt, str)
            or not isinstance(media_bytes, bytes) or not media_bytes
            or len(media_bytes) > 50 * 1024 * 1024
            or mime_type not in ('application/pdf', 'image/png', 'image/jpeg', 'image/webp')):
        raise ValueError('Explicit supported document input required')
    digest = hashlib.sha256(media_bytes).hexdigest()
    envelope = json.dumps(['media-v1', model, prompt, schema_text, mime_type, digest],
                          ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(envelope).hexdigest()


def media_input_tokens_for(receipt, *, model, prompt, media_bytes, mime_type,
                           response_json_schema=None):
    """The receipt includes MIME and every document byte, not a URL/filename."""
    fingerprint = _media_fingerprint(model, prompt, _schema_text(response_json_schema), media_bytes, mime_type)
    if (type(receipt) is not _VerifiedInput or receipt._seal is not _SEAL
            or type(receipt.input_tokens) is not int or receipt.input_tokens <= 0
            or not hmac.compare_digest(receipt._signature,
                _signature(receipt._fingerprint, receipt.input_tokens, receipt.method))
            or receipt._fingerprint != fingerprint):
        raise ValueError('Verified AI document input does not match the current request')
    return receipt.input_tokens


async def verify_media_input(client, prompt, media_bytes, mime_type, response_json_schema=None,
                             *, model, input_token_budget, privacy_reviewed=False):
    """Count the exact reviewed media + prompt; never trim or change a document.

    Compressed file size is not rendered image/PDF token usage. Require a valid
    provider count for generation. countTokens also sends media externally, so
    the caller's one-document consent is required before even cache inspection.
    Only hashes/counts are cached in memory; no document bytes are persisted.
    """
    if privacy_reviewed is not True:
        raise ValueError('Document privacy review is required before token counting')
    schema_text = _schema_text(response_json_schema)
    fingerprint = _media_fingerprint(model, prompt, schema_text, media_bytes, mime_type)
    ensure_ai_text_safe(prompt, schema_text)
    estimate = ai_coordinator.estimate_input_tokens([prompt, schema_text], media_bytes=len(media_bytes))
    if type(input_token_budget) is not int or input_token_budget <= 0:
        return _receipt(fingerprint, estimate, 'fallback_invalid_budget')
    cached = _cached(fingerprint)
    method = 'provider_media_plus_schema_bytes'
    if cached is not None:
        return _receipt(fingerprint, cached, method)
    if len(prompt.encode('utf-8')) + len(schema_text.encode('utf-8')) > _MAX_COUNT_BYTES:
        return _receipt(fingerprint, estimate, 'fallback_request_too_large')
    if not await _take_count_slot():
        return _receipt(fingerprint, estimate, 'fallback_count_cooldown')
    try:
        cached = _cached(fingerprint)
        if cached is not None:
            return _receipt(fingerprint, cached, method)
        from google.genai import types
        contents = [prompt, types.Part.from_bytes(data=media_bytes, mime_type=mime_type)]
        return await _provider_receipt(client, prompt, model, schema_text, fingerprint, estimate,
                                        contents=contents, method=method)
    finally:
        _release_count_slot()


def _reset_for_tests():
    global _BLOCKED_UNTIL, _COUNT_ACTIVE
    with _LOCK:
        _CACHE.clear()
        _STARTS.clear()
        _BLOCKED_UNTIL = 0.0
        _COUNT_ACTIVE = False
