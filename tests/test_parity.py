"""Parity with the private prototype, when it is present locally (it is never committed)."""
import importlib.util
import os
import random
import unittest
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from tests import helpers
from ats import config, engine

REFERENCE = os.path.join(helpers.REPO, "reference", "timesheet.py")
SEEDS = range(200)
PROJECTS = ("project-a", "project-b", "project-c")
BASE = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
PREFIX = "nb"


def load_reference():
    spec = importlib.util.spec_from_file_location("_ats_parity_reference", REFERENCE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def frozen_datetime(now):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)
    return Frozen


def thresholds(rng):
    """Random thresholds that form a valid config: the break gap is raised, when needed,
    to cover read + ramp-up (the prototype over-credits otherwise, which is not behaviour
    to preserve). The draws are the same as before, so the generated logs are too."""
    engaged = rng.choice([5, 15, 20])
    th = {
        "engaged_gap_minutes": engaged,
        "break_gap_minutes": engaged + rng.choice([0, 15, 30, 45]),
        "read_allowance_minutes": rng.choice([0, 2, 5, 7.5]),
        "rampup_allowance_minutes": rng.choice([0, 3, 5]),
        "unclosed_cap_minutes": rng.choice([4, 10, 30]),
    }
    th["break_gap_minutes"] = max(th["break_gap_minutes"],
                                  th["read_allowance_minutes"] + th["rampup_allowance_minutes"])
    return th


def span(rng, grid):
    """A duration from seconds to hours. On a grid it is a multiple of 5 minutes, so
    boundaries in different sessions coincide and gaps hit thresholds exactly."""
    if grid:
        return timedelta(minutes=5 * rng.choice([0, 1, 1, 2, 2, 3, 4, 6, 9, 12, 30]))
    scale = rng.choice([60, 20 * 60, 90 * 60, 4 * 3600])
    return timedelta(seconds=round(rng.uniform(1, scale)))


def random_log(rng):
    events = []
    grid = rng.random() < 0.5
    projects = PROJECTS[:rng.randint(1, 3)]
    for i in range(rng.randint(1, 4)):
        sid = (PREFIX if rng.random() < 0.2 else "s") + str(i)
        t = BASE + timedelta(minutes=5 * rng.randint(0, 36))
        for _ in range(rng.randint(1, 6)):
            project = rng.choice(projects)

            def add(kind, at, project=project):
                events.append({"ts": at.isoformat(), "event": kind, "session": sid,
                               "client": "acme", "project": project, "_t": at})

            add("start", t)
            t += span(rng, grid)
            r = rng.random()
            if r < 0.65:
                add("end", t)
            elif r < 0.8:
                add("session_end", t)
            elif r < 0.85:
                add("end", t)
                add("end", t + timedelta(seconds=rng.randint(0, 30)))
            # otherwise the turn is left open (interrupted, or still running)
            t += span(rng, grid)
    events.sort(key=lambda e: e["_t"])
    last = events[-1]["_t"]
    if grid:
        now = last + timedelta(minutes=5 * rng.randint(-6, 6))
    else:
        now = last + timedelta(seconds=rng.randint(-1800, 1800))
    return events, now


def reference_totals(ref, events, cfg, now):
    ref.datetime = frozen_datetime(now)
    evs = [dict(e) for e in events]
    for e in evs:
        if e["session"].startswith(PREFIX):
            e["project"] = ref.NON_BILLABLE
    turns, capped = ref.agent_turns(evs, timedelta(minutes=float(cfg["unclosed_cap_minutes"])))
    a_segs, blocks = ref.agent_segments(turns)
    h_segs = ref.human_segments(blocks, cfg)
    totals = defaultdict(float)
    for kind, segs in (("agent", a_segs), ("you", h_segs)):
        for a, b, project, weight in segs:
            if project != ref.NON_BILLABLE:
                totals[project, kind] += (b - a).total_seconds() * weight
    return totals, capped


def engine_totals(events, cfg, now):
    tl = engine.timeline([dict(e) for e in events], cfg, now, prototype_rules=True, pauses=[])
    totals = defaultdict(float)
    for s in tl.segs:
        project = s.key[1]
        if project != engine.NON_BILLABLE:
            totals[project, s.kind] += (s.end - s.start).total_seconds() * s.weight
    return totals, tl.capped


@unittest.skipUnless(os.path.exists(REFERENCE), "private prototype not present")
class ParityTests(unittest.TestCase):
    def test_matches_prototype_on_random_logs(self):
        ref = load_reference()
        for seed in SEEDS:
            rng = random.Random(seed)
            th = thresholds(rng)
            events, now = random_log(rng)
            raw = {"defaults": th,
                   "clients": {"acme": {"overrides": {"non_billable_sessions": [PREFIX]}}}}
            with helpers.temp_home() as home:
                cfg = config.load(helpers.write_config(home, raw))
            want, want_capped = reference_totals(ref, events, dict(th), now)
            got, got_capped = engine_totals(events, cfg, now)
            with self.subTest(seed=seed):
                self.assertEqual(got_capped, want_capped)
                for k in set(want) | set(got):
                    self.assertLess(abs(want[k] - got[k]), 1e-6, k)


if __name__ == "__main__":
    unittest.main()
