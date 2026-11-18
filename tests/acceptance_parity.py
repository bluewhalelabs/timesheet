#!/usr/bin/env python3
"""Acceptance check: the plugin's engine against the private prototype on a real log.

Not collected by unittest (the name does not start with test_) and silent on import.
Reads only; nothing is written to disk. Exit 0 when every project's agent and
your-time seconds agree within --tolerance and both sides see the same projects,
1 when they differ, 2 on bad arguments or missing files."""
import argparse
import importlib.util
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEGLIGIBLE = 1.0        # a project with less than this on both sides is ignored


class Problem(Exception):
    pass


def _paths():
    for p in (REPO, os.path.join(REPO, "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)


def build_parser():
    p = argparse.ArgumentParser(prog="acceptance_parity.py", description=__doc__)
    p.add_argument("--reference", required=True, help="the prototype's timesheet.py")
    p.add_argument("--old-log", required=True, help="the prototype's events.jsonl")
    p.add_argument("--old-config", help="the prototype's config.json (default: its built-in defaults)")
    p.add_argument("--home", required=True,
                   help="timesheet data dir with config.json and the imported events.jsonl")
    p.add_argument("--client", required=True, help="client key the log was imported under")
    p.add_argument("--now", help="ISO timestamp used as now by both sides (default: current time)")
    p.add_argument("--tolerance", type=float, default=1.0, help="seconds (default 1.0)")
    return p


def load_reference(path):
    spec = importlib.util.spec_from_file_location("_ats_acceptance_reference", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def reference_side(args, now):
    """Run the prototype exactly as its main does for an all-time report."""
    from tests.test_parity import frozen_datetime
    ref = load_reference(args.reference)
    ref.LOG = args.old_log
    # a path that does not exist makes the prototype use its built-in defaults
    ref.CONFIG = args.old_config or os.path.join(args.home, "no-such-prototype-config.json")
    ref.datetime = frozen_datetime(now)
    cfg = ref.load_config()
    excluded = tuple(cfg.get("exclude_sessions", [])) or ("\0",)
    events = ref.load_events()
    for e in events:
        if e.get("session", "").startswith(excluded):
            e["project"] = ref.NON_BILLABLE
    turns, capped = ref.agent_turns(events, timedelta(minutes=float(cfg["unclosed_cap_minutes"])))
    a_segs, blocks = ref.agent_segments(turns)
    h_segs = ref.human_segments(blocks, cfg)
    totals = defaultdict(float)
    for kind, segs in (("agent", a_segs), ("you", h_segs)):
        for a, b, project, weight in segs:
            if project != ref.NON_BILLABLE:
                totals[project, kind] += (b - a).total_seconds() * weight
    return totals, capped, len(events)


def plugin_side(args, now):
    os.environ["TIMESHEET_HOME"] = args.home
    from ats import config, engine, events as ev, rollup
    cfg = config.load()
    if args.client not in config.client_keys(cfg):
        raise Problem("client %r is not in %s" % (args.client, os.path.join(args.home, "config.json")))
    events, _skipped = ev.load_events()
    tl = engine.timeline(events, cfg, now, prototype_rules=True, pauses=[])
    totals = defaultdict(float)
    for s in rollup.billable(tl.segs, args.client):
        totals[s.key[1], s.kind] += (s.end - s.start).total_seconds() * s.weight
    return totals, tl.capped, len(events)


def compare(ref_totals, new_totals, tolerance):
    """(rows, offenders). rows: (project, ref_agent, new_agent, ref_you, new_you)."""
    rows, offenders = [], []
    for project in sorted({k[0] for k in ref_totals} | {k[0] for k in new_totals}):
        ra, ry = ref_totals[project, "agent"], ref_totals[project, "you"]
        na, ny = new_totals[project, "agent"], new_totals[project, "you"]
        if max(ra, ry, na, ny) < NEGLIGIBLE:
            continue
        rows.append((project, ra, na, ry, ny))
        on_ref, on_new = max(ra, ry) >= NEGLIGIBLE, max(na, ny) >= NEGLIGIBLE
        if on_ref != on_new:
            offenders.append("%s: only on the %s side" % (project, "reference" if on_ref else "plugin"))
        elif abs(ra - na) >= tolerance or abs(ry - ny) >= tolerance:
            offenders.append("%s: agent differs by %.1f s, your time by %.1f s"
                             % (project, na - ra, ny - ry))
    return rows, offenders


def print_table(rows, ref_capped, new_capped, ref_count, new_count):
    width = max([len(r[0]) for r in rows] + [len("project"), len("TOTAL")])
    head = "%-*s | %12s | %12s | %9s | %12s | %12s | %9s" % (
        width, "project", "agent ref", "agent new", "Δ", "you ref", "you new", "Δ")
    print(head)
    print("-" * len(head))
    def line(name, ra, na, ry, ny):
        print("%-*s | %12.1f | %12.1f | %+9.1f | %12.1f | %12.1f | %+9.1f" % (
            width, name, ra, na, na - ra, ry, ny, ny - ry))

    for row in rows:
        line(*row)
    print("-" * len(head))
    line("TOTAL", *[sum(r[i] for r in rows) for i in (1, 2, 3, 4)])
    print()
    print("capped turns:  reference %s, plugin %s" % (ref_capped, new_capped))
    print("events loaded: reference %d, plugin %d" % (ref_count, new_count))


def run(args):
    _paths()
    from ats import events as ev
    for label, path in (("--reference", args.reference), ("--old-log", args.old_log)):
        if not os.path.isfile(path):
            raise Problem("%s: no such file: %s" % (label, path))
    if args.old_config and not os.path.isfile(args.old_config):
        raise Problem("--old-config: no such file: %s" % args.old_config)
    if not os.path.isdir(args.home):
        raise Problem("--home: no such directory: %s" % args.home)
    if args.tolerance < 0:
        raise Problem("--tolerance must not be negative")
    try:
        now = ev.parse_ts(args.now) if args.now else datetime.now(timezone.utc)
    except ValueError:
        raise Problem("invalid --now timestamp: %r" % args.now)
    ref_totals, ref_capped, ref_count = reference_side(args, now)
    new_totals, new_capped, new_count = plugin_side(args, now)
    rows, offenders = compare(ref_totals, new_totals, args.tolerance)
    print_table(rows, ref_capped, new_capped, ref_count, new_count)
    if offenders:
        print()
        print("MISMATCH (tolerance %.1f s):" % args.tolerance)
        for line in offenders:
            print("  " + line)
        return 1
    print()
    print("OK: all projects agree within %.1f s" % args.tolerance)
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Problem as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
