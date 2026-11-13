"""Literal application deadlines, separate from event and page metadata dates.

This parser only verifies a proposed value. A year may be shared by the two
endpoints of one explicit application period; it is never borrowed from a title
or an unrelated sentence. No missing clock or date is generated.
"""
from datetime import date, datetime
import re


_VALUE = re.compile(r'\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?')
_CLOCK = (r'(?:(오전|오후)\s*)?'
          r'(?:T?(\d{1,2}):(\d{2})(?![\d:])|'
          r'(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?)?')
_POINT = re.compile(
    r'(?<!\d)(\d{4})\s*[년./-]\s*(\d{1,2})\s*[월./-]\s*(\d{1,2})(?!\d)'
    r'\s*일?\.?\s*(?:\([월화수목금토일](?:요일)?\)\s*)?'
    + _CLOCK)
_SHORT_POINT = re.compile(
    r'(?<!\d)(\d{1,2})\s*[월./-]\s*(\d{1,2})(?!\d)'
    r'\s*일?\.?\s*(?:\([월화수목금토일](?:요일)?\)\s*)?'
    + _CLOCK)
_LABEL = re.compile(
    r'(?:신청(?:서)?|접수|모집)\s*(?:접수\s*)?'
    r'(?P<kind>기간|마감(?:일시|일)?|기한|종료(?:일시|일)?)?'
    r'\s*(?:은|는)?\s*[:：]?\s*')
_RANGE = re.compile(r'\s*(?:~|∼|～|–|—|-|부터)\s*')
_UNTIL = re.compile(r'\s*(?:까지|마감|종료)(?![가-힣])')
_NEGATIVE = re.compile(r'아님|아니|아닌|아닙|잘못|취소|변경\s*전|종전|기존\s*(?:의\s*)?마감')
_SHORT_NEGATIVE = re.compile(r'제외|기존|종전|변경\s*전|취소|정정|불가|금지|(?:하지|되지)\s*않')
_SHORT_ADJACENT_NEGATIVE = re.compile(
    r'\s*(?:기존|종전|변경\s*전|취소|아님|제외(?:\s*(?:대상|조건))?|'
    r'(?:(?:신청|접수|모집|참가|지원)\s*)?'
    r'(?:불가|금지|하지\s*않(?:음|습니다)|되지\s*않(?:음|습니다)))'
    r'\s*(?:입니다|합니다)?\s*[.:：]?\s*')
_ALTERNATIVE_APPLICATION = re.compile(
    r'(?:행사\s*당일|당일\s*현장|현장)\s*(?:에서도?\s*)?(?:신청|접수)(?:도)?\s*가능')
_RELATIVE_APPLICATION_DATE = re.compile(
    r'회차\s*별|(?:매|각)\s*(?:교육일|행사일|회차)|'
    r'(?:추후|별도)\s*(?:안내|공지|공고|접수)|미정|상시')
_EXTRA_APPLICATION = re.compile(
    r'(?:추가|별도|회차\s*별)\s*(?:신청|접수|모집)'
    r'\s*(?:기간|기한|마감(?:일시|일)?)?\s*(?:은|는)?\s*[:：]?\s*')
_NONCURRENT_CONTEXT = re.compile(r'변경\s*전|종전|기존|예시|가정|인용|취소|제외')
_RELATIVE_REJECTION = re.compile(
    r'아님|아니|아닌|아닙|불가|금지|(?:하지|되지)\s*않|없습니다|없음')
_CLOSED_APPLICATION = re.compile(
    r'(?:(?:현재|이미|지금)\s*)?'
    r'(?:(?:본|이번)\s*(?:행사|교육|프로그램)\s*(?:의\s*)?)?'
    r'(?:(?:모든|전체)\s*(?:회차(?:의)?\s*)?)?'
    r'(?:신청|접수|모집)(?:\s*(?:과|와|및)\s*대기)?'
    r'\s*(?:이|가|은|는)?\s*(?:이미\s*)?(?:조기\s*)?(?:마감|종료)'
    r'\s*(?:되었습니다|됐습니다|되었음|됨|완료|입니다|하였습니다|했습니다)')
_CLOSED_STATUS = re.compile(r'(?:신청|접수|모집)\s*(?:현황|상태)\s*[:：]\s*(?:마감|종료)')
_STATUS_LABEL = re.compile(r'(?:신청|접수|모집)\s*(?:현황|상태)\s*[:：]?')


