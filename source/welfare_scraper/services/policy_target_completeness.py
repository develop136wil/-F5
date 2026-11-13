"""Narrow, source-owned reminders for explicit recipient search-tag coverage.

These are NOT eligibility decisions or automatic tag repairs. A candidate only
means that one independently supported household concept should not disappear
from a new model response. Alternative (OR) groups remain alternatives in the
unchanged source; their search tags do not turn them into cumulative conditions.
Unknown/custom classifications, incidental roles and ambiguous wording produce
no requirement. Absence of candidates is never a completeness certificate.
"""
import re


_MAX_SOURCE_CHARS = 6000
_MAX_SOURCE_LINES = 512
_MAX_QUOTE_CHARS = 800
_FIELD = 'target_characteristics'

# Deliberately narrower than the general taxonomy. Each entry names ONE source
# concept and the existing exact connected labels which can represent it. Broad
# combined labels can represent the explicit member without widening the
# source eligibility. Income thresholds alone are not a literal low-income tag.
_CONCEPTS = (
    (('장애인', '장애/발달지연', '장애', '등록장애인', '장애가구'),
     ('등록장애인', '장애인', '장애가구')),
    (('저소득', '저소득층'), ('저소득층', '저소득')),
    (('다자녀', '다자녀가구', '다자녀가정'), ('다자녀',)),
    (('한부모', '한부모가족', '한부모·조손'), ('한부모가족', '한부모')),
)
_TARGET_HEADING = re.compile(
    r'(?:(?:지원|신청|수급|서비스)(?:대상|대상자)|대상|지원자격|신청자격|선정기준|'
    r'[가-힣]{1,16}지원대상)')
_OTHER_HEADING = re.compile(
    r'지원내용|지원기간|지원금액|지원방법|신청방법|신청기간|접수기간|사업개요|'
    r'제출서류|구비서류|증빙서류|참고자료|관련자료|문의처?|연락처|담당부서')
_UNSAFE_ROLE = re.compile(
    r'보호자|(?<!한)부모|대리인|신청자(?!격)|신청인|제공기관|제공인력|수행기관|종사|'
    r'강사|직원|의사|봉사|업체|사업자|법인|단체|센터|복지관|기관|시설|'
    r'법령|법률|조례|시행령|시행규칙|복지법|지원법|서류|증빙|자료|예시|'
    r'참고|문의|연락|상담|돌보|입양|위탁|모집|제공하는|지원하는')
_AMBIGUOUS = re.compile(
    r'제외|불가|불필요|미해당|비대상|아닌|아니며|아니라|아님|아닙|않|못|'
    r'대상없|대상이없|보다|대신|대비|비교|관계없|상관없|무관|여부|이력|'
    r'지원받|지원을받|예를|예로|외의|외에|외가구|비장애|非')
_DETACHED_NEGATIVE = re.compile(
    r'(?:(?:지원|지급|수급|신청)?대상(?:이|은|는)?|지원|지급|수급|신청|접수)?'
    r'(?:미지원|미지급|미수급|미해당|없음|불가|불필요|아님|아닙니다|'
    r'제외(?:됨|됩니다)?|지원하지않음|지급하지않음|수급하지않음|'
    r'하지않음|하지않습니다|안됨)[.!。:：]?')


def _compact(text):
    return re.sub(r'\s+', '', text)


def _plain(text):
    value = re.sub(r'^\s*(?:#{1,6}\s*|[-•·○●□■▶▷◈※]\s*)*', '', text).strip()
    return re.sub(r'^(?:\d{1,2}[.)]|[가-하][.)]|[①-⑳])\s*', '', value)


def _has_direct_concept(text, terms, in_target):
    """Recognize a recipient noun, not an arbitrary mention in a target block."""
    # Entire lines with mixed roles/negative predicates are intentionally held
    # out, even if another noun in that line would be an eligible recipient.
    if _UNSAFE_ROLE.search(text) or _AMBIGUOUS.search(text):
        return False
    for term in terms:
        for match in re.finditer(re.escape(term), text):
            prefix, suffix = text[:match.start()], text[match.end():]
            # A noun must start an enumerated member or follow a narrow
            # qualifier, not occur inside a provider/comparative noun phrase.
            if prefix and not re.search(r'(?:[:：,，·ㆍ]|또는|및|그리고|의|둔|이하|이상)$', prefix):
                continue
            suffix = re.sub(r'^(?:가구|가정|가족)', '', suffix)
            suffix = re.sub(r'^\([^()]{0,60}\)', '', suffix)
            if in_target and (not suffix or re.match(
                    r'^(?:[.!。,:：，·ㆍ]|또는|및|과|와|중|수급대상가구|에해당|인경우|입니다|이다)', suffix)):
                return True
            # Outside a clearly named qualification block only a direct
            # recipient -> benefit statement establishes a reminder.
            if re.fullmatch(r'(?:에게|에|을대상으로|를대상으로).{0,160}지원(?:합니다|한다|됩니다)?[.!。]?', suffix):
                return True
    return False


