#!/usr/bin/env python3
"""Quick terminal report of tracked time, the invoicing status, pause and resume, the
non-billable session command, setup checks and the optional status line."""
import argparse
import csv
import io
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ats import (config, engine, events as ev, health, launcher, lines, pauses,  # noqa: E402
                 periods, rollup, scope, status)

_CENTS = Decimal("0.01")
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_WEEK_SPEC = {"type": "weekly", "week_starts": "monday"}


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def _num(v) -> str:
    return "%g" % v


def _dec(seconds: float) -> Decimal:
    return Decimal(str(round(float(seconds), 6)))


def _duration(seconds: float) -> str:
    minutes = int(Decimal(str(seconds)) / 60 + Decimal("0.5"))
    return "%dh %02dm" % (minutes // 60, minutes % 60)


def _hours2(seconds: float) -> Decimal:
    return (_dec(seconds) / 3600).quantize(_CENTS, rounding=ROUND_HALF_UP)


def _parse_now(text):
    if text is None:
        return datetime.now(timezone.utc)
    try:
        return ev.parse_ts(text)
    except ValueError:
        raise UsageError("invalid --now timestamp: %r" % text)


def _parse_day(text, today):
    if text == "today":
        return today
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise UsageError("invalid date %r (use YYYY-MM-DD or today)" % text)


def _midnight(d, tz):
    return datetime(d.year, d.month, d.day, tzinfo=tz)


def _resolve_period(arg, spec, today):
    try:
        return periods.resolve(arg, spec, today)
    except ValueError as e:
        raise UsageError(str(e))


def _period_range(p, tz):
    s, e = periods.bounds(p, tz)
    return {"start": s, "end": e, "first": p.start.isoformat(),
            "last": p.end.isoformat(), "label": periods.label(p)}


# ---------------------------------------------------------------- report

def _resolve_clients(cfg, wanted):
    keys = config.client_keys(cfg)
    if not keys:
        raise UsageError("no clients configured")
    if wanted is None:
        return keys[:], len(keys) == 1
    if wanted == "all":
        return keys[:], len(keys) == 1
    if wanted not in keys:
        raise UsageError("unknown client: %s (configured: %s)" % (wanted, ", ".join(keys)))
    return [wanted], True


def _range_for(args, cfg, client, tz, today):
    """The range dict for one client (or shared, when client is None)."""
    if args.all:
        return {"start": None, "end": None, "first": None, "last": None, "label": "All time"}
    if args.week is not None:
        return _period_range(_resolve_period(args.week, _WEEK_SPEC, today), tz)
    if args.since is not None or args.until is not None:
        since = _parse_day(args.since, today) if args.since else None
        until = _parse_day(args.until, today) if args.until else None
        if since and until and since > until:
            raise UsageError("--since %s is after --until %s" % (since, until))
        label = lambda d: periods.label(periods.Period(d, d, ""))
        if since and until:
            text = periods.label(periods.Period(since, until, ""))
        elif since:
            text = "Since " + label(since)
        else:
            text = "Until " + label(until)
        return {"start": _midnight(since, tz) if since else None,
                "end": _midnight(until + timedelta(days=1), tz) if until else None,
                "first": since.isoformat() if since else None,
                "last": until.isoformat() if until else None, "label": text}
    if args.period is not None or client is not None:
        spec = config.settings(cfg, client)["billing_period"]
        return _period_range(_resolve_period(args.period or "this", spec, today), tz)
    return _period_range(_resolve_period("this", _WEEK_SPEC, today), tz)


def _bucket_label(bucket, by):
    if by == "day":
        d = date.fromisoformat(bucket)
        return "%s %s" % (bucket, _DAYS[d.weekday()])
    return bucket


def _capped_count(tl_all, client, rng, project):
    """Capped turns of this client's billable projects that start inside the range."""
    n = 0
    for t in tl_all.capped_turns:
        c, proj = t.key
        if c != client or proj == engine.NON_BILLABLE:
            continue
        if project is not None and proj != project:
            continue
        if rng["start"] is not None and t.start < rng["start"]:
            continue
        if rng["end"] is not None and t.start >= rng["end"]:
            continue
        n += 1
    return n


def _manual_excluded(cfg, client, rng, project, manual):
    """Manual entries of this client in the range: the report leaves them out."""
    first = date.fromisoformat(rng["first"]) if rng["first"] else None
    last = date.fromisoformat(rng["last"]) if rng["last"] else None
    n, hours, amount = 0, Decimal(0), Decimal(0)
    for m in manual:
        if m["client"] != client or (project is not None and m["project"] != project):
            continue
        if (first is not None and m["date"] < first) or (last is not None and m["date"] > last):
            continue
        rate = Decimal(str(config.settings(cfg, client, m["project"])["hourly_rate"]))
        n += 1
        hours += m["hours"]
        amount += (m["hours"] * rate).quantize(_CENTS, rounding=ROUND_HALF_UP)
    return {"entries": n, "hours": hours.quantize(_CENTS, rounding=ROUND_HALF_UP),
            "amount": amount}


def _client_report(cfg, tl_all, client, rng, project, by, tz, manual):
    segs = rollup.billable(tl_all.segs, client, project)
    segs = rollup.clip(segs, rng["start"], rng["end"])
    totals = rollup.totals(segs, tz, by)
    rows = []
    for (bucket, (_, proj)), cell in sorted(totals.items(), key=lambda kv: (kv[0][0], kv[0][1][1])):
        you, agent = cell["you"], cell["agent"]
        if you + agent < 1:
            continue
        st = config.settings(cfg, client, proj)
        frac, rate = st["agent_billable_fraction"], st["hourly_rate"]
        billable = you + agent * frac
        amount = (_dec(billable) / 3600 * Decimal(str(rate))).quantize(_CENTS, rounding=ROUND_HALF_UP)
        rows.append({"bucket": bucket, "project": proj, "you": you, "agent": agent,
                     "billable": billable, "rate": rate, "frac": frac, "amount": amount})
    return {"client": client, "name": cfg["clients"][client].get("name", client),
            "range": rng, "rows": rows, "capped": _capped_count(tl_all, client, rng, project),
            "unclosed": health.unclosed_turns(tl_all, client, rng["start"], rng["end"], project),
            "manual": _manual_excluded(cfg, client, rng, project, manual),
            "paused": health.paused_summary(cfg, tl_all, client, rng["start"], rng["end"],
                                            tz, project),
            "by": by, "tz": tz,
            "settings": config.settings(cfg, client)}


def _table(rep, currency_money):
    by = rep["by"]
    head = ["Project", "Your time", "Agent time", "Billable", "Rate", "Amount"]
    body = []
    for r in rep["rows"]:
        cells = [r["project"], _duration(r["you"]), _duration(r["agent"]),
                 "%s h" % _hours2(r["billable"]), currency_money(r["rate"]) + "/h",
                 currency_money(r["amount"])]
        if by != "project":
            cells.insert(0, _bucket_label(r["bucket"], by))
        body.append(cells)
    rates = {r["rate"] for r in rep["rows"]}
    total = ["TOTAL", _duration(sum(r["you"] for r in rep["rows"])),
             _duration(sum(r["agent"] for r in rep["rows"])),
             "%s h" % _hours2(sum(r["billable"] for r in rep["rows"])),
             currency_money(rates.pop()) + "/h" if len(rates) == 1 else "",
             currency_money(sum((r["amount"] for r in rep["rows"]), Decimal(0)))]
    if by != "project":
        head.insert(0, "Period")
        total.insert(1, "")
    return head, body, total


def _footer(rep):
    st = rep["settings"]
    notes = ["Agent time billed at %s%%. Your time: gaps <= %sm in full, <= %sm capped at %sm, "
             "longer = %sm read + %sm ramp-up." % (
                 _num(st["agent_billable_fraction"] * 100), _num(st["engaged_gap_minutes"]),
                 _num(st["break_gap_minutes"]), _num(st["engaged_gap_minutes"]),
                 _num(st["read_allowance_minutes"]), _num(st["rampup_allowance_minutes"]))]
    if rep["capped"]:
        notes.append("Note: %d interrupted turn(s) capped at %s min."
                     % (rep["capped"], _num(st["unclosed_cap_minutes"])))
    w = health.unclosed_warning(rep["unclosed"], st["unclosed_cap_minutes"], rep["tz"])
    if w:
        notes.append("Note: %s." % w)
    w = health.paused_warning(*rep["paused"])
    if w:
        notes.append(w)
    return notes


def _manual_note(rep):
    m = rep["manual"]
    if not m["entries"]:
        return None
    return ("Excludes %d manual %s totalling %s h (%s) in this range; the invoice includes "
            "them." % (m["entries"], "entry" if m["entries"] == 1 else "entries", m["hours"],
                       lines.money(m["amount"], rep["settings"]["currency"])))


def _render_text(rep, md):
    st = rep["settings"]
    money = lambda v: lines.money(Decimal(str(v)), st["currency"])
    head, body, total = _table(rep, money)
    out = ["Client: %s" % rep["name"], rep["range"]["label"], ""]
    left = 2 if rep["by"] != "project" else 1
    if md:
        row = lambda cells: "| " + " | ".join(cells) + " |"
        out.append(row(head))
        out.append("| " + " | ".join("---" if i < left else "---:" for i in range(len(head))) + " |")
        out.extend(row(c) for c in body)
        out.append(row(["**%s**" % c if c else "" for c in total]))
    else:
        widths = [max(len(r[i]) for r in [head] + body + [total]) for i in range(len(head))]
        fmt = lambda cells: " | ".join(
            c.ljust(widths[i]) if i < left else c.rjust(widths[i]) for i, c in enumerate(cells)).rstrip()
        rule = "-+-".join("-" * w for w in widths)
        out.append(fmt(head))
        out.append(rule)
        out.extend(fmt(c) for c in body)
        out.append(rule)
        out.append(fmt(total))
    note = _manual_note(rep)
    if note:
        out.extend(["", note])
    out.append("")
    out.extend(_footer(rep))
    return out


def _json_client(rep):
    st = rep["settings"]
    rows = []
    for r in rep["rows"]:
        rows.append({"period": r["bucket"], "project": r["project"],
                     "you_seconds": r["you"], "agent_seconds": r["agent"],
                     "billable_seconds": r["billable"], "hourly_rate": r["rate"],
                     "amount": str(r["amount"])})
    rng = rep["range"]
    return {"client": rep["client"],
            "range": {"start": rng["first"], "end": rng["last"], "label": rng["label"]},
            "rows": rows, "capped": rep["capped"], "unclosed": len(rep["unclosed"]),
            "paused_hours": rep["paused"][0], "paused_kind": rep["paused"][2],
            "manual_excluded": {"entries": rep["manual"]["entries"],
                                "hours": str(rep["manual"]["hours"]),
                                "amount": str(rep["manual"]["amount"].quantize(_CENTS))}}


def _csv(reps):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["client", "period", "project", "you_hours", "agent_hours", "agent_billed_pct",
                "billable_hours", "hourly_rate", "amount"])
    for rep in reps:
        for r in rep["rows"]:
            w.writerow([rep["client"], r["bucket"], r["project"], _hours2(r["you"]),
                        _hours2(r["agent"]), _num(r["frac"] * 100), _hours2(r["billable"]),
                        _num(r["rate"]), r["amount"]])
    return buf.getvalue()


