#!/usr/bin/env python3
"""Claude Code status line command: the invoicing status of the client this session
belongs to, on one line.

Reads the status line JSON from stdin. Prints nothing when the working directory is
outside every client's scope, when there is no usable config, or on any error. Always
exits 0 and never writes to stderr. The line is cached for a minute.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CACHE = "status-cache.json"
CACHE_SECONDS = 60


def _str(v) -> str:
    return v if isinstance(v, str) else ""


def _now(argv):
    """The clock; `--now ISO` fixes it (tests)."""
    from datetime import datetime, timezone
    if "--now" in argv:
        from ats import events
        return events.parse_ts(argv[argv.index("--now") + 1])
    return datetime.now(timezone.utc)


def _cached(path, key, stamp):
    """(the fresh cached line or None, the still-fresh entries of other keys)."""
    try:
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)["entries"]
        fresh = {k: v for k, v in entries.items()
                 if isinstance(v, dict) and isinstance(v.get("line"), str)
                 and isinstance(v.get("at"), (int, float))
                 and 0 <= stamp - v["at"] < CACHE_SECONDS}
    except Exception:
        return None, {}
    hit = fresh.get(key)
    return (hit["line"] if hit else None), fresh


def main(argv) -> str:
    payload = json.loads(sys.stdin.read())
    workspace = payload.get("workspace")
    cwd = _str(workspace.get("current_dir")) if isinstance(workspace, dict) else ""
    cwd = cwd or _str(payload.get("cwd"))
    if not cwd:
        return ""
    from ats import config, status
    cfg = config.load()
    client = status.detect_client(cfg, cwd, status.account_dir(_str(payload.get("transcript_path"))))
    if client is None:
        return ""
    opts = cfg["status"]
    now = _now(argv)
    stamp = now.timestamp()
    path = os.path.join(config.home(), CACHE)
    try:                              # a pause or resume shows at once, not a minute later
        changed = os.stat(os.path.join(config.home(), "pauses.json")).st_mtime
    except OSError:
        changed = 0
    key = json.dumps([client, opts["components"], opts["show_amounts"], changed])
    line, fresh = _cached(path, key, stamp)
    if line is None:
        line = status.line(status.summary(cfg, client, now), opts["components"],
                           opts["show_amounts"], config.tz(cfg))
        fresh[key] = {"at": stamp, "line": line}
        try:
            status.write_private(path, json.dumps({"entries": fresh}) + "\n")
        except Exception:
            pass
    return line


if __name__ == "__main__":
    try:
        text = main(sys.argv[1:])
        if text:
            sys.stdout.buffer.write((text + "\n").encode("utf-8"))
            sys.stdout.flush()
    except BaseException:
        pass
    sys.exit(0)
