"""Internal consistency of model transcription, never independent image proof.

Every result remains a human-review candidate. This module does no I/O, fills
no unknown fact and does not treat the model's own quotation as ground truth.
"""
from copy import deepcopy
import re
from urllib.parse import urlsplit

TEXT_FIELDS = ('title', 'category', 'target', 'age_detail', 'target_income',
               'eligibility_detail', 'support_detail', 'contact')
LIST_FIELDS = ('target_characteristics', 'residence', 'life_cycle', 'related_urls')
DATE_FIELDS = ('deadline', 'event_date', 'event_end_date')
AGE_FIELDS = ('start_age_months', 'end_age_months')
EVIDENCE_FIELDS = (*TEXT_FIELDS, *LIST_FIELDS, *DATE_FIELDS, *AGE_FIELDS)
REVIEW_CODE = 'OCR_SOURCE_REVIEW_REQUIRED'
REVIEW_NOTE = (
    'AI가 읽은 내용입니다. 전사와 인용의 일치는 원본 이미지의 정확성을 보장하지 않습니다. '
    '원본에서 대상·금액·문의처·접수 및 이용 기간을 확인하고 필요한 내용을 직접 수정해주세요.'
)
_MISSING = {'', '미상', '없음', 'unknown', 'null', 'none', '-', '...'}
_EVENT_LABEL = re.compile(r'(?:행사|이용|운영|교육|강좌|수업|대출)\s*(?:기간|일시|일정|일)\s*[:：]?\s*')
_NONCURRENT = re.compile(r'변경\s*전|종전|기존|예시|가정|취소|제외|미정|추후|별도|아님|아니|불가')


def _metadata(source='', reasons=(), unverified=None, *, source_complete=False):
    return {'required': True, 'source_text': source,
            # Model self-report only; never independent image completeness.
            'source_complete': source_complete is True,
            'reasons': list(dict.fromkeys(['ocr_transcription_requires_review', *reasons])),
            'unverified_fields': deepcopy(unverified or {}), 'note': REVIEW_NOTE,
            'transcription_independently_verified': False}


def review_hold(reason='ocr_schema_invalid', *, source='', unverified=None):
    return {'is_relevant': False, 'needs_source_review': True,
            'error_code': REVIEW_CODE, 'error': REVIEW_NOTE,
            '_ocr_review': _metadata(source, [reason], unverified)}


