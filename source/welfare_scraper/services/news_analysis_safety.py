"""Local evidence checks shared by fresh, cached and previously saved analyses.

Never calls a model or changes a saved user item. Unverified extracted values are
kept separately for review and cannot establish either matching or exclusion.
"""
import re
from services.news_fact_grounding import (
    compact, grounded_facts, scoped_affirmative_quote, source_quote_issue,
)
from services.news_target_rules import evaluate_target
from services.news_profile_taxonomy import household_evidence_aliases
from services.policy_evidence import preserves_quoted_facts, _fact_clauses
from services.news_field_presentation import verified_labeled_display

VERSION = 'news-facts-v6-whole-short-source'
BOUNDED_VERSION = 'news-facts-v7-bounded-source-negative-evidence'
COMPLETE_VERSION = 'news-facts-v8-counted-complete-source'
_LABELS = {'start_age_months': '시작 나이', 'end_age_months': '종료 나이',
           'age_ranges': '분리된 나이 구간',
           'life_cycle': '생애주기', 'residence': '거주 조건',
           'target_characteristics': '가구상황', 'interest_topics': '관심주제',
           'support_detail': '지원 내용', 'eligibility_detail': '신청 자격',
           'contact': '연락처', 'deadline': '마감일', 'is_relevant': '소식 관련성',
           'omitted_conditions': '확인 필요한 제외·신청 조건'}
_TRAIT_ALIASES = {
    '장애인': ('장애인', '장애/발달지연'), '장애/발달지연': ('장애인', '발달지연'),
    '한부모·조손': ('한부모', '조손'), '다문화·탈북민': ('다문화', '탈북민', '북한이탈주민'),
    '탈북민': ('탈북민', '북한이탈주민'), '보훈': ('보훈',),
    '독거': ('독거', '1인가구'), '1인 가구': ('1인가구', '독거'),
}


def _clauses(content):
    return [s.strip() for s in re.split(r'\n|(?<!\d)[.!?;。](?!\d)', content) if s.strip()]


def _tag_supported(tag, content, field):
    if field == 'target_characteristics' and tag == '일반':
        return True  # nonrestrictive common tag, not proof of a specific household
    terms = _TRAIT_ALIASES.get(tag, (compact(tag),))
    if field == 'target_characteristics':
        terms = tuple(dict.fromkeys((*terms, *household_evidence_aliases(tag))))
    for clause in _clauses(content):
        text = compact(clause)
        if not any(re.search(r'(?<![비非])' + re.escape(compact(term)), text) for term in terms):
            continue
        if field == 'target_characteristics' and not re.search(r'대상|자격|가구|지원|신청|주민|제공', text):
            continue
        if scoped_affirmative_quote(clause, content) is not None:
            return True
    return False


