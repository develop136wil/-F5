"""Conservative grounding checks, not a semantic correctness guarantee."""
import re
from decimal import Decimal
from collections import Counter
from services.fact_presentation import presentation_clauses

# Diagnosis-only changes must not expire v6 proposals and spend another API call.
POLICY_EVIDENCE_VERSION = 'policy-extract-v6-payment-predicate'
POLICY_DIAGNOSTIC_VERSION = 1
POLICY_PROSE_FIELDS = ('support_detail', 'eligibility_detail', 'age_detail', 'target_income')
POLICY_DIAGNOSTIC_CODES = frozenset({
    'invalid_input', 'invalid_evidence_mapping', 'structured_fact_unsupported',
    'invalid_prose_value', 'instruction_placeholder', 'source_quote_missing',
    'source_quote_insufficient', 'source_quote_not_found', 'source_quote_boundary_changed',
    'presentation_not_preserved', 'number_not_supported', 'quantity_unit_changed',
    'label_quantity_changed', 'no_supported_prose', 'source_condition_not_preserved',
    'source_unit_evidence_invalid', 'source_calendar_age_review', 'source_income_criteria_review',
    'source_birth_date_age_review',
})
POLICY_CONDITION_KINDS = frozenset({'application', 'eligibility_or_restriction'})
_DIAGNOSTIC_LIMIT = 20

_QUANTITY_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(억원|만원|천원|개월|시간|분|세|년|월|일|명|회|원|%|퍼센트)')
_EXACT_UNITS = {'억원': ('원', 100000000), '만원': ('원', 10000),
                '천원': ('원', 1000), '원': ('원', 1),
                '시간': ('분', 60), '분': ('분', 1),
                '퍼센트': ('%', 1), '%': ('%', 1)}
_TEMPLATE_LINES = {'대상자 및 자격 요건', '방법 및 장소', '금액, 바우처, 서비스 내용',
                   '시작일 ~ 마감일', '기타 안내', '...'}
_DISPLAY_HEADERS = {'지원 내용', '신청 자격', '신청 방법', '지원 금액 및 혜택',
                    '신청 기간', '참고 사항', '대상', '지원 대상', '신청 대상',
                    '자격 조건', '지원 금액'}
_CONDITION_RE = re.compile(
    r'대상|자격|거주|탑승|보호자|신청|필요|불가|제외|중복|이하|이상|까지|부터|한부모|장애인|'
    r'^\s*(?:아님|아닌|아니|아닙|않|금지|없|안\s*(?:됨|돼|되))|'
    r'미지급|미지원|미수급|미수령|(?:지급|지원|수급|수령)\s*중단|'
    r'(?:지급|지원|수급|수령)\s*(?:을\s*)?(?:하지|되지|받지)\s*않|'
    r'(?:지급|지원|수급|수령)\s*(?:받을|할|될)\s*수\s*없')
_NAVIGATION_LABELS = {'민원신청', '전자민원신청', '온라인민원신청', '신청하기',
                      '이용약관', '개인정보처리방침', '저작권정책', '사이트맵'}
_DEPENDENT_PREDICATE_RE = re.compile(
    r'아님|아닌|아니|아닙|않|불가|불필요|제외|금지|없|안됨|안돼|안되|'
    r'미지급|미지원|미수급|미수령|(?:지급|지원|수급|수령)중단')
# Whitespace is already removed from fact clauses. In these payment nouns the
# middle characters of "현금 + 지급" spell "금지", but do not prohibit anything.
# Exempt only that exact overlapping match, never a later "금지/불가/않음".
_PAYMENT_PHRASE_RE = re.compile(r'(?:현금|지원금|보조금|장려금|환급금|급여금|수당금)지급')


def _has_dependent_predicate(clause):
    """Recognize restrictions in a normalized clause without cash-payment noise."""
    payments = [(match.start(), match.end()) for match in _PAYMENT_PHRASE_RE.finditer(clause)]
    for match in _DEPENDENT_PREDICATE_RE.finditer(clause):
        if match.group() == '금지' and any(
                left <= match.start() and match.end() <= right for left, right in payments):
            continue
        return True
    return False


