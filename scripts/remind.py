#!/usr/bin/env python3
"""Claude Code SessionStart hook: a notice that billing is paused (or that the user is
marked away), and an optional "invoice due" notice, for the client this session belongs to.

The paused / away notice is shown whenever that client is paused or away. The invoice
notice is off unless `status.reminders` is set in the config. Prints at most one JSON
object; never another client's name or numbers. Always exits 0, never blocks and never
writes to stderr.
It also records where the plugin is installed, for the status line launcher.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STATE = "reminders.json"


def _str(v) -> str:
    return v if isinstance(v, str) else ""


def _now(argv):
    """The clock; `--now ISO` fixes it (tests)."""
    from datetime import datetime, timezone
    if "--now" in argv:
        from ats import events
        return events.parse_ts(argv[argv.index("--now") + 1])
    return datetime.now(timezone.utc)


def _record_plugin_root() -> None:
    try:
        from ats import status
        status.record_plugin_root(os.environ.get("CLAUDE_PLUGIN_ROOT"))
    except Exception:
        pass


def _already_reminded(path: str, client: str, day: str) -> bool:
    """True when this client was reminded on this day; otherwise note that it is now."""
    try:
        with open(path, encoding="utf-8") as f:
            seen = json.load(f)
        if not isinstance(seen, dict):
            seen = {}
    except Exception:
        seen = {}
    if seen.get(client) == day:
        return True
    seen[client] = day
    try:
        from ats import status
        status.write_private(path, json.dumps(seen, indent=2, sort_keys=True) + "\n")
    except Exception:
        pass
    return False


def _invoice_notice(cfg, client, now):
    """(message, context) when reminders are on and an invoice is due, else None."""
    from ats import config, status
    mode = cfg["status"]["reminders"]
    if mode == "off":
        return None
    summary = status.summary(cfg, client, now)
    if not summary["due"]:
        return None
    if mode == "daily":
        day = now.astimezone(config.tz(cfg)).date().isoformat()
        if _already_reminded(os.path.join(config.home(), STATE), client, day):
            return None
    due = status.due_text(summary, cfg["status"]["show_amounts"])
    labels = ", ".join(d["label"] for d in summary["due"])
    return ("timesheet: %s. Run /timesheet:invoice %s" % (due, client),
            "timesheet: an invoice is due for %s for %s. Mention it once if it is "
            "relevant; do not start the invoice flow unless the user asks."
            % (summary["client"]["name"], labels))


def _paused_notice(cfg, client, now):
    """(message, context) when this client is paused or away now, else None. Not a reminder
    setting: a forgotten pause loses billable time, so it is always shown."""
    from ats import config, pauses
    window = pauses.active(pauses.load(), client, now)
    if window is None:
        return None
    name = cfg["clients"][client]["name"]
    since = pauses.local(pauses.start_of(window), config.tz(cfg))
    if pauses.mode_of(window) == "mine":
        return ("timesheet: AWAY for %s since %s; your own time is not being counted. Run "
                "/timesheet:resume when you are back." % (name, since),
                "timesheet: the user is marked away for %s since %s. Their own time is not "
                "counted; agent time is still recorded and billed. Mention it once; do not "
                "resume unless the user asks." % (name, since))
    return ("timesheet: PAUSED for %s since %s. Run /timesheet:resume to start billing "
            "again." % (name, since),
            "timesheet: billing is paused for %s since %s. Time is still recorded but not "
            "billed. Mention it once; do not resume unless the user asks." % (name, since))


def main(argv) -> str:
    _record_plugin_root()
    payload = json.loads(sys.stdin.read())
    cwd = _str(payload.get("cwd"))
    if not cwd:
        return ""
    from ats import config, status
    cfg = config.load()
    client = status.detect_client(cfg, cwd, status.account_dir(_str(payload.get("transcript_path"))))
    if client is None:
        return ""
    now = _now(argv)
    notices = [n for n in (_paused_notice(cfg, client, now), _invoice_notice(cfg, client, now))
               if n]
    if not notices:
        return ""
    return json.dumps({
        "systemMessage": " ".join(n[0] for n in notices),
        "hookSpecificOutput": {"hookEventName": "SessionStart",
                               "additionalContext": " ".join(n[1] for n in notices)}})


if __name__ == "__main__":
    try:
        text = main(sys.argv[1:])
        if text:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
    except BaseException:
        pass
    sys.exit(0)
