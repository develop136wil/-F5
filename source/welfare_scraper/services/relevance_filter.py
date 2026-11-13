"""수집 품질 정밀 필터 — 지역(도봉구) + 연령(영유아~초등저학년).

AI 분류가 추출한 구조화 필드(residence/target/start_age_months/life_cycle)를 이용해
"도봉구 영유아 대상 복지"에 맞는 항목만 남긴다. 의미 유사도보다 지역/연령 같은
명시적 제약에 더 정확하다. 모두 순수 함수라 단위 테스트가 쉽다.

판정 철학: **명백히 어긋난 것만 제외**, 미상(AI가 판단 못 함)은 통과시켜
좋은 정보를 잃지 않는다(사용자가 최종 검토하므로).
"""
import os
import utils
from typing import Any, Dict, Set, Tuple

# 서울 25개 자치구 — 구 단위 입력과 서울 전체 입력을 구분할 때 사용
_SEOUL_DISTRICTS = (
    '강남', '강동', '강북', '강서', '관악', '광진', '구로', '금천', '노원',
    '동대문', '동작', '마포', '서대문', '서초', '성동', '성북', '송파', '양천',
    '영등포', '용산', '은평', '종로', '중구', '중랑', '도봉',
)
# 전국/온라인 등 거주지 무관 태그
_NATIONWIDE = ('전국', '전체', '온라인', '비대면', '거주무관', '제한없음')

# 연령 키워드 (영유아~초등 저학년 범위와 겹치는 표현)
_YOUTH_KEYWORDS = (
    '영유아', '유아', '영아', '아동', '어린이', '미취학', '초등', '유치원',
    '어린이집', '보육', '신생아', '자녀', '키즈', '학령',
)
# 명백히 대상 밖인 키워드
_NON_YOUTH_KEYWORDS = ('청소년', '청년', '성인', '중장년', '노인', '어르신', '노년', '장년')
# life_cycle 태그 분류
_YOUTH_LIFECYCLE = {'임신/출산', '영아', '유아', '학령기', '전생애'}
_ADULT_LIFECYCLE = {'청소년', '청년', '중장년', '노년'}


def _to_set(val: Any) -> Set[str]:
    """리스트 또는 콤마 문자열을 문자열 집합으로 정규화."""
    if not val:
        return set()
    if isinstance(val, (list, tuple, set)):
        return {str(v).strip() for v in val if str(v).strip()}
    return {p.strip() for p in str(val).split(',') if p.strip()}


def region_ok(item: Dict[str, Any], target: str = '도봉') -> bool:
    """거주지 제약 통과 여부.

    - 목표가 전국/전체/제한없음 → 모든 지역 통과
    - 목표 지역 또는 그 행정구역의 넓은 지역이 명시됨 → 통과
    - 전국/온라인 등 거주지 무관 자료 → 통과
    - 도봉 등 서울 자치구를 고른 경우 다른 서울 자치구 전용 자료 → 제외
    - residence 미상 → 통과(판단 보류)
    - 그 외 명백한 다른 지역 → 제외
    """
    target = str(target or '').strip()
    compact_target = target.replace(' ', '')
    for suffix in ('특별자치시', '특별자치도', '특별시', '광역시', '자치구', '시', '군', '구', '도'):
        if compact_target.endswith(suffix) and len(compact_target) > len(suffix):
            compact_target = compact_target[:-len(suffix)]
            break
    if not compact_target or any(name in compact_target for name in _NATIONWIDE):
        return True
    regions = _to_set(item.get('residence'))
    if not regions:
        return True
    text = ' '.join(regions)
    compact_text = text.replace(' ', '')
    if compact_target in compact_text:
        return True
    if any(name in compact_text for name in _NATIONWIDE):
        return True
    mentioned_seoul_districts = {district for district in _SEOUL_DISTRICTS if district in compact_text}
    if compact_target == '서울' and mentioned_seoul_districts:
        return True
    if compact_target in _SEOUL_DISTRICTS and mentioned_seoul_districts:
        return False
    if compact_target in _SEOUL_DISTRICTS and '서울' in compact_text:
        return True
    return False


