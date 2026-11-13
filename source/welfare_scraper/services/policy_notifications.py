"""Count-only completion messages; never transmit source/review text."""


def policy_result_message(result, *, audit=False, candidates=None):
    result = result if isinstance(result, dict) else {}

    def count(key):
        value = result.get(key, 0)
        return value if type(value) is int and value >= 0 else 0

    failed, held = count('ai_failed'), count('review_count')
    notes = result.get('review_notes')
    if not held and isinstance(notes, list):
        held = len(notes)
    attention = bool(failed or held or count('ai_preflight_held') or result.get('review_storage_error'))
    paused = bool(result.get('pause_reason'))
    state = ('확인 필요' if result.get('ok') is not True else '잠시 멈춤' if paused
             else '일부 완료' if attention else '완료')
    title = '🔍 정책 최초 감사' if audit else '🔄 정책 최신화 점검'
    details = [f"점검 {count('checked')}건", f"변경 제안 {count('changed')}건"]
    if type(candidates) is int and candidates >= 0:
        details.append(f'신규 후보 {candidates}건')
    if failed:
        details.append(f'AI 분석 미완료 {failed}건')
    if held:
        details.append(f'검토 보류 {held}건')
    message = f'*{title} {state}*\n• ' + ' · '.join(details)
    if state != '완료':
        message += '\n완료하지 못한 작업이 있습니다. 앱에서 사유를 확인해주세요. 저장된 결과는 유지됩니다.'
    return message + '\nNotion에는 자동으로 반영하지 않았습니다.'