def _fact_clauses(text):
    """Literal fact comparison with narrow, exact presentation equivalences.

    This deliberately does not guess Korean paraphrase entailment. Unsupported
    paraphrases remain a review/extraction failure, not an approved fact.
    """
    clauses = []
    for raw in presentation_clauses(text):
        raw = raw.strip(' \t-•·#*✅📋📍💰📅📝')
        if not raw or re.sub(r'\s+', '', raw.rstrip(':：')) in {
                re.sub(r'\s+', '', label) for label in _DISPLAY_HEADERS}:
            continue
        def quantity(match):
            number, unit = match.groups()
            canonical, multiplier = _EXACT_UNITS.get(unit, (unit, 1))
            value = Decimal(number) * multiplier
            return format(value.normalize(), 'f') + canonical
        value = _QUANTITY_RE.sub(quantity, raw.replace(',', ''))
        value = value.replace('감면', '할인')
        value = re.sub(r'[\s:*#•·✅📋📍💰📅📝]+', '', value)
        if value:
            clauses.append(value)
    return clauses


def preserves_quoted_facts(value, quote):
    """Allow formatting, sentence order and exact units; preserve actual words."""
    value_clauses, quote_clauses = _fact_clauses(value), _fact_clauses(quote)
    # A detached predicate belongs to its neighbouring subject. Moving "아님"
    # after another subject must not pass merely because the words still match.
    # Korean compound predicates (신청할 수 없음, 지급하지 않음, 안 됨)
    # are not reliably identified by their first word. If any restriction or
    # negative phrase occurs, require source clause order rather than guessing
    # which neighbouring clause it qualifies.
    if any(_has_dependent_predicate(clause) for clause in (*value_clauses, *quote_clauses)):
        return value_clauses == quote_clauses
    return Counter(value_clauses) == Counter(quote_clauses)


def _compact(value):
    return re.sub(r'\s+', '', value)


def quote_matches_source(quote, content):
    """Verify the model quote against an actual, boundary-preserving source span.

    Whitespace-insensitive containment alone allows the model to move a negative
    predicate to another subject inside its quote. Locate the actual original
    characters, then require the same narrow presentation contract used for the
    displayed prose. Never trust a model-supplied quote as a new source.
    """
    if not isinstance(quote, str) or not isinstance(content, str):
        return False
    needle = _compact(quote)
    if not needle:
        return False
    positions = [index for index, char in enumerate(content) if not char.isspace()]
    for match in re.finditer(re.escape(needle), _compact(content)):
        original = content[positions[match.start()]:positions[match.end() - 1] + 1]
        if preserves_quoted_facts(quote, original):
            return True
    return False


def _quantities(text):
    """Keep units with numbers; 50원 is not interchangeable with 50%."""
    text = text.replace(',', '')
    quantities = set()
    for number, unit in _QUANTITY_RE.findall(text):
        canonical, multiplier = _EXACT_UNITS.get(unit, (unit, 1))
        quantities.add((Decimal(number) * multiplier, canonical))
    return quantities


def _bare_numbers(text):
    # Amounts are compared with exact unit conversions separately. Keep the
    # conservative check for identifiers and other numbers without known units.
    remainder = _QUANTITY_RE.sub('', text.replace(',', ''))
    return set(re.findall(r'\d+(?:\.\d+)?', remainder))


def _labelled_quantities(text):
    """Only explicit sentence/line labels; no fuzzy inference of beneficiaries.

    Duplicate labels or prose prefixes are ambiguous and are not treated as a
    complete mapping. This is an additional rejection check, not approval.
    """
    mapping = {}
    for clause in presentation_clauses(text):
        match = re.match(r'^\s*[-•·]?\s*([가-힣A-Za-z][가-힣A-Za-z /·_-]{1,39})\s*:?\s*(?=\d)', clause)
        if not match:
            continue
        label = _compact(match.group(1))
        quantities = _quantities(clause[match.end():])
        if not quantities:
            continue
        if label in mapping:
            return {}
        mapping[label] = quantities
    return mapping


