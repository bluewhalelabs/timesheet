"""Invoicing status of one client: hours in the current billing period and which
completed periods still need an invoice. Every number comes from drafts.compute, the
computation behind the invoice draft. Nothing here writes into a draft."""
import os
import tempfile
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from ats import config, drafts, health, lines, pauses, periods, scope

__all__ = ["LOOKBACK", "summary", "line", "due_text", "current_text", "paused_text",
           "detect_client",
           "account_dir", "write_private", "record_plugin_root"]

LOOKBACK = 6                      # completed periods checked for an invoice that is due
_DUE_STATES = ("not drafted", "stale")
_SEP = " • "


def _state(payload: dict) -> str:
    if payload.get("mercury") is not None:
        return "in Mercury"
    if Decimal(payload["total_hours"]) == 0:
        return "nothing to bill"
    if not payload.get("approved_at"):
        return "not drafted"
    if payload.get("stale"):
        return "stale"
    return "rendered" if payload.get("rendered") else "approved"


def summary(cfg: dict, client: str, now: datetime) -> dict:
    """{client, current, due, last, last_period, paused, paused_notes} for one client.
    Writes nothing.

    current: the billing period containing today (config timezone), with the hours and
    amount the invoice draft would show for it. due: completed periods among the
    previous LOOKBACK that have a non-zero total and are neither approved (with
    unchanged numbers) nor in Mercury, oldest first. last: the state of the most recent
    completed period (last_period). paused: {id, start, until} of the pause window
    covering now (mode "all": a full pause, "mine": away), or None. paused_notes: for the current and the last completed period,
    the note about time that was logged while paused and is not billed."""
    spec = config.settings(cfg, client)["billing_period"]
    today = now.astimezone(config.tz(cfg)).date()
    current = periods.containing(today, spec)
    shared = {}                       # one read of the event log for every period
    payload = drafts.compute(cfg, client, current, now, shared=shared)
    out = {
        "client": dict(payload["client"]),
        "current": {"period_id": current.id, "label": periods.label(current),
                    "short_label": periods.short_label(current),
                    "end": current.end.isoformat(), "hours": payload["total_hours"],
                    "amount": payload["total"], "currency": payload["currency"]},
        "due": [], "last": None, "last_period": None,
        "paused": None, "paused_notes": [],
    }
    window = pauses.active(pauses.load(), client, now)
    if window is not None:
        out["paused"] = {"id": window["id"], "mode": pauses.mode_of(window),
                         "start": window["start"],
                         "until": window["until"] if window.get("end") is None else None}
    notes = [health.paused_warning(payload["paused_hours"], payload["paused_dates"],
                                   payload["paused_kind"])]
    period = current
    for i in range(LOOKBACK):
        period = periods.previous_period(period, spec)
        if period.end >= today:           # cannot happen for a period before today's
            continue
        payload = drafts.compute(cfg, client, period, now, shared=shared)
        state = _state(payload)
        if i == 0:
            out["last"] = state
            out["last_period"] = {"period_id": period.id, "label": periods.label(period)}
            notes.append(health.paused_warning(payload["paused_hours"],
                                               payload["paused_dates"],
                                               payload["paused_kind"]))
        if state in _DUE_STATES:
            out["due"].append({"period_id": period.id, "label": periods.label(period),
                               "short_label": periods.short_label(period),
                               "hours": payload["total_hours"], "amount": payload["total"],
                               "state": state})
    out["due"].reverse()
    out["paused_notes"] = [n for n in notes if n]
    return out


def _money(summary: dict, amount: Decimal) -> str:
    return lines.money(amount, summary["current"]["currency"])


def _figures(summary: dict, hours: Decimal, amount: Decimal, show_amounts: bool,
             joiner: str) -> str:
    text = "%s h" % hours
    return text + joiner % _money(summary, amount) if show_amounts else text


def due_text(summary: dict, show_amounts: bool) -> str:
    """'Invoice due: Nov 9 – Nov 15 (12.40 h)', or '' when nothing is due."""
    due = summary["due"]
    if not due:
        return ""
    hours = sum((Decimal(d["hours"]) for d in due), Decimal(0))
    amount = sum((Decimal(d["amount"]) for d in due), Decimal(0))
    what = due[0]["short_label"] if len(due) == 1 else "%d periods" % len(due)
    return "Invoice due: %s (%s)" % (what, _figures(summary, hours, amount, show_amounts,
                                                    ", %s"))


def current_text(summary: dict, show_amounts: bool) -> str:
    """'Current hours: 5.06 h'"""
    cur = summary["current"]
    return "Current hours: " + _figures(summary, Decimal(cur["hours"]),
                                        Decimal(cur["amount"]), show_amounts, " (%s)")


def _word(paused: dict) -> str:
    return "AWAY" if paused.get("mode") == "mine" else "PAUSED"


def paused_text(summary: dict, tz) -> str:
    """'PAUSED since 2026-11-12 10:00 UTC until 2026-11-12 17:00 UTC' (AWAY for an away
    window), or ''."""
    p = summary.get("paused")
    if not p:
        return ""
    text = _word(p) + " since " + pauses.local(pauses.parse_ts(p["start"]), tz)
    if p.get("until"):
        text += " until " + pauses.local(pauses.parse_ts(p["until"]), tz)
    return text


def line(summary: dict, components: List[str], show_amounts: bool, tz=None) -> str:
    """The enabled components in order, joined with a bullet. '' when none has text.
    With show_amounts false no amount or currency symbol appears. While the client is
    paused the line starts with PAUSED, or AWAY for an away window (and 'until HH:MM' in
    tz when a resume is planned), whatever the components are."""
    parts = []
    p = summary.get("paused")
    if p:
        text = _word(p)
        if p.get("until"):
            until = pauses.parse_ts(p["until"])
            text += " until " + (until.astimezone(tz) if tz is not None else until).strftime("%H:%M")
        parts.append(text)
    for name in components:
        if name == "client":
            text = " ".join(str(summary["client"]["name"]).split())
        elif name == "invoice_due":
            text = due_text(summary, show_amounts)
        elif name == "current_hours":
            text = current_text(summary, show_amounts)
        else:
            text = ""
        if text:
            parts.append(text)
    return _SEP.join(parts)


def detect_client(cfg: dict, cwd: str, account: str) -> Optional[str]:
    """The key of the client whose scope cwd (under account) falls in, or None."""
    found = scope.detect(cfg, cwd, account)
    return found[0] if found else None


def account_dir(transcript_path: str = "") -> str:
    """The Claude config dir of this process, resolved the way the tracking hook does."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return env
    if transcript_path and "/projects/" in transcript_path:
        return transcript_path.rsplit("/projects/", 1)[0]
    return os.path.expanduser("~/.claude")


def write_private(path: str, text: str, mode: int = 0o600) -> None:
    """Atomically write text to path with the given mode. The directory must exist."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".status-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_plugin_root(root: Optional[str]) -> bool:
    """Note where the plugin is installed in <home>/plugin-root, for the status line
    launcher. Only when the data dir already exists: this never creates it."""
    home = config.home()
    if not root or not os.path.isdir(home):
        return False
    path = os.path.join(home, "plugin-root")
    text = os.path.abspath(root) + "\n"
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return True
    except OSError:
        pass
    write_private(path, text)
    return True