def _application_override(content):
    """Recognize narrow, explicit reasons a date cannot mean one open route.

    This does not infer a closed status or a replacement deadline. Conditional
    '선착순 마감', date labels, unrelated closures and quoted examples are not
    assertions that application has ended. Ordinary lines remain separate.
    """
    lines = [line for line in content.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        text = re.sub(r'^[\s○●•※-]+', '', line).strip()
        previous = lines[index - 1] if index else ''
        following = lines[index + 1] if index + 1 < len(lines) else ''
        if (_SHORT_ADJACENT_NEGATIVE.fullmatch(previous)
                or _SHORT_ADJACENT_NEGATIVE.fullmatch(following)):
            continue
        if _STATUS_LABEL.fullmatch(text):
            after_value = lines[index + 2] if index + 2 < len(lines) else ''
            if _SHORT_ADJACENT_NEGATIVE.fullmatch(after_value):
                continue
            separator = ' ' if text.endswith((':', '：')) else ': '
            text += separator + following.strip()
        for clause in re.split(r'[!?;。]|(?<=[가-힣])\.\s*', text):
            clause = clause.strip()
            if _NONCURRENT_CONTEXT.search(clause):
                continue
            if _CLOSED_APPLICATION.fullmatch(clause) or _CLOSED_STATUS.fullmatch(clause):
                return True
            alternative = _ALTERNATIVE_APPLICATION.search(clause)
            if alternative and re.fullmatch(
                    r'\s*(?:합니다|입니다)?\s*', clause[alternative.end():]):
                return True
            extra = _EXTRA_APPLICATION.search(clause)
            if (extra and _RELATIVE_APPLICATION_DATE.search(clause[extra.end():])
                    and not _RELATIVE_REJECTION.search(clause)):
                return True
    return False


def _parse_value(value):
    if not isinstance(value, str) or _VALUE.fullmatch(value) is None:
        raise ValueError('An ISO date or local minute-precision deadline is required')
    day = date.fromisoformat(value[:10])
    if len(value) == 10:
        return day
    # fromisoformat accepts 24:00 on Python 3.14 and rolls to the next day.
    # Constructor validation must retain the literal source calendar day.
    return datetime(day.year, day.month, day.day, int(value[11:13]), int(value[14:16]))


def deadline_calendar_day(value):
    """Strict shared calendar day for grounded deadlines and read-only displays.

    Raises ValueError for unknown or malformed values. Never converts zones,
    accepts a rolled 24:00, or silently drops an unsupported seconds suffix.
    """
    parsed = _parse_value(value)
    return parsed.date() if isinstance(parsed, datetime) else parsed


def _point(text, offset, *, shared_year=None):
    match = (_POINT if shared_year is None else _SHORT_POINT).match(text, offset)
    if match is None:
        return None
    parts = match.groups() if shared_year is None else (str(shared_year), *match.groups())
    year, month, day, meridiem, hour, minute, korean_hour, korean_minute = parts
    tail = text[match.end():]
    if ((hour is None and korean_hour is None and (
            meridiem is not None or re.match(r'(?:T|오전|오후|\d{1,2}\s*(?::|시))', tail)))
            or ((hour is not None or korean_hour is not None) and re.match(r'[:\d]|\s*초', tail))):
        # The optional clock must not silently fall back to a date-only parse
        # when the source contains a malformed or unsupported clock suffix.
        return None
    if meridiem is not None and (re.search(r'(?:오전|오후)\s*T', match.group())
            or re.match(r'\s*(?:[\d:]|시|분|초|반|오전|오후|정오|자정|T|Z|'
                        r'[+]\d|-\d{2}:\d{2}|[aApP]\.?[mM]\.?)', tail)):
        # A new 12-hour form must not hide another clock, seconds, a half hour
        # or timezone. Do not turn unsupported suffixes into date-only proof.
        return None
    if shared_year is not None and re.match(r'\s*(?:[\d:]|시|분|초|T|Z|[+-]\d)', tail):
        # A shortened endpoint must not hide an unparsed year, clock, seconds,
        # or timezone after the optional minute-precision clock. In particular,
        # "9월 16시" is not September 16 with its day marker omitted.
        return None
    try:
        day_value = date(int(year), int(month), int(day))
        clock = None
        if hour is not None or korean_hour is not None:
            hour_value = int(hour or korean_hour)
            if meridiem is not None:
                if not 1 <= hour_value <= 12:
                    return None
                hour_value = hour_value % 12 + (12 if meridiem == '오후' else 0)
            clock = (hour_value, int(minute or korean_minute or 0))
            # Reject malformed source times as well as malformed model values.
            datetime(day_value.year, day_value.month, day_value.day, *clock)
    except ValueError:
        return None
    return day_value, clock, match.end()


def deadline_supported(value, content):
    """Accept only an explicit application endpoint in a supported local form.

    A clock must be attached to that exact endpoint. One explicit application
    period may share its first year with a later month/day endpoint, but never
    infer a year rollover. A plain date after '신청' needs an explicit '까지'.
    '신청일시' alone is deliberately not an application deadline: event portals
    populate that metadata with midnight values even without an application.
    """
    if not isinstance(content, str):
        return False
    try:
        expected = _parse_value(value)
    except ValueError:
        return False
    if _application_override(content):
        return False
    expected_day = expected.date() if isinstance(expected, datetime) else expected
    expected_clock = (expected.hour, expected.minute) if isinstance(expected, datetime) else None
    complete_match = False
    shortened_match = False
    endpoints = set()
    unresolved_application_date = False

    # Keep ordinary lines separate; only a standalone application label can
    # introduce the immediately following date line. Never globally join text.
    lines = content.splitlines()
    for index, line in enumerate(lines):
        candidate_line = line
        next_index = index + 1
        stripped = re.sub(r'^[\s○●•※\d.)-]+', '', line)
        if _LABEL.fullmatch(stripped) and index + 1 < len(lines):
            candidate_line += ' ' + lines[index + 1]
            next_index += 1
        if (next_index < len(lines) and re.match(
                r'\s*(?:아님|아니|아닌|아닙|잘못|취소)(?:\s|[.:：]|$)', lines[next_index])):
            # A visually wrapped rejection is still a rejection, not proof of
            # the preceding date. Do not repair it to a different deadline.
            continue
        # Do not split a period before a Korean weekday or inside a date.
        # A period before a new word still separates unrelated explanations.
        segments = re.split(r'[!?;。]|(?<=[가-힣])\.\s*', candidate_line)
        for segment in segments:
            if _NEGATIVE.search(segment):
                continue
            for label in _LABEL.finditer(segment):
                if (label.group('kind') is not None
                        and _RELATIVE_APPLICATION_DATE.search(segment[label.end():])
                        and not _NONCURRENT_CONTEXT.search(segment)
                        and not _RELATIVE_REJECTION.search(segment)
                        and not (index > 0 and _SHORT_ADJACENT_NEGATIVE.fullmatch(lines[index - 1]))
                        and not (next_index < len(lines)
                                 and _SHORT_ADJACENT_NEGATIVE.fullmatch(lines[next_index]))):
                    # A relative/per-session or explicitly undecided period is
                    # not made irrelevant by another fully printed endpoint.
                    unresolved_application_date = True
                first = _point(segment, label.end())
                if first is None:
                    date_tail = segment[label.end():]
                    if label.group('kind') is not None and (
                            re.match(r'\d', date_tail) or (
                            not date_tail.strip() and next_index < len(lines)
                            and re.match(r'\s*[:：]?\s*\d', lines[next_index]))):
                        # A second, abbreviated/invalid application date is not
                        # absent evidence. Do not choose the sole parsed period.
                        unresolved_application_date = True
                    continue
                endpoint = first
                shortened = False
                separator = _RANGE.match(segment, first[2])
                if separator:
                    endpoint = _point(segment, separator.end())
                    if endpoint is None and label.group('kind') == '기간':
                        endpoint = _point(segment, separator.end(), shared_year=first[0].year)
                        shortened = endpoint is not None
                    if shortened and (_NEGATIVE.search(candidate_line)
                            or _SHORT_NEGATIVE.search(candidate_line)
                            or (index > 0 and _SHORT_ADJACENT_NEGATIVE.fullmatch(lines[index - 1]))
                            or (next_index < len(lines)
                                and _SHORT_ADJACENT_NEGATIVE.fullmatch(lines[next_index]))):
                        continue
                    if endpoint is None or endpoint[0] < first[0]:
                        unresolved_application_date = True
                        continue
                    if (endpoint[0] == first[0] and first[1] is not None
                            and endpoint[1] is not None and endpoint[1] < first[1]):
                        unresolved_application_date = True
                        continue
                elif label.group('kind') == '기간' or (
                        label.group('kind') is None and not _UNTIL.match(segment, first[2])):
                    continue
                # A separate event clause must not lend its clock to a date
                # merely because the publisher placed them on one line.
                if re.match(r'\s*(?:에\s*)?(?:행사|교육|개최|발표|문의|상담|운영)', segment[endpoint[2]:]):
                    continue
                endpoints.add(endpoint[:2])
                if endpoint[0] == expected_day and (
                        expected_clock is None or endpoint[1] == expected_clock):
                    if shortened:
                        shortened_match = True
                    else:
                        complete_match = True
    # A printed year does not resolve which session or route a date belongs to.
    # Repeated identical endpoints are harmless; competing dates/clock precision
    # and any unresolved application period require review, never min/max choice.
    return ((complete_match or shortened_match) and len(endpoints) == 1
            and not unresolved_application_date)
