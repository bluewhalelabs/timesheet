"""Pause windows: stretches of time that stay on the log but are not billed.

<home>/pauses.json is a JSON list of
  {"id": "p1", "client": "acme" | "*", "mode": "all" | "mine", "start": ISO,
   "end": ISO | null, "until": ISO | null, "created": ISO}
"*" means every client. mode "all" (the default when missing) is a full pause: nothing
in the window is billed. mode "mine" is "away": only the user's own time is left out,
agent time stays billable. A window ends at `end` when set, else at `until` (a planned
resume) when set, else it is open and runs to "now". The engine moves the time inside a
client's windows to that client's non-billable key; nothing here touches the event log.

The commands keep a client's windows (its own and the "*" ones) from overlapping; the
engine still merges them, in case the file was edited by hand.
"""
import copy
import json
import os
import re
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional, Tuple

from ats import config
from ats.events import parse_ts

__all__ = ["ALL", "MODES", "PauseError", "path", "load", "save", "start_of", "end_of",
           "mode_of", "windows_for", "active", "find", "conflicts", "overlaps", "pause",
           "open_window", "resume", "remove", "edit", "parse_duration", "parse_when",
           "local", "stamp"]

ALL = "*"
MODES = ("all", "mine")
_FILE = "pauses.json"
_ID = re.compile(r"\Ap([1-9][0-9]*)\Z")
_DURATION = re.compile(r"\A(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?\Z")
_CLOCK = re.compile(r"\A(\d{1,2}):(\d{2})\Z")
_DAY = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")


class PauseError(config.ConfigError):
    """pauses.json is unusable, or a requested change to it is not valid. A subclass of
    ConfigError so every CLI that bills stops (exit 2) instead of billing paused time."""


def path(home: Optional[str] = None) -> str:
    return os.path.join(home or config.home(), _FILE)


def stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _time(entry: dict, name: str, where: str, required: bool = False) -> Optional[datetime]:
    v = entry.get(name)
    if v is None and not required:
        return None
    try:
        return parse_ts(v)
    except ValueError:
        raise PauseError("%s: %s is not a timestamp: %r" % (where, name, v))


def _check(entry, where: str) -> None:
    if not isinstance(entry, dict):
        raise PauseError("%s: must be an object" % where)
    if not isinstance(entry.get("id"), str) or not _ID.match(entry["id"]):
        raise PauseError("%s: id must look like p1, p2, ..." % where)
    if not isinstance(entry.get("client"), str) or not entry["client"]:
        raise PauseError('%s: client must be a client key or "*"' % where)
    if "mode" in entry and (not isinstance(entry["mode"], str) or entry["mode"] not in MODES):
        raise PauseError('%s: mode must be "all" or "mine"' % where)
    start = _time(entry, "start", where, required=True)
    for name in ("end", "until"):
        t = _time(entry, name, where)
        if t is not None and t < start:
            raise PauseError("%s: %s is before start" % (where, name))


def load(home: Optional[str] = None) -> List[dict]:
    """The stored windows. A missing file is no pauses; anything unreadable or invalid
    raises PauseError (never ignored: ignoring it would bill paused time)."""
    p = path(home)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except ValueError as e:
        raise PauseError("invalid JSON in %s: %s" % (p, e))
    except OSError as e:
        raise PauseError("cannot read %s: %s" % (p, e.strerror or e))
    if not isinstance(data, list):
        raise PauseError("%s: must be a JSON list of pause windows" % p)
    seen = set()
    for i, entry in enumerate(data):
        _check(entry, "%s: entry %d" % (p, i + 1))
        if entry["id"] in seen:
            raise PauseError("%s: duplicate id %s" % (p, entry["id"]))
        seen.add(entry["id"])
    return data