def _policy_recipient_source(content):
    """Mask narrowly identified non-recipient roles for structured evidence only.

    Keep every physical line boundary. This is not a source rewrite, selection
    for AI, or a statement that masked conditions can be dropped from the policy.
    Exclusions, staff qualifications and document examples must stay in prose,
    but their incidental age/household names cannot prove a recipient tag.
    """
    reset = frozenset({
        '지원대상', '신청대상', '서비스대상', '서비스대상자', '수급대상', '대상',
        '신청자격', '지원자격', '선정기준', '자격기준', '대상자선정',
        '지원내용', '서비스지원내용', '서비스내용', '급여내용', '지원기간',
        '지원시간', '지원금액', '지원방법', '신청방법', '신청절차', '신청기간',
        '접수기간', '지원절차', '서비스비용', '서비스가격', '기본정보',
        '사업개요', '사업목적', '추진배경', '문의', '문의처', '담당기관',
        '담당부서', '연락처',
    })
    nonrecipient = frozenset({
        '제공기관', '제공인력', '서비스제공기관', '서비스제공인력',
        '서비스제공기관및제공인력', '제공기관및제공인력', '제공인력자격',
        '제공인력자격기준', '수행기관', '사업수행기관', '종사자자격',
        '제출서류', '구비서류', '필수서류', '증빙서류', '신청서류',
        '준비서류', '참고자료', '관련자료', '자료예시', '소득확인자료',
        '소득증빙자료', '추진근거', '법적근거', '관련법령', '근거법령',
    })
    recipient_headings = {'지원대상', '신청대상', '서비스대상', '서비스대상자',
                          '수급대상', '대상', '신청자격', '지원자격', '선정기준', '자격기준', '대상자선정'}
    output, target_clauses, blocked, target_section = [], set(), False, False
    for raw in content.splitlines():
        cleaned = re.sub(r'^\s*(?:#{1,6}\s*|[-•·○●□■▶▷◈※]\s*)*', '', raw).strip()
        cleaned = re.sub(r'^(?:\d{1,2}[.)]|[가-하][.)]|[①-⑳])\s*', '', cleaned)
        lead = re.split(r'[:：]', cleaned, maxsplit=1)[0]
        label = re.sub(r'[\s·ㆍ]', '', lead)
        label = re.sub(r'\([^()]{0,40}\)$', '', label)
        if (label in nonrecipient or re.fullmatch(r'.{0,40}(?:제외대상|제외조건|지원제외|대상제외)', label)
                or label in {'지원제외', '지원불가', '신청불가', '중복지원제한'}):
            blocked, target_section = True, False
        elif label in reset and not (blocked and label in recipient_headings):
            # A bare target/qualification label can be a table subheading under
            # an exclusion/staff/documents parent. Flattened text cannot prove
            # that it begins a new affirmative section, so keep that hold until
            # an independent content/overview/contact boundary is explicit.
            blocked = False
            target_section = label in recipient_headings
        compact = re.sub(r'[\s·ㆍ]', '', cleaned)
        role_line = re.match(
            r'^(?:제공인력|서비스제공인력|제공기관|수행기관|사업수행기관|종사자)'
            r'(?:은|는|이|가|의|자격|요건|기준|[:：])', compact)
        example_line = re.match(
            r'^(?:(?:자산)?소득조사(?:대상|자료)|자산소득조사(?:대상|자료)|'
            r'(?:제출|구비|증빙|참고)(?:서류|자료)|(?:서류|자료)예시)', compact)
        kept = '' if blocked or role_line or example_line else raw
        output.append(kept)
        if kept and target_section:
            target_clauses.update(value.strip() for value in re.split(r'\n|(?<!\d)[.!?;。](?!\d)', kept)
                                  if value.strip())
    return '\n'.join(output), target_clauses


def _structured_policy_source(content):
    """Prepare source-only evidence once; never cache it beyond the caller."""
    from services.news_analysis_safety import _age_evidence
    from services.news_fact_grounding import affirmative_quote_checker

    source_affirmative = affirmative_quote_checker(content)

    clauses = [value.strip() for value in re.split(r'\n|(?<!\d)[.!?;。](?!\d)', content)
               if value.strip()]
    affirmative = [clause for clause in clauses
                   if source_affirmative(clause) is not None]
    # A nationwide tag cannot encode excluded districts. A positive nationwide
    # sentence elsewhere must not erase a geographically restricted exception.
    geographic_exception = any(
        source_affirmative(clause) is None
        and re.search(r'거주|구민|시민|주민|지역|(?<![가-힣])[가-힣]{2,}(?:시|군|구|도)(?:\s|[은는을를])', clause)
        for clause in clauses)
    recipient_source, recipient_target_clauses = _policy_recipient_source(content)
    recipient_checker = (source_affirmative if recipient_source == content
                         else affirmative_quote_checker(recipient_source))
    recipient_clauses = [value.strip() for value in re.split(r'\n|(?<!\d)[.!?;。](?!\d)', recipient_source)
                         if value.strip()]
    recipient_affirmative = [clause for clause in recipient_clauses
                             if recipient_checker(clause) is not None]
    ages, intervals = _age_evidence(recipient_source)
    return (affirmative, geographic_exception, recipient_target_clauses,
            recipient_affirmative, ages, intervals)


