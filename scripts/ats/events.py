"""Append-only event log: writing one line, reading the whole log back."""
import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple

from ats import config

__all__ = ["append_event", "parse_ts", "load_events", "load_manual", "import_legacy",
           "raw_objects", "reattribute", "unattributed_path"]


def _default_path() -> str:
    return os.path.join(config.home(), "events.jsonl")


def append_event(record: dict, path: Optional[str] = None) -> None:
    """Append one JSON line to the events file with a single os.write."""
    path = path or _default_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    data = (json.dumps(record) + "\n").encode("utf-8")
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        size = os.fstat(fd).st_size
        if size:
            os.lseek(fd, size - 1, os.SEEK_SET)
            if os.read(fd, 1) != b"\n":
                data = b"\n" + data
        os.write(fd, data)
    finally:
        os.close(fd)


def parse_ts(s: str) -> datetime:
    """ISO 8601 timestamp -> aware UTC datetime. A trailing Z means UTC; naive means UTC."""
    if not isinstance(s, str):
        raise ValueError("timestamp must be a string: %r" % (s,))
    text = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    t = datetime.fromisoformat(text)
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    try:
        return t.astimezone(timezone.utc)
    except OverflowError:
        raise ValueError("timestamp out of range: %r" % (s,))


def load_events(path: Optional[str] = None) -> Tuple[List[dict], int]:
    """All usable events sorted by time, each with "_t" added, plus the count of
    skipped lines (not JSON, no parseable ts, or no client). Blank lines are ignored.
    session and project are normalised to strings ("" and "(unknown)" when absent)."""
    path = path or _default_path()
    out, skipped = [], 0
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return [], 0
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if not isinstance(e, dict):
                    raise ValueError("not an object")
                client = e.get("client")
                if not isinstance(client, str) or not client:
                    raise ValueError("no client")
                e["_t"] = parse_ts(e.get("ts"))
                for field, missing in (("session", ""), ("project", "(unknown)")):
                    v = e.get(field)
                    e[field] = missing if v is None else v if isinstance(v, str) else str(v)
            except ValueError:
                skipped += 1
                continue
            out.append(e)
    out.sort(key=lambda e: e["_t"])
    return out, skipped


def _manual_path() -> str:
    return os.path.join(config.home(), "manual.jsonl")


def _manual_entry(e) -> dict:
    """Validate one decoded manual-entry object; raise ValueError when unusable."""
    if not isinstance(e, dict):
        raise ValueError("not an object")
    day = e.get("date")
    if not isinstance(day, str):
        raise ValueError("no date")
    d = date.fromisoformat(day)
    client = e.get("client")
    if not isinstance(client, str) or not client:
        raise ValueError("no client")
    project = e.get("project", "(root)")
    if not isinstance(project, str):
        raise ValueError("bad project")
    description = e.get("description", "")
    if not isinstance(description, str):
        raise ValueError("bad description")
    h = e.get("hours")
    if isinstance(h, bool) or not isinstance(h, (int, float)):
        raise ValueError("bad hours")
    try:
        hours = Decimal(str(h))
    except InvalidOperation:
        raise ValueError("bad hours")
    if not hours.is_finite() or hours <= 0:
        raise ValueError("hours must be > 0")
    return {"date": d, "client": client, "project": project or "(root)",
            "hours": hours, "description": description}


def load_manual(path: Optional[str] = None) -> Tuple[List[dict], int]:
    """Manual time entries from manual.jsonl plus the count of skipped lines (not
    JSON, or missing/invalid date, client or hours). Blank lines are ignored.
    Entries keep file order."""
    path = path or _manual_path()
    out, skipped = [], 0
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return [], 0
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(_manual_entry(json.loads(line)))
            except ValueError:
                skipped += 1
    return out, skipped


_LEGACY_KINDS = ("start", "end", "session_end")
_RECORD_ORDER = ("ts", "event", "hook", "session", "client", "project", "cwd",
                 "transcript_path")


def raw_objects(path: str):
    """Yield every JSON-object line of a file as read; non-objects and bad lines are
    yielded as None. Blank lines are not yielded. A missing file yields nothing."""
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                yield None
                continue
            yield e if isinstance(e, dict) else None