def save(pauses: List[dict], home: Optional[str] = None) -> None:
    """Atomically write the list with mode 0o600."""
    p = path(home)
    d = os.path.dirname(os.path.abspath(p))
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pauses-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(pauses, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def start_of(p: dict) -> datetime:
    return parse_ts(p["start"])


def end_of(p: dict) -> Optional[datetime]:
    """The effective end: `end`, else `until`, else None (still open)."""
    for name in ("end", "until"):
        if p.get(name) is not None:
            return parse_ts(p[name])
    return None


def mode_of(p: dict) -> str:
    """"all" (a full pause; also when the field is missing) or "mine" (away)."""
    return p.get("mode", "all")


def _covers(p: dict, client: str) -> bool:
    return p["client"] == client or p["client"] == ALL


def windows_for(pauses: List[dict], client: str, now: datetime,
                mode: Optional[str] = None) -> List[Tuple[datetime, datetime]]:
    """This client's windows (its own and the "*" ones) as merged, sorted (start, end)
    pairs; an open window is closed at now. mode keeps only windows of that mode."""
    spans = []
    for p in pauses:
        if not _covers(p, client) or (mode is not None and mode_of(p) != mode):
            continue
        a, b = start_of(p), end_of(p)
        if b is None:
            b = now
        if b > a:
            spans.append((a, b))
    spans.sort()
    out = []  # type: List[Tuple[datetime, datetime]]
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _active(p: dict, now: datetime) -> bool:
    end = end_of(p)
    return start_of(p) <= now and (end is None or now < end)


def active(pauses: List[dict], client: str, now: datetime) -> Optional[dict]:
    """The window covering now for this client (its own or a "*" one); a full pause
    before an away window, then the earliest. With client "*": only a window that
    covers every client."""
    found = [p for p in pauses if _covers(p, client) and _active(p, now)]
    return min(found, key=lambda p: (mode_of(p) != "all", start_of(p))) if found else None


def find(pauses: List[dict], pause_id: str) -> dict:
    for p in pauses:
        if p["id"] == pause_id:
            return p
    raise PauseError("no pause with id %s" % pause_id)


def conflicts(pauses: List[dict], entry: dict) -> List[dict]:
    """The other windows that cover a client `entry` covers and overlap it in time. An
    open window has no end; windows that only touch, and empty ones, do not overlap."""
    a, b = start_of(entry), end_of(entry)
    if b is not None and b <= a:
        return []
    out = []
    for p in pauses:
        if p is entry or p["id"] == entry["id"]:
            continue
        if p["client"] != entry["client"] and ALL not in (p["client"], entry["client"]):
            continue
        c, d = start_of(p), end_of(p)
        if d is not None and d <= c:
            continue
        if (d is None or a < d) and (b is None or c < b):
            out.append(p)
    return out


def overlaps(pauses: List[dict]) -> List[Tuple[str, str]]:
    """Every pair of ids whose windows overlap for some client."""
    out = []
    for i, p in enumerate(pauses):
        later = {id(q) for q in pauses[i + 1:]}
        out += [(p["id"], q["id"]) for q in conflicts(pauses, p) if id(q) in later]
    return out


def _kind(p: dict) -> str:
    return "away" if mode_of(p) == "mine" else "pause"


def _refuse_overlap(pauses: List[dict], entry: dict) -> None:
    bad = conflicts(pauses, entry)
    if bad:
        raise PauseError("that would overlap %s (%s); change the times, or edit or remove "
                         "that window first (timesheet pauses)" % (
                             ", ".join("%s %s" % (_kind(p), p["id"]) for p in bad),
                             "; ".join("%s from %s to %s" % (
                                 p["id"], p["start"], p.get("end") or p.get("until") or "now")
                                 for p in bad)))


def _next_id(pauses: List[dict]) -> str:
    return "p%d" % (1 + max([int(_ID.match(p["id"]).group(1)) for p in pauses] or [0]))


def _new(pauses, client, start, until, now, mode="all") -> dict:
    entry = {"id": _next_id(pauses), "client": client, "mode": mode, "start": stamp(start),
             "end": None, "until": stamp(until) if until is not None else None,
             "created": stamp(now)}
    pauses.append(entry)
    return entry


def _end(pauses, p, at, now, others) -> List[dict]:
    """End p at `at` (not before its own start) and open equal windows from there for
    the clients in others. Returns the new windows."""
    end = max(at, start_of(p))
    until = parse_ts(p["until"]) if p.get("until") is not None else None
    p["end"] = stamp(end)
    return [_new(pauses, c, end, until, now, mode_of(p)) for c in others]


def _check_new(mode, start, until, now) -> None:
    if mode not in MODES:
        raise PauseError('mode must be "all" or "mine", not %r' % (mode,))
    if start > now:
        raise PauseError("a pause cannot start in the future")
    if until is not None and until <= start:
        raise PauseError("the pause would end before it starts")


def pause(pauses: List[dict], client: str, start: datetime, until: Optional[datetime],
          now: datetime, mode: str = "all") -> dict:
    """Append a window for client (or "*") and return it, without looking at the other
    windows (open_window does). The caller saves."""
    _check_new(mode, start, until, now)
    return _new(pauses, client, start, until, now, mode)


def open_window(pauses: List[dict], client: str, mode: str, start: datetime,
                until: Optional[datetime], now: datetime,
                clients: List[str]) -> Tuple[List[dict], List[tuple]]:
    """Open a window for client (or "*") so that no client ends up under two windows.

    Returns (the new list, events); the given list is not changed and the caller saves.
    Events, in order, are (what, window, re-opened windows):
      "already": a window that is as strong or stronger covers the request; no change
      "ended":   a weaker or equal window was closed at start (a "*" one is re-opened
                 from there for the clients the request does not cover)
      "left":    a client's own window stays as it is
      "opened":  a new window
    A full pause ("all") is stronger than away ("mine"). Away for every client while
    some client is fully paused opens one away window per other uncovered client instead
    of a "*" window. A window that is already over (until not after now) changes no
    active window. Raises PauseError when a new window would overlap another one."""
    _check_new(mode, start, until, now)
    work = copy.deepcopy(pauses)
    events = []  # type: List[tuple]

    def covers_request(p):
        return mode_of(p) == "all" or mode == "mine"

    def first(found):
        return min(found, key=lambda p: (mode_of(p) != "all", start_of(p)))

    live = [p for p in work if _active(p, now)]
    wanted = []
    if until is not None and until <= now:
        wanted = [client]
    elif client != ALL:
        mine = [p for p in live if _covers(p, client)]
        strong = [p for p in mine if covers_request(p)]
        if strong:
            return pauses, [("already", first(strong), [])]
        for p in mine:
            others = [c for c in clients if c != client] if p["client"] == ALL else []
            events.append(("ended", p, _end(work, p, start, now, others)))
        wanted = [client]
    else:
        stars = [p for p in live if p["client"] == ALL]
        strong = [p for p in stars if covers_request(p)]
        if strong:
            return pauses, [("already", first(strong), [])]
        for p in stars:
            events.append(("ended", p, _end(work, p, start, now, [])))
        own = [p for p in live if p["client"] != ALL]
        if any(mode_of(p) == "all" for p in own) and mode == "mine":
            events += [("left", p, []) for p in own]
            covered = {p["client"] for p in own}
            wanted = [c for c in clients if c not in covered]
        else:
            events += [("ended", p, _end(work, p, start, now, [])) for p in own]
            wanted = [ALL]
    for c in wanted:
        entry = _new(work, c, start, until, now, mode)
        _refuse_overlap(work, entry)
        events.append(("opened", entry, []))
    return work, events


def resume(pauses: List[dict], client: str, at: datetime, now: datetime,
           clients: List[str]) -> Tuple[List[dict], List[dict]]:
    """End the windows active at now for client at `at`. client "*" ends every active
    window. A "*" window resumed for one client is ended and re-opened from `at` for the
    other clients. Returns (closed windows, re-opened windows); the caller saves."""
    if at > now:
        raise PauseError("cannot resume in the future")
    if client == ALL:
        hits = [p for p in pauses if _active(p, now)]
    else:
        hits = [p for p in pauses if _covers(p, client) and _active(p, now)]
    for p in hits:
        if at < start_of(p):
            raise PauseError("cannot resume before %s %s started (%s)"
                             % (_kind(p), p["id"], p["start"]))
    reopened = []
    for p in hits:
        split = p["client"] == ALL and client != ALL
        reopened += _end(pauses, p, at, now, [c for c in clients if c != client] if split else [])
    return hits, reopened


def remove(pauses: List[dict], pause_id: str) -> dict:
    entry = find(pauses, pause_id)
    pauses.remove(entry)
    return entry


def edit(pauses: List[dict], pause_id: str, start: Optional[datetime],
         end: Optional[datetime], now: datetime, mode: Optional[str] = None) -> Tuple[dict, dict]:
    """Set a window's start, end and/or mode. Returns (before, after); the caller saves.
    Nothing is changed when the result would be invalid or overlap another window."""
    entry = find(pauses, pause_id)
    before = dict(entry)
    if start is None and end is None and mode is None:
        raise PauseError("nothing to change: pass --start, --end and/or --mode")
    if mode is not None and mode not in MODES:
        raise PauseError('mode must be "all" or "mine", not %r' % (mode,))
    new_start = start if start is not None else start_of(entry)
    new_end = end if end is not None else end_of(entry)
    if new_start > now or (end is not None and end > now):
        raise PauseError("a pause cannot be edited to a time in the future")
    if new_end is not None and new_start >= new_end:
        raise PauseError("the start must be before the end")
    after = dict(entry)
    if start is not None:
        after["start"] = stamp(start)
    if end is not None:
        after["end"] = stamp(end)
    if mode is not None:
        after["mode"] = mode
    _refuse_overlap(pauses, after)
    entry.update(after)
    return before, entry


# ---------------------------------------------------------------- user input

def parse_duration(text: str) -> timedelta:
    """'90m', '2h', '1h30m', '1d' -> timedelta."""
    m = _DURATION.match(text.strip().lower()) if isinstance(text, str) else None
    if not m or not any(m.groups()):
        raise PauseError("invalid duration %r (use e.g. 90m, 2h, 1h30m or 1d)" % (text,))
    d, h, mins = (int(g or 0) for g in m.groups())
    out = timedelta(days=d, hours=h, minutes=mins)
    if not out:
        raise PauseError("the duration must be longer than zero: %r" % (text,))
    return out


def parse_when(text: str, tz, now: datetime, roll_forward: bool = False) -> datetime:
    """HH:MM (today in tz; with roll_forward a time already past means tomorrow),
    YYYY-MM-DD (the start of that day in tz) or a full ISO timestamp (no offset: tz)."""
    text = text.strip() if isinstance(text, str) else ""
    today = now.astimezone(tz).date()
    m = _CLOCK.match(text)
    try:
        if m:
            t = datetime.combine(today, time(int(m.group(1)), int(m.group(2)))).replace(tzinfo=tz)
            if roll_forward and t <= now:
                t = datetime.combine(today + timedelta(days=1), t.time()).replace(tzinfo=tz)
        elif _DAY.match(text):
            t = datetime.combine(date.fromisoformat(text), time.min).replace(tzinfo=tz)
        else:
            raw = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
            if "T" not in raw.upper() and " " not in raw:
                raise ValueError(text)
            t = datetime.fromisoformat(raw)
            if t.tzinfo is None:
                t = t.replace(tzinfo=tz)
        return t.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise PauseError("invalid time %r (use HH:MM, YYYY-MM-DD or an ISO timestamp)" % (text,))


def local(t: datetime, tz) -> str:
    """'2026-11-12 14:30 PST'"""
    return t.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
