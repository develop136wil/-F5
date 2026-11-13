"""Conservative single-property outcome check; never approve sibling proposals.

Extraction verifies the union of proposed fields. Applying only one field must
instead preserve conditions in the actual document which will remain after the
PATCH. Absence from the latest source is not proof that a condition was repealed.
"""
import re

from services.policy_evidence import _CONDITION_RE, _fact_clauses, _has_dependent_predicate

POLICY_PROPOSAL_SCHEMA_VERSION = 2

_PROSE = {'eligibility_detail', 'support_detail', 'age_detail', 'target_income'}
_HEADINGS = {'지원대상', '신청대상', '대상', '신청자격', '자격요건', '추가자격요건',
             '지원내용', '지원제외대상', '제외사항', '할인요건', '신청방법', '지원조건'}
_QUALIFICATION = re.compile(r'소득|연령|월령|영유아|영아|유아|아동|청소년|노인|가구|'
                            r'수급|등록|보유|자동차|차량|승차|배기량|장애|\d\s*(?:세|개월)')


def _clauses(value):
    if not isinstance(value, str):
        return []
    return [clause.strip(' .。:：📌⛔') for clause in _fact_clauses(value)
            if clause.strip(' .。:：📌⛔') not in _HEADINGS]


def _present(clause, values):
    # Require exact words; no paraphrase entailment or uncommitted siblings.
    # In particular, '대상' is not preserved by '대상 아님', nor may a
    # detached prohibition be attached to a different neighbouring subject.
    return any(clause == value or (clause in value
               and not _has_dependent_predicate(value)
               and not _has_dependent_predicate(clause)) for value in values)


def standalone_apply_hold(fkey, new_value, current_fields, source=None):
    """Hold a removed condition unless the actual remaining document keeps it.

    Not a completeness certificate. A strictly numeric revision of the same
    sentence may pass only with its complete latest-source wording. Ambiguous
    deletion needs linked review or explicit Notion editing, never force flags.
    """
    if fkey not in _PROSE or not isinstance(current_fields, dict):
        return None
    old = _clauses(current_fields.get(fkey))
    proposed = _clauses(new_value)
    remaining = [clause for key in _PROSE for clause in
                 _clauses(new_value if key == fkey else current_fields.get(key))]
    source_clauses = _clauses(source)
    for clause in old:
        critical = bool(_CONDITION_RE.search(clause) or _QUALIFICATION.search(clause))
        if not critical or _present(clause, remaining):
            continue
        # Predicates, units and relation words must remain identical.
        skeleton = re.sub(r'\d+(?:\.\d+)?', '#', clause)
        replacement = [value for value in proposed
                       if skeleton == re.sub(r'\d+(?:\.\d+)?', '#', value)
                       and value != clause and '#' in skeleton]
        if any(_present(value, source_clauses) for value in replacement):
            continue
        return ('이 항목만 반영하면 기존 조건이나 제외사항이 다른 미반영 제안과 함께 '
                '사라질 수 있어 보류했습니다. 관련 항목과 최신 원문을 함께 확인해주세요. '
                '조건의 삭제가 맞다면 Notion에서 직접 수정한 뒤 다시 불러와주세요. '
                '기존 내용은 변경하지 않았습니다.')
    return None
