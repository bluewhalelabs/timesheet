"""Rollup: cut segments to a period, split them at local midnights, sum them, and
group one client's time into work sessions."""
from collections import namedtuple
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Iterator, List, Optional, Tuple

from ats.engine import NON_BILLABLE, Key, Seg, Turn

__all__ = ["WorkSession", "billable", "clip", "split_days", "totals", "work_sessions"]

WorkSession = namedtuple("WorkSession", "key start end you agent by_project threads days")


def billable(segs: List[Seg], client: Optional[str] = None,
             project: Optional[str] = None) -> List[Seg]:
    """Drop NON_BILLABLE keys; keep only the given client / project when set."""
    out = []
    for s in segs:
        c, p = s.key
        if p == NON_BILLABLE:
            continue
        if client is not None and c != client:
            continue
        if project is not None and p != project:
            continue
        out.append(s)
    return out


def clip(segs: List[Seg], start: Optional[datetime], end: Optional[datetime]) -> List[Seg]:
    """Trim segments to [start, end]; None is unbounded. Empty results are dropped."""
    out = []
    for s in segs:
        a = s.start if start is None else max(s.start, start)
        b = s.end if end is None else min(s.end, end)
        if b > a:
            out.append(s._replace(start=a, end=b))
    return out


def split_days(a: datetime, b: datetime, tz) -> Iterator[Tuple[date, datetime, datetime]]:
    """Cut [a, b) at local midnights of tz. Yields (local_date, piece_start, piece_end),
    pieces as aware UTC datetimes. Midnights come from calendar-date arithmetic, so
    23- and 25-hour days are handled."""
    cursor = a.astimezone(timezone.utc)
    stop = b.astimezone(timezone.utc)
    while cursor < stop:
        local_date = cursor.astimezone(tz).date()
        midnight = datetime.combine(local_date + timedelta(days=1), time.min).replace(tzinfo=tz)
        piece_end = min(midnight.astimezone(timezone.utc), stop)
        yield local_date, cursor, piece_end
        cursor = piece_end


def _seconds(a: datetime, b: datetime, weight: float) -> float:
    return (b.astimezone(timezone.utc) - a.astimezone(timezone.utc)).total_seconds() * weight


def totals(segs: List[Seg], tz, by: str) -> Dict[Tuple[str, Key], Dict[str, float]]:
    """Sum credited seconds per (bucket, key). by: "project" (bucket ""), "day"
    (YYYY-MM-DD local date) or "week" (ISO YYYY-Www of the local date)."""
    if by not in ("project", "day", "week"):
        raise ValueError("by must be 'project', 'day' or 'week', got %r" % (by,))
    out = {}  # type: Dict[Tuple[str, Key], Dict[str, float]]
    for s in segs:
        for d, a, b in split_days(s.start, s.end, tz):
            if by == "day":
                bucket = d.isoformat()
            elif by == "week":
                y, w, _ = d.isocalendar()
                bucket = "%d-W%02d" % (y, w)
            else:
                bucket = ""
            cell = out.setdefault((bucket, s.key), {"agent": 0.0, "you": 0.0})
            cell[s.kind] += _seconds(a, b, s.weight)
    return out


def work_sessions(segs: List[Seg], turns: List[Turn], client: str,
                  session_break: timedelta, tz) -> List[WorkSession]:
    """Merge one client's segments into sessions: a gap of at most session_break joins."""
    mine = sorted((s for s in segs if s.key[0] == client and s.key[1] != NON_BILLABLE),
                  key=lambda s: (s.start, s.end))
    groups = []  # type: List[list]
    cur_end = None
    for s in mine:
        if groups and s.start - cur_end <= session_break:
            groups[-1].append(s)
            cur_end = max(cur_end, s.end)
        else:
            groups.append([s])
            cur_end = s.end

    my_turns = [t for t in turns if t.key[0] == client and t.key[1] != NON_BILLABLE]
    out = []
    for g in groups:
        start = g[0].start
        end = max(s.end for s in g)
        you = agent = 0.0
        by_project = {}  # type: Dict[str, Dict[str, float]]
        for s in g:
            n = _seconds(s.start, s.end, s.weight)
            cell = by_project.setdefault(s.key[1], {"you": 0.0, "agent": 0.0})
            cell[s.kind] += n
            if s.kind == "you":
                you += n
            else:
                agent += n
        threads = sorted({t.session for t in my_turns if t.start < end and t.end > start})
        days = sorted({d for d, _, _ in split_days(start, end, tz)})
        key = "S" + start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out.append(WorkSession(key, start, end, you, agent, by_project, threads, days))
    return out
