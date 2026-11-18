"""Invoice drafts: compute one client's draft for one billing period from the event
log and keep the approved summaries / edited descriptions in lines.json.

Numbers are always recomputed; only the free text the caller passes in is stored."""
import json
import os
import tempfile
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from ats import config, engine, events, health, lines, periods, rollup, transcripts

__all__ = ["DraftError", "draft_dir", "load", "compute", "save", "is_approved",
           "overlapping", "overlap_text", "write_render_record", "RENDERED_FILES"]

_FILE = "lines.json"
_RENDER = "render.json"
# Written by invoice.py render; removed whenever a new approval is written.
RENDERED_FILES = (_RENDER, "timesheet.html", "timesheet.pdf")


class DraftError(ValueError):
    pass


def draft_dir(cfg: dict, client: str, period: periods.Period) -> str:
    base = os.path.abspath(config.expand(cfg["pdf"]["output_dir"]))
    path = os.path.normpath(os.path.join(base, client, period.id))
    if os.path.commonpath([base, path]) != base or path == base:
        raise ValueError("draft directory escapes the output directory: %s" % client)
    return path


def _makedirs_private(path: str) -> None:
    """Create path and any missing parents with mode 0o700 (umask-proof)."""
    missing = []
    p = path
    while not os.path.isdir(p):
        missing.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    for d in reversed(missing):
        try:
            os.mkdir(d, 0o700)
            os.chmod(d, 0o700)
        except FileExistsError:
            pass


def _empty() -> dict:
    return {"version": 1, "summaries": {}, "descriptions": {}, "approved_at": None}


def _text_map(value) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