def _age_evidence(content):
    """Explicit recipient ages, with paired intervals instead of loose endpoints.

    The stored schema represents one inclusive interval. Keep distinct source
    intervals so an ineligible gap cannot be silently filled by their endpoints.
    This is a local evidence guard, not an inference of ages from audience tags.
    """
    bounds = set()
    intervals = []
    for clause in _clauses(content):
        if not re.search(r'대상|자격|연령|나이|생후|만\s*\d|\d\s*(?:세|살)', clause):
            continue
        if re.search(r'기간|근무|납부', clause) and not re.search(r'연령|나이|생후|만\s*\d', clause):
            continue
        # An applicant/guardian's age is not the recipient's age. A programme
        # explicitly naming parents as recipients may still establish an age;
        # mixed child/guardian clauses remain ambiguous and must be reviewed.
        proxy = r'보호자|부모|대리인|신청자|신청인'
        if re.search(proxy, clause):
            direct_recipient = re.search(
                r'(?:보호자|부모)\s*(?:가|는)?\s*(?:지원\s*)?대상|'
                r'(?:지원\s*)?대상\s*(?:은|자)?\s*[:：]?[^.\n]*(?:보호자|부모)', clause)
            if not direct_recipient or re.search(r'아동|영유아|어린이|자녀', clause):
                continue
        if scoped_affirmative_quote(clause, content) is None:
            continue
        for branch in re.split(r'또는|혹은|이거나', clause):
            ranges = list(re.finditer(r'(?:만|생후)?\s*(\d+)\s*(개월|세|살)?\s*(?:~|∼|–|-|부터)\s*'
                                     r'(?:만|생후)?\s*(\d+)\s*(개월|세|살)', branch))
            for match in ranges:
                first, first_unit, last, last_unit = match.groups()
                unit = first_unit or last_unit
                lower = int(first) * (1 if unit == '개월' else 12)
                upper = int(last) if last_unit == '개월' else int(last) * 12 + 11
                bounds.update((('start_age_months', lower), ('end_age_months', upper)))
                intervals.append((lower, upper))
            rest = branch
            for match in reversed(ranges):
                rest = rest[:match.start()] + rest[match.end():]
            lowers, uppers = [], []
            for number, unit, relation in re.findall(r'(\d+)\s*(개월|세|살)\s*(이상|이하|초과|미만|부터|까지)', rest):
                value = int(number) * (1 if unit == '개월' else 12)
                if relation in ('이상', '부터', '초과'):
                    lower = value + ((1 if unit == '개월' else 12) if relation == '초과' else 0)
                    bounds.add(('start_age_months', lower))
                    lowers.append(lower)
                else:
                    upper = value - 1 if relation == '미만' else value + (0 if unit == '개월' else 11)
                    bounds.add(('end_age_months', upper))
                    uppers.append(upper)
            if lowers or uppers:
                intervals.append((max(lowers, default=0), min(uppers) if uppers else None))
    # Repeated, overlapping and adjacent ranges still fit the single-interval
    # schema. Only an actual gap requires a hold; None means no upper bound.
    merged = []
    for lower, upper in sorted(set(intervals), key=lambda item: (item[0], float('inf') if item[1] is None else item[1])):
        if merged and (merged[-1][1] is None or lower <= merged[-1][1] + 1):
            previous = merged[-1][1]
            merged[-1] = (merged[-1][0], None if previous is None or upper is None else max(previous, upper))
        else:
            merged.append((lower, upper))
    return bounds, merged


def _omitted_conditions(result, content):
    """Demand explicit restrictive/application conditions, not navigation labels.

    Keeping the missing source text for review is preferable to inventing a
    repaired summary or quietly treating the programme as unrestricted.
    """
    retained = {clause for key in ('support_detail', 'eligibility_detail')
                for clause in _fact_clauses(result.get(key))}
    restrictive = re.compile(
        r'제외|불가|중복|필수|^\s*(?:아님|아닌|아니|아닙|않|금지|없|안\s*(?:됨|돼|되))|'
        r'신청할\s*수\s*없|지원받을\s*수\s*없|'
        r'미지급|미지원|미수급|미수령|(?:지급|지원|수급|수령)\s*중단|'
        r'(?:지급|지원|수급|수령)\s*(?:을\s*)?(?:하지|되지|받지)\s*않|'
        r'(?:지급|지원|수급|수령)\s*(?:받을|할|될)\s*수\s*없|'
        r'(?:신청|접수)[^.\n]*(?:필요|해야)|(?:방문|온라인|우편|팩스)\s*(?:으로만\s*)?신청|'
        r'(?:보호자|부모|대리인|신청자|신청인)[^.\n]*(?:이상|이하|미만|초과|이어야|필요)')
    missing = []
    from services.fact_presentation import presentation_clauses
    from services.policy_evidence import _has_dependent_predicate
    for clause in presentation_clauses(content):
        payment_restriction = (re.search(r'지급|지원|수급|수령|신청|접수', clause)
                               and any(_has_dependent_predicate(value) for value in _fact_clauses(clause)))
        if (restrictive.search(clause) or payment_restriction) and any(
                value not in retained for value in _fact_clauses(clause)):
            missing.append(clause.strip())
    return missing


def _date_supported(value, content):
    from services.news_deadline_grounding import deadline_supported
    return deadline_supported(value, content)