def _warnings(cfg, events, skipped, manual, manual_skipped):
    out = [health.skipped_warning(skipped, "events.jsonl"),
           health.skipped_warning(manual_skipped, "manual.jsonl"),
           health.hook_error_warning(True), health.unattributed_warning()]
    out += health.unknown_client_warnings(cfg, events, manual)
    return [w for w in out if w]


def cmd_report(args):
    cfg = config.load()
    tz = config.tz(cfg)
    now = _parse_now(args.now)
    today = now.astimezone(tz).date()
    if (args.since or args.until) and (args.period or args.week or args.all):
        raise UsageError("--since/--until cannot be combined with --period, --week or --all")
    selected, specific = _resolve_clients(cfg, args.client)
    if args.period is not None and not specific:
        raise UsageError("--period needs a specific --client when several clients are "
                         "configured (%s)" % ", ".join(selected))
    events, skipped = ev.load_events()
    manual, manual_skipped = ev.load_manual()
    if args.project is not None:
        known = set()
        for c in selected:
            known.update(cfg["clients"][c].get("projects", {}))
        known.update(e["project"] for e in events if e["client"] in selected)
        if args.project not in known:
            raise UsageError("unknown project: %s" % args.project)
    tl = engine.timeline(events, cfg, now)
    shared = None
    if not specific:
        shared = _range_for(args, cfg, None, tz, today)
    reps = []
    for c in selected:
        rng = shared if shared is not None else _range_for(args, cfg, c, tz, today)
        reps.append(_client_report(cfg, tl, c, rng, args.project, args.by, tz, manual))
    warnings = _warnings(cfg, events, skipped, manual, manual_skipped)

    if args.json:
        if specific:
            doc = _json_client(reps[0])
            doc["warnings"] = warnings
        else:
            doc = {"clients": [_json_client(r) for r in reps],
                   "capped": sum(r["capped"] for r in reps),
                   "unclosed": sum(len(r["unclosed"]) for r in reps), "warnings": warnings}
        print(json.dumps(doc, indent=2))
        return 0
    for w in warnings:
        print("warning: " + w, file=sys.stderr)
    if args.csv:
        sys.stdout.write(_csv(reps))
        return 0
    with_rows = [r for r in reps if r["rows"] or r["manual"]["entries"]
                 or r["paused"][2] is not None]
    if not with_rows:
        print("%s: no tracked time." % reps[0]["range"]["label"])
        return 0
    blocks = []
    for r in with_rows:
        blocks.append("\n".join(_render_text(r, args.md)))
    out = "\n\n".join(blocks)
    if not specific:
        sums = {}
        for r in with_rows:
            cur = r["settings"]["currency"]
            sums[cur] = sums.get(cur, Decimal(0)) + sum((x["amount"] for x in r["rows"]), Decimal(0))
        grand = ["Grand total %s: %s" % (cur, lines.money(v, cur)) for cur, v in sums.items()]
        if args.md:
            grand = ["**%s**" % g for g in grand]
        out += "\n\n" + "\n".join(grand)
    print(out)
    return 0