def load(cfg: dict, client: str, period: periods.Period) -> dict:
    """lines.json content, or an empty draft when missing or unreadable."""
    path = os.path.join(draft_dir(cfg, client, period), _FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    out = dict(data)
    out["summaries"] = _text_map(data.get("summaries"))
    out["descriptions"] = _text_map(data.get("descriptions"))
    out.setdefault("version", 1)
    out.setdefault("approved_at", None)
    return out


def _mercury(cfg, client, period):
    path = os.path.join(draft_dir(cfg, client, period), "mercury.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def overlapping(cfg: dict, client: str, period: periods.Period) -> List[dict]:
    """Other periods of this client whose dates intersect period and that were approved
    or have a Mercury record: [{period_id, approved_at, total, mercury}], by start date."""
    base = os.path.dirname(draft_dir(cfg, client, period))
    try:
        names = os.listdir(base)
    except OSError:
        return []
    out = []
    for name in sorted(names):
        if name == period.id or not os.path.isdir(os.path.join(base, name)):
            continue
        other = periods.parse_id(name)
        if other is None or other.id != name:
            continue
        if other.start > period.end or other.end < period.start:
            continue
        doc = _read_json(os.path.join(base, name, _FILE)) or {}
        approved = doc.get("approved_at") if isinstance(doc.get("approved_at"), str) else None
        mercury = os.path.exists(os.path.join(base, name, "mercury.json"))
        if not approved and not mercury:
            continue
        total = doc.get("total") if approved and isinstance(doc.get("total"), str) else None
        out.append({"period_id": name, "approved_at": approved, "total": total,
                    "mercury": mercury})
    out.sort(key=lambda o: o["period_id"])
    return out


def overlap_text(overlaps: List[dict], currency: str) -> str:
    """'<id> (approved, total $X; Mercury invoice on record), ...'"""
    parts = []
    for o in overlaps:
        bits = []
        if o["approved_at"]:
            bits.append("approved, total %s" % _money(o["total"], currency))
        if o["mercury"]:
            bits.append("Mercury invoice on record")
        parts.append("%s (%s)" % (o["period_id"], "; ".join(bits)))
    return ", ".join(parts)


def _render_record(cfg, client, period):
    return _read_json(os.path.join(draft_dir(cfg, client, period), _RENDER))


def write_render_record(cfg: dict, client: str, period: periods.Period, payload: dict,
                        pdf: bool, now: datetime) -> None:
    """Atomically write render.json (0600): which approval and total were rendered."""
    rec = {"approved_at": payload["approved_at"], "total": payload["total"], "html": True,
           "pdf": bool(pdf),
           "rendered_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    _write_private(draft_dir(cfg, client, period), _RENDER, rec)


def _write_private(d, name, doc):
    fd, tmp = tempfile.mkstemp(prefix="." + name.split(".")[0] + "-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, os.path.join(d, name))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _extra_warnings(cfg, client, period, st, tl, tz, now, skipped, manual_skipped, empty,
                    evs, manual):
    out = []
    if empty:
        out.append("no tracked time in this period")
    for w in (health.skipped_warning(skipped, "events.jsonl"),
              health.skipped_warning(manual_skipped, "manual.jsonl")):
        if w:
            out.append(w)
    start, end = periods.bounds(period, tz)
    capped = 0
    for t in tl.capped_turns:
        c, proj = t.key
        if c == client and proj != engine.NON_BILLABLE and start <= t.start < end:
            capped += 1
    if capped:
        out.append("%d interrupted turn(s) capped at %s min"
                   % (capped, "%g" % st["unclosed_cap_minutes"]))
    w = health.unclosed_warning(health.unclosed_turns(tl, client, start, end),
                                st["unclosed_cap_minutes"], tz)
    if w:
        out.append(w)
    out += health.unknown_client_warnings(cfg, evs, manual)
    w = health.paused_warning(*_paused(cfg, tl, client, period, tz))
    if w:
        out.append(w)
    if period.end >= now.astimezone(tz).date():
        out.append("period is not complete yet")
    w = health.hook_error_warning(False)     # no last line: it may name another client's path
    if w:
        out.append(w)
    w = health.unattributed_warning()        # a count only
    if w:
        out.append(w)
    return out


def _paused(cfg, tl, client, period, tz):
    """(hours as text, local dates, "paused" | "away" | None) of the time pause and away
    windows kept off this period's bill."""
    start, end = periods.bounds(period, tz)
    return health.paused_summary(cfg, tl, client, start, end, tz)


def _money(text, currency):
    if text is None:
        return "unknown"
    try:
        return lines.money(Decimal(str(text)), currency)
    except ArithmeticError:
        return str(text)


def _snapshot_lines(lns):
    """What an approval vouches for, per line: the numbers and the printed text."""
    return [(l.get("key"), l.get("label"), l.get("description"), l.get("hours"), l.get("rate"),
             l.get("amount")) for l in lns]


def _inputs(cfg, now, shared):
    """The event log, its timeline and the manual entries. shared (a dict owned by the
    caller) keeps them between calls that use the same cfg and now."""
    if shared is not None and "inputs" in shared:
        return shared["inputs"]
    evs, skipped = events.load_events()
    tl = engine.timeline(evs, cfg, now)          # global: all clients' events
    manual, manual_skipped = events.load_manual()
    out = (evs, skipped, tl, manual, manual_skipped)
    if shared is not None:
        shared["inputs"] = out
    return out


def _build(cfg, client, period, now, summaries, descriptions, approved_at, redraft=False,
           snapshot=None, shared=None):
    tz = config.tz(cfg)
    st = config.settings(cfg, client)
    evs, skipped, tl, manual, manual_skipped = _inputs(cfg, now, shared)
    start, end = periods.bounds(period, tz)
    segs = rollup.clip(rollup.billable(tl.segs, client), start, end)
    sessions = rollup.work_sessions(segs, tl.turns, client,
                                    timedelta(minutes=st["session_break_minutes"]), tz)
    if redraft:
        summaries, descriptions = {}, {}
    result = lines.build_lines(cfg, client, period, segs, sessions, manual,
                               summaries, descriptions)
    warnings = list(result["warnings"])
    blank = sum(1 for l in result["lines"] if not l["description"].strip())
    if blank:
        warnings.append("%d line(s) have no description" % blank)
    warnings += _extra_warnings(cfg, client, period, st, tl, tz, now, skipped,
                                manual_skipped, not result["lines"], evs, manual)
    sess = []
    for s in sessions:
        sess.append({
            "key": s.key,
            "start": s.start.astimezone(tz).isoformat(),
            "end": s.end.astimezone(tz).isoformat(),
            "date": s.days[0].isoformat(),
            "days": [d.isoformat() for d in s.days],
            "you_seconds": s.you, "agent_seconds": s.agent,
            "billable_seconds": sum(
                c["you"] + c["agent"] * float(
                    config.settings(cfg, client, p)["agent_billable_fraction"])
                for p, c in s.by_project.items()),
            "projects": sorted(s.by_project),
            "summary": summaries.get(s.key, ""),
            "threads": [{"session": sid, "transcript_path": transcripts.locate(sid, evs, cfg)}
                        for sid in s.threads]})
    out = {
        "client": {"key": client, "name": cfg["clients"][client]["name"]},
        "period": {"id": period.id, "start": period.start.isoformat(),
                   "end": period.end.isoformat(), "label": periods.label(period)},
        "reference": "%s-%s" % (client, period.id),
        "sessions": sess,
    }
    out.update(result)
    out["warnings"] = warnings
    out["paused_hours"], out["paused_dates"], out["paused_kind"] = _paused(
        cfg, tl, client, period, tz)
    out["approved_at"] = approved_at
    snapshot = snapshot or {}
    approved_total = snapshot.get("total") if approved_at else None
    stale = bool(approved_at) and (
        approved_total is None
        or snapshot.get("total") != result["total"]
        or snapshot.get("total_hours") != result["total_hours"]
        or snapshot.get("currency") != result["currency"]
        or _snapshot_lines(snapshot.get("lines") or []) != _snapshot_lines(result["lines"]))
    out["approved_total"] = approved_total
    out["stale"] = stale
    if stale:
        cur = result["currency"]
        out["warnings"].append(
            "numbers changed since approval: approved total %s, now %s. "
            "Review the draft and save again to re-approve."
            % (_money(approved_total, cur), _money(result["total"], cur)))
    out["mercury"] = _mercury(cfg, client, period)
    out["overlaps"] = overlapping(cfg, client, period)
    if out["overlaps"]:
        out["warnings"].append("overlaps already approved period(s): "
                               + ", ".join(o["period_id"] for o in out["overlaps"]))
    rec = _render_record(cfg, client, period)
    out["rendered"] = bool(approved_at) and rec is not None and \
        rec.get("approved_at") == approved_at and rec.get("total") == result["total"]
    out["text"] = dict(st["text"], counting_note=config.counting_note(st))
    out["show_session_detail"] = bool(st["show_session_detail"])
    out["show_project_subtotals"] = bool(st["show_project_subtotals"])
    out["show_agent_time"] = bool(st["show_agent_time"])
    out["line_items"] = st["line_items"]
    return out


def compute(cfg: dict, client: str, period: periods.Period, now: datetime,
            redraft: bool = False, shared: Optional[dict] = None) -> dict:
    """The draft payload. Writes nothing. redraft ignores the stored text. A caller
    computing several periods with the same cfg and now can pass one dict as shared, so
    the event log is read and its timeline built once."""
    stored = load(cfg, client, period)
    approved = None if redraft else stored["approved_at"]
    return _build(cfg, client, period, now, stored["summaries"], stored["descriptions"],
                  approved, redraft, snapshot=stored, shared=shared)


def is_approved(payload: dict) -> bool:
    """True when the draft was approved and its numbers have not changed since."""
    return bool(payload.get("approved_at")) and not payload.get("stale")


def _merge(stored: Dict[str, str], new: Dict[str, Optional[str]]) -> Dict[str, str]:
    out = dict(stored)
    for k, v in new.items():
        if v is None or v == "":
            out.pop(k, None)
        else:
            out[k] = v
    return out


def save(cfg: dict, client: str, period: periods.Period, summaries: dict,
         descriptions: dict, now: datetime, allow_overlap: bool = False) -> dict:
    """Merge summaries / descriptions into lines.json, stamp approved_at, write the
    snapshot and return the recomputed payload. Refuses when the period overlaps
    another approved (or invoiced) period unless allow_overlap. Removes earlier
    rendered files: they show the previous approval."""
    stored = load(cfg, client, period)
    merged_s = _merge(stored["summaries"], summaries)
    merged_d = _merge(stored["descriptions"], descriptions)
    payload = _build(cfg, client, period, now, merged_s, merged_d, None)
    if not payload["lines"]:
        raise DraftError("this period has no lines to save")
    session_keys = {s["key"] for s in payload["sessions"]}   # keys do not depend on the text
    line_keys = {l["key"] for l in payload["lines"]}
    bad = []
    for k in summaries:
        if k not in stored["summaries"] and k not in session_keys:
            bad.append("session key %s" % k)
    for k in descriptions:
        if k not in stored["descriptions"] and k not in line_keys:
            bad.append("line key %s" % k)
    if bad:
        raise DraftError("unknown key(s): " + ", ".join(bad))
    if payload["overlaps"] and not allow_overlap:
        raise DraftError(
            "this period overlaps already approved period(s): %s. Approving it would bill "
            "those days again; pass --allow-overlap only if that is intended"
            % overlap_text(payload["overlaps"], payload["currency"]))
    stamp = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    payload["approved_at"] = stamp
    payload["approved_total"] = payload["total"]
    payload["stale"] = False
    payload["rendered"] = False
    doc = {"version": 1, "client": client, "period_id": period.id,
           "summaries": merged_s, "descriptions": merged_d, "approved_at": stamp,
           "lines": payload["lines"], "total_hours": payload["total_hours"],
           "total": payload["total"], "currency": payload["currency"]}
    d = draft_dir(cfg, client, period)
    _makedirs_private(d)
    for name in RENDERED_FILES:            # render.json first: it vouches for the others
        try:
            os.unlink(os.path.join(d, name))
        except FileNotFoundError:
            pass
        except OSError as e:
            raise DraftError("cannot remove the earlier %s (it shows the previous approval): "
                             "%s" % (name, e.strerror or e))
    _write_private(d, _FILE, doc)
    return payload