def _condition_diagnostics(original, result, missing, *, auxiliary=None, content=''):
    """Describe a hold's origin, never weaken the condition approval guard.

    Literal text appearing in a one-line model response may still fail the
    boundary-preserving display contract. Report that as validation withholding,
    not as proof that the model omitted the words. Absence is an unconfirmed
    coverage finding, not semantic proof that a paraphrase omitted a condition.
    """
    diagnostics = []
    for condition in missing:
        needle = ''.join(_fact_clauses(condition))
        fields = [key for key in ('support_detail', 'eligibility_detail')
                  if needle and needle in ''.join(_fact_clauses(original.get(key)))]
        origin = ('validation_withheld' if any(original.get(key) != result.get(key) for key in fields)
                  else 'presentation_coverage_unconfirmed' if fields
                  else 'model_output_unconfirmed')
        if not fields:
            from services.news_review_evidence import auxiliary_condition_fields
            auxiliary_fields = auxiliary_condition_fields(auxiliary, condition, content)
            if auxiliary_fields:
                origin, fields = 'auxiliary_evidence_only', auxiliary_fields
        diagnostics.append(dict(condition=condition, origin=origin, fields=fields))
    return diagnostics


def applicant_status(result, content):
    role = result.get('applicant_type')
    if role not in ('individual', 'proxy', 'organization', 'mixed', 'unknown'):
        return 'review'
    quote = result.get('applicant_evidence')
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 2000:
        return 'review'
    evidence = scoped_affirmative_quote(quote, content)
    if evidence is None:
        return 'review'
    text = compact(evidence)
    if not re.search(r'신청|접수|모집|공모|참여', text):
        return 'review'
    from services.news_applicant_context import school_group_application_context
    if school_group_application_context(content):
        # Personal authentication by a teacher is not proof of an individual
        # route. Preserve class-only and mixed routes for review, never exclude.
        return 'review'
    organization = bool(re.search(r'기관|단체|업체|매장|사업자|법인|운영센터|업소|영업자|가맹점|점포', text))
    individual = bool(re.search(r'개인|본인|시민|주민|가구|보호자|부모|가족|아동|청년|노인|노년', text))
    if role == 'organization':
        return 'excluded' if organization and not individual else 'review'
    if role in ('individual', 'proxy'):
        return 'match' if individual and not organization else 'review'
    return 'review'


def _confirmed_login_notice(result, content):
    """Narrow negative evidence, not a general administrative classifier.

    A title or an incidental login requirement cannot exclude a programme.
    Any actionable wording anywhere in the bounded body keeps it for review.
    """
    if result.get('is_relevant') is not False:
        return False
    evidence = result.get('fact_evidence')
    quote = evidence.get('is_relevant') if isinstance(evidence, dict) else None
    if (not isinstance(quote, str) or not quote.strip()
            or source_quote_issue(quote, content)
            or scoped_affirmative_quote(quote, content) is None):
        return False
    if re.search(r'모집|신청|접수|참가|참여|교육|강좌|강연|행사|체험|프로그램|'
                 r'지원금|보조금|바우처|지급|수당|수강|장학|무료|유료|'
                 r'예약|입장|증정|기부|보육|돌봄|상담|\d\s*(?:만원|원|명)|'
                 r'지원\s*(?:대상|내용|자격|사업)', content):
        return False
    if re.search(r'이전\s*(?:의\s*)?지시|시스템\s*프롬프트|is_relevant|_support_source_units|'
                 r'ignore[^\n]{0,80}instructions|\b(?:assistant|system|developer)\b', content, re.I):
        return False
    # Absence of recruitment keywords cannot prove an entire notice unrelated.
    # Accept only complete lines in a deliberately small account-operation
    # grammar. Unrecognized service benefits, metadata, institution prose and
    # even otherwise valid login notices stay review; no URL/title whitelist.
    auth = r'(?:간편인증|모바일신분증|민간ID|아이디/비밀번호|아이디와비밀번호)'
    patterns = (
        r'(?:통합)?(?:로그인|인증)(?:방식|방법)?(?:변경|개선|추가)안내',
        r'(?:(?:안전한|편리한)서비스이용을위해)?(?:로그인|인증)(?:방식|방법|환경)?'
        r'(?:이|가)?(?:개선|변경|적용|도입|추가|전환)(?:됩니다|합니다|될예정입니다)',
        r'(?:기존)?(?:아이디와비밀번호|아이디/비밀번호|아이디|비밀번호)로그인'
        r'(?:도|은|을)?(?:계속)?(?:이용|사용)할수있습니다',
        auth + r'(?:(?:과|와|,|·)' + auth + r')+(?:중|중에서)선택할수있습니다',
        auth + r'(?:이|가|을|를)?(?:추가|도입|적용)(?:됩니다|합니다)',
    )
    lines = [compact(line).strip('.。') for line in content.splitlines() if line.strip()]
    if not lines or any(not any(re.fullmatch(pattern, line) for pattern in patterns) for line in lines):
        return False
    # Require a declarative body sentence, not merely '로그인 개선 안내'.
    text = compact(quote)
    return bool(re.search(r'(?:로그인|인증)[^.!?。]{0,100}'
                          r'(?:개선|변경|적용|도입|추가|전환)[^.!?。]{0,35}'
                          r'(?:됩니다|합니다|예정입니다)', text))


