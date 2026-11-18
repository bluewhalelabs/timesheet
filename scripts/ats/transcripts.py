"""Find a Claude Code transcript and print a short time-windowed digest of it.

The digest is returned as a string for stdout; nothing here writes transcript
content to disk."""
import glob
import json
import os
import re
from datetime import datetime, timezone, tzinfo
from typing import List, Optional

from ats import config
from ats.events import parse_ts

__all__ = ["locate", "digest"]

_WRAPPER_TAGS = frozenset((
    "command-name", "command-message", "local-command-stdout",
    "local-command-caveat", "system-reminder", "task-notification",
))
_TAG = re.compile(r"<([A-Za-z0-9_-]+)")
_PROMPT_MAX = 500
_REPLY_MAX = 800
_ELLIPSIS = "…"
_TRUNCATED = "[…truncated]"
_OLDEST = datetime.min.replace(tzinfo=timezone.utc)
_MIN_CHARS = 100


def _safe_session(session) -> bool:
    return (isinstance(session, str) and session != "" and "/" not in session
            and ".." not in session and "\0" not in session
            and os.sep not in session)


def _config_dirs(cfg: dict) -> List[str]:
    dirs = []
    clients = cfg.get("clients", {}) if isinstance(cfg, dict) else {}
    for key in config.client_keys(cfg):
        client = clients.get(key)
        scope = client.get("scope", {}) if isinstance(client, dict) else {}
        for acct in scope.get("accounts", []) if isinstance(scope, dict) else []:
            if isinstance(acct, str) and acct:
                dirs.append(config.expand(acct))
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        dirs.append(config.expand(env))
    dirs.append(config.expand("~/.claude"))
    return list(dict.fromkeys(d for d in dirs if d))


def locate(session: str, events: List[dict], cfg: dict) -> Optional[str]:
    """Real path of the transcript for a session, or None.

    Prefers the latest logged non-empty transcript_path that still exists, else
    searches <dir>/projects/*/<session>.jsonl over every client's accounts,
    CLAUDE_CONFIG_DIR and ~/.claude, in that order."""
    if not _safe_session(session):
        return None
    logged = [e for e in events
              if e.get("session") == session
              and isinstance(e.get("transcript_path"), str) and e["transcript_path"]]
    logged.sort(key=lambda e: e.get("_t") or _OLDEST, reverse=True)
    if logged:
        p = os.path.expanduser(logged[0]["transcript_path"])
        if os.path.isfile(p):
            return os.path.realpath(p)
    name = glob.escape(session) + ".jsonl"
    for d in _config_dirs(cfg):
        hits = sorted(glob.glob(os.path.join(glob.escape(d), "projects", "*", name)))
        for hit in hits:
            if os.path.isfile(hit):
                return os.path.realpath(hit)
    return None


def _blocks(message) -> Optional[list]:
    """Content as a list of blocks (a bare string becomes one text block)."""
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return None


def _text_of(blocks: list) -> str:
    parts = [b["text"] for b in blocks
             if b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip()]
    return " ".join(parts)


def _is_wrapper(text: str) -> bool:
    s = text.strip()
    if not s.startswith("<"):
        return False
    m = _TAG.match(s)
    return bool(m) and m.group(1) in _WRAPPER_TAGS


def _squash(text: str, limit: int) -> str:
    s = " ".join(text.split())
    if len(s) > limit:
        s = s[:limit] + _ELLIPSIS
    return s


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc)


def digest(path: str, start: datetime, end: datetime, max_chars: int = 6000,
           tz: Optional[tzinfo] = None) -> str:
    """Time-windowed digest: each user prompt and the last assistant text before
    the next prompt, as "[HH:MM] USER: ..." / "[HH:MM] AGENT: ..." lines in tz
    (UTC when None). Empty string when nothing matches or the file is unreadable.
    max_chars below 100 is raised to 100 so the truncation marker always fits."""
    max_chars = max(max_chars, _MIN_CHARS)
    zone = tz if tz is not None else timezone.utc
    start, end = _aware(start), _aware(end)
    turns = []   # each: [prompt_time, prompt_text, reply_time, reply_text]
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return ""
    with f:
        for line in f:
            try:
                e = json.loads(line)
                if not isinstance(e, dict) or e.get("type") not in ("user", "assistant"):
                    continue
                if e.get("isSidechain") or e.get("isMeta") or e.get("isCompactSummary"):
                    continue
                t = parse_ts(e.get("timestamp"))
            except (ValueError, RecursionError, OverflowError):
                continue
            if not (start <= t <= end):
                continue
            blocks = _blocks(e.get("message"))
            if blocks is None:
                continue
            text = _text_of(blocks)
            if not text:
                continue
            if e["type"] == "user":
                if _is_wrapper(text):
                    continue
                turns.append([t, text, None, None])
            elif turns:
                turns[-1][2], turns[-1][3] = t, text
    lines = []
    for pt, ptext, rt, rtext in turns:
        lines.append("[%s] USER: %s" % (pt.astimezone(zone).strftime("%H:%M"),
                                        _squash(ptext, _PROMPT_MAX)))
        if rtext is not None:
            lines.append("[%s] AGENT: %s" % (rt.astimezone(zone).strftime("%H:%M"),
                                             _squash(rtext, _REPLY_MAX)))
    out = "\n".join(lines)
    if len(out) <= max_chars:
        return out
    kept = []
    size = len(_TRUNCATED)
    for ln in lines:
        if size + 1 + len(ln) > max_chars:
            break
        kept.append(ln)
        size += 1 + len(ln)
    return "\n".join(kept + [_TRUNCATED])
