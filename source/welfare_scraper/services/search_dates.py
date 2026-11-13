"""One Korean calendar-day contract for keyword and semantic deadline filters."""
from datetime import date, datetime, timedelta, timezone


KST = timezone(timedelta(hours=9))


def today_kst(moment=None):
    moment = datetime.now(timezone.utc) if moment is None else moment
    if moment.tzinfo is None:
        raise ValueError('An aware timestamp is required')
    return moment.astimezone(KST).date().isoformat()


def deadline_filter(*, active, as_of_date=None):
    """Return trusted SQL and its bound date; unknown dates are not closed.

    A hybrid request supplies the same day to both branches, including requests
    spanning midnight. Older empty/whitespace deadlines behave like NULL.
    """
    day = today_kst() if as_of_date is None else as_of_date
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError('An ISO calendar date is required')
    if active:
        return "(NULLIF(TRIM(deadline), '') IS NULL OR deadline >= ?)", [day]
    return "(NULLIF(TRIM(deadline), '') IS NOT NULL AND deadline < ?)", [day]
