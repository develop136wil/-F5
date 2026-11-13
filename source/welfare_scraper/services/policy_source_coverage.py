"""Literal source-line checklist for range-based policy extraction.

This is a conservative coverage candidate detector, not semantic completeness
or permission to approve an extraction. Callers bound the complete source before
using the checklist; lines are never truncated here to make a proposal pass.
"""
import re


COVERAGE_KINDS = frozenset({
    'benefit', 'eligibility', 'exclusion', 'application', 'documents', 'continuation',
})

# Exact labels establish context for otherwise keyword-free child/table rows.
# Unknown headings do NOT end a critical section: their descendants may still
# contain an exception or a required document.
_SECTIONS = {
    'benefit': (
        '지원내용', '지원금액', '지원금액및혜택', '지원혜택', '혜택', '급여내용',
        '급여액', '지급액', '지급금액', '지원규모', '지원인원', '대상자별추가지원금',
        '추가지원금', '추가지원', '추가혜택', '지급방법', '지급시기', '지원기간',
        '사용기간', '사용기한', '유효기간', '이용시간',
    ),
    'eligibility': (
        '지원대상', '신청대상', '이용대상', '대상', '신청자격', '지원자격', '자격요건',
        '자격조건', '선정기준', '가구소득', '소득기준', '연령', '나이', '거주요건',
        '거주조건', '근로기준', '주택기준', '가구기준',
    ),
    'exclusion': (
        '지원제외대상', '신청제외대상', '제외대상', '제외조건', '지원제외',
        '지원불가', '신청불가', '중복지원제한', '중복가입제한', '유의사항',
        '주의사항', '예외사항', '예외조건', '이용규칙',
    ),
    'application': (
        '신청안내', '이용안내', '신청방법', '신청기간', '접수기간', '접수방법', '신청경로',
        '신청장소', '접수장소', '신청및접수', '지원절차', '신청절차',
        '추진일정', '결과통보', '결과발표', '신청및신청내역확인',
    ),
    'documents': (
        '제출서류', '구비서류', '필수서류', '증빙서류', '필수제출서류',
        '신청서류', '준비서류',
    ),
    'continuation': (
        '가입및유지기준', '가입기준', '유지기준', '유지조건', '지급조건',
        '지급중단', '지원중단', '중도해지', '해지조건', '환수', '환수기준',
        '재신청', '재신청방법', '재판정', '갱신', '갱신조건',
    ),
}
_LABEL_TO_KIND = {label: kind for kind, labels in _SECTIONS.items() for label in labels}
# These are table/role sublabels inside eligibility or exclusion sections. A
# bare "연령" below "지원 제외 대상" must not turn its values into a new
# affirmative age section detached from the negative parent.
_SUBORDINATE_ROLE_LABELS = frozenset({
    '가구소득', '소득기준', '연령', '나이', '거주요건', '거주조건',
    '근로기준', '주택기준', '가구기준',
})
_NEUTRAL_LABELS = frozenset({
    '문의', '문의처', '연락처', '담당기관', '담당부서', '담당자', '자료관리담당자',
    '자료담당부서', '자료담당자', '민원상담', '민원상담(지침및서류관련문의)',
    '알림자료', '공고문', '관련자료', '관련링크', '관련사이트', '참고자료',
    '만족도조사', '사이트맵', '개인정보처리방침', '저작권정책', '이용약관',
    '사업개요', '사업명', '사업목적', '추진배경',
})
_CONTACT_LABELS = frozenset({'문의', '문의처', '연락처', '민원상담', '민원상담(지침및서류관련문의)'})
_NAVIGATION_LINES = frozenset({
    '민원신청', '전자민원신청', '온라인민원신청', '신청하기', '확인하기',
    '바로가기', '복지로사이트바로가기', '민원상담챗봇', 'Q&A',
    '이용약관', '개인정보처리방침', '저작권정책', '사이트맵',
})
_ACCESSIBILITY_LINES = frozenset({
    '←→표를가로로스크롤해주세요', '표를가로로스크롤해주세요',
})
_TABLE_CAPTION = re.compile(
    r'^(?:구분|매월\s*저축액)\s*,[^\n]{1,240}에\s*관한\s*표$')
