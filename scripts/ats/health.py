"""Shared health warnings: the hook error log, unreadable-line counts, events and
manual entries for clients no longer in the config, and turns that never closed."""
import os
from datetime import timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Iterable, List, Optional, Tuple

from ats import config, engine, rollup

__all__ = ["hook_errors", "hook_error_warning", "skipped_warning", "unknown_clients",
           "unknown_client_warnings", "unclosed_turns", "unclosed_warning",
           "unattributed_count", "unattributed_warning", "paused_time", "paused_hours",
           "paused_summary", "paused_warning"]


def hook_errors() -> Tuple[int, str]:
    """(count of non-empty lines, last such line) of <home>/hook-errors.log."""
    try:
        with open(os.path.join(config.home(), "hook-errors.log"),
                  encoding="utf-8", errors="replace") as f:
            logged = [l.rstrip("\n") for l in f if l.strip()]
    except OSError:
        return 0, ""
    return (len(logged), logged[-1]) if logged else (0, "")


def hook_error_warning(include_last: bool) -> Optional[str]:
    """The warning text, or None when nothing is logged. The last line can name paths
    of other clients, so client-scoped output passes include_last=False."""
    n, last = hook_errors()
    if not n:
        return None
    if include_last:
        return "hook errors logged: %d; last: %s" % (n, last)
    return 'hook errors logged: %d (run "timesheet doctor" for details)' % n


def skipped_warning(n: int, filename: str) -> Optional[str]:
    if not n:
        return None
    return "%d unreadable line(s) skipped in %s" % (n, filename)


def unknown_clients(cfg: dict, items: Iterable[dict]) -> Dict[str, int]:
    """client key -> count, for events or manual entries whose client is not configured
    (in first-seen order)."""
    known = cfg.get("clients", {})
    out = {}  # type: Dict[str, int]
    for e in items:
        c = e.get("client")
        if c not in known:
            out[c] = out.get(c, 0) + 1
    return out


def unknown_client_warnings(cfg: dict, events: Iterable[dict],
                            manual: Iterable[dict]) -> List[str]:
    out = ['%d event(s) logged for unknown client "%s" (renamed or removed from config?)'
           % (n, k) for k, n in unknown_clients(cfg, events).items()]
    out += ['%d manual %s for unknown client "%s"' % (n, "entry" if n == 1 else "entries", k)
            for k, n in unknown_clients(cfg, manual).items()]
    return out


def unclosed_turns(tl, client: str, start, end, project: Optional[str] = None) -> list:
    """Dropped (never closed) turns of this client's billable projects that start in
    [start, end); None means unbounded."""
    out = []
    for t in tl.dropped_turns:
        c, proj = t.key
        if c != client or proj == engine.NON_BILLABLE:
            continue
        if project is not None and proj != project:
            continue
        if (start is not None and t.start < start) or (end is not None and t.start >= end):
            continue
        out.append(t)
    return out


def unclosed_warning(turns: list, cap_minutes, tz) -> Optional[str]:
    if not turns:
        return None
    days = sorted({t.start.astimezone(tz).date().isoformat() for t in turns})
    return ("%d turn(s) never closed (no Stop or SessionEnd logged) and were not counted: "
            "%s; they are counted, capped at %s min, once the session ends"
            % (len(turns), ", ".join(days), "%g" % cap_minutes))


def unattributed_count() -> int:
    """Non-empty lines in <home>/unattributed.jsonl (events parked by the hook)."""
    try:
        with open(os.path.join(config.home(), "unattributed.jsonl"),
                  encoding="utf-8", errors="replace") as f:
            return sum(1 for l in f if l.strip())
    except OSError:
        return 0


def unattributed_warning() -> Optional[str]:
    n = unattributed_count()
    if not n:
        return None
    return ('%d event(s) were logged while the config was unreadable; run '
            '"timesheet reattribute"' % n)


def paused_time(cfg: dict, tl, client: str, start, end, tz, project: Optional[str] = None,
                mode: Optional[str] = None) -> Tuple[float, List[str]]:
    """(seconds, local dates) of this client's time that pause windows kept off the bill
    in [start, end); None means unbounded. Seconds are what would have been billed: the
    user's time plus agent time at the project's agent_billable_fraction. mode "mine"
    counts only what an away window alone removed, "all" only what a full pause did."""
    pieces = tl.paused_pieces
    if mode == "mine":
        pieces = tl.away_pieces
    elif mode == "all":
        away = {id(s) for s in tl.away_pieces}
        pieces = [s for s in pieces if id(s) not in away]
    mine = [s for s in pieces
            if s.key[0] == client and (project is None or s.key[1] == project)]
    seconds, days = 0.0, set()
    fractions = {}  # type: Dict[str, float]
    for s in rollup.clip(mine, start, end):
        factor = s.weight
        if s.kind == "agent":
            proj = s.key[1]
            if proj not in fractions:
                st = (config.settings(cfg, client, proj) if client in cfg.get("clients", {})
                      else cfg["defaults"])
                fractions[proj] = float(st["agent_billable_fraction"])
            factor *= fractions[proj]
        for d, a, b in rollup.split_days(s.start, s.end, tz):
            n = (b.astimezone(timezone.utc) - a.astimezone(timezone.utc)).total_seconds() * factor
            if n > 0:
                seconds += n
                days.add(d.isoformat())
    return seconds, sorted(days)


def paused_hours(seconds: float) -> str:
    """Seconds as hours with two decimals, e.g. '1.25'."""
    return str((Decimal(str(round(float(seconds), 6))) / 3600).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP))


def paused_summary(cfg: dict, tl, client: str, start, end, tz,
                   project: Optional[str] = None) -> Tuple[str, List[str], Optional[str]]:
    """(hours as text, local dates, kind) of the time left out in [start, end). kind is
    None when there was none, "away" when only away windows removed it, else "paused"."""
    seconds, days = paused_time(cfg, tl, client, start, end, tz, project)
    hours = paused_hours(seconds)
    if Decimal(hours) == 0:
        return hours, days, None
    full = paused_time(cfg, tl, client, start, end, tz, project, mode="all")[0]
    return hours, days, "paused" if full > 0 else "away"


def paused_warning(hours: str, days: List[str], kind: Optional[str] = "paused") -> Optional[str]:
    """The note shown wherever paused time was left out, or None when there was none."""
    if Decimal(hours) == 0:
        return None
    when = ", ".join(days) if len(days) <= 3 else "%s to %s" % (days[0], days[-1])
    if kind == "away":
        return ("%s h of your own time were logged while away and are not billed (%s). If "
                "you were back sooner: timesheet pauses" % (hours, when))
    return ("%s h were logged while paused and are not billed (%s). If a pause ran too "
            "long: timesheet pauses" % (hours, when))