def target_evidence_candidates(content, allowed):
    """Return bounded {labels, quote, line} reminders from trusted source input.

    ``allowed`` is the complete, request-specific taxonomy dict (typically the
    source-screened version). ``labels`` is an exact-label OR group, not a list
    of tags to inject. ``quote`` is the complete physical source line, never a
    stitched excerpt. Only the new extraction request consumes this projection.
    """
    if (not isinstance(content, str) or not content.strip()
            or len(content) > _MAX_SOURCE_CHARS
            or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', content)
            or not isinstance(allowed, dict)):
        return []
    options = allowed.get(_FIELD)
    if (not isinstance(options, list) or len(options) > 100
            or any(not isinstance(value, str) or not value or len(value) > 80 for value in options)):
        return []
    lines = content.splitlines()
    if len(lines) > _MAX_SOURCE_LINES:
        return []
    from services.policy_evidence import _policy_recipient_source, structured_policy_issues

    masked, _ = _policy_recipient_source(content)
    kept_lines = masked.split('\n')
    nonempty = [index for index, line in enumerate(lines) if line.strip()]
    uncertain_neighbors = set()
    for position, index in enumerate(nonempty):
        if _DETACHED_NEGATIVE.fullmatch(_compact(_plain(lines[index]))):
            # Flattened tables may place a predicate on either side. Blank
            # lines do not prove that it belongs to a different recipient.
            uncertain_neighbors.update(nonempty[max(0, position - 1):position + 2])
    available = set(options)
    full_guard = {}
    found = {}
    in_target = False
    for number, raw in enumerate(lines, 1):
        # Reuse the existing exclusion/provider/document parent masking; a
        # subordinate "대상" label must never restart an excluded parent.
        kept = kept_lines[number - 1] if number <= len(kept_lines) else ''
        if raw.strip() and not kept.strip():
            in_target = False
            continue
        text = _compact(_plain(raw))
        lead = re.split(r'[:：]', text, maxsplit=1)[0]
        if _TARGET_HEADING.fullmatch(lead) and not _UNSAFE_ROLE.search(lead):
            in_target = True
        elif _OTHER_HEADING.fullmatch(lead) or _UNSAFE_ROLE.search(lead):
            in_target = False
        if not text or len(raw) > _MAX_QUOTE_CHARS or number - 1 in uncertain_neighbors:
            continue
        for index, (labels, terms) in enumerate(_CONCEPTS):
            if index in found or not _has_direct_concept(text, terms, in_target):
                continue
            supported = []
            for label in labels:
                if label not in available:
                    continue
                if label not in full_guard:
                    full_guard[label] = _FIELD not in structured_policy_issues({_FIELD: [label]}, content)
                # A passing occurrence elsewhere must not turn THIS line into
                # evidence. Require the same guard on the complete local line.
                if full_guard[label] and _FIELD not in structured_policy_issues({_FIELD: [label]}, raw):
                    supported.append(label)
            if supported:
                found[index] = {'labels': supported, 'quote': raw, 'line': number}
    return list(found.values())


def missing_target_groups(result, candidates):
    """Report missing supported groups without changing any result or source.

    Call only with candidates recomputed from the current source/taxonomy, not
    metadata provided by a model or a saved user record. Other validators still
    reject malformed, invented and unsupported result values independently.
    """
    values = result.get(_FIELD) if isinstance(result, dict) else None
    selected = {value for value in values if isinstance(value, str)} if isinstance(values, list) else set()
    return [dict(labels=list(candidate['labels']), quote=candidate['quote'], line=candidate['line'])
            for candidate in candidates if not selected.intersection(candidate['labels'])]