_PREFIX = re.compile(r'^\s*(?:#{1,6}\s*|[-•·○●□■▶▷◈※✅📋📍💰📅📝]\s*)*')


def _compact(value):
    return re.sub(r'[\s·ㆍ]+', '', value)


def _label(raw):
    """Return a known exact/colon label; never infer a heading from a sentence."""
    cleaned = _PREFIX.sub('', raw).strip()
    cleaned = re.sub(r'^(?:\d{1,2}[.)]|[가-하][.)]|[①-⑳])\s*', '', cleaned)
    cleaned = cleaned.strip('*').strip()
    label = _compact(re.split(r'[:：]', cleaned, maxsplit=1)[0].strip())
    if label in _LABEL_TO_KIND or label in _NEUTRAL_LABELS:
        return label
    return None


def _contact_destination_only(raw):
    text = _compact(raw).strip('.。')
    if re.fullmatch(r'(?:거주지|주소지|주민등록(?:상)?주소지)(?:관할)?(?:읍면동|동)?'
            r'(?:주민센터|행정복지센터)', text):
        return True
    # A department name can itself contain "장애인/한부모". Under an explicit
    # enquiry heading it is a contact, not a qualification, ONLY when the whole
    # line is a short department name plus a phone. Never swallow added terms.
    if re.search(r'신청|접수|제외|불가|금지|중복|거주|자격|대상|필요|서류|증빙|'
                 r'이상|이하|미만|초과|부터|까지|소급|개월|기간|하지않|되지않', text):
        return False
    return re.fullmatch(r'[가-힣A-Za-z·ㆍ]{1,35}(?:과|팀|담당관|센터)'
        r'(?:[:：]?[☎☏]?\(?)(?:0\d{1,3}[- ]?)?\d{3,4}[- ]\d{4}\)?', text) is not None


def contact_destination_lines(content):
    """Exact contact-only source lines mapped to their destination, not facts.

    Consumers may account for these only when that SAME destination survives
    in the separately source-validated contact field. Nothing is auto-filled.
    Keys/values remove whitespace and terminal stops, not meaningful words.
    """
    if not isinstance(content, str):
        return {}
    result, contact_context = {}, False
    plain = lambda text: re.sub(r'\s+', '', text).strip('.。')
    for raw in content.splitlines():
        label = _label(raw)
        stripped = _PREFIX.sub('', raw).strip()
        if label is not None:
            contact_context = label in _CONTACT_LABELS
            parts = re.split(r'[:：]', stripped, maxsplit=1)
            destination = parts[1] if contact_context and len(parts) == 2 else ''
        else:
            destination = stripped if contact_context else ''
        if destination and _contact_destination_only(destination):
            result[plain(raw)] = plain(destination)
    return result


def _standalone_kind(raw, *, contact_context=False):
    """Narrow independent facts outside recognized sections remain candidates."""
    if re.search(r'제외|불가|금지|중복.{0,16}(?:제한|가입|지원)|하지\s*않|되지\s*않', raw):
        return 'exclusion'
    if re.search(r'서류|증빙|계약서|확인서|증명서|통장\s*사본|등록증', raw):
        return 'documents'
    if re.search(r'재신청|재판정|환수|중도\s*해지|유지\s*(?:기준|조건)|지급\s*중단', raw):
        return 'continuation'
    if re.search(r'신청|접수|방문\s*등록|온라인\s*등록', raw):
        return 'application'
    # An exact contact destination is not a residence qualification. This
    # exception requires an explicit contact heading and the WHOLE line; any
    # extra eligibility, application or exclusion clause remains a candidate.
    # Keep the original line as an optional source unit and contact evidence.
    if contact_context and _contact_destination_only(raw):
        return None
    if re.search(r'대상|자격|거주|근로.{0,10}소득|중위\s*소득|\d\s*(?:세|개월)\s*(?:이상|이하|미만|초과)', raw):
        return 'eligibility'
    # A quantity alone could be a phone number or table metadata. Require a
    # benefit/payment noun plus an explicit amount or a literal waiver/discount.
    if (re.search(r'지원|지급|수당|급여|교통비|교육비|이사비|장려금|지원금', raw)
            and re.search(r'\d[\d,.]*\s*(?:억\s*원|만\s*원|천\s*원|원|%|퍼센트)|무료|면제|할인|감면', raw)):
        return 'benefit'
    return None