def assess(profile, analysis, content, *, require_source_selection=False,
           whole_document_rules=False, source_payload=None, complete_document_rules=False):
    """Return a new code-owned decision; leave caller/user data untouched."""
    result = dict(analysis)
    original = {key: result.get(key) for key in _LABELS}
    # Previously withheld text is only used to diagnose a repeated preview.
    # Never put it back into the approved fields or trust stored diagnostics.
    prior = analysis.get('unverified_facts')
    diagnostic_original = dict(original)
    if isinstance(prior, dict):
        for key in ('support_detail', 'eligibility_detail', 'contact'):
            if not diagnostic_original.get(key) and isinstance(prior.get(key), str):
                diagnostic_original[key] = prior[key]
    issues = []
    unverified = {}
    field_holds = {}
    from services.news_source_selection import selection_issue, source_review_issue, eligible_source
    scope_hold = ''
    if whole_document_rules:
        scope_hold = source_review_issue(source_payload, complete_document=complete_document_rules)
        if not isinstance(source_payload, dict) or source_payload.get('content') != content:
            scope_hold = 'source_scope_unconfirmed'
        # Recompute transport/page flags from captured input, not a model or
        # cached analysis that supplied its own 'complete' value.
        from services import page_coverage, news_ai_input
        _, result['source_input'] = news_ai_input.prepare(content, 30000 if complete_document_rules else 6000, strategy='balanced')
        supplied_input = source_payload.get('source_input') if isinstance(source_payload, dict) else None
        if isinstance(supplied_input, dict) and supplied_input.get('status') != 'complete':
            result['source_input']['status'] = 'partial'
            result['source_input']['note'] = '수집 입력의 원문 전체 확보 여부를 확인하지 못했습니다.'
        prior_input = analysis.get('source_input')
        if isinstance(prior_input, dict) and prior_input.get('contact_minimization'):
            from services import news_contact_input
            from services.ai_input_safety import AiInputReviewRequired
            try:
                _, contact_metadata = news_contact_input.prepare_content(content)
            except AiInputReviewRequired:
                contact_metadata = None
            # Revalidate persisted counts against the original; do not accept a
            # provider/cache-authored note or invent a past minimization event.
            if contact_metadata == prior_input['contact_minimization']:
                result['source_input'] = news_contact_input.add_coverage_metadata(
                    result['source_input'], contact_metadata)
        page = page_coverage.normalize(source_payload.get('source_page')) if isinstance(source_payload, dict) else {}
        result.pop('source_page', None)
        if page and not (scope_hold and page.get('status') == 'complete'):
            result['source_page'] = page
        if scope_hold:
            issues.append('원문 확보 범위가 불완전하거나 확인되지 않아 자동 판정하지 않습니다. 확보한 본문과 첨부 원문을 확인해주세요.')
    selection_hold = selection_issue(analysis, content, required=(require_source_selection or (
        whole_document_rules and eligible_source(content, extended=True, complete_document=complete_document_rules))),
                                     extended=whole_document_rules, complete_document=complete_document_rules)
    selection_meta = analysis.get('source_support_selection')
    split_review = complete_document_rules and isinstance(selection_meta, dict) and selection_meta.get('part_count', 1) != 1
    if split_review:
        issues.append('긴 원문 전체를 나누어 확인했습니다. 부분 간 대상·예외 조건은 원문과 함께 확인해주세요.')
    negative_notice = (whole_document_rules and not scope_hold
                       and selection_hold in ('', 'source_selection_unconfirmed')
                       and _confirmed_login_notice(result, content))
    if selection_hold and not (negative_notice and selection_hold == 'source_selection_unconfirmed'):
        field_holds['support_detail'] = selection_hold
        # Never reconstruct a previously withheld/saved field. Keep the reason
        # even for []/missing provenance, which has no rejected prose to retain.
        if result.get('support_detail'):
            unverified['support_detail'] = result['support_detail']
        result['support_detail'] = ''
        issues.append('본문 전체의 원문 선택을 확인하지 못해 지원 내용을 보류했습니다. 원문과 비교해주세요.')
    try:
        result, note = grounded_facts(result, content, scoped_context=True)
    except (ValueError, KeyError, TypeError):
        result['life_cycle'], result['residence'] = [], []
        note = '생애주기·거주 조건의 원문 근거를 확인하지 못했습니다.'
    if note:
        issues.append(note)
        for key in ('life_cycle', 'residence'):
            if original.get(key) != result.get(key):
                unverified[key] = original.get(key)
    for key in ('target_characteristics', 'interest_topics'):
        tags = result.get(key) or []
        confirmed = [tag for tag in tags if isinstance(tag, str) and _tag_supported(tag, content, key)]
        if confirmed != tags:
            unverified[key] = tags
        result[key] = confirmed
    ages, intervals = _age_evidence(content)
    disjoint = len(intervals) > 1
    if disjoint:
        unverified['age_ranges'] = [dict(start_age_months=lower, end_age_months=upper)
                                    for lower, upper in intervals]
    for key in ('start_age_months', 'end_age_months'):
        value = result.get(key)
        if value is not None and (disjoint or (key, value) not in ages):
            unverified[key], result[key] = value, None
    evidence = result.get('fact_evidence')
    evidence = evidence if isinstance(evidence, dict) else {}
    for key in ('support_detail', 'eligibility_detail', 'contact'):
        value = result.get(key)
        diagnostic_value = diagnostic_original.get(key)
        if not diagnostic_value:
            continue
        quote = evidence.get(key, diagnostic_value)
        reason = source_quote_issue(quote, content)
        if not reason and (not isinstance(diagnostic_value, str)
                           or not preserves_quoted_facts(diagnostic_value, quote)):
            display = verified_labeled_display(key, diagnostic_value, quote, content)
            if display is None:
                reason = 'presentation_not_preserved'
            elif value:
                # Keep all verified label/institution context; never restore a
                # previously withheld empty field from diagnostic-only data.
                result[key] = display
        if reason:
            field_holds[key] = reason
        if value and reason:
            unverified[key], result[key] = value, ''
    if result.get('deadline') and not _date_supported(result['deadline'], content):
        unverified['deadline'], result['deadline'] = result['deadline'], ''
    missing = _omitted_conditions(result, content)
    if missing:
        unverified['omitted_conditions'] = missing
    condition_holds = _condition_diagnostics(diagnostic_original, result, missing,
                                            auxiliary=analysis, content=content)
    # Check any explicit relevance citation against the actual source, not the
    # classifier prompt. Old recipes without this citation keep their existing
    # conservative fallback; supplying a fabricated citation never gains it.
    relevance_quote = evidence.get('is_relevant')
    if relevance_quote:
        reason = source_quote_issue(relevance_quote, content)
        if reason:
            unverified['is_relevant'] = result.get('is_relevant')
            field_holds['is_relevant'] = reason
            issues.append('소식 관련성의 인용문이 원문과 일치하지 않아 자동 판정을 보류했습니다. 판단 지침이나 설명은 원문 근거로 사용할 수 없습니다. 원문과 비교해주세요.')
    # Keep a previous hold when its rejected facts were already removed. This is
    # important for cache hits and repeated previews of the same saved result.
    if isinstance(prior, dict):
        unverified = {**{k: v for k, v in prior.items() if k in _LABELS}, **unverified}
    if unverified:
        fields = [key for key in unverified if key not in ('omitted_conditions', 'age_ranges', 'is_relevant')]
        if 'is_relevant' in unverified and 'is_relevant' not in field_holds:
            issues.append('소식 관련성은 이전 검토에서 보류된 항목입니다. 원문과 비교해주세요.')
        if fields:
            issues.append('원문에서 명시적 근거를 확인하지 못한 ' + '·'.join(_LABELS[k] for k in fields) + ' 값은 판정과 저장에서 제외했습니다.')
        if 'omitted_conditions' in unverified:
            origins = {item['origin'] for item in condition_holds}
            if 'validation_withheld' in origins:
                issues.append('AI 응답에 있던 제외·필수 신청 조건이 본문 검증 중 보류되었습니다. 원문과 비교해주세요.')
            if 'presentation_coverage_unconfirmed' in origins:
                issues.append('응답에 조건 문구는 있지만 표시 형식에서 조건의 연결 관계를 확인하지 못했습니다.')
            if 'auxiliary_evidence_only' in origins:
                issues.append('제외·필수 신청 조건의 문구는 보조 인용에 있지만 표시 본문에서 확인하지 못했습니다. 원문과 함께 확인해주세요.')
            if not origins or 'model_output_unconfirmed' in origins:
                issues.append('원문의 제외·필수 신청 조건을 AI 응답에서 확인하지 못했습니다. 원문과 비교해주세요.')
        if 'age_ranges' in unverified:
            issues.append('원문의 분리된 나이 구간은 하나의 범위로 저장할 수 없어 확인이 필요합니다.')
    applicant = applicant_status(result, content)
    decision = evaluate_target(profile, result)
    status, checks = decision.status, dict(decision.checks)
    if not negative_notice:
        checks['applicant'] = applicant
    if applicant == 'review' and not negative_notice:
        issues.append('신청 주체를 원문에서 확인해주세요.')
    is_relevant = result.get('is_relevant')
    exclusion_quote = evidence.get('is_relevant') or result.get('evidence_snippet') or ''
    explicit_non_news = (is_relevant is False and isinstance(exclusion_quote, str)
        and not source_quote_issue(exclusion_quote, content)
        and scoped_affirmative_quote(exclusion_quote, content) is not None
        and bool(re.search(r'직원\s*채용|채용\s*공고|입찰\s*공고|서버\s*점검|로그인\s*(?:폼|페이지|필요)', exclusion_quote)))
    if whole_document_rules:
        # v15 does not mistake a programme's incidental login requirement for
        # an administrative notice. Existing non-login negative patterns remain
        # conservative: unresolved selection/fact holds cannot auto-exclude.
        explicit_non_news = bool((negative_notice or (
            explicit_non_news and not re.search(r'로그인', exclusion_quote)))
            and not issues and not unverified and not scope_hold)
    if is_relevant is False and not explicit_non_news:
        issues.append('관련 없음 판정의 원문 근거를 확인해주세요.')
    if issues:
        status = 'review'
    if applicant == 'excluded' or explicit_non_news:
        status = 'excluded'
    if whole_document_rules and (scope_hold or split_review or unverified or (
            selection_hold and not negative_notice)):
        status = 'review'  # source/provenance hold wins over every exclusion
        explicit_non_news = False
    result['unverified_facts'] = unverified
    result['validation_diagnostics'] = dict(field_holds=field_holds, condition_holds=condition_holds)
    result['grounding_note'] = ' '.join(dict.fromkeys(issues))
    result['validation_version'] = COMPLETE_VERSION if complete_document_rules else BOUNDED_VERSION if whole_document_rules else VERSION
    return dict(analysis=result, accepted=status == 'match', review_required=status == 'review',
                target_status=status, target_checks=checks,
                filter_reason='not_relevant' if explicit_non_news else status)
