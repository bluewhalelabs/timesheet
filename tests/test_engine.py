import unittest
from collections import defaultdict
from datetime import datetime, timezone

from tests import helpers
from ats import config, engine
from ats.engine import NON_BILLABLE, timeline
from ats.events import load_events

NOW = datetime(2026, 12, 1, tzinfo=timezone.utc)


def at(hhmm):
    h, m = hhmm.split(":")
    return datetime(2026, 11, 10, int(h), int(m), tzinfo=timezone.utc)


def ev(hhmm, kind, session, project, client="acme"):
    t = at(hhmm)
    return {"ts": t.isoformat(), "event": kind, "session": session,
            "client": client, "project": project, "_t": t}


def turn(start, end, session, project, client="acme"):
    return [ev(start, "start", session, project, client), ev(end, "end", session, project, client)]


def make_cfg(acme=None, beta=None, acme_projects=None):
    raw = {
        "defaults": {"engaged_gap_minutes": 15, "break_gap_minutes": 45,
                     "read_allowance_minutes": 5, "rampup_allowance_minutes": 5,
                     "unclosed_cap_minutes": 10},
        "clients": {
            "acme": {"name": "Acme Co",
                     "overrides": dict({"hourly_rate": 150,
                                        "billing_period": {"type": "weekly"},
                                        "rounding": {"increment_hours": 0.01}}, **(acme or {})),
                     "projects": acme_projects or {}},
            "beta": {"name": "Beta",
                     "overrides": dict({"hourly_rate": 200,
                                        "billing_period": {"type": "monthly"},
                                        "rounding": {"increment_hours": 0.25, "mode": "up"}},
                                       **(beta or {}))},
        },
    }
    with helpers.temp_home() as home:
        return config.load(helpers.write_config(home, raw))


def run(events, cfg=None, now=NOW):
    events = sorted(events, key=lambda e: e["_t"])
    return timeline(events, cfg or make_cfg(), now)


def secs(tl):
    out = defaultdict(float)
    for s in tl.segs:
        out[s.key, s.kind] += (s.end - s.start).total_seconds() * s.weight
    return out


A1, A2, B1 = ("acme", "p1"), ("acme", "p2"), ("beta", "p1")


