"""Conservative, offline custom-target evaluation. No legacy rules changed.

Alternatives within one field are OR; different fields are AND. Missing evidence
is review, not acceptance. This evaluates extracted facts, not raw text accuracy.
"""
from dataclasses import dataclass
from services.news_profile import NewsProfile
from services.news_region_rules import normalize_region
from services.news_profile_taxonomy import (
    LIFE_CYCLES, INTEREST_TOPICS, canonical_life_cycle, canonical_household_situation,
)


@dataclass(frozen=True)
class TargetDecision:
    status: str  # match / excluded / review
    checks: tuple[tuple[str, str], ...]


def _tags(value, canonical=None):
    if isinstance(value, str):
        value = value.split(',')
    if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) for v in value):
        return set()
    result = {v.strip() for v in value if v.strip()}
    return {canonical(v) for v in result} if canonical else result


def _age(profile, item):
    low, high = profile.min_age_months, profile.max_age_months
    if low is None and high is None:
        return 'match'
    start, end = item.get('start_age_months'), item.get('end_age_months')
    for value in (start, end):
        if value is not None and (type(value) is not int or not 0 <= value <= 1800):
            return 'review'
    if start is not None and end is not None and start > end:
        return 'review'
    if (high is not None and start is not None and start > high
            or low is not None and end is not None and end < low):
        return 'excluded'
    # Prove overlap without guessing an unknown endpoint.
    lower, upper = low if low is not None else 0, high if high is not None else 1800
    if any(value is not None and lower <= value <= upper for value in (start, end)):
        return 'match'
    if start is not None and end is not None and start <= lower and end >= upper:
        return 'match'
    return 'review'


def evaluate_target(profile: NewsProfile, item: dict) -> TargetDecision:
    if profile.general_mode != 'custom_v1':
        raise ValueError('Legacy profiles must use their original filter')
    if not isinstance(item, dict):
        raise ValueError('Extracted facts must be an object')
    if not profile.relevance_enabled:
        return TargetDecision('review', (('filter_disabled', 'review'),))
    regions = _tags(item.get('residence'))
    # Do not assume a city/ward hierarchy, neighboring region, or nationwide
    # eligibility from an institution address. Ambiguous locality stays review.
    region = ('match' if profile.region.strip() == '전국' or
              regions & {'전국', '거주무관', '제한없음'} or
              normalize_region(profile.region) in {normalize_region(r) for r in regions} else 'review')
    requested_cycles = _tags(profile.life_cycles, canonical_life_cycle)
    cycles = _tags(item.get('life_cycle'), canonical_life_cycle)
    lifecycle = 'match' if (not requested_cycles or '전생애' in requested_cycles
                           or requested_cycles & cycles or '전생애' in cycles) else 'review'
    known_cycles = set(LIFE_CYCLES) | {'전생애'}
    if (requested_cycles and requested_cycles <= known_cycles and '전생애' not in requested_cycles
            and cycles and cycles <= known_cycles and not cycles & requested_cycles
            and '전생애' not in cycles):
        lifecycle = 'excluded'
    # Characteristics may be incompletely extracted. Absence of a requested tag
    # is not evidence that a benefit excludes that family characteristic.
    # Official taxonomy combines these groups, but the user's specific target
    # must not broaden to its sibling. Combined evidence alone remains review.
    def target_trait(value):
        value = value.strip()
        if value in ('다문화', '탈북민', '북한이탈주민'):
            return '탈북민' if value == '북한이탈주민' else value
        return canonical_household_situation(value)

    requested_traits = _tags(profile.characteristics, target_trait)
    # General is shared by every welfare item, not evidence for a specific
    # household. General alone is unrestricted; general + disability must still
    # prove disability. Never turn missing specific evidence into exclusion.
    requested_traits.discard('일반')
    traits = _tags(item.get('target_characteristics'), target_trait)
    if traits & {'다문화', '탈북민'}:
        traits.add('다문화·탈북민')
    characteristic = 'match' if not requested_traits or traits & requested_traits else 'review'
    # 관심주제도 화면의 다른 줄과 같은 AND 조건이다. AI가 근거를 못 찾은 경우는
    # 누락으로 단정하지 않고 review, 서로 다른 공식 주제만 명확하면 excluded로 둔다.
    requested_topics = _tags(profile.interest_topics)
    topics = _tags(item.get('interest_topics'))
    interest_topic = 'match' if not requested_topics or topics & requested_topics else 'review'
    known_topics = set(INTEREST_TOPICS)
    if (requested_topics and requested_topics <= known_topics and topics
            and topics <= known_topics and not topics & requested_topics):
        interest_topic = 'excluded'
    checks = (('region', region), ('age', _age(profile, item)),
              ('life_cycle', lifecycle), ('characteristics', characteristic),
              ('interest_topics', interest_topic))
    statuses = {status for _, status in checks}
    status = 'excluded' if 'excluded' in statuses else 'review' if 'review' in statuses else 'match'
    return TargetDecision(status, checks)