def _dedupe_key(e: dict, check_kind: bool):
    """(session, event, instant) for a raw event, or None when it has no usable ts
    (or, with check_kind, an unknown event kind)."""
    event = e.get("event")
    if not isinstance(event, str) or (check_kind and event not in _LEGACY_KINDS):
        return None
    try:
        t = parse_ts(e.get("ts"))
    except ValueError:
        return None
    session = e.get("session")
    return (session if isinstance(session, str) else "" if session is None else str(session),
            event, t)


def import_legacy(src: str, client: str, dest: Optional[str] = None,
                  dry_run: bool = False) -> dict:
    """Append events from an older log (no client field) to the destination log,
    stamped with client. Events whose (session, event, instant) already exist in the
    destination, or earlier in this run, are skipped as duplicates. Returns
    {"read", "imported", "duplicates", "skipped"}; with dry_run nothing is written."""
    dest = dest or _default_path()
    if os.path.exists(dest) and os.path.realpath(src) == os.path.realpath(dest):
        raise ValueError("source and destination are the same file: %s" % src)
    seen = set()
    for e in raw_objects(dest):
        if e is not None:
            k = _dedupe_key(e, False)
            if k is not None:
                seen.add(k)
    counts = {"read": 0, "imported": 0, "duplicates": 0, "skipped": 0}
    with open(src, encoding="utf-8", errors="replace") as f:
        pass  # raises FileNotFoundError / OSError early, before anything is counted
    for e in raw_objects(src):
        key = _dedupe_key(e, True) if e is not None else None
        if key is None:
            counts["read"] += 1
            counts["skipped"] += 1
            continue
        counts["read"] += 1
        if key in seen:
            counts["duplicates"] += 1
            continue
        seen.add(key)
        rec = {}
        for k in _RECORD_ORDER:
            if k == "client":
                rec[k] = client
            elif k == "transcript_path":
                rec[k] = e.get(k, "")
            elif k in e:
                rec[k] = e[k]
        for k, v in e.items():
            if k not in rec:
                rec[k] = v
        if not dry_run:
            append_event(rec, dest)
        counts["imported"] += 1
    return counts


def unattributed_path() -> str:
    """Where the hook keeps events logged while the config could not be read."""
    return os.path.join(config.home(), "unattributed.jsonl")


def reattribute(attribute, src: Optional[str] = None, dest: Optional[str] = None,
                dry_run: bool = False) -> dict:
    """Move parked events into the log. attribute(raw_event) -> (client, project) or None.
    Matched events are appended in the normal record shape unless their (session, event,
    instant) is already logged; unmatched or unreadable ones are dropped. Without
    dry_run the parked file is consumed (renamed away first, so an event the hook parks
    meanwhile starts a new file). Returns {"read", "attributed", "duplicates",
    "unmatched"}."""
    src = src or unattributed_path()
    dest = dest or _default_path()
    counts = {"read": 0, "attributed": 0, "duplicates": 0, "unmatched": 0}
    if not os.path.exists(src):
        return counts
    work = src
    if not dry_run:
        work = src + ".processing"
        if os.path.exists(work):
            raise ValueError("%s exists from an interrupted run; append it back to %s or "
                             "remove it, then try again" % (work, src))
        os.replace(src, work)
    try:
        seen = set()
        for e in raw_objects(dest):
            if e is not None:
                k = _dedupe_key(e, False)
                if k is not None:
                    seen.add(k)
        for e in raw_objects(work):
            counts["read"] += 1
            key = _dedupe_key(e, True) if e is not None else None
            found = attribute(e) if key is not None else None
            if found is None:
                counts["unmatched"] += 1
                continue
            if key in seen:
                counts["duplicates"] += 1
                continue
            seen.add(key)
            client, project = found
            rec = {"ts": e["ts"], "event": e["event"], "hook": e.get("hook", ""),
                   "session": e.get("session", ""), "client": client, "project": project,
                   "cwd": e.get("cwd", ""), "transcript_path": e.get("transcript_path", "")}
            if not dry_run:
                append_event(rec, dest)
            counts["attributed"] += 1
    except BaseException:
        if work != src:                     # put the parked events back
            with open(work, "rb") as f:
                data = f.read()
            fd = os.open(src, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            os.unlink(work)
        raise
    if work != src:
        os.unlink(work)
    return counts