def _structured_policy_checker(content):
    """Request-local option screening; no global/disk cache or shared mutation.

    The content is captured with its own derived evidence. A different source
    always gets a new checker. Final response validation calls the public guard
    again, so no request-time preparation becomes an approval credential.
    """
    source = _structured_policy_source(content)
    return lambda result: _structured_policy_issues(result, content, source)


def structured_policy_issues(result, content):
    """Find unsupported eligibility facts without changing the proposed values.

    Empty values remain unknown, not deletion instructions. This deliberately
    holds ambiguous labels instead of inferring eligibility from a publisher or
    visit address. Existing news guards are reused only at call time so their
    shared prose helper does not introduce a module-import cycle.
    """
    return _structured_policy_checker(content)(result)


def _structured_policy_issues(result, content, source):
    from services.news_analysis_safety import _tag_supported, _TRAIT_ALIASES
    from services.news_region_rules import residence_quote_matches
    from services.news_profile_taxonomy import household_evidence_aliases

    affirmative, geographic_exception, recipient_target_clauses, recipient_affirmative, ages, intervals = source
    issues = []
    # A bound that occurs in one eligibility branch is not necessarily the
    # programme-wide bound. The shared parser merges overlapping intervals;
    # that must not turn a 5-year exception into an 18-year programme ceiling
    # (or lose the exception by approving the wider number). Different lower
    # or upper candidates require review. Repeated equivalent year/month
    # expressions remain a single bound. Do not repair values or request AI.
    ambiguous_age_bounds = any(len({value for field, value in ages if field == key}) > 1
                              for key in ('start_age_months', 'end_age_months'))
    for key in ('start_age_months', 'end_age_months'):
        value = result.get(key)
        if value is not None and (type(value) is not int or not 0 <= value <= 1800
                or ambiguous_age_bounds or len(intervals) > 1 or (key, value) not in ages):
            issues.append(key)
    start, end = result.get('start_age_months'), result.get('end_age_months')
    if type(start) is int and type(end) is int and start > end:
        issues.extend(('start_age_months', 'end_age_months'))
    # Overlapping child-stage labels describe recipients within a verified
    # interval, not permission to expand it to the whole labelled stage.
    # Require both numeric fields to reproduce the one complete source range;
    # proxy/exclusion/disjoint-range checks in _age_evidence remain authoritative.
    exact_child_range = (len(intervals) == 1 and type(start) is int and type(end) is int
        and intervals[0] == (start, end)
        and not {'start_age_months', 'end_age_months'}.intersection(issues))
    child_bands = {'영아': (0, 35), '유아': (36, 83), '학령기': (84, 227)}

    lifecycle_aliases = {
        '임신/출산': ('임신', '임산부', '출산'), '임신·출산': ('임신', '임산부', '출산'),
        '영아': ('영아', '영유아'), '유아': ('유아', '영유아'), '영유아': ('영유아', '영아', '유아'),
        '학령기': ('학령기', '초등학생'), '아동': ('아동', '학령기', '초등학생'),
        '청소년': ('청소년', '중학생', '고등학생'), '청년': ('청년',),
        '중장년': ('중장년', '중년', '장년'), '노년': ('노년', '노인', '어르신', '고령자'),
        '전생애': ('전생애', '전연령', '모든연령', '연령제한없', '나이제한없'),
    }
    recipient = re.compile(r'대상|자격|지원|신청|가구|주민|제공|에게|위한')

    def recipient_tag_occurs(terms, *, lifecycle=False):
        for clause in recipient_affirmative:
            if not recipient.search(clause) and clause not in recipient_target_clauses:
                continue
            if not lifecycle and re.search(r'(?<![가-힣])(?:보호자|부모|대리인|신청자)', clause):
                continue  # mixed applicant/recipient traits need individual review
            if lifecycle and re.search(r'보호자|부모|대리인|신청자', clause) and re.search(r'아동|영유아|어린이|자녀', clause):
                continue  # mixed recipient/proxy evidence requires a person
            text = _compact(clause)
            for term in terms:
                for match in re.finditer(r'(?<![비非])' + re.escape(_compact(term)), text):
                    if re.match(r'(?:복지|기본|보호|지원|교육|고용|장기요양보험){0,3}'
                                r'(?:법(?:률)?|법령|조례|시행령|시행규칙)', text[match.end():]):
                        continue  # a cited law's name does not identify recipients
                    if (re.match(r'(?:인|인경우의)?(?:보호자|부모|대리인|신청자)', text[match.end():])
                            or re.search(r'(?:보호자|부모|대리인|신청자)(?:는|가|인)?$', text[:match.start()])):
                        continue  # the applicant's trait is not the beneficiary's
                    # A named welfare centre is the publisher/provider, not
                    # evidence that everybody in its name can receive this aid.
                    if re.match(r'(?:가족)?(?:복지|지원|상담|종합|건강|행정|사업|교육|정책){0,3}'
                                r'(?:센터|관|재단|협회|기관|위원회|담당|과)', text[match.end():]):
                        continue
                    return True
        return False

    for key in ('residence', 'life_cycle', 'target_characteristics'):
        values = result.get(key)
        if values is None or values == []:
            continue
        if (not isinstance(values, list) or len(values) > 100
                or any(not isinstance(value, str) or not value.strip() or len(value) > 80 for value in values)):
            issues.append(key)
            continue
        for value in values:
            if key == 'residence':
                if _compact(value) in {'전국', '거주무관', '제한없음'}:
                    supported = not geographic_exception and any(re.search(
                        r'전국(?:민|대상|지원대상|(?:에)?거주|어디(?:서|에서)?나)'
                        r'|거주무관|거주지제한없|지역제한없|거주지(?:와)?관계없', _compact(clause))
                        for clause in affirmative)
                else:
                    supported = any(residence_quote_matches(value, clause) for clause in affirmative)
            elif key == 'target_characteristics':
                # 일반 is an existing nonrestrictive classification marker, not
                # evidence of unrestricted ages, residence or income.
                terms = (*_TRAIT_ALIASES.get(value, (value,)), *household_evidence_aliases(value))
                supported = (_tag_supported(value, content, key) and
                             (value == '일반' or recipient_tag_occurs(terms)))
            else:
                terms = lifecycle_aliases.get(value, (value,))
                supported = recipient_tag_occurs(terms, lifecycle=True)
                # A precise recipient range may establish these narrow stages;
                # it can never establish 전생애 or a guessed adult age band.
                if not supported and len(intervals) == 1:
                    lower, upper = intervals[0]
                    supported = (value == '노년' and lower >= 780 or
                        value == '영아' and upper is not None and upper <= 35 or
                        value == '유아' and lower >= 36 and upper is not None and upper <= 83 or
                        value == '학령기' and lower >= 84 and upper is not None and upper <= 227)
                if not supported and exact_child_range and value in child_bands:
                    lower, upper = child_bands[value]
                    supported = max(start, lower) <= min(end, upper)
                # A literal tag cannot override a contradictory verified age
                # range. Reuse only age bands already defined by this guard;
                # never infer new adult bounds or replace the proposed tags.
                if supported and len(intervals) == 1:
                    lower, upper = intervals[0]
                    band = child_bands.get(value, (780, None) if value == '노년' else None)
                    if band is not None and (upper is not None and upper < band[0]
                            or band[1] is not None and lower > band[1]):
                        supported = False
                    if value == '전생애' and (lower > 0 or upper is not None):
                        supported = False
            if not supported:
                issues.append(key)
                break
    contact = result.get('contact')
    if contact not in (None, '') and (not isinstance(contact, str) or not contact.strip()
            or _compact(contact) not in _compact(content)):
        issues.append('contact')
    return sorted(set(issues))


