"""Conservative display timing for explicitly reviewed news, with no I/O.

Never infer dates from a title or restore a withheld AI value. The import path
projects only a code-owned reason; rejected values stay in the analysis record.
Legacy automatic event handling is unchanged unless it carries an explicit hold.
"""
import json

from services.news_deadline_grounding import deadline_calendar_day


DEADLINE_UNVERIFIED = 'deadline_unverified'
TIMING_NOTE = '대표 마감일과 신청 가능 여부를 확정하지 않았습니다. 회차별 일정과 접수 상태를 원문에서 확인해주세요.'
UNKNOWN_NOTE = '신청 가능 여부와 기간은 원문에서 확인해주세요.'


def _metadata(item):
    value = item.get('ai_data')
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def timing_review_note(item):
    """Return a static warning, never model metadata text or a rejected date.

    A review about age/applicants/attachments alone does not erase a valid date.
    The real manual_news SQL projection also covers old dateless imported rows.
    Marker strings (including 'false') do not authorize this manual-news branch.
    """
    ai = _metadata(item)
    if ai.get('news_timing_review') == DEADLINE_UNVERIFIED:
        return TIMING_NOTE
    if not (item.get('manual_news') in (True, 1)
            or type(ai.get('news_import_review')) is bool):
        return ''
    values = [item.get(key) for key in ('deadline', 'event_end_date', 'event_date')
              if item.get(key) is not None and item.get(key) != '']
    if not values:
        return UNKNOWN_NOTE
    for value in values:
        try:
            deadline_calendar_day(value)
        except ValueError:
            return UNKNOWN_NOTE
    return ''