# ---------------------------------------------------------------- nonbillable

def _resolve_session(args, events):
    ids = {}
    for e in events:
        if e["session"]:
            ids.setdefault(e["session"], []).append(e)
    if args.session:
        if args.session in ids:
            return args.session, ids[args.session]
        matches = sorted(s for s in ids if s.startswith(args.session))
        if not matches:
            raise UsageError("no logged session matches %r" % args.session)
        if len(matches) > 1:
            raise UsageError("ambiguous session prefix %r matches: %s"
                             % (args.session, ", ".join(matches)))
        return matches[0], ids[matches[0]]
    env = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if env:              # inside Claude Code: never guess another session from the cwd
        if env in ids:
            return env, ids[env]
        raise UsageError("this session (%s) has no tracked events (it is outside every "
                         "client's scope)" % env[:8])
    cwd = os.path.realpath(args.cwd or os.getcwd())
    best = None
    for e in events:                      # events are sorted by time
        if e.get("event") == "start" and e["session"] and isinstance(e.get("cwd"), str) \
                and e["cwd"] and os.path.realpath(e["cwd"]) == cwd:
            best = e
    if best is None:
        raise UsageError("no logged session found for %s; pass a SESSION id" % cwd)
    return best["session"], ids[best["session"]]


def _save_validated(raw, path, original):
    """Write raw to path and re-validate it; restore the original text if that fails."""
    try:
        config.save(raw, path)
        config.load(path)
    except (config.ConfigError, OSError) as e:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(original)
        except OSError:
            pass
        raise UsageError("config would be invalid after the change, restored: %s" % e)


def _matching_prefix(cfg, client, sid):
    for prefix in config.settings(cfg, client)["non_billable_sessions"]:
        if sid.startswith(prefix):
            return prefix
    return None