def diagnose_policy_evidence(result, content, *, draft_income_review=False):
    """Explain conservative holds without changing the boolean approval contract.

    Diagnostics contain only code-owned field names, reason codes and bounded
    source *presentation-clause indexes*. Never copy source/model text into a
    log or status payload. Condition candidates are literal coverage warnings,
    not a semantic count of missing requirements: table captions can occur too.
    """
    diagnostic = {'diagnostic_version': POLICY_DIAGNOSTIC_VERSION,
                  'valid': False, 'issues': [], 'fields': [],
                  'condition_candidates': [], 'condition_candidate_count': 0,
                  'condition_candidates_truncated': False}

    def issue(code, field=None):
        item = {'code': code}
        if field is not None:
            item['field'] = field
            if field not in diagnostic['fields']:
                diagnostic['fields'].append(field)
        if item not in diagnostic['issues'] and len(diagnostic['issues']) < _DIAGNOSTIC_LIMIT:
            diagnostic['issues'].append(item)

    if not isinstance(result, dict) or not isinstance(content, str) or not content.strip():
        issue('invalid_input')
        return diagnostic
    # Recheck saved/cache responses too; matching source bytes alone does not
    # resolve an ambiguous admission threshold or a calendar-relative age.
    from services.policy_source_review import source_criteria_issues
    source_review = source_criteria_issues(content)
    # Only a new-policy preview may defer income-scope interpretation to a
    # human. All quoted facts and source conditions are still checked below.
    # Saved proposals/apply validators never enable this option.
    if draft_income_review is True:
        source_review = [item for item in source_review
                         if item['code'] != 'source_income_criteria_review']
    if source_review:
        for item in source_review:
            issue(item['code'], item['field'])
        return diagnostic
    evidence = result.get('_source_evidence', {})
    if not isinstance(evidence, dict):
        issue('invalid_evidence_mapping')
        diagnostic['fields'] = [key for key in POLICY_PROSE_FIELDS if result.get(key) not in (None, '')]
        return diagnostic
    unit_fields = set()
    if '_source_unit_selection' in result:
        from services.policy_source_units import verified_unit_fields
        unit_fields = verified_unit_fields(result, content)
        if unit_fields is None:
            issue('source_unit_evidence_invalid')
            diagnostic['fields'] = list(POLICY_PROSE_FIELDS)
            return diagnostic
    unsupported = structured_policy_issues(result, content)
    if unsupported:
        for key in unsupported:
            issue('structured_fact_unsupported', key)
        return diagnostic
    # Prose fields carry the largest risk of importing another benefit.
    # A missing quote is a failed analysis, never evidence that a field was deleted.
    supported_fields = 0
    for key in POLICY_PROSE_FIELDS:
        value = result.get(key)
        if value is None or value == '':
            continue
        if not isinstance(value, str) or value.strip().casefold() in {'', 'none', 'null', 'undefined', 'nan', '-', '(없음)'}:
            issue('invalid_prose_value', key)
            continue
        # Exact legacy instruction bullets only; never strip real requirements.
        # Reject rather than silently editing the proposed policy text.
        source_lines = {line.strip().lstrip('-• ').strip() for line in content.splitlines()}
        placeholder = False
        for line in value.splitlines():
            normalized = line.strip().lstrip('-• ').strip()
            if normalized in _TEMPLATE_LINES and normalized not in source_lines:
                issue('instruction_placeholder', key)
                placeholder = True
        if placeholder:
            continue
        quote = evidence.get(key)
        if not isinstance(quote, str) or not quote.strip():
            issue('source_quote_missing', key)
            continue
        if len(quote.strip()) < 4:
            issue('source_quote_insufficient', key)
            continue
        if key not in unit_fields and not quote_matches_source(quote, content):
            issue('source_quote_boundary_changed' if _compact(quote) in _compact(content)
                  else 'source_quote_not_found', key)
            continue
        field_failed = False
        if not preserves_quoted_facts(value, quote):
            issue('presentation_not_preserved', key)
            field_failed = True
        # Number spelling may vary (1,000 vs 1000), but new numbers require evidence.
        if not _bare_numbers(value) <= _bare_numbers(quote):
            issue('number_not_supported', key)
            field_failed = True
        # Both directions: do not silently omit an evidenced discount/time/age,
        # or reuse the same number with a different unit. This is deliberately
        # conservative: only money and duration use exact unit conversions.
        if _quantities(value) != _quantities(quote):
            issue('quantity_unit_changed', key)
            field_failed = True
        labelled = _labelled_quantities(quote)
        if len(labelled) >= 2 and labelled != _labelled_quantities(value):
            issue('label_quantity_changed', key)
            field_failed = True
        if not field_failed:
            supported_fields += 1
    if diagnostic['issues']:
        return diagnostic
    # An empty object or tags alone cannot establish what a policy provides.
    # Return extraction failure, not an apparently valid deletion baseline.
    if not supported_fields:
        issue('no_supported_prose')
        diagnostic['fields'] = list(POLICY_PROSE_FIELDS)
        return diagnostic
    # A short real quote is insufficient when another sentence in the selected
    # policy section restricts eligibility/application. Require those conditions
    # in the extracted prose, even if the model omitted them from its quote.
    values = [clause for key in POLICY_PROSE_FIELDS
              for clause in _fact_clauses(result.get(key))]
    from services.policy_source_coverage import contact_destination_lines, contact_condition_clauses
    contact_destinations = contact_destination_lines(content)
    contact_conditions = contact_condition_clauses(content)
    contact = result.get('contact')
    exact_contacts = {_compact(clause).strip('.。') for clause in presentation_clauses(contact)} \
        if isinstance(contact, str) else set()
    for index, raw in enumerate(presentation_clauses(content)):
        # Exact standalone page-navigation labels do not impose policy terms.
        # Do not infer boundaries from arbitrary plain-text policy titles or
        # strip actual instructions such as "방문 신청 필요".
        if _compact(raw.strip(' \t-•·#*')) in _NAVIGATION_LABELS:
            continue
        if contact_destinations.get(_compact(raw).strip('.。')) in exact_contacts:
            # Only the complete, pure enquiry destination may live in contact.
            # Eligibility/application/document clauses never enter this map.
            continue
        clauses = _fact_clauses(raw)
        # A real prohibition can follow its subject ("현금 지급 금지"). It is
        # still binding when the model selected a shorter otherwise true quote.
        attached_restriction = (re.search(r'지급|지원|수급|수령|신청|접수', raw)
                                and any(_has_dependent_predicate(clause) for clause in clauses))
        contact_kind = contact_conditions.get(_compact(raw).strip('.。'))
        if not _CONDITION_RE.search(raw) and not attached_restriction and not contact_kind:
            continue
        if any(clause not in values for clause in clauses):
            diagnostic['condition_candidate_count'] += 1
            if len(diagnostic['condition_candidates']) < _DIAGNOSTIC_LIMIT:
                diagnostic['condition_candidates'].append({
                    'clause_index': index,
                    'kind': 'application' if contact_kind == 'application' or re.search(r'신청|접수', raw)
                            else 'eligibility_or_restriction',
                })
    if diagnostic['condition_candidate_count']:
        issue('source_condition_not_preserved')
        # These are the review scope, not an assertion that every field omitted
        # a separate condition. The original contract allows coverage anywhere
        # across these four prose fields.
        diagnostic['fields'] = list(POLICY_PROSE_FIELDS)
        diagnostic['condition_candidates_truncated'] = (
            diagnostic['condition_candidate_count'] > len(diagnostic['condition_candidates']))
        return diagnostic
    diagnostic['valid'] = True
    return diagnostic


