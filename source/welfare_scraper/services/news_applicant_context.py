"""Bounded school-group application context, never an exclusion classifier.

A teacher authenticating personally may still apply for one class. Conversely,
a teacher, parent or pupil can receive a personal benefit. School/teacher words
alone therefore say nothing about the application route. This helper identifies
only explicit group-application wording and asks the caller to keep it review.
"""
import re


_MAX_SOURCE_CHARS = 6000
_MAX_CLAUSE_CHARS = 2000
_APPLY = (r'(?:신청|접수)(?:이|가|을|를)?'
          r'(?:가능(?:합니다|하다)?|할수있(?:습니다|다|음)|합니다|한다|해주세요|해주십시오|하세요)?$')
_GROUP_APPLICATION = re.compile(
    r'(?:'
    r'(?:학급단체|학교단체)(?:만|별로|별|단위로|단위|로)?'
    r'|학급만'
    r'|(?:학급|학교)(?:별로|별|단위로|단위|단체로|가|이|에서대표로)'
    r'|(?:[1-9]\d{0,2}개?|한개?)학급(?:만|씩)?'
    r')' + _APPLY)
# "학교만" can be an object: a pupil selects one school. Treat it as an
# applicant only in the complete standalone school-only statement; never find
# it as a suffix of "개인이 희망하는 학교만 신청".
_SCHOOL_ONLY_STATEMENT = re.compile(r'^학교만' + _APPLY)
_APPLICANT_LABEL = re.compile(
    r'^(?:신청|접수)(?:대상|단위|주체)[:：](?:학교|학교단체|학급|학급단체)(?:만)?$')
_NONCURRENT_OR_COMPARISON = re.compile(
    r'예시|예제|참고|비교|달리|대신|과거|전년도|지난해|작년|이전안내|'
    r'제외|불가|미접수|미신청|아닌|아니라|아니며|아님|않|못|대상없|대상이없')
_CONTEXT_LABEL = re.compile(
    r'^(?:참고(?:자료)?|예시|예제|비교|과거안내|이전안내|전년도안내|'
    r'제출서류|구비서류|교육주제|교육내용|방문장소|교육장소|장소)[:：]?$')
_DETACHED_NEGATIVE = re.compile(
    r'^(?:(?:신청|접수|지원)?대상(?:이|은|는)?|신청|접수|지원)?'
    r'(?:미접수|미신청|미지원|없음|불가|불필요|아님|아닙니다|제외(?:됨|됩니다)?|'
    r'하지않음|하지않습니다|할수없음|할수없습니다|안됨)[.!。:：]?$')


def _compact(text):
    return re.sub(r'\s+', '', text)


def _label(text):
    return _compact(text).strip('[]【】* -•·○●□■▶▷◈※')


def school_group_application_context(content):
    """Whether captured source explicitly describes school/class applications.

    Only a review signal is returned. Do not convert this into ``excluded`` or
    infer applicant/beneficiary ages, personal eligibility or an organization
    label from it. Unknown or over-limit content supplies no new proof.
    """
    if (not isinstance(content, str) or not content.strip()
            or len(content) > _MAX_SOURCE_CHARS
            or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', content)):
        return False
    from services.news_fact_grounding import scoped_affirmative_quote

    clauses = [part.strip() for part in re.split(r'\r?\n|\r|(?<!\d)[.!?;。](?!\d)', content)
               if part.strip()]
    for index, clause in enumerate(clauses):
        if len(clause) > _MAX_CLAUSE_CHARS:
            continue
        text = _compact(clause)
        if _NONCURRENT_OR_COMPARISON.search(text):
            continue
        neighbors = clauses[max(0, index - 1):index] + clauses[index + 1:index + 2]
        if any(_DETACHED_NEGATIVE.fullmatch(_label(item)) for item in neighbors):
            continue
        if index and _CONTEXT_LABEL.fullmatch(_label(clauses[index - 1])):
            continue
        if (_GROUP_APPLICATION.search(text) or _SCHOOL_ONLY_STATEMENT.fullmatch(text)
                or _APPLICANT_LABEL.fullmatch(text)):
            # Recheck the WHOLE original clause against the complete source.
            # A shortened model quote cannot omit the school-group condition,
            # or detach a preceding/next-line exclusion to establish a match.
            if scoped_affirmative_quote(clause, content) is not None:
                return True
    return False