def cmd_nonbillable(args):
    cfg = config.load()
    tz = config.tz(cfg)
    events, _ = ev.load_events()
    sid, evs = _resolve_session(args, events)
    clients = sorted({e["client"] for e in evs})
    if args.client is not None:
        if args.client not in cfg["clients"]:
            raise UsageError("unknown client: %s" % args.client)
        if args.client not in clients:
            raise UsageError("session %s has no events for client %s" % (sid, args.client))
        client = args.client
    elif len(clients) > 1:
        raise UsageError("session %s spans several clients (%s); pass --client"
                         % (sid, ", ".join(clients)))
    else:
        client = clients[0]
        if client not in cfg["clients"]:
            raise UsageError("session %s belongs to unknown client: %s" % (sid, client))
    mine = [e for e in evs if e["client"] == client]

    path = os.path.join(config.home(), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            original = f.read()
        raw = json.loads(original)
    except (OSError, ValueError) as e:
        raise UsageError("cannot read %s: %s" % (path, e))
    raw_client = raw["clients"][client]
    listed = raw_client.get("non_billable_sessions", [])
    over = raw_client.get("overrides", {}).get("non_billable_sessions", [])
    after = cfg
    if args.remove:
        if sid in listed or sid in over:
            raw_client["non_billable_sessions"] = [x for x in listed if x != sid]
            if sid in over:
                raw_client["overrides"]["non_billable_sessions"] = [x for x in over if x != sid]
            _save_validated(raw, path, original)
            after = config.load(path)
            status = "billable again"
        else:
            status = "already billable"
    elif _matching_prefix(cfg, client, sid) is not None:
        status = "already non-billable"
    else:
        raw_client["non_billable_sessions"] = list(listed) + [sid]
        _save_validated(raw, path, original)
        status = "now non-billable"
    if args.remove:
        prefix = _matching_prefix(after, client, sid)
        if prefix is not None:
            status = 'still non-billable: matched by "%s"' % prefix

    first = min(e["_t"] for e in mine).astimezone(tz)
    last = max(e["_t"] for e in mine).astimezone(tz)
    fmt = "%Y-%m-%d %H:%M %Z"
    print("Session: %s" % sid)
    print("Client: %s" % client)
    print("Project: %s" % ", ".join(sorted({e["project"] for e in mine})))
    print("First event: %s" % first.strftime(fmt))
    print("Last event: %s" % last.strftime(fmt))
    print(status)
    return 0


# ---------------------------------------------------------------- import

def cmd_import(args):
    cfg = config.load()
    keys = config.client_keys(cfg)
    if args.client not in keys:
        raise UsageError("unknown client: %s (configured: %s)" % (args.client, ", ".join(keys)))
    if not os.path.isfile(args.src):
        raise UsageError("source file not found: %s" % args.src)
    try:
        c = ev.import_legacy(args.src, args.client, dry_run=args.dry_run)
    except (ValueError, OSError) as e:      # the importer's own refusals and I/O errors
        raise UsageError(str(e))
    print("%sread %d, imported %d, duplicates %d, skipped %d" % (
        "dry run: " if args.dry_run else "", c["read"], c["imported"],
        c["duplicates"], c["skipped"]))
    return 0


# ---------------------------------------------------------------- reattribute

def cmd_reattribute(args):
    cfg = config.load()

    def attribute(e):
        cwd, account = e.get("cwd"), e.get("account")
        if not isinstance(cwd, str) or not cwd:
            return None
        return scope.detect(cfg, cwd, account if isinstance(account, str) else "")

    try:
        c = ev.reattribute(attribute, dry_run=args.dry_run)
    except (ValueError, OSError) as e:
        raise UsageError(str(e))
    print("%sread %d, attributed %d, duplicates %d, unmatched %d" % (
        "dry run: " if args.dry_run else "", c["read"], c["attributed"], c["duplicates"],
        c["unmatched"]))
    return 0


# ---------------------------------------------------------------- doctor

def _check_scope(key, client):
    scope = client["scope"]
    if not any(scope.get(k) for k in ("accounts", "directories", "repos")):
        return ("FAIL", "client %s scope" % key, "no accounts, directories or repos listed")
    missing = [p for k in ("directories", "accounts") for p in scope.get(k, [])
               if not os.path.isdir(config.expand(p))]
    if missing:
        return ("WARN", "client %s scope" % key, "not found: %s" % ", ".join(missing))
    return ("OK", "client %s scope" % key, "ok")


def _check_browser(cfg):
    try:
        from ats import render
        found = render.find_browser(cfg)
    except (ImportError, AttributeError):
        return ("WARN", "pdf browser", "not checked")
    if found:
        return ("OK", "pdf browser", str(found))
    return ("WARN", "pdf browser", "no Chrome/Edge/Chromium found; the timesheet will be "
            "written as HTML to print manually")


def _check_hook_errors():
    n, last = health.hook_errors()
    if not n:
        return ("OK", "hook errors", "none")
    return ("WARN", "hook errors", "%d logged; last: %s" % (n, last))


def _clear_hook_errors():
    """Truncate hook-errors.log; returns how many errors it held."""
    n, _ = health.hook_errors()
    path = os.path.join(config.home(), "hook-errors.log")
    if os.path.exists(path):
        try:
            with open(path, "w"):
                pass
        except OSError as e:
            raise UsageError("cannot clear %s: %s" % (path, e.strerror or e))
    return n


def _check_plugin(account):
    name = "plugin enabled in %s" % account
    try:
        with open(os.path.join(config.expand(account), "settings.json"), encoding="utf-8") as f:
            plugins = json.load(f).get("enabledPlugins", {})
        if not isinstance(plugins, dict):
            plugins = {}
    except (OSError, ValueError, AttributeError):
        return ("WARN", name, "unknown")
    if any(k.startswith("timesheet@") and v for k, v in plugins.items()):
        return ("OK", name, "enabled")
    return ("WARN", name, "not enabled")


def _check_events_log():
    count, latest = 0, None
    for e in ev.raw_objects(os.path.join(config.home(), "events.jsonl")):
        try:
            t = ev.parse_ts(e.get("ts")) if e is not None else None
        except ValueError:
            t = None
        if t is not None:
            count += 1
            if latest is None or t > latest[0]:
                latest = (t, e["ts"])
    if not count:
        return ("WARN", "events log", "no events logged yet")
    return ("OK", "events log", "%d events; latest %s" % (count, latest[1]))


def _check_unknown_clients(cfg):
    events, _ = ev.load_events()
    manual, _ = ev.load_manual()
    found = health.unknown_client_warnings(cfg, events, manual)
    if not found:
        return ("OK", "unknown clients", "none")
    return ("WARN", "unknown clients", "; ".join(found))


def _check_pauses(cfg):
    try:
        items = pauses.load()
    except pauses.PauseError as e:
        return ("FAIL", "pauses", str(e))
    now = datetime.now(timezone.utc)
    tz = config.tz(cfg) if cfg is not None else timezone.utc
    live = [p for p in items if pauses.active([p], p["client"], now) is not None]
    found = ["%s since %s for %s" % (
        "away" if pauses.mode_of(p) == "mine" else "paused",
        pauses.local(pauses.start_of(p), tz),
        "all clients" if p["client"] == pauses.ALL else p["client"]) for p in live]
    pairs = pauses.overlaps(items)
    if pairs:
        found.append("overlapping windows: " + ", ".join("%s and %s" % pair for pair in pairs))
    if not found:
        return ("OK", "pauses", "%d window(s), none active" % len(items) if items else "none")
    return ("WARN", "pauses", "; ".join(found))


def cmd_doctor(args):
    checks = [("OK" if sys.version_info >= (3, 9) else "FAIL", "python",
               "%d.%d.%d" % sys.version_info[:3])]
    cfg = None
    try:
        cfg = config.load()
        checks.append(("OK", "config", "loaded"))
    except config.ConfigError as e:
        checks.append(("FAIL", "config", str(e)))
    if cfg is not None:
        for key in config.client_keys(cfg):
            checks.append(_check_scope(key, cfg["clients"][key]))
        checks.append(_check_browser(cfg))
    checks.append(_check_hook_errors())
    cleared = _clear_hook_errors() if args.clear_hook_errors else None
    if cfg is not None:
        accounts = []
        for client in cfg["clients"].values():
            for a in client["scope"].get("accounts", []):
                if a not in accounts:
                    accounts.append(a)
        checks.extend(_check_plugin(a) for a in accounts)
    checks.append(_check_events_log())
    if cfg is not None:
        checks.append(_check_unknown_clients(cfg))
    w = health.unattributed_warning()
    checks.append(("WARN", "unattributed events", w) if w else
                  ("OK", "unattributed events", "none"))
    checks.append(_check_pauses(cfg))
    if cfg is not None:
        var = cfg["mercury"].get("api_key_env", "")
        for key in config.client_keys(cfg):
            if cfg["clients"][key]["mercury"].get("enabled"):
                if not var:
                    checks.append(("WARN", "mercury token %s" % key,
                                   "no token env var configured (mercury.api_key_env)"))
                    continue
                isset = bool(os.environ.get(var))
                checks.append(("OK" if isset else "WARN", "mercury token %s" % key,
                               "set" if isset else "not set"))
    ok = all(c[0] != "FAIL" for c in checks)
    if args.json:
        doc = {"checks": [{"name": n, "status": s, "detail": d} for s, n, d in checks], "ok": ok}
        if cleared is not None:
            doc["cleared_hook_errors"] = cleared
        print(json.dumps(doc, indent=2))
    else:
        for s, n, d in checks:
            print("%s  %s: %s" % (s, n, d))
        if cleared is not None:
            print("cleared %d hook error(s) from hook-errors.log" % cleared)
    return 0 if ok else 1


# ---------------------------------------------------------------- status

def _status_clients(cfg, args):
    """The client keys to show: the named one, else the one the cwd belongs to, else
    the only one, else all of them."""
    keys = config.client_keys(cfg)
    if not keys:
        raise UsageError("no clients configured")
    if args.client is not None:
        if args.client not in keys:
            raise UsageError("unknown client: %s (configured: %s)"
                             % (args.client, ", ".join(keys)))
        return [args.client]
    found = status.detect_client(cfg, args.cwd or os.getcwd(), status.account_dir())
    if found is not None:
        return [found]
    return keys[:]


def _short_day(iso):
    d = date.fromisoformat(iso)
    return periods.short_label(periods.Period(d, d, ""))


def _status_block(summary, show_amounts, tz):
    """One client's status as text lines; only that client's figures."""
    cur = summary["current"]

    def money(text):
        return lines.money(Decimal(text), cur["currency"])

    head = "Current period: %s (ends %s): %s h" % (cur["label"], _short_day(cur["end"]),
                                                   cur["hours"])
    if show_amounts:
        head += " (%s)" % money(cur["amount"])
    out = [summary["client"]["name"], head]
    if summary["paused"]:
        out.insert(1, status.paused_text(summary, tz))
    if summary["last_period"] is not None:
        out.append("Last period: %s: %s" % (summary["last_period"]["label"], summary["last"]))
    for d in summary["due"]:
        figures = "%s h" % d["hours"]
        if show_amounts:
            figures += ", %s" % money(d["amount"])
        out.append("Invoice due: %s: %s (%s)" % (d["label"], figures, d["state"]))
    out.extend(summary["paused_notes"])
    return out


def cmd_status(args):
    cfg = config.load()
    now = _parse_now(args.now)
    clients = _status_clients(cfg, args)
    opts = cfg["status"]
    if args.line:
        if len(clients) == 1:         # outside every scope there is no one client to show
            text = status.line(status.summary(cfg, clients[0], now), opts["components"],
                               opts["show_amounts"], config.tz(cfg))
            if text:
                print(text)
        return 0
    summaries = [status.summary(cfg, c, now) for c in clients]
    if args.json:
        doc = summaries[0] if len(summaries) == 1 else {"clients": summaries}
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        return 0
    tz = config.tz(cfg)
    print("\n\n".join("\n".join(_status_block(s, opts["show_amounts"], tz)) for s in summaries))
    return 0


# ---------------------------------------------------------------- pause / resume

_STALE_NOTE = ("An approved draft for an affected period is now marked stale and has to be "
               "approved again.")


def _pause_client(cfg, args):
    """The client to pause or resume: "*" for --all, the named one, else the one the cwd
    belongs to, else the only one."""
    keys = config.client_keys(cfg)
    if not keys:
        raise UsageError("no clients configured")
    if getattr(args, "all", False):
        return pauses.ALL
    if args.client is not None:
        if args.client not in keys:
            raise UsageError("unknown client: %s (configured: %s)"
                             % (args.client, ", ".join(keys)))
        return args.client
    found = status.detect_client(cfg, args.cwd or os.getcwd(), status.account_dir())
    if found is not None:
        return found
    if len(keys) == 1:
        return keys[0]
    raise UsageError("this directory belongs to no client; pass --client KEY (%s) or --all"
                     % ", ".join(keys))


def _who(cfg, client):
    if client == pauses.ALL:
        return "all clients"
    return cfg["clients"].get(client, {}).get("name", client)


def _pause_timeline(cfg, now, items):
    events, _ = ev.load_events()
    return engine.timeline(events, cfg, now, pauses=items)


def _unbilled(cfg, tl, window, now, tz, only=None):
    """Hours (text) of logged time inside the window that it keeps off the bill, for
    the client `only`, or for every client the window covers."""
    start = pauses.start_of(window)
    end = pauses.end_of(window) or now
    if only is not None:
        clients = [only]
    elif window["client"] == pauses.ALL:
        clients = config.client_keys(cfg)
    else:
        clients = [window["client"]]
    return health.paused_hours(sum(health.paused_time(cfg, tl, c, start, end, tz)[0]
                                   for c in clients))


def _away(window):
    return pauses.mode_of(window) == "mine"


def _kind(window):
    return "away" if _away(window) else "pause"


def _span(window, now, tz):
    """'<start> to <end>' / '<start> until <planned>' / '<start>, still paused'."""
    text = pauses.local(pauses.start_of(window), tz)
    if window.get("end") is not None:
        return "%s to %s" % (text, pauses.local(pauses.parse_ts(window["end"]), tz))
    if window.get("until") is not None:
        until = pauses.parse_ts(window["until"])
        return "%s %s %s" % (text, "until" if until > now else "to", pauses.local(until, tz))
    return text + (", still away" if _away(window) else ", still paused")


def _others_note(window, reopened):
    """What happened to the other clients of a "*" window that one client left."""
    kind = _kind(window)
    return ("That %s covered all clients: the other %d client(s) stay %s (%s)."
            % (kind, len(reopened), "away" if kind == "away" else "paused",
               ", ".join(r["id"] for r in reopened)))


def cmd_pause(args):
    """pause and away: the same command, opening an "all" or a "mine" window."""
    cfg = config.load()
    tz = config.tz(cfg)
    now = _parse_now(args.now)
    client = _pause_client(cfg, args)
    mode = args.mode
    items = pauses.load()
    start = pauses.parse_when(args.at, tz, now) if args.at is not None else now
    if start > now:
        raise UsageError("--at is in the future: %s" % pauses.local(start, tz))
    until = None
    if args.duration is not None:
        until = start + pauses.parse_duration(args.duration)
    elif args.until is not None:
        until = pauses.parse_when(args.until, tz, now, roll_forward=True)
        if until <= start:
            raise UsageError("--until is not after the start of the %s" % (
                "away window" if mode == "mine" else "pause"))
    items, events = pauses.open_window(items, client, mode, start, until, now,
                                       config.client_keys(cfg))
    opened = False
    for what, w, reopened in events:
        if what == "already":
            text = "Already %s: %s since %s" % (
                "away" if _away(w) else "paused", _who(cfg, client),
                pauses.local(pauses.start_of(w), tz))
            if w.get("until") is not None:
                text += " until " + pauses.local(pauses.parse_ts(w["until"]), tz)
            if mode == "mine" and not _away(w):
                text += " (a full pause covers away)"
            print(text + ". Nothing changed.")
            return 0
        if what == "ended":
            print("Ended %s %s at %s" % (_kind(w), w["id"],
                                         pauses.local(pauses.parse_ts(w["end"]), tz)))
            if reopened:
                print(_others_note(w, reopened))
        elif what == "left":
            print("Left %s %s as it is%s." % (
                _kind(w), w["id"],
                " (a full pause covers away)" if mode == "mine" and not _away(w) else ""))
        else:
            opened = True
            text = ("Away for %s from %s" if mode == "mine" else "Paused %s at %s") % (
                _who(cfg, w["client"]), pauses.local(start, tz))
            if w["until"] is not None:
                text += " until " + pauses.local(until, tz)
            print(text)
    if not opened:
        print("Nothing changed.")
        return 0
    pauses.save(items)
    if mode == "mine":
        print("Your own time is not counted while away; agent work you started keeps "
              "running and is still billed. Come back with /timesheet:resume.")
    else:
        print("Time is still recorded while paused; it is just not billed. "
              "Resume with /timesheet:resume.")
    return 0


def cmd_resume(args):
    cfg = config.load()
    tz = config.tz(cfg)
    now = _parse_now(args.now)
    client = _pause_client(cfg, args)
    items = pauses.load()
    at = pauses.parse_when(args.at, tz, now) if args.at is not None else now
    if at > now:
        raise UsageError("--at is in the future: %s" % pauses.local(at, tz))
    closed, reopened = pauses.resume(items, client, at, now, config.client_keys(cfg))
    if not closed:
        print("Nothing is paused." if client == pauses.ALL
              else "%s is not paused. Nothing changed." % _who(cfg, client))
        return 0
    pauses.save(items)
    tl = _pause_timeline(cfg, now, items)
    only = None if client == pauses.ALL else client
    print("Resumed %s at %s" % (_who(cfg, client), pauses.local(at, tz)))
    for w in closed:
        length = (at - pauses.start_of(w)).total_seconds()
        print("Closed %s %s: %s (%s)" % (_kind(w), w["id"], _span(w, now, tz),
                                         _duration(length)))
        print("%s h of logged time in that window are not billed%s."
              % (_unbilled(cfg, tl, w, now, tz, only),
                 "" if only is None else " for " + _who(cfg, client)))
        if w["client"] == pauses.ALL and only is not None and reopened:
            print(_others_note(w, reopened))
    if at < now:
        print(_STALE_NOTE)
    return 0


def _pause_rows(cfg, items, tl, now, tz, only):
    rows = []
    for w in sorted(items, key=lambda w: (pauses.start_of(w), w["id"]), reverse=True):
        if only is not None and w["client"] not in (only, pauses.ALL):
            continue
        rows.append({"id": w["id"], "client": w["client"], "mode": pauses.mode_of(w),
                     "start": w["start"],
                     "end": w["end"], "until": w["until"],
                     "active": pauses.active([w], w["client"], now) is not None,
                     "unbilled_hours": _unbilled(cfg, tl, w, now, tz, only),
                     "_span": _span(w, now, tz)})
    return rows


def _describe(cfg, tl, window, now, tz):
    return "%s (%s, %s): %s, %s h unbilled" % (
        window["id"], _who(cfg, window["client"]), "away" if _away(window) else "paused",
        _span(window, now, tz),
        _unbilled(cfg, tl, window, now, tz))


def cmd_pauses(args):
    cfg = config.load()
    tz = config.tz(cfg)
    now = _parse_now(args.now)
    if args.client is not None and args.client not in config.client_keys(cfg):
        raise UsageError("unknown client: %s (configured: %s)"
                         % (args.client, ", ".join(config.client_keys(cfg))))
    if args.edit is None and (args.start is not None or args.end is not None
                              or args.mode is not None):
        raise UsageError("--start, --end and --mode need --edit ID")
    items = pauses.load()
    if args.remove is not None:
        window = pauses.find(items, args.remove)
        print("Removed %s " % _kind(window) + _describe(cfg, _pause_timeline(cfg, now, items), window,
                                           now, tz))
        pauses.remove(items, args.remove)
        pauses.save(items)
        print("That time is billable again.")
        print(_STALE_NOTE)
        return 0
    if args.edit is not None:
        window = pauses.find(items, args.edit)
        before = "Before: " + _describe(cfg, _pause_timeline(cfg, now, items), window, now, tz)
        start = pauses.parse_when(args.start, tz, now) if args.start is not None else None
        end = pauses.parse_when(args.end, tz, now) if args.end is not None else None
        pauses.edit(items, args.edit, start, end, now, args.mode)
        pauses.save(items)
        print(before)
        print("After:  " + _describe(cfg, _pause_timeline(cfg, now, items), window, now, tz))
        print(_STALE_NOTE)
        return 0
    rows = _pause_rows(cfg, items, _pause_timeline(cfg, now, items), now, tz, args.client)
    if args.json:
        print(json.dumps([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
                         indent=2))
        return 0
    if not rows:
        print("No pauses.")
        return 0
    for r in rows:
        print("%s  %s  %s  %s  %s h unbilled" % (
            r["id"], "all" if r["client"] == pauses.ALL else r["client"],
            "away" if r["mode"] == "mine" else "paused", r["_span"], r["unbilled_hours"]))
    return 0


# ---------------------------------------------------------------- statusline

def cmd_statusline(args):
    path = launcher.settings_path(args.settings)
    try:
        if args.print:
            print("launcher: %s" % launcher.launcher_path())
            print(json.dumps(launcher.snippet(), indent=2))
        elif args.install:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            print(launcher.install(path, root, force=args.force))
        else:
            print(launcher.uninstall(path))
    except launcher.LauncherError as e:
        raise UsageError(str(e))
    return 0


# ---------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(prog="timesheet.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser(
        "report", help="tracked time per client and project",
        description="Quick report of tracked time. Each row's amount is billable time x "
                    "rate, rounded to cents. It differs from the invoice in two ways: it "
                    "leaves out manual entries (manual.jsonl; a note says how many and how "
                    "much), and it does not apply the invoice's per-line rounding.")
    r.add_argument("--client", help="client key, or 'all' (default: the only client, else all)")
    rng = r.add_mutually_exclusive_group()
    rng.add_argument("--period", help="this | last | YYYY-MM-DD | YYYY-Www | START_END, "
                                      "in the client's billing period (needs one client)")
    rng.add_argument("--week", help="this | last | YYYY-Www | YYYY-MM-DD; always Monday-Sunday")
    rng.add_argument("--all", action="store_true", help="all recorded time")
    r.add_argument("--since", help="YYYY-MM-DD or today")
    r.add_argument("--until", help="YYYY-MM-DD or today (inclusive)")
    r.add_argument("--by", choices=["project", "day", "week"], default="project")
    r.add_argument("--project", help="only this project")
    out = r.add_mutually_exclusive_group()
    out.add_argument("--md", action="store_true", help="GitHub markdown table")
    out.add_argument("--csv", action="store_true")
    out.add_argument("--json", action="store_true")
    r.add_argument("--now", help="fix the clock (ISO 8601), for tests")
    r.set_defaults(func=cmd_report)

    n = sub.add_parser("ignore", aliases=["nonbillable"],
                       help="ignore a session: mark it non-billable (or --remove)")
    n.add_argument("session", nargs="?", help="session id or unique prefix "
                   "(default: $CLAUDE_CODE_SESSION_ID when set, else the latest session "
                   "in --cwd)")
    n.add_argument("--client", help="client key (default: the client of the session's events)")
    n.add_argument("--cwd", help="directory used to find the current session (default: here)")
    n.add_argument("--remove", action="store_true", help="make the session billable again")
    n.set_defaults(func=cmd_nonbillable)

    i = sub.add_parser("import", help="import an older event log (no client field)")
    i.add_argument("--from", dest="src", required=True, metavar="PATH",
                   help="legacy events file (JSON lines)")
    i.add_argument("--client", required=True, help="client key to assign the events to")
    i.add_argument("--dry-run", action="store_true", help="count only; write nothing")
    i.set_defaults(func=cmd_import)

    a = sub.add_parser("reattribute",
                       help="attribute events logged while the config was unreadable")
    a.add_argument("--dry-run", action="store_true", help="count only; write nothing")
    a.set_defaults(func=cmd_reattribute)

    d = sub.add_parser("doctor", help="check the setup")
    d.add_argument("--json", action="store_true")
    d.add_argument("--clear-hook-errors", action="store_true",
                   help="empty hook-errors.log after reporting its count")
    d.set_defaults(func=cmd_doctor)

    s = sub.add_parser(
        "status", help="where each client stands with invoicing",
        description="Hours in the current billing period and the completed periods that "
                    "still need an invoice, from the same computation as the invoice draft. "
                    "Without --client the client is the one whose scope --cwd falls in; "
                    "outside every scope, all clients are listed. Amounts are shown only "
                    "when status.show_amounts is true, except in --json, which is meant "
                    "for Claude and other tools and always includes amounts.")
    s.add_argument("--client", help="client key (default: detected from --cwd)")
    s.add_argument("--cwd", help="directory used to detect the client (default: here)")
    fmt = s.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true",
                     help="the summary as JSON (always includes amounts)")
    fmt.add_argument("--line", action="store_true",
                     help="the one-line form used by the status line (status.components, "
                          "status.show_amounts); prints nothing unless one client applies")
    s.add_argument("--now", help="fix the clock (ISO 8601), for tests")
    s.set_defaults(func=cmd_status)

    when = "HH:MM (today), YYYY-MM-DD or an ISO timestamp"
    kinds = (
        ("pause", "all", "pause", "stop billing for a while; time is still recorded",
         "Open a pause window. The hooks keep logging; time inside the window is not "
         "billed and is reported, so a pause that ran too long can be fixed afterwards "
         "(resume --at, pauses --edit). "),
        ("away", "mine", "away window", "stop counting your own time; agent work stays billed",
         "Open an away window: your own time inside it is not billed, agent work that "
         "keeps running is. It is reported and can be fixed afterwards like a pause "
         "(resume --at, pauses --edit). A full pause is stronger and covers away. "))
    for name, mode, noun, short, text in kinds:
        pz = sub.add_parser(
            name, help=short,
            description=text + "Without --client or --all the client is the one --cwd "
                               "belongs to, else the only one.")
        who = pz.add_mutually_exclusive_group()
        who.add_argument("--client", help="client key (default: detected from --cwd)")
        who.add_argument("--all", action="store_true", help="every client")
        span = pz.add_mutually_exclusive_group()
        span.add_argument("--for", dest="duration", metavar="DURATION",
                          help="resume automatically after e.g. 90m, 2h, 1h30m or 1d")
        span.add_argument("--until", metavar="WHEN",
                          help="resume automatically at " + when + "; an HH:MM already "
                               "past means tomorrow")
        pz.add_argument("--at", metavar="WHEN", help="the %s began at " % noun + when
                        + " (not in the future; default: now)")
        pz.add_argument("--cwd", help="directory used to detect the client (default: here)")
        pz.add_argument("--now", help="fix the clock (ISO 8601), for tests")
        pz.set_defaults(func=cmd_pause, mode=mode)

    rs = sub.add_parser(
        "resume", help="end the current pause or away window",
        description="End the pause or away window that is active now. --at ends it earlier (for a pause "
                    "you forgot to end): the time after it is billable again.")
    who = rs.add_mutually_exclusive_group()
    who.add_argument("--client", help="client key (default: detected from --cwd)")
    who.add_argument("--all", action="store_true", help="end every active pause")
    rs.add_argument("--at", metavar="WHEN", help="billing resumed at " + when
                    + " (not before the pause began, not in the future; default: now)")
    rs.add_argument("--cwd", help="directory used to detect the client (default: here)")
    rs.add_argument("--now", help="fix the clock (ISO 8601), for tests")
    rs.set_defaults(func=cmd_resume)

    pl = sub.add_parser(
        "pauses", help="list, remove or edit pause and away windows",
        description="List the pause and away windows, newest first, with the logged hours each one "
                    "keeps off the bill. --remove makes a window's time billable again; "
                    "--edit changes when it began or ended.")
    pl.add_argument("--client", help="only this client's windows and hours")
    pl.add_argument("--json", action="store_true")
    change = pl.add_mutually_exclusive_group()
    change.add_argument("--remove", metavar="ID", help="delete a window, e.g. p2")
    change.add_argument("--edit", metavar="ID", help="change a window; with --start / --end")
    pl.add_argument("--start", metavar="WHEN", help="with --edit: new start, " + when)
    pl.add_argument("--end", metavar="WHEN", help="with --edit: new end, " + when)
    pl.add_argument("--mode", choices=list(pauses.MODES),
                    help="with --edit: all = a full pause, mine = away (only your own time)")
    pl.add_argument("--now", help="fix the clock (ISO 8601), for tests")
    pl.set_defaults(func=cmd_pauses)

    b = sub.add_parser(
        "statusline", help="install or remove the optional Claude Code status line",
        description="Claude Code has a single statusLine setting per account, so nothing "
                    "is changed unless you ask. --install writes a small launcher into the "
                    "data dir and points statusLine at it; a different existing status "
                    "line is never replaced unless --force is given.")
    act = b.add_mutually_exclusive_group(required=True)
    act.add_argument("--install", action="store_true",
                     help="write the launcher and set statusLine in the settings file")
    act.add_argument("--uninstall", action="store_true",
                     help="remove statusLine if it is this plugin's; leave anything else")
    act.add_argument("--print", action="store_true",
                     help="show the launcher path and the settings snippet; change nothing")
    b.add_argument("--settings", metavar="PATH",
                   help="settings file to edit (default: settings.json in "
                        "$CLAUDE_CONFIG_DIR, else ~/.claude)")
    b.add_argument("--force", action="store_true",
                   help="with --install: replace a different existing status line")
    b.set_defaults(func=cmd_statusline)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (config.ConfigError, UsageError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