def validate_policy_evidence(result, content):
    """Backward-compatible approval boolean; diagnostics never grant approval."""
    return diagnose_policy_evidence(result, content)['valid']


def public_policy_diagnostics(diagnostic):
    """Allowlist code/index metadata for a public review status; no arbitrary text."""
    if not isinstance(diagnostic, dict):
        return {}
    allowed_fields = set(POLICY_PROSE_FIELDS) | {
        'residence', 'life_cycle', 'target_characteristics',
        'start_age_months', 'end_age_months', 'contact',
    }
    issues = []
    raw_issues = diagnostic.get('issues')
    for item in raw_issues[:_DIAGNOSTIC_LIMIT] if isinstance(raw_issues, list) else ():
        if (not isinstance(item, dict) or not isinstance(item.get('code'), str)
                or item['code'] not in POLICY_DIAGNOSTIC_CODES):
            continue
        safe = {'code': item['code']}
        if isinstance(item.get('field'), str) and item['field'] in allowed_fields:
            safe['field'] = item['field']
        issues.append(safe)
    candidates = []
    raw_candidates = diagnostic.get('condition_candidates')
    for item in raw_candidates[:_DIAGNOSTIC_LIMIT] if isinstance(raw_candidates, list) else ():
        if not isinstance(item, dict):
            continue
        index, kind = item.get('clause_index'), item.get('kind')
        if type(index) is int and 0 <= index <= 100000 and isinstance(kind, str) and kind in POLICY_CONDITION_KINDS:
            candidates.append({'clause_index': index, 'kind': kind})
    count = diagnostic.get('condition_candidate_count')
    count = max(count, len(candidates)) if type(count) is int and 0 <= count <= 100000 else len(candidates)
    return {'diagnostic_version': POLICY_DIAGNOSTIC_VERSION,
            'issues': issues, 'condition_candidates': candidates,
            'condition_candidate_count': count,
            'condition_candidates_truncated': count > len(candidates)}
