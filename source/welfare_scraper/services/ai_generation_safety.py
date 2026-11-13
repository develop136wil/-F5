"""Bound generation size and reject incomplete provider output before parsing.

This is response integrity, not a factual-accuracy or monetary-budget guarantee.
No retry, provider payload logging, or operational state write happens here.
"""
from collections.abc import Mapping


# Structured extraction includes multiple rich-text fields and quoted evidence.
# Keep ample room for those fields; a cap hit is a review hold, never permission
# to accept an apparently valid but incomplete JSON object.
STRUCTURED_OUTPUT_TOKENS = 8192
STYLE_GUIDE_OUTPUT_TOKENS = 2048
SELECTOR_OUTPUT_TOKENS = 2048
VISION_OUTPUT_TOKENS = 8192

_MISSING = object()
_MESSAGES = {
    'output_limit': 'AI 응답이 출력 길이 한도에서 중단되어 보류했습니다. 원문을 확인해주세요.',
    'blocked': 'AI 제공자가 응답을 차단하여 보류했습니다. 원문을 확인해주세요.',
    'incomplete': 'AI 응답의 정상 완료를 확인하지 못해 보류했습니다. 원문을 확인해주세요.',
    'empty': 'AI 응답 내용이 비어 있어 보류했습니다. 원문을 확인해주세요.',
}


class AiResponseReviewRequired(RuntimeError):
    """Safe, terminal review reason; never contains provider/source text."""

    code = 'ai_response_review_required'

    def __init__(self, reason):
        self.reason = reason if reason in _MESSAGES else 'incomplete'
        super().__init__(_MESSAGES[self.reason])


def _field(value, name, default=None):
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _enum_name(value):
    # google-genai uses string-valued enums. Do not stringify arbitrary objects
    # or provider fields into errors/logs; unsupported metadata fails closed.
    if value is None:
        return None
    value = getattr(value, 'value', value)
    return value.rsplit('.', 1)[-1].upper() if isinstance(value, str) else ''


def complete_response_text(response):
    """Return nonempty text only after checking the available completion data.

    Text-only offline/replay adapters predate provider metadata and are retained
    without inventing STOP. The production SDK always exposes ``candidates``;
    its None/empty/unknown finish data is rejected, not treated as legacy input.
    """
    feedback = _field(response, 'prompt_feedback')
    block_reason = _enum_name(_field(feedback, 'block_reason'))
    if block_reason not in (None, 'BLOCKED_REASON_UNSPECIFIED', 'BLOCK_REASON_UNSPECIFIED'):
        raise AiResponseReviewRequired('blocked')

    candidates = _field(response, 'candidates', _MISSING)
    if candidates is not _MISSING:
        if not isinstance(candidates, (list, tuple)) or len(candidates) != 1:
            raise AiResponseReviewRequired('incomplete')
        candidate = candidates[0]
        ratings = _field(candidate, 'safety_ratings')
        if isinstance(ratings, (list, tuple)) and any(
                _field(rating, 'blocked') is True for rating in ratings):
            raise AiResponseReviewRequired('blocked')
        finish = _enum_name(_field(candidate, 'finish_reason'))
        if finish == 'MAX_TOKENS':
            raise AiResponseReviewRequired('output_limit')
        if finish in ('SAFETY', 'RECITATION', 'BLOCKLIST', 'PROHIBITED_CONTENT',
                      'SPII', 'IMAGE_SAFETY', 'IMAGE_PROHIBITED_CONTENT'):
            raise AiResponseReviewRequired('blocked')
        if finish != 'STOP':
            raise AiResponseReviewRequired('incomplete')

    try:
        text = _field(response, 'text')
    except (ValueError, TypeError, AttributeError):
        raise AiResponseReviewRequired('incomplete') from None
    if not isinstance(text, str) or not text.strip():
        raise AiResponseReviewRequired('empty')
    return text.strip()
