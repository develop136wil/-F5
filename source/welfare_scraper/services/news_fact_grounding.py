"""Conservative checks of explicit lifecycle/residence quotes; no model calls."""
import re
from services.news_profile_taxonomy import canonical_life_cycle
from services.news_region_rules import normalize_region, residence_quote_matches


def compact(text):
    return re.sub(r'\s+', '', text)


def source_quote_issue(quote, content):
    """Identify unsupported citations without treating model instructions as data.

    Classification rules, examples and the model's explanation are not source
    evidence, in any language. Keep absence distinct from changed boundaries;
    neither can authorize a fact or an automatic exclusion.
    """
    if not isinstance(quote, str) or not quote.strip():
        return 'source_quote_missing'
    if not isinstance(content, str) or compact(quote) not in compact(content):
        return 'source_quote_not_found'
    from services.policy_evidence import quote_matches_source
    return '' if quote_matches_source(quote, content) else 'source_quote_boundary_changed'


def has_nonexcluded_occurrence(quote, source):
    """Check nearby suffix too: a shortened quote may omit an exclusion."""
    for match in re.finditer(re.escape(quote), source):
        suffix = source[match.end():match.end() + 30]
        if not re.match(r'^(?:은|는|을|를|이|가|만)?(?:제외|불가|불필요|아닌|아니며|아님|대상아님)', suffix):
            return True
    return False


def has_affirmative_context(quote, content):
    """Do not accept quotes that omit a nearby explicit negative qualifier.

    Keep line/sentence boundaries so a separate exclusion does not invalidate
    an affirmative eligibility line. This is a guard, not semantic proof.
    """
    clauses = re.split(r'[\n\r.!?;。]+', content)
    negative = r'제외|불가|불필요|아닌|아니며|아니라|아니다|아님|아닙|않습니다|않음|수없'
    for clause in clauses:
        normalized = compact(clause)
        for match in re.finditer(re.escape(quote), normalized):
            nearby = normalized[max(0, match.start() - 30):match.end() + 30]
            if not re.search(negative, nearby):
                return True
    return False


def scoped_affirmative_quote(raw_quote, content):
    """Keep quoted line breaks and separate a narrowly recognized constraint.

    This is deliberately not a general Korean clause parser. In particular, a
    comma alone cannot end an exclusion: ``제외 대상: 청년, 노년`` must stay
    negative. Only an independent, explicitly named duplicate-use restriction
    may be removed from the eligibility evidence.
    """
    return affirmative_quote_checker(content)(raw_quote)


def affirmative_quote_checker(content):
    """Prepare exact source indexing once within one analysis, never globally.

    Every quote still follows the original boundary/negative-context checks.
    The local memo only avoids repeating identical queries against this exact
    immutable source; it cannot approve a quote against another document.
    """
    positions = [index for index, char in enumerate(content) if not char.isspace()]
    normalized = compact(content)
    cache = {}

    def check(raw_quote):
        if raw_quote not in cache:
            cache[raw_quote] = _scoped_affirmative_quote(raw_quote, content, positions, normalized)
        return cache[raw_quote]

    return check


