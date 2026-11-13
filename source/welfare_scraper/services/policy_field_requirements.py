"""Source-owned field reminders for NEW extractions, never response repair.

Only unambiguous source facts produce requirements. Empty requirements do not
certify completeness. Existing evidence, selection and source-review guards
remain mandatory; stored proposals are not reclassified by this helper.
"""
import re

from services.policy_source_coverage import (
    _CONTACT_LABELS, _LABEL_TO_KIND, _label, _contact_destination_only,
)


_FOOTER = re.compile(
    r'^(?:본저작물은|공공누리|copyright|©|개인정보처리방침|저작권정책|이용약관|'
    r'사이트맵|자료관리담당|페이지담당|정보담당|만족도)', re.I)
_NON_POLICY = frozenset({
    '검색', '검색조건', '검색필터', '필터', '조건검색', '맞춤검색', '소득선택',
    '제공기관', '제공인력', '종사자', '법적근거', '관련법령', '근거법령',
    '추진근거', '자료예시', '자료관리담당자', '자료담당자', '자료담당부서',
    '담당기관', '담당부서', '담당자',
})
_INCOME_LABELS = frozenset({'소득', '소득기준', '가구소득', '소득요건', '소득조건'})
_UNRESTRICTED = re.compile(r'^(?:소득(?:기준|요건|조건|제한)?(?:은|이)?'
    r'(?:무관|불문|없음|없습니다|제한없음)|소득에관계없이)(?:[.!。])?$')
_NUMERIC_INCOME = re.compile(
    r'(?:기준)?중위\s*소득\s*\d[\d,.]*\s*(?:%|퍼센트)\s*(?:이하|미만|이상|초과)|'
    r'(?:가구|월평균|월|연간)?\s*소득\s*(?:기준)?\s*\d[\d,.]*\s*'
    r'(?:만\s*원|천\s*원|원)\s*(?:이하|미만|이상|초과)')
_AGE_LABELS = frozenset({'나이', '연령', '대상연령', '지원연령', '수급연령', '월령'})
_AGE_OTHER_HEADINGS = frozenset({
    '신청자', '신청인', '보호자', '대리인', '동반자', '부모',
    '신청자나이', '신청자연령', '신청인연령', '보호자나이', '보호자연령',
    '대리인연령', '부모나이', '부모연령', '임신기간', '거주기간',
    '지원기간', '사용기간', '유효기간', '지급기간', '납부기간', '근무기간',
})
_AGE_CONTEXT_RESET = frozenset({
    '지원내용', '서비스내용', '서비스지원내용', '급여내용',
    '사업개요', '사업목적', '추진배경', '기본정보',
})
_AGE_OTHER_ROLE = re.compile(r'(?<!한)부모|보호자|대리인|동반자|신청자(?!격)|신청인|'
    r'제공인력|제공기관|종사자|서류|증빙|법령|법률|법\s*제\s*\d+|예시|검색|필터|'
    r'기간|근무|납부|임신|전입|거주')
_AGE_UNCERTAIN = re.compile(r'전연령|전생애|모든(?:연령|나이)|'
    r'(?:연령|나이|월령)[^\r\n;。]{0,30}(?:\r?\n)?'
    r'(?:무관|불문|상관없|관계없|제한(?:이|은|는)?없|무제한|예외|완화|'
    r'적용제외|(?:제한|적용)(?:하지|받지)않)')


def _compact(value):
    return re.sub(r'\s+', '', value) if isinstance(value, str) else ''


def _clean(raw):
    return re.sub(r'^\s*(?:#{1,6}\s*|[-•·○●□■▶▷◈※]\s*)*', '', raw).strip()


def _pure_contact(raw):
    value = _clean(raw)
    if _contact_destination_only(value):
        return True
    # This location is required ONLY under an explicit enquiry heading. A
    # visit/application address never creates a contact or residence fact.
    return re.fullmatch(r'(?:각|관할)?(?:읍[·ㆍ]?면[·ㆍ]?동|동)?'
        r'(?:행정복지센터|주민센터)', _compact(value)) is not None


