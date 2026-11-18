#!/usr/bin/env python3
"""Claude Code hook: append one event line when the session belongs to a client.

Fast, silent, and never fails a session: always exits 0, never prints to stdout.
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

EVENTS = {
    "UserPromptSubmit": "start",
    "Stop": "end",
    "StopFailure": "end",
    "SessionEnd": "session_end",
}
ERROR_LOG = "hook-errors.log"
UNATTRIBUTED = "unattributed.jsonl"
ERROR_LOG_MAX_BYTES = 64 * 1024
ERROR_LOG_KEEP_LINES = 200


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def _log_error(exc: BaseException) -> None:
    """Best effort: append one line to the error log, trimming it when large."""
    try:
        from ats import config
        d = config.home()
        os.makedirs(d, mode=0o700, exist_ok=True)
        path = os.path.join(d, ERROR_LOG)
        msg = " ".join(str(exc).split())
        line = "%s\t%s: %s\n" % (_now(), type(exc).__name__, msg)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8", errors="replace") as f:
            f.write(line)
        if os.path.getsize(path) > ERROR_LOG_MAX_BYTES:
            with open(path, encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-ERROR_LOG_KEEP_LINES:]
            with open(path, "w", encoding="utf-8", errors="replace") as f:
                f.writelines(tail)
    except Exception:
        pass


def _park(record: dict) -> None:
    """Best effort: append record to <home>/unattributed.jsonl."""
    try:
        from ats import config, events
        events.append_event(record, os.path.join(config.home(), UNATTRIBUTED))
    except Exception as e:
        _log_error(e)


def _account(transcript_path: str) -> str:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return env
    if transcript_path and "/projects/" in transcript_path:
        return transcript_path.rsplit("/projects/", 1)[0]
    return os.path.expanduser("~/.claude")


def _read_payload():
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _str(v) -> str:
    return v if isinstance(v, str) else ""


def main() -> None:
    payload = _read_payload()
    if payload is None:
        return
    hook = _str(payload.get("hook_event_name")) or (sys.argv[1] if len(sys.argv) > 1 else "")
    event = EVENTS.get(hook)
    if event is None:
        return
    try:
        from ats import config, events, scope
        cwd = _str(payload.get("cwd")) or os.getcwd()
        transcript = _str(payload.get("transcript_path"))
        account = _account(transcript)
        ts, session = _now(), _str(payload.get("session_id"))
        try:
            cfg = config.load()
            found = scope.detect(cfg, cwd, account)
        except Exception as e:
            # Keep the event so `timesheet reattribute` can attribute it once the config
            # is fixed. A missing config means nothing is set up: nothing is kept.
            if os.path.exists(os.path.join(config.home(), "config.json")):
                _park({"ts": ts, "event": event, "hook": hook, "session": session,
                       "cwd": cwd, "transcript_path": transcript, "account": account})
            _log_error(e)
            return
        if found is None:
            return
        client, project = found
        events.append_event({
            "ts": ts,
            "event": event,
            "hook": hook,
            "session": session,
            "client": client,
            "project": project,
            "cwd": cwd,
            "transcript_path": transcript,
        })
    except Exception as e:
        _log_error(e)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
