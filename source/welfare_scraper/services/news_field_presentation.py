"""Narrow labeled scalar equivalence; retain the complete verified source quote.

This is not a general label stripper or Korean paraphrase matcher. Labeled values
must cover their entire payload. Wrapped eligibility prose may only differ in
bounded whitespace and a sentence-ending Korean verb seam; its literal source
quote, never a reconstructed model sentence, is returned for display.
"""
import re

from services.news_fact_grounding import scoped_affirmative_quote, source_quote_issue
from services.policy_evidence import preserves_quoted_facts


_LABELS = {
    'eligibility_detail': r'(?:(?:참가|참여|모집|지원|신청)[ \t]*대상|신청[ \t]*자격|대상)',
    'contact': r'(?:문의처|문의|연락처|전화번호|전화)',
}
_SEPARATOR = r'(?:[ \t]*[:：][ \t]*(?:\n[ \t]*)?|[ \t]*\n[ \t]*[:：]?[ \t]*)'
_PHONE = r'(?:0\d{1,2}-\d{3,4}-\d{4}|\d{4}-\d{4})'
_PHONES = re.compile(_PHONE + r'(?:[ \t]*[,/·][ \t]*(?:' + _PHONE + r'|\d{3,4}))*')
_INSTITUTION = re.compile(r'\([가-힣A-Za-z ·ㆍ/-]{1,80}\)')


def _wrapped_comparison(text):
    """Comparison only: do not join numbers, negative words or arbitrary Korean."""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u2028\u2029]', text):
        return None
    text = re.sub(r'(?<=[가-힣])\n[ \t]*(합니다|됩니다|입니다)(?=[.!?。]|$)', r'\1', text)
    return re.sub(r'[ \t\n]+', ' ', text).strip()


def verified_labeled_display(field, value, quote, content):
    """Return full source-backed display text, or None without repairing facts.

Callers still run all other grounding/condition/target checks. A previous empty
held field must not be restored merely because its old rejected value is here.
"""
    if field not in _LABELS or not all(isinstance(item, str) for item in (value, quote, content)):
        return None
    if not value.strip() or len(quote) > 2000 or source_quote_issue(quote, content):
        return None
    if scoped_affirmative_quote(quote, content) is None:
        return None
    display = quote.strip()
    normalized = display.replace('\r\n', '\n').replace('\r', '\n')
    if field == 'eligibility_detail' and '\n' in normalized and display in content:
        comparable = _wrapped_comparison(display)
        if comparable is not None and comparable == _wrapped_comparison(value):
            # Keep the exact captured source, including every line break and
            # condition. Other condition/context guards still run in assess.
            return display
    match = re.fullmatch(_LABELS[field] + _SEPARATOR + r'([^\n]+)(?:\n([^\n]+))?', normalized)
    if match is None:
        return None
    payload, context = match.groups()
    payload = payload.strip()
    if field == 'eligibility_detail':
        if context is not None or not preserves_quoted_facts(value, payload):
            return None
    else:
        # Only telephone lists with literally the same complete/short numbers.
        # A second phone line, a named second institution, extensions or free
        # prose remain on the conservative existing full-quote path.
        if _PHONES.fullmatch(payload) is None or re.sub(r'[ \t]', '', value) != re.sub(r'[ \t]', '', payload):
            return None
        if context is not None and _INSTITUTION.fullmatch(context.strip()) is None:
            return None
    return display
