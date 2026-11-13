"""Conservative publication-date selection for News only; no AI or I/O.

Dates labelled as application deadlines, events or crawl times are never treated
as publication dates. Unknown dates remain candidates, not silently discarded.
"""
from datetime import date, datetime, timedelta, timezone
import re


PERIODS = frozenset({'7', '30', '90', 'custom'})
PUBLICATION_LABEL = r'(?:게시(?:일|날짜)|등록(?:일|날짜)|작성(?:일|날짜)|보도일|발행일|게재일|date\s*published|published(?:\s*(?:date|on))?)'
NON_PUBLICATION_LABEL = r'(?:신청|접수|모집|마감|행사|교육|운영|수집|수정|변경|업데이트|deadline|updated|modified)'
_DATE = re.compile(r'(?<!\d)(20\d{2})\s*(?:[-./]|년)\s*(\d{1,2})\s*(?:[-./]|월)\s*(\d{1,2})(?:\s*일)?(?!\d)')


def parse_date(value):
    if not isinstance(value, str):
        return None
    match = _DATE.search(value[:300])
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None


def valid_iso(value):
    return isinstance(value, str) and bool(re.fullmatch(r'20\d{2}-\d{2}-\d{2}', value)) and parse_date(value) == value


def validate_settings(period, start, end):
    if not isinstance(period, str) or period not in PERIODS:
        raise ValueError('게시 기간은 7일·30일·90일 또는 직접 지정으로 선택해주세요.')
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError('게시 기간 날짜는 YYYY-MM-DD로 입력해주세요.')
    if any(value and not valid_iso(value) for value in (start, end)):
        raise ValueError('게시 기간 날짜는 YYYY-MM-DD로 입력해주세요.')
    if period == 'custom' and (not start or not end or start > end):
        raise ValueError('게시 기간의 시작일과 종료일을 올바르게 입력해주세요.')


def freeze_window(profile, *, today=None):
    validate_settings(profile.publication_period, profile.publication_start_date,
                      profile.publication_end_date)
    today = today or datetime.now(timezone(timedelta(hours=9))).date()
    if profile.publication_period == 'custom':
        start, end = profile.publication_start_date, profile.publication_end_date
    else:
        start = (today - timedelta(days=int(profile.publication_period) - 1)).isoformat()
        end = today.isoformat()
    return {'start_date': start, 'end_date': end, 'period': profile.publication_period}


def window_from_snapshot(snapshot):
    """Old immutable runs have no window; do not reinterpret them on resume."""
    value = snapshot.get('publication_window')
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'start_date', 'end_date', 'period'}
            or value['period'] not in PERIODS
            or not valid_iso(value['start_date']) or not valid_iso(value['end_date'])
            or value['start_date'] > value['end_date']):
        raise ValueError('Invalid captured publication window')
    return value


def outside_window(published, window):
    return bool(window and published and not window['start_date'] <= published <= window['end_date'])


def from_labelled_text(text):
    """Accept independent publication metadata, never a label inside another field.

    A form's 작성일 or course's 등록일 is not the article's publication date.
    Ambiguous/conflicting metadata stays unknown rather than excluding a post.
    """
    if not isinstance(text, str):
        return None
    lines = text[:12000].splitlines()
    found_dates = set()

    def independent_prefix(prefix):
        # HTML metadata may be separated by a bar/bullet or consecutive dates.
        # Words such as '신청서 ', '교육 ', '재' before the label are not separators.
        segment = re.split(r'[|｜;·•]', prefix)[-1].strip()
        if not segment or segment in ('-', '*'):
            return True
        previous = list(_DATE.finditer(segment))
        return bool(previous and not segment[previous[-1].end():].strip(' .\t'))

    def form_heading(index, label):
        if index == 0 or not re.match(r'(?:등록|작성)', label):
            return False
        # Extracted HTML/PDF often inserts blank lines between a form heading
        # and its fields. Whitespace cannot turn a form date into publication
        # metadata; stop at the nearest actual text rather than scanning past it.
        previous = index - 1
        while previous >= 0 and not lines[previous].strip():
            previous -= 1
        if previous < 0:
            return False
        return bool(re.fullmatch(
            r'\s*(?:신청서|참가\s*신청서|입학\s*원서|교육|수강|접수|재등록)\s*[:：]?\s*',
            lines[previous], re.I))

    inline = re.compile(r'(?<!\w)(' + PUBLICATION_LABEL + r')\s*[:：]?\s*(' + _DATE.pattern + ')', re.I)
    standalone = re.compile(r'(?<!\w)(' + PUBLICATION_LABEL + r')\s*[:：]?\s*$', re.I)
    for index, line in enumerate(lines):
        for match in inline.finditer(line):
            if independent_prefix(line[:match.start()]) and not form_heading(index, match.group(1)):
                found = parse_date(match.group(2))
                if found:
                    found_dates.add(found)
        # A value on the next line must start with its date, not contain a later
        # application/deadline field whose date happens to be nearby.
        label = standalone.search(line)
        if (label and independent_prefix(line[:label.start()]) and index + 1 < len(lines)
                and not form_heading(index, label.group(1))):
            right = lines[index + 1].strip()
            if _DATE.match(right) and not re.search(NON_PUBLICATION_LABEL, right, re.I):
                found = parse_date(right)
                if found:
                    found_dates.add(found)
    return next(iter(found_dates)) if len(found_dates) == 1 else None


def from_list_item(item):
    # Only the locally marked publication field; generic `date` can be a deadline.
    value = item.get('published_date')
    return value if valid_iso(value) else None


def attach(candidate, published):
    candidate['published_date'] = published
    candidate['publication_status'] = 'known' if published else 'unknown'
    return candidate


def analysis_payload(payload):
    """Remove known display-only metadata; unknown keys still fail AI validation."""
    return {key: value for key, value in payload.items()
            if key not in ('published_date', 'publication_status', 'selection_reason')}