def contact_condition_clauses(content):
    """Critical prose beside/below an enquiry, never a contact-only exception.

    Reuse the source checklist's classifier and the final guard's presentation
    boundaries. This keeps an age or application period on the next line from
    falling through the legacy keyword guard after a pure contact was skipped.
    A new recognized section ends enquiry inheritance; its first joined clause
    still retains its own explicit source classification.
    """
    if not isinstance(content, str):
        return {}
    from services.fact_presentation import presentation_clauses
    result, contact_context = {}, False
    for raw in presentation_clauses(content):
        label = _label(raw)
        was_contact = contact_context
        if label in _NEUTRAL_LABELS:
            contact_context = label in _CONTACT_LABELS
        elif label in _LABEL_TO_KIND:
            contact_context = False
        if not contact_context and not (was_contact and label in _LABEL_TO_KIND):
            continue
        parts = re.split(r'[:：]', _PREFIX.sub('', raw).strip(), maxsplit=1)
        payload = parts[1] if label in _CONTACT_LABELS and len(parts) == 2 else raw
        kind = _standalone_kind(payload, contact_context=True)
        if kind:
            result[re.sub(r'\s+', '', raw).strip('.。')] = kind
    return result


def _scan(content):
    """Build candidate rows and heading/context groups from the same boundaries."""
    if not isinstance(content, str):
        return [], []
    candidates = []
    sections = []
    active = None
    contact_context = False
    for number, raw in enumerate(content.splitlines(), start=1):
        stripped = _PREFIX.sub('', raw).strip()
        if not stripped:
            continue
        compact = _compact(stripped)
        label = _label(raw)
        if label in _NEUTRAL_LABELS:
            if active is not None:
                sections.append(active)
            active = None
            contact_context = label in _CONTACT_LABELS
            # A neutral label is not permission to discard a real condition
            # written beside it, e.g. "문의처: 주민센터. 방문 신청만 가능".
            parts = re.split(r'[:：]', stripped, maxsplit=1)
            kind = _standalone_kind(parts[1], contact_context=contact_context) if len(parts) == 2 else None
            if kind:
                candidates.append({'line_number': number, 'kind': kind})
            continue
        if label in _LABEL_TO_KIND:
            contact_context = False
            subordinate = (active is not None
                           and active['kind'] in {'eligibility', 'exclusion'}
                           and label in _SUBORDINATE_ROLE_LABELS)
            if not subordinate:
                if active is not None:
                    sections.append(active)
                active = {'start_line': number, 'end_line': number, 'kind': _LABEL_TO_KIND[label]}
        if (compact in _NAVIGATION_LINES or compact in _ACCESSIBILITY_LINES
                or _TABLE_CAPTION.fullmatch(stripped)):
            continue
        kind = active['kind'] if active is not None else _standalone_kind(stripped, contact_context=contact_context)
        if kind:
            candidates.append({'line_number': number, 'kind': kind})
            if active is not None:
                active['end_line'] = number
    if active is not None:
        sections.append(active)
    return candidates, sections


def coverage_candidates(content):
    """Return unique ``{line_number, kind}`` candidates in source order.

    Line numbers are one-based physical source lines. Empty rows, a small set of
    exact navigation labels and recognizable table accessibility captions are
    skipped. Neutral headings end inherited context; later substantive facts
    can still be detected independently. No source text enters this metadata.
    """
    return _scan(content)[0]


def coverage_sections(content):
    """Return inclusive ``{start_line, end_line, kind}`` heading/context groups.

    These nonoverlapping ranges include the critical heading and its last
    substantive child. An interior navigation/accessibility row stays inside
    the range to preserve contiguous source wording. A selected field touching
    a section must contain its entire range, not split its exclusion/role header
    from child rows across fields. Standalone facts are not invented sections.
    """
    return _scan(content)[1]