def _single_contact_block(content):
    lines = content.splitlines(keepends=True)
    headings = [index for index, raw in enumerate(lines) if _label(raw) in _CONTACT_LABELS]
    if len(headings) != 1:
        return None
    index = headings[0]
    # A generic page-management/footer enquiry is not a program contact.
    if any(_FOOTER.match(_compact(_clean(raw))) for raw in lines[:index]):
        return None
    preceding = [_label(raw) or _compact(_clean(raw)).rstrip(':：') for raw in lines[:index]]
    for label in reversed(preceding):
        if label in _NON_POLICY:
            return None
        if label in _LABEL_TO_KIND:
            break
    parts = re.split(r'[:：]', lines[index], maxsplit=1)
    start = index if len(parts) == 2 and parts[1].strip() else index + 1
    pieces, last = [], None
    for number in range(start, len(lines)):
        raw = parts[1] if number == index else lines[number]
        if not raw.strip():
            pieces.append(raw)
            continue
        if number != index and (_label(raw) is not None or _FOOTER.match(_compact(_clean(raw)))):
            break
        if not _pure_contact(raw):
            # Never select just the recognizable prefix of an ambiguous block.
            # Real conditions are still required by the existing prose guards.
            return None
        pieces.append(raw)
        last = number
    text = ''.join(pieces).strip()
    if last is None or not text or text not in content:
        return None
    return {'text': text, 'start_line': start + 1, 'end_line': last + 1}


def _income_requirements(content, catalog):
    from services.policy_evidence import _policy_recipient_source
    from services.news_fact_grounding import scoped_affirmative_quote
    recipient, _ = _policy_recipient_source(content)
    source_lines, recipient_lines = content.splitlines(keepends=True), recipient.splitlines()
    required, blocked, prior = [], False, ''
    for index, raw in enumerate(source_lines):
        clean = _clean(raw)
        label = _compact(re.split(r'[:：]', clean, maxsplit=1)[0])
        known = _label(raw)
        if label in _NON_POLICY or _FOOTER.match(_compact(clean)):
            blocked = True
        elif known in _LABEL_TO_KIND and label not in _INCOME_LABELS:
            blocked = False
        kept = recipient_lines[index] if index < len(recipient_lines) else ''
        if (blocked or not kept.strip() or re.search(
                r'예시|예제|샘플|검색|필터|선택지|제공인력|제공기관|종사자|'
                r'법령|법률|법\s*제\s*\d+|소득\s*(?:확인|증빙|증명)(?:서|자료)', clean)):
            prior = label
            continue
        inline = re.split(r'[:：]', clean, maxsplit=1)
        payload = inline[1].strip() if len(inline) == 2 and label in _INCOME_LABELS else clean
        explicit = bool(_UNRESTRICTED.fullmatch(_compact(payload)) or _NUMERIC_INCOME.search(payload))
        # A bare "무관" is meaningful only with its exact adjacent income
        # heading. Keep both lines, not a new "소득 무관" paraphrase.
        paired = _compact(payload).rstrip('.。') in {'무관', '제한없음', '없음'} and prior in _INCOME_LABELS
        if (explicit or paired) and scoped_affirmative_quote(raw, recipient) is not None:
            unit = next((u for u in catalog if u['start_line'] <= index + 1 <= u['end_line']), None)
            # A leading policy profile ("소득\n소득무관") may be an optional
            # generic unit in the UNION checklist. Its explicit income fact
            # still belongs in target_income; that unrelated flag is not a
            # field-specific completeness decision. Role/context guards above
            # and unchanged evidence validation remain authoritative.
            if unit is not None and 'documents' not in unit['kinds']:
                start = index - 1 if paired else index
                quote = ''.join(source_lines[start:index + 1]).rstrip('\r\n')
                required.append({'unit_id': unit['id'], 'quote': quote,
                                 'start_line': start + 1, 'end_line': index + 1})
        prior = label
    return required