class EngineTests(unittest.TestCase):
    def test_rubric_tiers_and_attribution(self):
        evs = (turn("09:00", "09:10", "s1", "p1") + turn("09:20", "09:30", "s2", "p2")
               + turn("10:00", "10:05", "s3", "p1") + turn("12:00", "12:10", "s4", "p2"))
        tl = run(evs)
        s = secs(tl)
        self.assertEqual(s[A1, "agent"], 900)
        self.assertEqual(s[A2, "agent"], 1200)
        self.assertEqual(s[A1, "you"], 600)
        self.assertEqual(s[A2, "you"], 2100)
        self.assertEqual(tl.capped, 0)
        self.assertEqual(len(tl.turns), 4)

    def test_parallel_projects_split_evenly(self):
        evs = turn("09:00", "09:20", "s1", "p1") + turn("09:10", "09:30", "s2", "p2")
        tl = run(evs)
        s = secs(tl)
        self.assertEqual((s[A1, "agent"], s[A2, "agent"]), (900, 900))
        wall = sum((g.end - g.start).total_seconds() * g.weight for g in tl.segs if g.kind == "agent")
        self.assertEqual(wall, 1800)
        self.assertEqual((s[A1, "you"], s[A2, "you"]), (300, 300))
        self.assertTrue(all(g.weight == 0.5 for g in tl.segs
                            if g.kind == "agent" and g.start == at("09:10")))

    def test_parallel_clients_split_evenly(self):
        evs = turn("09:00", "09:20", "s1", "p1") + turn("09:10", "09:30", "s2", "p1", client="beta")
        s = secs(run(evs))
        self.assertEqual((s[A1, "agent"], s[B1, "agent"]), (900, 900))
        self.assertEqual((s[A1, "you"], s[B1, "you"]), (300, 300))

    def test_gap_into_other_clients_prompt_is_not_credited_to_first_client(self):
        evs = turn("09:00", "09:10", "s1", "p1") + turn("09:20", "09:30", "s2", "p1", client="beta")
        s = secs(run(evs))
        self.assertEqual(s[A1, "agent"], 600)
        self.assertEqual(s[A1, "you"], 300)
        self.assertEqual(s[B1, "agent"], 600)
        self.assertEqual(s[B1, "you"], 900)   # whole 10-min gap 600 + last read 300

    def test_each_client_uses_its_own_thresholds(self):
        cfg = make_cfg(beta={"engaged_gap_minutes": 5})
        evs = turn("09:00", "09:10", "s1", "p1") + turn("09:20", "09:30", "s2", "p1", client="beta")
        s = secs(run(evs, cfg))
        self.assertEqual(s[A1, "you"], 600)   # ramp-up 300 + 5 min of the 10-min gap
        self.assertEqual(s[B1, "you"], 300)   # last read only

    def test_interrupted_turn_is_capped(self):
        evs = [ev("09:00", "start", "s1", "p1"), ev("09:30", "start", "s1", "p1"),
               ev("09:35", "end", "s1", "p1")]
        tl = run(evs)
        self.assertEqual([(t.start, t.end) for t in tl.turns],
                         [(at("09:00"), at("09:10")), (at("09:30"), at("09:35"))])
        self.assertEqual(tl.capped, 1)
        self.assertEqual(tl.capped_turns, [tl.turns[0]])
        self.assertEqual(len(tl.capped_turns), tl.capped)
        s = secs(tl)
        self.assertEqual(s[A1, "agent"], 900)
        gap = [g for g in tl.segs if g.kind == "you" and g.start == at("09:10")]
        self.assertEqual([(g.end, g.key) for g in gap], [(at("09:25"), A1)])
        self.assertEqual(s[A1, "you"], 300 + 900 + 300)

    def test_final_unclosed_turn_past_the_cap_is_dropped_but_reported(self):
        evs = [ev("09:00", "start", "s1", "p1"), ev("09:20", "start", "s2", "p2"),
               ev("09:21", "end", "s2", "p2")]
        tl = run(evs)                                   # NOW is weeks later
        self.assertEqual(tl.dropped_turns, [engine.Turn(at("09:00"), at("09:10"), A1, "s1")])
        self.assertNotIn(tl.dropped_turns[0], tl.turns)
        self.assertEqual(secs(tl)[A1, "agent"], 0)      # no time credited
        self.assertEqual(tl.capped, 0)
        # still running within the cap: counted up to now, not dropped
        tl = run([ev("09:00", "start", "s1", "p1")], now=at("09:05"))
        self.assertEqual(tl.dropped_turns, [])
        self.assertEqual(secs(tl)[A1, "agent"], 300)

    def test_mid_turn_follow_up_is_contiguous(self):
        evs = [ev("09:00", "start", "s1", "p1"), ev("09:03", "start", "s1", "p1"),
               ev("09:08", "end", "s1", "p1")]
        tl = run(evs)
        self.assertEqual(secs(tl)[A1, "agent"], 480)
        self.assertEqual(tl.capped, 0)
        self.assertEqual(len(tl.turns), 2)

    def test_session_end_closes_open_turn(self):
        tl = run([ev("09:00", "start", "s1", "p1"), ev("09:04", "session_end", "s1", "p1")])
        self.assertEqual((secs(tl)[A1, "agent"], tl.capped), (240, 0))
        tl = run([ev("09:00", "start", "s1", "p1"), ev("09:30", "session_end", "s1", "p1")])
        self.assertEqual((secs(tl)[A1, "agent"], tl.capped), (600, 1))

    def test_turn_running_now_counts_up_to_now(self):
        evs = [ev("09:00", "start", "s1", "p1")]
        self.assertEqual(secs(run(evs, now=at("09:06")))[A1, "agent"], 360)
        self.assertEqual(secs(run(evs, now=at("09:10")))[A1, "agent"], 600)   # exactly the cap
        tl = run(evs, now=at("09:30"))
        self.assertEqual(tl.turns, [])
        self.assertEqual(secs(tl)[A1, "agent"], 0)

    def test_last_read_is_limited_by_now(self):
        tl = run(turn("09:00", "09:10", "s1", "p1"), now=at("09:12"))
        reads = [g for g in tl.segs if g.kind == "you" and g.start == at("09:10")]
        self.assertEqual([(g.end - g.start).total_seconds() for g in reads], [120])
        self.assertEqual(secs(tl)[A1, "you"], 300 + 120)

    def test_non_billable_stays_on_timeline(self):
        cfg = make_cfg(acme={"non_billable_sessions": ["nb"]})
        evs = (turn("09:00", "09:10", "s1", "p1") + turn("09:15", "09:25", "nb1", "p1")
               + turn("09:30", "09:40", "s1", "p1"))
        s = secs(run(evs, cfg))
        NB = ("acme", NON_BILLABLE)
        self.assertEqual(s[A1, "agent"], 1200)
        self.assertEqual(s[A1, "you"], 900)
        self.assertEqual(s[NB, "agent"], 600)
        self.assertEqual(s[NB, "you"], 300)

    def test_medium_gap_into_an_ignored_session_credits_only_the_read_allowance(self):
        cfg = make_cfg(acme={"non_billable_sessions": ["nb"]})
        # response at 09:10, then 40 minutes later a prompt in an ignored session
        evs = turn("09:00", "09:10", "s1", "p1") + turn("09:50", "09:55", "nb1", "p1")
        s = secs(run(evs, cfg))
        self.assertEqual(s[A1, "you"], 300 + 300)          # ramp-up before 09:00 + 5 min read
        # the same gap into billable work still credits the engaged allowance
        evs = turn("09:00", "09:10", "s1", "p1") + turn("09:50", "09:55", "s2", "p1")
        self.assertEqual(secs(run(evs, cfg))[A1, "you"], 300 + 900 + 300)
        # the original rule is still available for comparison with the prototype
        evs = turn("09:00", "09:10", "s1", "p1") + turn("09:50", "09:55", "nb1", "p1")
        tl = timeline(sorted(evs, key=lambda e: e["_t"]), cfg, NOW, prototype_rules=True)
        self.assertEqual(secs(tl)[A1, "you"], 300 + 900)

    def test_mark_non_billable_uses_each_clients_prefixes(self):
        cfg = make_cfg(acme={"non_billable_sessions": ["nb"]})
        evs = [ev("09:00", "start", "nb1", "p1"), ev("09:00", "start", "nb1", "p1", client="beta"),
               ev("09:00", "start", "s1", "p1"), ev("09:00", "start", "nb1", "p1", client="gone")]
        engine.mark_non_billable(evs, cfg)
        self.assertEqual([e["_key"] for e in evs],
                         [("acme", NON_BILLABLE), ("beta", "p1"), ("acme", "p1"), ("gone", "p1")])

    def test_gap_settings_come_from_the_prompt_that_ends_it(self):
        cfg = make_cfg(acme_projects={"p2": {"engaged_gap_minutes": 20}})
        # 18-min gap ending in a p2 prompt: p2's 20-minute engaged tier credits it all to p2.
        s = secs(run(turn("09:00", "09:10", "s1", "p1") + turn("09:28", "09:38", "s2", "p2"), cfg))
        self.assertEqual(s[A2, "you"], 1080 + 300)
        self.assertEqual(s[A1, "you"], 300)
        # 18-min gap ending in a p1 prompt: p1's 15-minute tier credits 15 minutes to p2.
        s = secs(run(turn("09:00", "09:10", "s1", "p2") + turn("09:28", "09:38", "s2", "p1"), cfg))
        self.assertEqual(s[A2, "you"], 300 + 900)
        self.assertEqual(s[A1, "you"], 300)

    def test_read_allowance_comes_from_the_response_before_the_gap(self):
        cfg = make_cfg(acme_projects={"p1": {"read_allowance_minutes": 2}})
        s = secs(run(turn("09:00", "09:10", "s1", "p1") + turn("11:00", "11:10", "s2", "p2"), cfg))
        self.assertEqual(s[A1, "you"], 300 + 120)
        self.assertEqual(s[A2, "you"], 300 + 300)

    def test_long_gap_allowances_never_exceed_the_gap(self):
        # p1 reads for 20 min, p2 ramps up for 20 min and breaks at 30: each level is valid,
        # but across a 35-minute gap from p1 into p2 the two would cover 40 minutes.
        cfg = make_cfg(acme_projects={"p1": {"read_allowance_minutes": 20},
                                      "p2": {"rampup_allowance_minutes": 20,
                                             "break_gap_minutes": 30}})
        tl = run(turn("09:00", "09:10", "s1", "p1") + turn("09:45", "09:55", "s2", "p2"), cfg)
        s = secs(tl)
        self.assertEqual(s[A1, "you"], 300 + 1200)            # ramp-up before 09:00 + read
        self.assertEqual(s[A2, "you"], 900 + 300)             # ramp clamped to 35 - 20 min
        you = sorted((g.start, g.end) for g in tl.segs if g.kind == "you")
        for (a, b), (c, d) in zip(you, you[1:]):
            self.assertLessEqual(b, c)                        # credited time never overlaps

    def test_unknown_client_falls_back_to_defaults(self):
        s = secs(run(turn("09:00", "09:10", "s1", "p1", client="gone")))
        self.assertEqual(s[("gone", "p1"), "agent"], 600)
        self.assertEqual(s[("gone", "p1"), "you"], 600)

    def test_timeline_survives_events_with_odd_session_and_project(self):
        import json
        import os
        cfg = make_cfg(acme={"non_billable_sessions": ["nb"]})
        recs = [{"ts": "2026-11-10T09:00:00Z", "event": "start", "client": "acme"},
                {"ts": "2026-11-10T09:05:00Z", "event": "end", "client": "acme",
                 "session": None, "project": None},
                {"ts": "2026-11-10T09:10:00Z", "event": "start", "client": "acme",
                 "session": 5, "project": 6},
                {"ts": "2026-11-10T09:12:00Z", "event": "end", "client": "acme",
                 "session": 5, "project": 6}]
        with helpers.temp_home() as home:
            path = os.path.join(home, "events.jsonl")
            with open(path, "w") as f:
                f.write("\n".join(json.dumps(r) for r in recs) + "\n")
            evs, skipped = load_events(path)
        self.assertEqual(skipped, 0)
        tl = timeline(evs, cfg, NOW)
        s = secs(tl)
        self.assertEqual(s[("acme", "(unknown)"), "agent"], 300)
        self.assertEqual(s[("acme", "6"), "agent"], 120)

    def test_same_key_overlap_does_not_change_the_split(self):
        evs = (turn("09:00", "09:20", "s1", "p1") + turn("09:05", "09:15", "s2", "p1")
               + turn("09:10", "09:30", "s3", "p2") + turn("09:20", "09:25", "s4", "p1"))
        tl = run(evs)
        s = secs(tl)
        self.assertEqual((s[A1, "agent"], s[A2, "agent"]), (1050, 750))
        wall = sum((g.end - g.start).total_seconds() * g.weight for g in tl.segs if g.kind == "agent")
        self.assertEqual(wall, 1800)

    def test_block_last_key_is_the_turn_that_extended_the_end(self):
        def turns(*specs):
            return [engine.Turn(at(a), at(b), key, "s%d" % i) for i, (a, b, key) in enumerate(specs)]

        _, blocks = engine.agent_segments(turns(("09:00", "09:30", A1), ("09:05", "09:10", A2)))
        self.assertEqual(blocks, [engine.Block(at("09:00"), at("09:30"), A1, A1)])
        _, blocks = engine.agent_segments(turns(("09:00", "09:10", A1), ("09:05", "09:30", A2)))
        self.assertEqual(blocks, [engine.Block(at("09:00"), at("09:30"), A1, A2)])

    def test_no_events_returns_empty_timeline(self):
        tl = run([])
        self.assertEqual((tl.segs, tl.turns, tl.capped, tl.capped_turns, tl.dropped_turns),
                         ([], [], 0, [], []))


if __name__ == "__main__":
    unittest.main()