def _safe_candidates(value):
    """Preserve bounded candidate values, never an arbitrary model object."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in EVIDENCE_FIELDS:
        item = value.get(key)
        if isinstance(item, str) and 0 < len(item) <= 30000:
            result[key] = item
        elif key in LIST_FIELDS and isinstance(item, list) and len(item) <= 100 and all(
                isinstance(v, str) and len(v) <= 2048 for v in item):
            if item:
                result[key] = list(item)
        elif key in AGE_FIELDS and type(item) is int and 0 <= item <= 1800:
            result[key] = item
    return result


def _whole_line_quote(quote, source):
    # A fragment "무료" from "무료 아님" is not a sufficient quotation. Keep
    # full physical lines, including their predicates and adjacent conditions.
    if not isinstance(quote, str) or not quote.strip():
        return False
    offset = source.find(quote)
    while offset >= 0:
        end = offset + len(quote)
        if (offset == 0 or source[offset - 1] == '\n') and (end == len(source) or source[end] == '\n'):
            return True
        offset = source.find(quote, offset + 1)
    return False


def _quote_text(quotes, source):
    if not quotes or not all(_whole_line_quote(q, source) for q in quotes):
        return ''
    return '\n'.join(quotes)


def _exact_prose(value, quotes):
    return value in quotes or value == '\n'.join(quotes)


def _title_span_matches(value, quotes, source):
    """Only whitespace presentation of every quoted line in one unique span.

    Do not remove a word boundary, omit a quoted subtitle, reorder lines or
    splice text across an intervening condition. This proves neither that the
    model selected every relevant title line nor that its transcription is true.
    """
    if not _quote_text(quotes, source):
        return False

    def display(text):
        # Deliberately not strip-all-whitespace: '공공 지원' != '공공지원'.
        return re.sub(r'[ \t\r\n]+', ' ', text).strip(' ')

    if display(value) != display('\n'.join(quotes)):
        return False
    # Exact full-line quotations must occur together, in the same order. Only
    # ordinary whitespace may separate them; duplicate spans stay on review.
    # The lookahead also counts overlapping duplicate spans.
    pattern = r'(?m)(?=^' + r'[ \t\r\n]+'.join(re.escape(q) for q in quotes) + r'(?=\n|$))'
    spans = re.finditer(pattern, source)
    return next(spans, None) is not None and next(spans, None) is None


def _literal(value, quote):
    # Avoid partial phone/URL/label matches within a longer identifier.
    return bool(value and re.search(r'(?<![\w])' + re.escape(value) + r'(?![\w])', quote))


def _iso_date(value):
    """Recognize one exact calendar day; never normalize or fill missing parts."""
    from datetime import date
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _event_bounds(source):
    """Only one explicit event/use period; never relabel application dates."""
    from services.news_deadline_grounding import _point, _RANGE
    lines = source.splitlines()
    periods = set()
    for index, line in enumerate(lines):
        label = _EVENT_LABEL.search(line)
        if not label:
            continue
        if re.search(r'신청|접수|모집', line) or _NONCURRENT.search(line):
            return None
        tail = line[label.end():].strip()
        if not tail and index + 1 < len(lines):
            tail = lines[index + 1].strip()
        if _NONCURRENT.search(tail):
            return None
        first = _point(tail, 0)
        if first is None:
            return None
        start, _clock, offset = first
        end = None
        connector = _RANGE.match(tail, offset)
        if connector:
            second = _point(tail, connector.end()) or _point(tail, connector.end(), shared_year=start.year)
            if second is None:
                return None
            end, _clock, offset = second
            if end < start:
                return None
        # Additional dates/branches cannot be encoded by a single event pair.
        if re.search(r'\d|추후|별도|회차|매주|상시', tail[offset:]):
            return None
        periods.add((start.isoformat(), end.isoformat() if end else None))
    return next(iter(periods)) if len(periods) == 1 else None


def _structured_supported(key, value, quote, source, output):
    from services.policy_evidence import structured_policy_issues
    candidate = {key: value}
    if key == 'life_cycle':
        candidate.update({name: output.get(name) for name in AGE_FIELDS})
    # Both the selected quote and complete transcription must support the value;
    # an isolated affirmative quote cannot hide an exception elsewhere.
    return (key not in structured_policy_issues(candidate, quote)
            and key not in structured_policy_issues(candidate, source))


def ground_fields(output, raw, *, taxonomy=None):
    """Remove unsupported derived fields; retain full transcription for review."""
    source = raw.get('source_text')
    if not isinstance(source, str) or source.strip().casefold() in _MISSING:
        return review_hold('ocr_transcription_missing', unverified=_safe_candidates(raw))
    if len(source) > 30000:
        return review_hold('ocr_schema_invalid', unverified=_safe_candidates(raw))
    evidence = raw.get('field_evidence')
    if (type(raw.get('source_complete')) is not bool
            or not isinstance(evidence, dict) or set(evidence) != set(EVIDENCE_FIELDS)
            or any(not isinstance(evidence[k], list) or len(evidence[k]) > 12
                   or any(not isinstance(q, str) or len(q) > 30000 for q in evidence[k])
                   for k in EVIDENCE_FIELDS)
            or sum(len(q) for rows in evidence.values() for q in rows) > 120000
            or set(raw) != {*EVIDENCE_FIELDS, 'is_relevant', 'source_text', 'source_complete', 'field_evidence'}):
        return review_hold('ocr_schema_invalid', source=source, unverified=_safe_candidates(raw))
    result = deepcopy(output)
    unverified = {}
    reasons = [] if raw['source_complete'] else ['ocr_transcription_partial']
    allowed = taxonomy if isinstance(taxonomy, dict) else {}

    def remove(key, value):
        unverified[key] = deepcopy(value)
        result[key] = [] if key in LIST_FIELDS else None if key in (*AGE_FIELDS, *DATE_FIELDS) else ''

    # The source is model-produced too. Copying it preserves context, not truth.
    previous_support = result.get('support_detail')
    if previous_support and previous_support != source:
        unverified['support_detail'] = previous_support
    result['support_detail'] = source
    for key in TEXT_FIELDS:
        value = result.get(key)
        if key == 'support_detail' or not value:
            continue
        quotes = evidence[key]
        quote = _quote_text(quotes, source)
        supported = bool(quote and _exact_prose(value, quotes))
        if key == 'title':
            supported = _title_span_matches(value, quotes, source)
        if key == 'contact' and quote:
            supported = supported or _literal(value, quote)
        if key == 'category' and allowed.get(key) and value not in allowed[key]:
            supported = False
        if key in ('target', 'age_detail', 'target_income') and quote:
            # Do not convert a negated/submitted/example condition into a
            # positive metadata statement. The full text remains visible below.
            from services.news_fact_grounding import scoped_affirmative_quote
            supported = supported and scoped_affirmative_quote(quote, source) is not None
        if not supported:
            remove(key, value)

    for key in AGE_FIELDS:
        value = result.get(key)
        if value is None:
            continue
        quote = _quote_text(evidence[key], source)
        if not quote or not _structured_supported(key, value, quote, source, result):
            remove(key, value)
    for key in LIST_FIELDS:
        values = result.get(key, [])
        if not values:
            continue
        quote = _quote_text(evidence[key], source)
        kept, removed = [], []
        for value in values:
            supported = bool(quote)
            if key == 'related_urls':
                try:
                    parsed = urlsplit(value)
                    supported = (supported and _literal(value, quote) and parsed.scheme in ('http', 'https')
                                 and bool(parsed.hostname) and not parsed.username and not parsed.password)
                except ValueError:
                    supported = False
            else:
                supported = (supported and (not allowed.get(key) or value in allowed[key])
                             and _structured_supported(key, [value], quote, source, result))
                # 일반 is a UI classification, not an observed fact about all
                # recipients. Keep it only when explicitly written in evidence.
                if key == 'target_characteristics' and value == '일반':
                    supported = supported and _literal(value, quote)
            (kept if supported else removed).append(value)
        result[key] = kept
        if removed:
            unverified[key] = removed

    for key in DATE_FIELDS:
        value = result.get(key)
        if not value:
            continue
        if not _iso_date(value):
            remove(key, value)
            reasons.append('ocr_date_format_unverified')
            continue
        quote = _quote_text(evidence[key], source)
        if key == 'deadline':
            from services.news_deadline_grounding import deadline_supported
            supported = bool(quote and deadline_supported(value, quote) and deadline_supported(value, source))
        else:
            bounds = _event_bounds(source)
            quoted_bounds = _event_bounds(quote) if quote else None
            index = 0 if key == 'event_date' else 1
            supported = bool(bounds and quoted_bounds == bounds and bounds[index] == value)
        if not supported:
            remove(key, value)
    if unverified:
        reasons.append('ocr_fields_unverified')
    result['needs_source_review'] = True
    result['_ocr_review'] = _metadata(source, reasons, unverified, source_complete=raw['source_complete'])
    return result
