"""Minimize explicit office contact values in a news-only AI input copy.

This does not declare public contacts safe to transmit. Raw source text is
retained by callers, and the ordinary sensitive-input check still inspects the
entire remaining input before excerpt selection or provider admission.
"""
import re

from services.ai_input_safety import (
    AiInputReviewRequired, _EMAIL, _PHONE, ensure_ai_text_safe, inspect_ai_text,
)

RECIPE = 'news-office-contact-minimization-v1'
MARKER = '[업무연락처 생략]'
NOTE = '문의·담당 업무 연락처는 AI 전송용 사본에서만 생략했습니다. 원문은 보존됩니다.'
PROMPT_NOTICE = '''
[Local contact minimization]
Office contact values marked [업무연락처 생략] were withheld before transmission.
Markers are not source evidence. Do not infer, reconstruct or invent contact
values. Return contact and fact_evidence.contact as empty strings. A whole-source
selection preserves the original text locally, including the withheld contacts.
'''

# Restrict removal to an explicitly labelled field on the same line. A phone
# number elsewhere, including the next line after a heading, remains reviewable.
_CONTACT_FIELD = re.compile(
    r'^\s*(?:[•●○▪■□※*\-–]\s*|\d{1,2}[.)]\s*)?'
    r'(?:문의(?:처|사항|전화|\s*담당자)?|신청\s*문의|접수\s*문의|담당(?:자|부서)?|'
    r'연락처|접수(?:처|\s*(?:이메일|메일|전화))?)'
    r'(?:\s*(?:[/·ㆍ&]|및)\s*(?:문의|담당자?|연락처|접수처?))*'
    r'(?:\s*[:：（(\-–]\s*|[ \t]+(?=\S))'
)
_ROSTER = re.compile(
    r'(?:신청자|참여자|참가자|수혜자|대상자|선정자|당첨자|이용자|회원|직원)'
    r'\s*(?:명단|목록|연락망)|(?:신청자|참여자|대상자|개인)\s*별\s*(?:명단|목록|연락처|전화|휴대폰|이메일)'
    r'|성명\s*[,|/\t ]+\s*(?:연락처|전화|휴대폰|이메일)'
    r'|(?:^|\n)\s*(?:성명|이름|생년월일)\s*[:：]'
)
_URL = re.compile(r'(?:https?://|mailto:)[^\s<>]+', re.I)


def prepare_content(content):
    """Return an inspected transmission copy and value-free provenance.

    The original is checked first so removing an address cannot conceal a key
    or identity number embedded in it. Ambiguous rosters and unlabelled contact
    values are never minimized. Unicode variants we cannot map exactly remain
    subject to the global check, rather than rewriting unrelated source text.
    """
    original_check = inspect_ai_text(content)
    protected = tuple(reason for reason in original_check.reasons
                      if reason not in ('mobile_number', 'email_address'))
    if protected:
        raise AiInputReviewRequired(protected)
    counts = {'mobile_number': 0, 'email_address': 0}
    lines = []
    roster = bool(_ROSTER.search(content))
    contact_fields = 0
    for line in content.splitlines(keepends=True):
        label = _CONTACT_FIELD.match(line) if not roster else None
        spans = []
        if label:
            urls = [(match.start(), match.end()) for match in _URL.finditer(line)]
            for kind, pattern in (('email_address', _EMAIL), ('mobile_number', _PHONE)):
                for match in pattern.finditer(line, label.end()):
                    # URLs and their query parameters are never rewritten here.
                    if any(start <= match.start() < end for start, end in urls):
                        continue
                    if any(start <= match.start() < end for start, end, _ in spans):
                        continue
                    if kind in inspect_ai_text(match.group()).reasons:
                        spans.append((match.start(), match.end(), kind))
            # A long list of contacts is not an ordinary office contact field.
            if len(spans) > 3:
                spans = []
        if spans:
            contact_fields += 1
        for start, end, kind in sorted(spans, reverse=True):
            line = line[:start] + MARKER + line[end:]
            counts[kind] += 1
        lines.append(line)
    text = ''.join(lines)
    if contact_fields > 3 or sum(counts.values()) > 6:
        # Repeated per-person records are not a small announcement contact
        # footer even if every row happens to carry a contact label.
        ensure_ai_text_safe(content)
    ensure_ai_text_safe(text)
    changed = bool(sum(counts.values()))
    return text, {'recipe': RECIPE, 'status': 'minimized' if changed else 'unchanged',
                  'removed_counts': counts, 'note': NOTE if changed else ''}


def ensure_news_payload_safe(payload, *extra_texts):
    """Inspect every source field and preference; copy only the source body."""
    ensure_ai_text_safe(*(payload.get(key, '') for key in ('title', 'link', 'site_name')),
                        *extra_texts)
    content, metadata = prepare_content(payload.get('content', ''))
    return dict(payload, content=content), metadata


def add_coverage_metadata(coverage, metadata):
    """Attach only code-owned, value-free metadata using the existing note UI."""
    if metadata.get('status') != 'minimized':
        return coverage
    result = dict(coverage, contact_minimization=metadata)
    result['note'] = ' '.join(filter(None, (coverage.get('note', ''), metadata['note'])))
    return result


def clear_model_contact(result, metadata):
    """Withheld contact values cannot be filled back in by model inference."""
    if metadata.get('status') != 'minimized':
        return
    result['contact'] = ''
    if isinstance(result.get('fact_evidence'), dict):
        result['fact_evidence'] = dict(result['fact_evidence'], contact='')
