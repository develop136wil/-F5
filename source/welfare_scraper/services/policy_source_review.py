"""Bounded review signals, not a legal interpretation or general contradiction solver.

Never choose a winning threshold, infer a date/age, rewrite a source or call AI.
Explicitly separated cohorts/years and maintenance criteria are not compared.
Unknown scope is deliberately reviewed rather than guessed.
"""
import re
from decimal import Decimal

_UPPER_INCOME = re.compile(r'(?:기준)?중위소득(\d+(?:\.\d+)?)%(이하|미만)')
_PHASE = re.compile(r'유지기준|유지조건|가입기준|신청기준|신청자격|지원대상|가구소득|소득기준')
_COHORTS = {
    'intake': re.compile(r'신규|기존'),
    'tier': re.compile(r'차상위이하|차상위초과'),
    'audience': re.compile(r'청년|노년|아동|장애인|한부모|일반가구'),
    'household': re.compile(r'\d+인가구'),
    'type': re.compile(r'(?:[A-Z가나다라]|\d+)유형'),
}
_YEAR = re.compile(r'(?<!\d)(20\d{2})년')
_CALENDAR_AGE = re.compile(r'(?:신청|접수)(?:하는|한)?(?:월|달|연도|년도|연말)[^\n;。]{0,100}'
                           r'(?:만)?\d+세(?:가|이)?되는')
_NUMERIC_AGE = re.compile(
    r'(?<!\d)(?:만)?\d{1,3}(?:세|살)?(?:[~∼–-]|부터)(?:만)?\d{1,3}(?:세|살)'
    r'|(?<!\d)(?:만)?\d{1,3}(?:세|살)(?:이상|이하|초과|미만|부터|까지)')
_BIRTH_BASIS = re.compile(
    r'(?:출생연도|출생년도|출생일|생년월일)[^\n;。]{0,40}(?:19|20)\d{2}'
    r'|(?:19|20)\d{2}[^\n;。]{0,65}출생')


def _birth_age_needs_review(content):
    # A nearby explicit birth-date criterion is not a rolling month-age range.
    # Only inspect the age line and its immediate neighbours; do not borrow an
    # unrelated birth-date form field or a year elsewhere on a long page.
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if _NUMERIC_AGE.search(line):
            window = '\n'.join(lines[max(0, index - 1):index + 2])
            if _BIRTH_BASIS.search(window):
                return True
    return False


def _scope(text):
    scope = {}
    for axis, pattern in _COHORTS.items():
        matches = list(pattern.finditer(text))
        if matches:
            scope[axis] = matches[-1].group()
    years = _YEAR.findall(text)
    if len(set(years)) == 1:
        scope['year'] = years[0]
    return scope


def _separate(left, right):
    return any(left[key] != right[key] for key in left.keys() & right.keys())


def source_criteria_issues(content):
    """Return only fixed codes/fields, never source text or inferred corrections.

    Income support is intentionally limited to explicit median-income upper
    thresholds. Other axes, tables without text labels and broader semantic
    conflicts still need a human; an empty list is not a correctness guarantee.
    """
    if not isinstance(content, str):
        return []
    issues = []
    compact = re.sub(r'[ \t\u00a0]', '', content)
    if _CALENDAR_AGE.search(compact):
        issues.append({'code': 'source_calendar_age_review', 'field': 'age_detail'})
    if _birth_age_needs_review(compact):
        issues.append({'code': 'source_birth_date_age_review', 'field': 'age_detail'})

    candidates, heading_scope, maintenance = [], {}, False
    for raw in re.split(r'[\r\n;。]+|(?<!\d)\.(?!\d)', compact):
        text = raw.strip(' \t-•·#*※①②③④⑤⑥')
        if not text:
            continue
        matches = list(_UPPER_INCOME.finditer(text))
        phases = list(_PHASE.finditer(text))
        if not matches:
            if phases:
                maintenance = phases[-1].group().startswith('유지')
            # Short standalone cohort/year headings apply to subsequent rows.
            # Plain section headings reset that context; never borrow a page
            # title or distant beneficiary mention for a later unscoped rule.
            if len(text) <= 50 and not re.search(r'%|이상|이하|초과|미만|제외|중단|계속', text):
                scope = _scope(text)
                if scope:
                    heading_scope = scope
                elif re.search(r'가입|유지|지원대상|신청자격|기준표|기준$|조건$', text):
                    heading_scope = {}
            continue
        previous_end = 0
        for match in matches:
            prefix = text[:match.start()]
            preceding_phases = [phase for phase in phases if phase.start() < match.start()]
            is_maintenance = (preceding_phases[-1].group().startswith('유지')
                              if preceding_phases else maintenance)
            # A second explicit threshold may have its own cohort/year on the
            # same line. Do not borrow scope from the preceding threshold.
            local_scope = _scope(text[previous_end:match.start()])
            previous_end = match.end()
            # Maintenance thresholds are not competing admission thresholds.
            if is_maintenance or re.search(r'제외|중단|초과자', text):
                continue
            # Numbered admission footnotes stand on their own, not under the
            # last column/row label of a preceding table.
            numbered_admission = re.match(r'\s*[①②③④⑤⑥]', raw) and re.search(r'가입기준|신청기준', prefix)
            scope = dict({} if numbered_admission else heading_scope)
            scope.update(local_scope)
            value = (Decimal(match[1]), match[2])
            if any(previous != value and not _separate(old_scope, scope)
                   for previous, old_scope in candidates):
                issues.append({'code': 'source_income_criteria_review', 'field': 'target_income'})
                return issues
            candidates.append((value, scope))
            if len(candidates) > 64:
                issues.append({'code': 'source_income_criteria_review', 'field': 'target_income'})
                return issues
        if phases:
            maintenance = phases[-1].group().startswith('유지')
    return issues


def source_criteria_note(issues):
    codes = {item['code'] for item in issues}
    notes = []
    if 'source_income_criteria_review' in codes:
        notes.append('원문의 소득 상한이 둘 이상이며 적용 대상·시기 구분을 확정하기 어렵습니다.')
    if 'source_calendar_age_review' in codes:
        notes.append('신청 월·연도에 특정 나이가 되는 조건은 단순 월령 범위로 확정할 수 없습니다.')
    if 'source_birth_date_age_review' in codes:
        notes.append('출생연도·출생일 조건과 나이 범위가 함께 있어 기준일·계산 방식 확인이 필요합니다. 단순 월령 범위로 확정하지 않았습니다.')
    return ' '.join(notes) + ' 원문의 적용 조건을 확인해주세요. 기존 내용은 유지됩니다.'
