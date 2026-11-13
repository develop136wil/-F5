"""Separate local collection readiness from the later AI analysis readiness."""


def collection_readiness(*, selection_reason='', shutting_down=False):
    if shutting_down:
        return False, 'SHUTTING_DOWN', '프로그램을 종료하는 중입니다.'
    if selection_reason:
        return False, 'CONDITION_NEEDS_ATTENTION', selection_reason
    return True, '', ''

def readiness(*, ai_required, key_ready, threshold_ready, settings_ready,
              selection_reason='', shutting_down=False, direct_analysis=False):
    if shutting_down:
        return False, 'SHUTTING_DOWN', '프로그램을 종료하는 중입니다.'
    if selection_reason:
        return False, 'CONDITION_NEEDS_ATTENTION', selection_reason
    if not ai_required:
        return True, '', ''
    if not key_ready:
        return False, 'AI_CONNECTION_REQUIRED', 'Google AI API 키를 확인해주세요.'
    if not threshold_ready and not direct_analysis:
        return False, 'QUALITY_VALIDATION_REQUIRED', '대상별 수집의 검색 품질 검증이 필요합니다. 조건은 저장되었지만 아직 실행할 수 없습니다. API 키를 다시 입력할 필요는 없습니다.'
    if not settings_ready:
        return False, 'ANALYSIS_SETTINGS_INVALID', '의미 비교 설정을 확인해주세요.'
    return True, '', ''
