"""Bokjiro-aligned labels used by opt-in news profile trials.

These are search taxonomy labels, not a claim that everyone with a label is
eligible for every service. Programme-specific age, income, and residence
requirements must continue to be checked from each service detail.
"""

SOURCE_URL = 'https://www.bokjiro.go.kr/ssis-tbu/twataa/wlfareInfo/moveTWAT52005M.do'
VERIFIED_ON = '2026-09-05'

LIFE_CYCLES = ('임신·출산', '영유아', '아동', '청소년', '청년', '중장년', '노년')
HOUSEHOLD_SITUATIONS = ('저소득', '장애인', '한부모·조손', '다자녀', '다문화·탈북민', '보훈대상자')
INTEREST_TOPICS = (
    '신체건강', '정신건강', '생활지원', '주거', '일자리', '문화·여가', '안전·위기',
    '임신·출산', '보육', '교육', '입양·위탁', '보호·돌봄', '서민금융', '법률', '에너지',
)

_LIFE_ALIASES = {
    "임신 · 출산": "임신·출산",
    '임신/출산': '임신·출산', '임신출산': '임신·출산', '출산': '임신·출산',
    '영아': '영유아', '유아': '영유아', '아동·청소년': '아동', '학령기': '아동',
    '어르신': '노년', '노인': '노년', '고령': '노년',
}
_HOUSEHOLD_ALIASES = {
    '저소득층': '저소득', '기초생활수급자': '저소득', '차상위': '저소득',
    '장애': '장애인', '등록장애인': '장애인', '장애가구': '장애인',
    '한부모': '한부모·조손', '한부모가족': '한부모·조손', '조손': '한부모·조손',
    '조손가족': '한부모·조손', '조손가구': '한부모·조손',
    '다자녀가구': '다자녀', '다자녀가정': '다자녀',
    '다문화': '다문화·탈북민', '탈북민': '다문화·탈북민', '북한이탈주민': '다문화·탈북민',
    '보훈': '보훈대상자', '국가유공자': '보훈대상자',
}

PRESETS = (
    {'id': 'senior', 'label': '노년', 'name': '노년 복지 소식',
     'life_cycles': ['노년'], 'characteristics': [],
     'interest_topics': ['신체건강', '생활지원', '보호·돌봄', '주거'],
     'keywords': ['노년', '노인', '어르신', '고령'],
     'semantic_text': '노년층이 이용할 수 있는 돌봄·건강·생활·주거·일자리 복지 지원 소식'},
    {'id': 'single_parent', 'label': '한부모·조손', 'name': '한부모·조손 복지 소식',
     'life_cycles': [], 'characteristics': ['한부모·조손'],
     'interest_topics': ['생활지원', '주거', '보육', '교육'],
     'keywords': ['한부모', '조손', '미혼모', '미혼부'],
     'semantic_text': '한부모가족과 조손가족이 이용할 수 있는 양육·생활·주거·교육 복지 지원 소식'},
    {'id': 'disabled', 'label': '장애인', 'name': '장애인 복지 소식',
     'life_cycles': [], 'characteristics': ['장애인'],
     'interest_topics': ['신체건강', '정신건강', '생활지원', '보호·돌봄'],
     'keywords': ['장애인', '장애', '발달장애', '장애아'],
     'semantic_text': '장애인과 장애인 가구가 이용할 수 있는 건강·재활·돌봄·생활 복지 지원 소식'},
)


def canonical_life_cycle(value):
    value = value.strip() if isinstance(value, str) else ''
    return _LIFE_ALIASES.get(value, value)


def canonical_household_situation(value):
    value = value.strip() if isinstance(value, str) else ''
    return _HOUSEHOLD_ALIASES.get(value, value)


def household_evidence_aliases(value):
    """Explicit aliases for a canonical label, never its sibling categories."""
    if value not in HOUSEHOLD_SITUATIONS:
        return (value,)
    return (value, *(alias for alias, label in _HOUSEHOLD_ALIASES.items() if label == value))


def public_taxonomy():
    return {
        'version': 2, 'verified_on': VERIFIED_ON, 'source_url': SOURCE_URL,
        'life_cycles': list(LIFE_CYCLES),
        'household_situations': list(HOUSEHOLD_SITUATIONS),
        'interest_topics': list(INTEREST_TOPICS),
        'presets': [dict(item) for item in PRESETS],
        'groups': [
            {'id': 'life_cycles', 'question': '누구를 찾나요?', 'label': '생애주기',
             'help': '나이에 따른 생활 단계를 고릅니다. 여러 개를 고르면 그중 하나에 해당하는 자료를 찾습니다.'},
            {'id': 'household_situations', 'question': '어떤 상황인가요?', 'label': '가구상황',
             'help': '저소득·장애·한부모처럼 필요한 상황만 고릅니다. 제한하지 않으려면 비워두세요.'},
            {'id': 'interest_topics', 'question': '어떤 도움이 필요한가요?', 'label': '관심주제',
             'help': '건강·주거·돌봄처럼 찾을 지원 분야를 고릅니다. 제한하지 않으려면 비워두세요.'},
        ],
        'combination_rule': '같은 줄에서 여러 개를 고르면 그중 하나, 서로 다른 줄을 고르면 모든 줄에 맞는 자료를 찾습니다.',
        'notice': '복지로 검색 분류 기준입니다. 실제 지원 자격은 각 사업의 상세 조건을 확인해야 합니다.',
    }