def _age_requirements(content, catalog):
    """Require literal recipient-age units, without deriving output bounds.

    Reuse the existing scalar-evidence interpretation only as an intersection
    check. A role, duration, calendar basis or ambiguous source never acquires
    a field requirement from an audience tag or a nearby unrelated number.
    """
    from services.policy_evidence import _policy_recipient_source
    from services.news_analysis_safety import _age_evidence
    from services.news_fact_grounding import scoped_affirmative_quote
    from services.policy_source_review import source_criteria_issues

    if (_AGE_UNCERTAIN.search(re.sub(r'[ \t\u00a0]', '', content))
            or any(item['field'] == 'age_detail' for item in source_criteria_issues(content))):
        return []
    recipient, target_clauses = _policy_recipient_source(content)
    lines, recipient_lines = content.splitlines(keepends=True), recipient.splitlines()
    # This private evidence projection retains every physical line position;
    # it is never supplied to AI or copied into an output field. A subordinate
    # "지원대상/신청자격" in a guardian table must not end its parent role.
    kept_lines, blocked = [], False
    for index, raw in enumerate(lines):
        clean = _clean(raw)
        label = _compact(re.split(r'[:：]', clean, maxsplit=1)[0])
        if label in _NON_POLICY or label in _AGE_OTHER_HEADINGS or _FOOTER.match(_compact(clean)):
            blocked = True
        elif label in _AGE_CONTEXT_RESET:
            blocked = False
        kept_lines.append('' if blocked else recipient_lines[index] if index < len(recipient_lines) else '')
    age_source = '\n'.join(kept_lines)
    bounds, intervals = _age_evidence(age_source)
    if (not bounds or len(intervals) != 1
            or any(not 0 <= value <= 1800 for _, value in bounds)):
        return []
    required, prior = [], ''
    for index, raw in enumerate(lines):
        clean = _clean(raw)
        label = _compact(re.split(r'[:：]', clean, maxsplit=1)[0])
        kept = kept_lines[index] if index < len(kept_lines) else ''
        if not kept.strip() or _AGE_OTHER_ROLE.search(clean):
            prior = label
            continue
        # Priority or alternative-age exceptions are not an ordinary universal
        # recipient bound. Explicit non-age income notes do not change age.
        qualified = False
        for neighbour in range(max(0, index - 1), min(len(lines), index + 2)):
            note = lines[neighbour]
            if re.search(r'우선|우대|예외|완화|원칙|가점', note) and (
                    neighbour == index or not re.search(r'소득|거주|주소|전입|출생신고', note)
                    or re.search(r'연령|나이|월령', note)):
                qualified = True
        local_bounds, local_intervals = _age_evidence(raw)
        recipient_context = (raw.strip() in target_clauses or label in _AGE_LABELS
            or prior in _AGE_LABELS or re.search(r'영아|영유아|아동|어린이|청년|노인|어르신|'
                                               r'주민|대상|연령|나이|생후', clean))
        if (qualified or not recipient_context or not local_bounds
                or not local_bounds.issubset(bounds) or local_intervals != intervals
                or scoped_affirmative_quote(raw, age_source) is None):
            prior = label
            continue
        unit = next((u for u in catalog if u['start_line'] <= index + 1 <= u['end_line']), None)
        if unit is not None and 'documents' not in unit['kinds']:
            required.append({'unit_id': unit['id'], 'quote': raw.rstrip('\r\n'),
                             'start_line': index + 1, 'end_line': index + 1})
        prior = label
    return required


def policy_field_requirements(content, catalog=None):
    """Return literal requirements, never values to insert into an answer."""
    if not isinstance(content, str) or not content.strip():
        return {'contact': None, 'target_income': [], 'age_detail': []}
    if catalog is None:
        from services.policy_source_units import source_units
        catalog = source_units(content)
    return {'contact': _single_contact_block(content),
            'target_income': _income_requirements(content, catalog),
            'age_detail': _age_requirements(content, catalog)}


def missing_policy_fields(result, requirements):
    """Called after evidence validation for a fresh response; never mutate it."""
    missing = []
    contact = requirements['contact']
    if contact is not None and _compact(contact['text']) not in _compact(result.get('contact')):
        missing.append('contact')
    metadata = result.get('_source_unit_selection')
    for field in ('target_income', 'age_detail'):
        text = _compact(result.get(field))
        selected = metadata.get('fields', {}).get(field, []) if isinstance(metadata, dict) else None
        if any(_compact(item['quote']) not in text or
               (selected is not None and item['unit_id'] not in selected)
               for item in requirements.get(field, [])):
            missing.append(field)
    return missing
