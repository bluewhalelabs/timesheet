"""Billing periods: pure date arithmetic from a client's billing_period spec."""
import calendar
import re
from collections import namedtuple
from datetime import date, datetime, timedelta
from typing import Optional, Tuple

Period = namedtuple("Period", ["start", "end", "id"])

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_ID_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})$")
_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _make(start: date, end: date) -> Period:
    return Period(start, end, "{}_{}".format(start, end))


def containing(day: date, spec: dict) -> Period:
    kind = spec.get("type")
    one = timedelta(days=1)
    if kind == "weekly":
        name = spec.get("week_starts", "monday")
        if name not in _WEEKDAYS:
            raise ValueError("unknown week_starts: {!r}".format(name))
        start = day - timedelta(days=(day.weekday() - _WEEKDAYS.index(name)) % 7)
        return _make(start, start + timedelta(days=6))
    if kind == "biweekly":
        anchor = spec.get("anchor")
        if not anchor:
            raise ValueError("biweekly billing period needs an anchor date (YYYY-MM-DD)")
        try:
            a = date.fromisoformat(anchor)
        except (ValueError, TypeError):
            raise ValueError("invalid biweekly anchor {!r}: it must be a date as "
                             "YYYY-MM-DD".format(anchor))
        start = a + timedelta(days=14 * ((day - a).days // 14))
        return _make(start, start + timedelta(days=13))
    if kind == "semimonthly":
        last = calendar.monthrange(day.year, day.month)[1]
        if day.day <= 15:
            return _make(day.replace(day=1), day.replace(day=15))
        return _make(day.replace(day=16), day.replace(day=last))
    if kind == "monthly":
        last = calendar.monthrange(day.year, day.month)[1]
        return _make(day.replace(day=1), day.replace(day=last))
    raise ValueError("unknown billing period type: {!r}".format(kind))


def next_period(period: Period, spec: dict) -> Period:
    return containing(period.end + timedelta(days=1), spec)


def previous_period(period: Period, spec: dict) -> Period:
    return containing(period.start - timedelta(days=1), spec)


def last_complete(today: date, spec: dict) -> Period:
    return previous_period(containing(today, spec), spec)


def _unrecognised(arg: str) -> ValueError:
    return ValueError("unrecognised period: {!r} (use last, this, YYYY-MM-DD, "
                      "YYYY-Www or YYYY-MM-DD_YYYY-MM-DD)".format(arg))


def parse_id(text: str) -> Optional[Period]:
    """The period a START_END id (a draft folder name) stands for, or None."""
    m = _ID_RE.match(text or "")
    if not m:
        return None
    try:
        start, end = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
    except ValueError:
        return None
    return _make(start, end) if start <= end else None


def resolve(arg: Optional[str], spec: dict, today: date) -> Period:
    if arg is None or arg == "last":
        return last_complete(today, spec)
    if arg == "this":
        return containing(today, spec)
    m = _ID_RE.match(arg)
    if m:
        try:
            start, end = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
        except ValueError:
            raise _unrecognised(arg)
        if start > end:
            raise ValueError("period start {} is after end {}".format(start, end))
        return _make(start, end)
    m = _WEEK_RE.match(arg)
    if m:
        try:
            monday = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
        except ValueError:
            raise _unrecognised(arg)
        return containing(monday, spec)
    try:
        day = date.fromisoformat(arg)
    except ValueError:
        raise _unrecognised(arg)
    return containing(day, spec)


def bounds(period: Period, tz) -> Tuple[datetime, datetime]:
    after = period.end + timedelta(days=1)
    return (datetime(period.start.year, period.start.month, period.start.day, tzinfo=tz),
            datetime(after.year, after.month, after.day, tzinfo=tz))


def _fmt(d: date, year: bool) -> str:
    s = "{} {}".format(_MONTHS[d.month - 1], d.day)
    return "{}, {}".format(s, d.year) if year else s


def label(period: Period) -> str:
    s, e = period.start, period.end
    if s == e:
        return _fmt(s, True)
    if s.year == e.year:
        return "{} – {}".format(_fmt(s, False), _fmt(e, True))
    return "{} – {}".format(_fmt(s, True), _fmt(e, True))


def short_label(period: Period) -> str:
    """The label without the year, for one-line summaries: 'Nov 9 – Nov 15'."""
    s, e = period.start, period.end
    if s == e:
        return _fmt(s, False)
    return "{} – {}".format(_fmt(s, False), _fmt(e, False))