def age_ok(item: Dict[str, Any], max_months: int = 108) -> bool:
    """연령 제약 통과 여부. 기본 상한 108개월(만 9세, 초등 저학년).

    프로그램 연령 범위가 [0, max_months]와 겹치면 통과.
    - start_age_months 있으면 그 값이 상한 이내인지로 판정(숫자 우선)
    - 숫자 미상이면 life_cycle / target / age_detail 키워드로 판정
    - 전부 미상 → 통과
    """
    start = item.get('start_age_months')
    end = item.get('end_age_months')
    if isinstance(start, (int, float)):
        return start <= max_months          # 최소 연령이 상한 이내 → 겹침
    if isinstance(end, (int, float)):
        return end >= 0                      # 시작 미상이나 종료 있음 → 0부터로 간주(겹침)

    lifecycles = _to_set(item.get('life_cycle'))
    if lifecycles & _YOUTH_LIFECYCLE:
        return True

    text = f"{item.get('target', '')} {item.get('age_detail', '')}"
    has_youth = any(k in text for k in _YOUTH_KEYWORDS)
    if has_youth:
        return True
    # life_cycle이 성인계열만 있으면 제외
    if lifecycles and lifecycles <= _ADULT_LIFECYCLE:
        return False
    if any(k in text for k in _NON_YOUTH_KEYWORDS):
        return False
    return True  # 미상 → 통과


def load_config() -> Dict[str, Any]:
    """env 기반 필터 설정."""
    return {
        'enabled': os.getenv('RELEVANCE_FILTER_ENABLED', '1') != '0',
        'target_region': os.getenv('TARGET_REGION', '도봉'),
        'max_months': utils.safe_config_int('TARGET_AGE_MAX_MONTHS', 108, 0, 240),
    }


def passes_post_ai(item: Dict[str, Any], cfg: Dict[str, Any] = None) -> Tuple[bool, str]:
    """AI 분류 후 지역+연령 정밀 필터. (통과여부, 사유) 반환.

    item에 region_match / age_match 플래그도 기록(투명성·디버깅용).
    """
    cfg = cfg or load_config()
    r_ok = region_ok(item, cfg['target_region'])
    a_ok = age_ok(item, cfg['max_months'])
    item['region_match'] = r_ok
    item['age_match'] = a_ok
    if not cfg['enabled']:
        return True, ''
    if not r_ok:
        return False, f"지역 불일치(residence={item.get('residence')})"
    if not a_ok:
        return False, f"연령 범위 밖(target={item.get('target')}, start={item.get('start_age_months')})"
    return True, ''


# ── 의미 게이트(②)용 기준 프로파일 ───────────────────────────────────────────
# 제목이 등록 키워드와 정확히 안 맞아도, 이 프로파일과 의미가 가까우면 통과시킨다.
# 임계값(0.83)은 이 문장의 정확한 표현으로 실측 캘리브레이션된 값이라, 문구가 조금만
# 바뀌어도 임베딩 벡터가 흔들려 경계 근처 항목들의 통과/탈락이 뒤집힐 수 있다. 그래서
# 기본 설정(도봉/108개월)일 때는 반드시 기존 문장과 글자 단위로 동일하게 조립하고,
# 사용자가 값을 바꿨을 때만 새 문구를 생성한다(그 경우엔 어차피 새 기준으로 재판정하는
# 것이 맞으므로 "저하"가 아니라 "의도된 변경").


def _region_label(region: str) -> str:
    """지역명을 프로파일 문구용으로 다듬는다. 기본값('도봉')이면 기존 문구와 동일하게 '도봉구'."""
    region = (region or '').strip()
    if not region or region == '도봉':
        return '도봉구'
    return region


def _age_phrase(max_months) -> str:
    """연령 상한(개월)을 프로파일 문구로 변환. 기본값(108개월=만9세)이면 기존 문구 그대로 유지."""
    try:
        max_months = int(max_months)
    except (TypeError, ValueError):
        max_months = 108
    if max_months == 108:
        return '0~9세, 초등 저학년 포함'
    return f'0~{max_months // 12}세'


def build_profile_text(cfg: Dict[str, Any] = None) -> str:
    """현재 설정으로 의미 게이트 기준 문장을 만든다.

    설정 화면은 실행 중인 프로세스 환경을 즉시 갱신하므로, 이 함수도 수집 시작 시점에
    호출해야 저장 직후의 지역·나이가 반영된다. RELEVANCE_PROFILE을 명시한 사용자는
    자동 생성 문장보다 그 값을 우선한다(빈 문자열도 기존 동작대로 존중).
    """
    override = os.getenv('RELEVANCE_PROFILE')
    if override is not None:
        return override
    cfg = cfg or load_config()
    return (
        f"{_region_label(cfg.get('target_region', '도봉'))} 영유아 및 아동"
        f"({_age_phrase(cfg.get('max_months', 108))}) 대상 복지·돌봄·보육·교육·"
        '양육 지원, 가족지원, 건강·의료, 바우처, 수당, 프로그램 및 행사 신청 안내'
    )


# 외부 확장 코드와 저장된 테스트의 호환성을 위한 최초 실행 시점 스냅샷.
# 실제 수집·프로필 생성에서는 반드시 build_profile_text()를 호출한다.
DEFAULT_PROFILE_TEXT = build_profile_text()