def _scoped_affirmative_quote(raw_quote, content, positions, normalized):
    independent_duplicate = re.compile(
        r'[,，]\s*중복\s*(?:신청|수급|참여)(?:자)?\s*'
        r'(?:불가|제외|금지)(?:합니다|됩니다)?\s*[.!?。]?\s*$')
    negative = r'제외|불가|불필요|아닌|아니며|아니라|아니다|아님|아닙|않습니다|않음|수없'
    boundaries = '\n\r.!?;。'
    quote = compact(raw_quote)
    cleaned_quote = independent_duplicate.sub('', raw_quote).strip()
    if not quote or not cleaned_quote:
        return None
    for match in re.finditer(re.escape(quote), normalized):
        start, end = positions[match.start()], positions[match.end() - 1] + 1
        # Boundaries inside an exact quote are part of its evidence, not a
        # reason to fail matching a heading followed by a resident description.
        left = max((content.rfind(char, 0, start) for char in boundaries), default=-1) + 1
        right = min((index for char in boundaries
                     if (index := content.find(char, end)) != -1), default=len(content))
        if left and content[left - 1] in '\n\r':
            previous_end = len(content[:left].rstrip('\n\r'))
            previous_start = max((content.rfind(char, 0, previous_end)
                                  for char in boundaries), default=-1) + 1
            previous_line = content[previous_start:previous_end].strip()
            # Preserve headings such as "제외 대상:\n도봉구 거주자".
            if previous_line.endswith((':', '：')) or re.fullmatch(
                    r'(?:지원|신청)?(?:제외|불가)(?:대상|조건)', compact(previous_line)):
                left = previous_start
        if right < len(content) and content[right] in '\n\r':
            next_line = content[right:].lstrip('\n\r').split('\n')[0]
            # A wrapped negative predicate is still about the quoted subject.
            if re.match(r'\s*(?:(?:지원|신청)\s*)?(?:대상(?:이|에)?\s*)?'
                        r'(?:아닙|아님|아닌|제외|불가|할\s*수\s*없|지원하지\s*않)', next_line):
                right += len(content[right:]) - len(content[right:].lstrip('\n\r')) + len(next_line)
        context = independent_duplicate.sub('', content[left:right]).strip()
        context_quote = compact(cleaned_quote)
        context_normalized = compact(context)
        at = context_normalized.find(context_quote)
        if at < 0:
            continue
        nearby = context_normalized[max(0, at - 30):at + len(context_quote) + 30]
        if not re.search(negative, nearby):
            return cleaned_quote
    return None


def grounded_facts(result, content, *, short_regions=True, strict_context=True,
                   scoped_context=False):
    output = dict(result)
    removed = []
    source = compact(content)
    aliases = {
        '임신·출산': ('임신', '출산'), '영유아': ('영유아', '영아', '유아'),
        '아동': ('아동', '학령기', '초등학생'), '청소년': ('청소년', '중학생', '고등학생'),
        '청년': ('청년',), '중장년': ('중장년', '중년', '장년'),
        '노년': ('노년', '노인', '어르신', '고령자'),
        '전생애': ('전생애', '연령제한없', '나이제한없', '모든연령'),
    }
    for field in ('life_cycle', 'residence'):
        entries = result.get(field + '_evidence')
        if not isinstance(entries, list) or len(entries) > 100:
            raise ValueError('Invalid grounding evidence')
        confirmed = set()
        for entry in entries:
            if not isinstance(entry, dict) or any(not isinstance(entry.get(k), str)
                    or len(entry[k]) > 2000 for k in ('value', 'quote', 'basis')):
                raise ValueError('Invalid grounding entry')
            if entry['basis'] not in ('explicit', 'inferred', 'publisher', 'proxy'):
                raise ValueError('Invalid grounding basis')
            quote = compact(entry['quote'])
            if entry['basis'] != 'explicit' or not quote or quote not in source:
                continue
            evidence_quote = entry['quote']
            if strict_context:
                if scoped_context:
                    evidence_quote = scoped_affirmative_quote(entry['quote'], content)
                    if evidence_quote is None:
                        continue
                    quote = compact(evidence_quote)
                elif not has_affirmative_context(quote, content):
                    continue
            if short_regions and not has_nonexcluded_occurrence(quote, source):
                continue
            # A mention in an exclusion clause cannot establish eligibility.
            if re.search(r'제외|불가|아닌|아니며|대상아님', quote):
                continue
            value = entry['value']
            if field == 'life_cycle':
                value = canonical_life_cycle(value)
                supported = any(term in quote for term in aliases.get(value, ()))
            else:
                value = normalize_region(value)
                if value in ('전국', '거주무관', '제한없음'):
                    supported = any(term in quote for term in ('전국민', '전국대상', '전국거주', '거주무관', '거주지제한없', '지역제한없'))
                else:
                    # Must describe eligible residents, not just where to visit.
                    locality = compact(value)
                    supported = (residence_quote_matches(value, evidence_quote) if short_regions else
                                 locality in quote and bool(re.search(r'거주|구민|시민|주민', quote)))
            if supported:
                confirmed.add(value)
        canonical = canonical_life_cycle if field == 'life_cycle' else normalize_region
        output[field] = [tag for tag in result[field] if canonical(tag) in confirmed]
        if len(output[field]) != len(result[field]):
            removed.append('생애주기' if field == 'life_cycle' else '거주 조건')
    return output, ('원문에서 명시적 근거를 확인하지 못한 ' + '·'.join(removed) + ' 분류는 판정에서 제외했습니다. 원문을 확인해주세요.') if removed else ''
