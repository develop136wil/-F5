"""Manual source-comparison aid; never an approved fact or model repair.

Only complete, short collected input is displayed. Long input is not clipped
into a seemingly complete claim, and model quotes never become display text.
No model, network, persistence, automatic import or eligibility decision here.
"""
import re

from services.news_fact_grounding import source_quote_issue
from services.fact_presentation import presentation_clauses
from services.policy_evidence import _fact_clauses

MAX_REVIEW_SOURCE_CHARS = 1200
_UNSAFE_DISPLAY = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]')
_NOTICE = ('확보한 텍스트이며 AI 검증 완료나 지원 자격 판정을 뜻하지 않습니다. '
           '자동 반영하지 않으므로 원문 전체와 대조하고 첨부·최신 변경도 확인해주세요.')
_PROSE = ('support_detail', 'eligibility_detail', 'contact')
_APPLICATION_LABEL = re.compile(r'^\s*(?:신청방법|접수방법|신청경로)\s*[:：]\s*[:：]?\s*')


def _comparison_clauses(text):
    # presentation_clauses may join a standalone label with a following ':'.
    # Only diagnostic comparison omits that exact application label; complete
    # channel wording, qualifiers, negatives and original display remain intact.
    return [fact for clause in presentation_clauses(text)
            for fact in _fact_clauses(_APPLICATION_LABEL.sub('', clause, count=1))]


def auxiliary_condition_fields(analysis, condition, content):
    """Name valid auxiliary quotes containing the whole missing clause sequence.

    This only says where the words occurred. It never supplies missing facts,
    approves an applicant, or certifies that an excerpt contains all conditions.
    """
    if not isinstance(analysis, dict) or not isinstance(condition, str):
        return []
    needed = _comparison_clauses(condition)
    if not needed:
        return []
    quotes = [('applicant_evidence', analysis.get('applicant_evidence'))]
    evidence = analysis.get('fact_evidence')
    if isinstance(evidence, dict):
        quotes.extend(('fact_evidence.' + field, evidence.get(field))
                      for field in ('support_detail', 'eligibility_detail'))
    fields = []
    for field, quote in quotes:
        if (not isinstance(quote, str) or len(quote) > 2000
                or source_quote_issue(quote, content)):
            continue
        clauses = _comparison_clauses(quote)
        if any(clauses[index:index + len(needed)] == needed
               for index in range(len(clauses) - len(needed) + 1)):
            fields.append(field)
    return fields


def build_review_evidence(analysis, content):
    """Return a bounded review-only projection, or None when no aid is needed.

    Caller exposes this only for a currently rechecked review result. Stored
    validation diagnostics and provider-supplied preview objects are ignored.
    A retained rejected value may trigger display of the ORIGINAL source, but
    no model value/quote/diagnostic text enters the returned display payload.
    """
    if not isinstance(analysis, dict) or not isinstance(content, str) or not content.strip():
        return None
    from services.news_analysis_safety import _omitted_conditions
    prior = analysis.get('unverified_facts')
    withheld = isinstance(prior, dict) and any(
        not analysis.get(field) and isinstance(prior.get(field), str) and prior[field].strip()
        for field in _PROSE)
    if not withheld and not _omitted_conditions(analysis, content):
        return None
    packet = {'version': 1, 'notice': _NOTICE, 'items': [], 'omitted_count': 0}
    # Never drop a heading, tail restriction, or an arbitrary context window to
    # fit the UI. An unsafe/large block is omitted in full, not auto-truncated.
    if len(content) > MAX_REVIEW_SOURCE_CHARS or _UNSAFE_DISPLAY.search(content):
        packet['omitted_count'] = 1
        return packet
    packet['items'].append({
        'field': 'source_content', 'reason': 'manual_source_comparison',
        'label': '확보한 원문 · 요약 아님', 'start_line': 1,
        'end_line': len(content.splitlines()), 'text': content,
    })
    return packet
