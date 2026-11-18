"""Pause and resume: the pauses.json store, the engine's clipping rule, the pause /
resume / pauses commands and every place a pause shows up."""
import json
import os
import stat
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tests import helpers
from tests import test_status as ts
from tests.test_engine import at, make_cfg, secs, turn as eturn
from tests.test_timesheet_cli import turn

from ats import config, engine, health, pauses, status
from ats.engine import NON_BILLABLE

NOW = ts.NOW                       # 2026-11-12T12:00:00Z, a Thursday
NOW_DT = ts.NOW_DT
DAY = "2026-11-12"
# acme works 09:00-10:00, beta 10:30-11:30. Without pauses: acme 60 min agent + 5 min
# ramp-up + 15 min engaged (10:00-10:15) = 1.33 h; beta 60 min + 5 min read = 1.25 h (0.25 up).
TODAY = (turn(DAY, "09:00", "10:00", "a1", "project-a")
         + turn(DAY, "10:30", "11:30", "b1", "project-b", client="beta"))
AWAY_NOTE = "%s h of your own time were logged while away and are not billed (%s). If you " \
            "were back sooner: timesheet pauses"
AWAY_STILL = "Your own time is not counted while away; agent work you started keeps running " \
             "and is still billed. Come back with /timesheet:resume."
NOTE = "%s h were logged while paused and are not billed (%s). If a pause ran too long: " \
       "timesheet pauses"
STILL = "Time is still recorded while paused; it is just not billed. " \
        "Resume with /timesheet:resume."


def window(pid, client, start, end=None, until=None, mode=None):
    def iso(hhmm):
        return None if hhmm is None else pauses.stamp(at(hhmm))
    out = {"id": pid, "client": client, "start": iso(start), "end": iso(end),
           "until": iso(until), "created": iso(start)}
    if mode is not None:
        out["mode"] = mode
    return out


def run(events, windows, now=None, cfg=None):
    events = sorted(events, key=lambda e: e["_t"])
    return engine.timeline(events, cfg or make_cfg(), now or at("23:00"), pauses=windows)


# ------------------------------------------------------------------ storage

class StoreTests(unittest.TestCase):
    def test_missing_file_is_no_pauses(self):
        with helpers.temp_home():
            self.assertEqual(pauses.load(), [])

    def test_round_trip_ids_and_private_mode(self):
        with helpers.temp_home() as home:
            items = pauses.load()
            a = pauses.pause(items, "acme", at("09:00"), at("11:00"), at("09:00"))
            b = pauses.pause(items, "*", at("12:00"), None, at("12:30"))
            pauses.save(items)
            path = os.path.join(home, "pauses.json")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(os.listdir(home), ["pauses.json"])       # no temp file left
            self.assertEqual(pauses.load(), items)
            self.assertEqual(pauses.load(home), items)
        self.assertEqual(a, {"id": "p1", "client": "acme", "mode": "all",
                             "start": "2026-11-10T09:00:00Z",
                             "end": None, "until": "2026-11-10T11:00:00Z",
                             "created": "2026-11-10T09:00:00Z"})
        self.assertEqual((b["id"], b["client"], b["until"], b["created"]),
                         ("p2", "*", None, "2026-11-10T12:30:00Z"))
        pauses.remove(items, "p1")
        self.assertEqual(pauses.pause(items, "acme", at("13:00"), None, at("13:00"))["id"], "p3")

    def test_invalid_files_fail_loudly(self):
        good = window("p1", "acme", "09:00")
        bad = ["{not json", '{"id": "p1"}', '["p1"]',
               json.dumps([dict(good, id="x1")]), json.dumps([dict(good, client="")]),
               json.dumps([dict(good, start="yesterday")]), json.dumps([dict(good, start=None)]),
               json.dumps([dict(good, end="soon")]), json.dumps([dict(good, until=5)]),
               json.dumps([dict(good, end="2026-11-10T08:00:00Z")]),
               json.dumps([good, good])]
        for text in bad:
            with helpers.temp_home() as home:
                with open(os.path.join(home, "pauses.json"), "w") as f:
                    f.write(text)
                with self.subTest(text=text):
                    with self.assertRaises(pauses.PauseError) as cm:
                        pauses.load()
                    self.assertIn("pauses.json", str(cm.exception))
                    with self.assertRaises(pauses.PauseError):     # the engine never ignores it
                        engine.timeline([], make_cfg(), at("12:00"))
        self.assertTrue(issubclass(pauses.PauseError, config.ConfigError))

    def test_windows_are_merged_sorted_and_closed_at_now(self):
        items = [window("p1", "acme", "11:00"), window("p2", "*", "08:00", "09:00"),
                 window("p3", "acme", "08:30", "09:30"), window("p4", "beta", "07:00", "07:30"),
                 window("p5", "acme", "10:00", until="10:20"), window("p6", "acme", "10:10", "10:10")]
        now = at("12:00")
        self.assertEqual(pauses.windows_for(items, "acme", now),
                         [(at("08:00"), at("09:30")), (at("10:00"), at("10:20")),
                          (at("11:00"), now)])
        self.assertEqual(pauses.windows_for(items, "beta", now),
                         [(at("07:00"), at("07:30")), (at("08:00"), at("09:00"))])
        self.assertEqual(pauses.windows_for(items, "acme", at("10:30")),
                         [(at("08:00"), at("09:30")), (at("10:00"), at("10:20"))])

    def test_active_window(self):
        items = [window("p1", "acme", "09:00", "10:00"), window("p2", "*", "11:00", until="11:30"),
                 window("p3", "beta", "12:00")]
        self.assertEqual(pauses.active(items, "acme", at("09:30"))["id"], "p1")
        self.assertIsNone(pauses.active(items, "acme", at("10:00")))
        self.assertIsNone(pauses.active(items, "beta", at("09:30")))
        self.assertEqual(pauses.active(items, "acme", at("11:10"))["id"], "p2")
        self.assertEqual(pauses.active(items, "beta", at("11:10"))["id"], "p2")
        self.assertEqual(pauses.active(items, "*", at("11:10"))["id"], "p2")
        self.assertIsNone(pauses.active(items, "acme", at("11:30")))     # until has passed
        self.assertEqual(pauses.active(items, "beta", at("13:00"))["id"], "p3")
        self.assertIsNone(pauses.active(items, "*", at("13:00")))        # only beta is paused
        self.assertIsNone(pauses.active(items, "acme", at("13:00")))

    def test_parse_duration(self):
        for text, minutes in (("90m", 90), ("2h", 120), ("1h30m", 90), ("1d", 1440),
                              ("1d2h3m", 1563), (" 2H ", 120)):
            self.assertEqual(pauses.parse_duration(text), timedelta(minutes=minutes), text)
        for text in ("", "abc", "2", "0m", "1.5h", "30m1h", "-1h"):
            with self.assertRaises(pauses.PauseError, msg=text):
                pauses.parse_duration(text)

    def test_parse_when_uses_the_config_timezone(self):
        tz = ZoneInfo("America/Los_Angeles")
        now = datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)          # 12:00 local
        utc = lambda *a: datetime(*a, tzinfo=timezone.utc)
        self.assertEqual(pauses.parse_when("09:30", tz, now), utc(2026, 11, 12, 17, 30))
        self.assertEqual(pauses.parse_when("9:30", tz, now, roll_forward=True),
                         utc(2026, 11, 13, 17, 30))                       # already past
        self.assertEqual(pauses.parse_when("17:00", tz, now, roll_forward=True),
                         utc(2026, 11, 13, 1, 0))
        self.assertEqual(pauses.parse_when("2026-11-11", tz, now), utc(2026, 11, 11, 8, 0))
        self.assertEqual(pauses.parse_when("2026-11-11T10:00:00Z", tz, now),
                         utc(2026, 11, 11, 10, 0))
        self.assertEqual(pauses.parse_when("2026-11-11T10:00", tz, now), utc(2026, 11, 11, 18, 0))
        for text in ("", "25:00", "noon", "2026-13-01", "12", "tomorrow"):
            with self.assertRaises(pauses.PauseError, msg=text):
                pauses.parse_when(text, tz, now)
        self.assertEqual(pauses.local(utc(2026, 11, 12, 17, 30), tz), "2026-11-12 09:30 PST")


# ------------------------------------------------------------------ engine

class ClippingTests(unittest.TestCase):
    """One acme turn 09:00-10:00: ramp-up 08:55-09:00, agent 09:00-10:00, read 10:00-10:05."""
    A = ("acme", "project-a")
    B = ("beta", "project-b")

    def base(self):
        return eturn("09:00", "10:00", "s1", "project-a")

    def test_agent_segment_is_cut_at_both_edges(self):
        tl = run(self.base(), [window("p1", "acme", "09:30", "09:50")])
        got = secs(tl)
        self.assertEqual(got[self.A, "agent"], 40 * 60)
        self.assertEqual(got[self.A, "you"], 10 * 60)
        self.assertEqual(got[("acme", NON_BILLABLE), "agent"], 20 * 60)
        self.assertEqual(tl.paused, {"acme": 20 * 60.0})
        agent = [(s.start, s.end) for s in tl.segs if s.kind == "agent" and s.key == self.A]
        self.assertEqual(agent, [(at("09:00"), at("09:30")), (at("09:50"), at("10:00"))])
        self.assertEqual([(s.start, s.end, s.key, s.kind) for s in tl.paused_pieces],
                         [(at("09:30"), at("09:50"), self.A, "agent")])

    def test_your_time_is_cut_where_the_pause_starts_and_where_it_ends(self):
        tl = run(self.base(), [window("p1", "acme", "10:02", "11:00")])     # read runs into it
        self.assertEqual(secs(tl)[self.A, "you"], 5 * 60 + 2 * 60)
        self.assertEqual(tl.paused, {"acme": 3 * 60.0})
        tl = run(self.base(), [window("p1", "acme", "08:00", "08:57")])     # ramp-up before resume
        self.assertEqual(secs(tl)[self.A, "you"], 3 * 60 + 5 * 60)
        self.assertEqual(secs(tl)[self.A, "agent"], 60 * 60)
        self.assertEqual(tl.paused, {"acme": 2 * 60.0})
        self.assertEqual({s.kind for s in tl.paused_pieces}, {"you"})

    def test_weights_are_respected_and_the_other_client_is_untouched(self):
        events = self.base() + eturn("09:00", "10:00", "s2", "project-b", client="beta")
        plain = run(events, [])
        self.assertEqual(plain.paused, {})
        self.assertEqual(plain.paused_pieces, [])
        tl = run(events, [window("p1", "acme", "09:00", "09:30")])
        got, want = secs(tl), secs(plain)
        self.assertEqual(got[self.A, "agent"], 15 * 60)
        self.assertEqual(tl.paused["acme"], 15 * 60 + (want[self.A, "you"] - got[self.A, "you"]))
        self.assertNotIn("beta", tl.paused)
        for kind in ("agent", "you"):
            self.assertEqual(got[self.B, kind], want[self.B, kind])
        self.assertEqual([s for s in tl.segs if s.key[0] == "beta"],
                         [s for s in plain.segs if s.key[0] == "beta"])
        tl = run(events, [window("p1", "beta", "09:00", "09:30")])           # and vice versa
        self.assertEqual([s for s in tl.segs if s.key[0] == "acme"],
                         [s for s in plain.segs if s.key[0] == "acme"])
        self.assertEqual(set(tl.paused), {"beta"})

    def test_star_window_pauses_every_client(self):
        events = self.base() + eturn("11:00", "12:00", "s2", "project-b", client="beta")
        tl = run(events, [window("p1", "*", "09:30", "11:30")])
        got = secs(tl)
        self.assertEqual(got[self.A, "agent"], 30 * 60)
        self.assertEqual(got[self.B, "agent"], 30 * 60)
        self.assertEqual(tl.paused["acme"], 30 * 60 + 5 * 60)        # agent + the read allowance
        self.assertEqual(tl.paused["beta"], 30 * 60 + 5 * 60)        # agent + the ramp-up
        self.assertEqual(sum(v for (k, _), v in got.items() if k[1] != NON_BILLABLE)
                         + sum(tl.paused.values()),
                         sum(v for (k, _), v in secs(run(events, [])).items()))

    def test_open_window_is_closed_at_now(self):
        tl = run(eturn("09:00", "09:40", "s1", "project-a"), [window("p1", "acme", "09:30")],
                 now=at("09:45"))
        got = secs(tl)
        self.assertEqual(got[self.A, "agent"], 30 * 60)
        self.assertEqual(got[self.A, "you"], 5 * 60)                  # the ramp-up only
        self.assertEqual(tl.paused, {"acme": 10 * 60 + 5 * 60.0})

    def test_until_in_the_past_ends_the_window(self):
        tl = run(self.base(), [window("p1", "acme", "09:30", until="09:40")])
        self.assertEqual(secs(tl)[self.A, "agent"], 50 * 60)
        self.assertEqual(tl.paused, {"acme": 10 * 60.0})
        ended = window("p1", "acme", "09:30", end="09:35", until="09:40")   # end wins over until
        self.assertEqual(run(self.base(), [ended]).paused, {"acme": 5 * 60.0})

    def test_ignored_session_time_is_not_counted_as_paused(self):
        cfg = make_cfg(acme={"non_billable_sessions": ["nb"]})
        events = eturn("09:00", "10:00", "nb1", "project-a")
        plain = run(events, [], cfg=cfg)
        tl = run(events, [window("p1", "acme", "08:00", "11:00")], cfg=cfg)
        self.assertEqual(tl.paused, {})
        self.assertEqual(tl.paused_pieces, [])
        self.assertEqual(tl.segs, plain.segs)

    def test_no_pauses_file_changes_nothing(self):
        events = self.base() + eturn("10:20", "11:00", "s2", "project-b", client="beta")
        cfg = make_cfg()
        with helpers.temp_home() as home:
            from_file = engine.timeline(events, cfg, at("23:00"))
            self.assertEqual(os.listdir(home), [])
        none = run(events, [], cfg=cfg)
        self.assertEqual(from_file, none)
        self.assertEqual(from_file.segs, run(events, [window("p1", "acme", "20:00", "21:00")],
                                             cfg=cfg).segs)

    def test_paused_time_helper_counts_billable_seconds_and_local_dates(self):
        cfg = make_cfg(acme={"agent_billable_fraction": 0.5})
        tl = run(self.base(), [window("p1", "acme", "09:30", "10:02")], cfg=cfg)
        tz = timezone.utc
        seconds, days = health.paused_time(cfg, tl, "acme", None, None, tz)
        self.assertEqual(seconds, 30 * 60 * 0.5 + 2 * 60)
        self.assertEqual(days, ["2026-11-10"])
        self.assertEqual(health.paused_time(cfg, tl, "acme", at("09:50"), at("10:00"), tz),
                         (10 * 60 * 0.5, ["2026-11-10"]))
        self.assertEqual(health.paused_time(cfg, tl, "beta", None, None, tz), (0.0, []))
        self.assertEqual(health.paused_time(cfg, tl, "acme", None, None, tz, "other"), (0.0, []))
        self.assertEqual(health.paused_hours(seconds), "0.28")
        self.assertIsNone(health.paused_warning("0.00", []))
        self.assertEqual(health.paused_warning("0.28", days), NOTE % ("0.28", "2026-11-10"))
        self.assertIn("(2026-11-01 to 2026-11-04)", health.paused_warning(
            "9.00", ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"]))


# ------------------------------------------------------------------ commands

class Case(ts.Case):
    def setup(self, cfg=None, events=TODAY):
        super().setup(cfg, events)

    def cli(self, *args, now=NOW):
        r = self.timesheet(*(list(args) + ["--now", now]))
        return r

    def ok(self, *args, now=NOW):
        r = self.cli(*args, now=now)
        self.assertEqual((r.returncode, r.stderr), (0, ""), r.stdout)
        return r.stdout

    def fails(self, *args, now=NOW):
        r = self.cli(*args, now=now)
        self.assertEqual((r.returncode, r.stdout), (2, ""), r.stderr)
        self.assertTrue(r.stderr.startswith("error: "), r.stderr)
        return r.stderr

    def stored(self):
        try:
            with open(os.path.join(self.home, "pauses.json")) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def write_pauses(self, items):
        with open(os.path.join(self.home, "pauses.json"), "w") as f:
            f.write(items if isinstance(items, str) else json.dumps(items))

    def hours(self, client, period="this"):
        return self.compute(client, period)["total_hours"]


class PauseCommandTests(Case):
    def test_pause_now_for_the_detected_client(self):
        self.setup()
        out = self.ok("pause", "--cwd", self.dirs["acme"])
        self.assertEqual(out.splitlines(), ["Paused Acme Co at 2026-11-12 12:00 UTC", STILL])
        self.assertEqual(self.stored(), [{"id": "p1", "client": "acme", "mode": "all",
                                          "start": "2026-11-12T12:00:00Z", "end": None,
                                          "until": None, "created": "2026-11-12T12:00:00Z"}])
        mode = stat.S_IMODE(os.stat(os.path.join(self.home, "pauses.json")).st_mode)
        self.assertEqual(mode, 0o600)
        out = self.ok("pause", "--cwd", self.dirs["beta"])
        self.assertEqual(out.splitlines()[0], "Paused Beta at 2026-11-12 12:00 UTC")
        self.assertNotIn("Acme", out)
        self.assertEqual([(p["id"], p["client"]) for p in self.stored()],
                         [("p1", "acme"), ("p2", "beta")])

    def test_client_must_be_known_when_it_cannot_be_detected(self):
        self.setup()
        err = self.fails("pause", "--cwd", self.dirs["elsewhere"])
        self.assertIn("--client KEY", err)
        self.assertIn("--all", err)
        self.assertIn("unknown client: gamma", self.fails("pause", "--client", "gamma"))
        self.assertIn("--client KEY", self.fails("resume", "--cwd", self.dirs["elsewhere"]))
        self.assertIsNone(self.stored())
        r = self.timesheet("pause", "--client", "acme", "--all", "--now", NOW)
        self.assertEqual(r.returncode, 2)                              # argparse refuses both

    def test_the_only_client_is_used_outside_every_scope(self):
        cfg = self.cfg()
        del cfg["clients"]["beta"]
        self.setup(cfg)
        out = self.ok("pause", "--cwd", self.dirs["elsewhere"])
        self.assertIn("Paused Acme Co at", out)
        self.assertIn("Resumed Acme Co at", self.ok("resume", "--cwd", self.dirs["elsewhere"]))

    def test_for_until_and_at(self):
        cases = [(["--for", "90m"], "2026-11-12T12:00:00Z", "2026-11-12T13:30:00Z"),
                 (["--for", "1h30m"], "2026-11-12T12:00:00Z", "2026-11-12T13:30:00Z"),
                 (["--for", "1d"], "2026-11-12T12:00:00Z", "2026-11-13T12:00:00Z"),
                 (["--for", "2h", "--at", "11:00"], "2026-11-12T11:00:00Z",
                  "2026-11-12T13:00:00Z"),
                 (["--until", "17:00"], "2026-11-12T12:00:00Z", "2026-11-12T17:00:00Z"),
                 (["--until", "09:00"], "2026-11-12T12:00:00Z", "2026-11-13T09:00:00Z"),
                 (["--until", "2026-11-14"], "2026-11-12T12:00:00Z", "2026-11-14T00:00:00Z"),
                 (["--until", "2026-11-12T18:30:00Z"], "2026-11-12T12:00:00Z",
                  "2026-11-12T18:30:00Z"),
                 (["--at", "09:30"], "2026-11-12T09:30:00Z", None),
                 (["--at", "2026-11-11"], "2026-11-11T00:00:00Z", None),
                 (["--at", "2026-11-11T08:00:00Z", "--until", "2026-11-11T09:00:00Z"],
                  "2026-11-11T08:00:00Z", "2026-11-11T09:00:00Z")]
        for flags, start, until in cases:
            self.setup()
            if os.path.exists(os.path.join(self.home, "pauses.json")):
                os.unlink(os.path.join(self.home, "pauses.json"))
            with self.subTest(flags=flags):
                out = self.ok("pause", "--client", "acme", *flags)
                p = self.stored()[0]
                self.assertEqual((p["start"], p["until"], p["end"]), (start, until, None))
                first = "Paused Acme Co at %s UTC" % start[:16].replace("T", " ")
                if until:
                    first += " until %s UTC" % until[:16].replace("T", " ")
                self.assertEqual(out.splitlines(), [first, STILL])

    def test_invalid_times_exit_2_and_change_nothing(self):
        self.setup()
        for flags, needle in ((["--for", "abc"], "invalid duration"),
                              (["--for", "0m"], "longer than zero"),
                              (["--until", "25:00"], "invalid time"),
                              (["--until", "soon"], "invalid time"),
                              (["--at", "nine"], "invalid time"),
                              (["--at", "13:00"], "future"),
                              (["--at", "2026-11-13"], "future"),
                              (["--at", "11:00", "--until", "2026-11-12T10:00:00Z"],
                               "not after the start")):
            with self.subTest(flags=flags):
                self.assertIn(needle, self.fails("pause", "--client", "acme", *flags))
        r = self.timesheet("pause", "--client", "acme", "--for", "1h", "--until", "17:00")
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid --now", self.fails("pause", "--client", "acme", now="today"))
        self.assertIsNone(self.stored())

    def test_already_paused_changes_nothing(self):
        self.setup()
        self.ok("pause", "--client", "acme", "--at", "09:30", "--until", "17:00")
        before = self.stored()
        out = self.ok("pause", "--client", "acme", "--for", "1h")
        self.assertEqual(out, "Already paused: Acme Co since 2026-11-12 09:30 UTC until "
                              "2026-11-12 17:00 UTC. Nothing changed.\n")
        self.assertEqual(self.stored(), before)
        self.write_pauses([])
        self.ok("pause", "--all", "--at", "10:00")
        before = self.stored()
        self.assertEqual(before[0]["client"], "*")
        for who, name in ((["--client", "acme"], "Acme Co"), (["--all"], "all clients")):
            out = self.ok("pause", *who)
            self.assertEqual(out, "Already paused: %s since 2026-11-12 10:00 UTC. "
                                  "Nothing changed.\n" % name)
        self.assertEqual(self.stored(), before)

    def test_missing_config_and_invalid_pauses_file_exit_2(self):
        for sub in (["pause", "--client", "acme"], ["resume", "--client", "acme"], ["pauses"]):
            self.assertIn("config file not found", self.fails(*sub))
        self.setup()
        self.write_pauses("{broken")
        for sub in (["pause", "--client", "acme"], ["resume", "--client", "acme"], ["pauses"],
                    ["pauses", "--remove", "p1"], ["pauses", "--edit", "p1", "--end", "11:00"]):
            self.assertIn("pauses.json", self.fails(*sub))
        with open(os.path.join(self.home, "pauses.json")) as f:
            self.assertEqual(f.read(), "{broken")


class ResumeCommandTests(Case):
    def test_resume_closes_the_window_and_reports_unbilled_hours(self):
        self.setup()
        self.assertEqual((self.hours("acme"), self.hours("beta")), ("1.33", "1.25"))
        self.ok("pause", "--client", "acme", "--at", "09:30")
        self.assertEqual((self.hours("acme"), self.hours("beta")), ("0.58", "1.25"))
        out = self.ok("resume", "--cwd", self.dirs["acme"])
        self.assertEqual(out.splitlines(), [
            "Resumed Acme Co at 2026-11-12 12:00 UTC",
            "Closed pause p1: 2026-11-12 09:30 UTC to 2026-11-12 12:00 UTC (2h 30m)",
            "0.75 h of logged time in that window are not billed for Acme Co."])
        self.assertNotIn("$", out)
        self.assertNotIn("Beta", out)
        self.assertEqual(self.stored()[0]["end"], "2026-11-12T12:00:00Z")
        self.assertEqual(self.hours("acme"), "0.58")

    def test_backdated_resume_gives_the_time_back(self):
        self.setup()
        self.ok("pause", "--client", "acme", "--at", "09:30")
        out = self.ok("resume", "--client", "acme", "--at", "09:45")
        self.assertEqual(out.splitlines()[:3], [
            "Resumed Acme Co at 2026-11-12 09:45 UTC",
            "Closed pause p1: 2026-11-12 09:30 UTC to 2026-11-12 09:45 UTC (0h 15m)",
            "0.25 h of logged time in that window are not billed for Acme Co."])
        self.assertIn("stale", out.splitlines()[3])
        self.assertEqual(self.stored()[0]["end"], "2026-11-12T09:45:00Z")
        self.assertEqual((self.hours("acme"), self.hours("beta")), ("1.08", "1.25"))
        self.assertEqual(self.compute("acme", "this")["paused_hours"], "0.25")

    def test_resume_at_must_be_inside_the_pause_and_not_in_the_future(self):
        self.setup()
        self.ok("pause", "--client", "acme", "--at", "09:30")
        before = self.stored()
        self.assertIn("before pause p1 started", self.fails("resume", "--client", "acme",
                                                            "--at", "09:00"))
        self.assertIn("future", self.fails("resume", "--client", "acme", "--at", "13:00"))
        self.assertIn("invalid time", self.fails("resume", "--client", "acme", "--at", "x"))
        self.assertEqual(self.stored(), before)

    def test_not_paused(self):
        self.setup()
        self.assertEqual(self.ok("resume", "--client", "acme"),
                         "Acme Co is not paused. Nothing changed.\n")
        self.assertEqual(self.ok("resume", "--all"), "Nothing is paused.\n")
        self.assertIsNone(self.stored())
        self.ok("pause", "--client", "beta", "--at", "10:00", "--for", "1h")   # over at 11:00
        before = self.stored()
        self.assertEqual(self.ok("resume", "--client", "beta"),
                         "Beta is not paused. Nothing changed.\n")
        self.assertEqual(self.ok("resume", "--client", "acme"),
                         "Acme Co is not paused. Nothing changed.\n")
        self.assertEqual(self.stored(), before)

    def test_resuming_one_client_splits_a_pause_of_all_clients(self):
        self.setup()
        self.ok("pause", "--all", "--at", "09:30", "--until", "18:00")
        out = self.ok("resume", "--client", "acme", "--at", "11:00")
        self.assertEqual(out.splitlines()[:4], [
            "Resumed Acme Co at 2026-11-12 11:00 UTC",
            "Closed pause p1: 2026-11-12 09:30 UTC to 2026-11-12 11:00 UTC (1h 30m)",
            "0.75 h of logged time in that window are not billed for Acme Co.",
            "That pause covered all clients: the other 1 client(s) stay paused (p2)."])
        self.assertNotIn("Beta", out)
        self.assertNotIn("beta", out)
        p1, p2 = self.stored()
        self.assertEqual((p1["client"], p1["end"]), ("*", "2026-11-12T11:00:00Z"))
        self.assertEqual(p2, {"id": "p2", "client": "beta", "mode": "all",
                              "start": "2026-11-12T11:00:00Z",
                              "end": None, "until": "2026-11-12T18:00:00Z",
                              "created": "2026-11-12T12:00:00Z"})
        cfg = config.load()
        self.assertIsNone(status.summary(cfg, "acme", NOW_DT)["paused"])
        self.assertEqual(status.summary(cfg, "beta", NOW_DT)["paused"]["id"], "p2")
        self.assertEqual(self.hours("beta"), "0.00")          # all of beta's time stays paused
        self.assertEqual(self.compute("beta", "this")["paused_hours"], "1.08")

    def test_resume_all_ends_every_active_window(self):
        self.setup()
        self.ok("pause", "--client", "acme", "--at", "09:30")
        self.ok("pause", "--client", "beta", "--at", "11:00")
        out = self.ok("resume", "--all")
        self.assertEqual(out.splitlines()[0], "Resumed all clients at 2026-11-12 12:00 UTC")
        self.assertEqual(out.count("Closed pause"), 2)
        self.assertEqual([p["end"] for p in self.stored()], ["2026-11-12T12:00:00Z"] * 2)


class PausesCommandTests(Case):
    def seed(self):
        self.setup()
        self.ok("pause", "--client", "acme", "--at", "09:30", "--until", "2026-11-12T09:45:00Z")
        self.ok("pause", "--client", "beta", "--at", "11:00")
        self.ok("pause", "--all", "--at", "2026-11-11T08:00:00Z",
                "--until", "2026-11-11T09:00:00Z")

    def test_list_is_newest_first_with_unbilled_hours(self):
        self.setup()
        self.assertEqual(self.ok("pauses"), "No pauses.\n")
        self.assertEqual(json.loads(self.ok("pauses", "--json")), [])
        self.seed()
        self.assertEqual(self.ok("pauses").splitlines(), [
            "p2  beta  paused  2026-11-12 11:00 UTC, still paused  0.58 h unbilled",
            "p1  acme  paused  2026-11-12 09:30 UTC to 2026-11-12 09:45 UTC  0.25 h unbilled",
            "p3  all  paused  2026-11-11 08:00 UTC to 2026-11-11 09:00 UTC  0.00 h unbilled"])
        out = self.ok("pauses", "--client", "acme")
        self.assertEqual([l.split()[0] for l in out.splitlines()], ["p1", "p3"])
        self.assertNotIn("beta", out)
        doc = json.loads(self.ok("pauses", "--json"))
        self.assertEqual(doc[0], {"id": "p2", "client": "beta", "mode": "all",
                                  "start": "2026-11-12T11:00:00Z",
                                  "end": None, "until": None, "active": True,
                                  "unbilled_hours": "0.58"})
        self.assertEqual([(d["id"], d["active"], d["unbilled_hours"]) for d in doc[1:]],
                         [("p1", False, "0.25"), ("p3", False, "0.00")])
        self.assertIn("unknown client", self.fails("pauses", "--client", "gamma"))

    def test_planned_resume_is_shown_as_until(self):
        self.setup()
        self.ok("pause", "--client", "acme", "--until", "17:00")
        self.assertEqual(self.ok("pauses"), "p1  acme  paused  2026-11-12 12:00 UTC until "
                                            "2026-11-12 17:00 UTC  0.00 h unbilled\n")

    def test_remove_makes_the_time_billable_again(self):
        self.seed()
        self.assertEqual(self.hours("acme"), "1.08")
        out = self.ok("pauses", "--remove", "p1")
        self.assertEqual(out.splitlines()[:2], [
            "Removed pause p1 (Acme Co, paused): 2026-11-12 09:30 UTC to 2026-11-12 09:45 UTC, "
            "0.25 h unbilled",
            "That time is billable again."])
        self.assertIn("stale", out.splitlines()[2])
        self.assertEqual([p["id"] for p in self.stored()], ["p2", "p3"])
        self.assertEqual(self.hours("acme"), "1.33")
        self.assertEqual(self.compute("acme", "this")["paused_hours"], "0.00")
        self.assertIn("no pause with id p1", self.fails("pauses", "--remove", "p1"))
        self.ok("pause", "--client", "acme")
        self.assertEqual(self.stored()[-1]["id"], "p4")       # 1 + the highest existing

    def test_edit_moves_the_start_or_end(self):
        self.seed()
        out = self.ok("pauses", "--edit", "p2", "--end", "11:15")
        self.assertEqual(out.splitlines()[:2], [
            "Before: p2 (Beta, paused): 2026-11-12 11:00 UTC, still paused, 0.58 h unbilled",
            "After:  p2 (Beta, paused): 2026-11-12 11:00 UTC to 2026-11-12 11:15 UTC, "
            "0.25 h unbilled"])
        self.assertIn("stale", out.splitlines()[2])
        self.assertNotIn("Acme", out)
        p2 = pauses.find(self.stored(), "p2")
        self.assertEqual((p2["start"], p2["end"]), ("2026-11-12T11:00:00Z", "2026-11-12T11:15:00Z"))
        out = self.ok("pauses", "--edit", "p1", "--start", "09:15", "--end", "10:00")
        self.assertIn("After:  p1 (Acme Co, paused): 2026-11-12 09:15 UTC to 2026-11-12 10:00 UTC, "
                      "0.75 h unbilled", out)
        self.assertEqual(self.hours("acme"), "0.58")
        self.assertEqual(self.hours("beta"), "1.00")           # 0.83 h, rounded up to 0.25

    def test_edit_is_validated(self):
        self.seed()
        before = self.stored()
        for flags, needle in ((["--edit", "p1", "--start", "09:50"], "start must be before"),
                              (["--edit", "p1", "--end", "09:30"], "start must be before"),
                              (["--edit", "p1", "--end", "13:00"], "future"),
                              (["--edit", "p2", "--start", "12:30"], "future"),
                              (["--edit", "p9", "--end", "10:00"], "no pause with id p9"),
                              (["--edit", "p1"], "nothing to change"),
                              (["--edit", "p1", "--end", "later"], "invalid time"),
                              (["--start", "09:00"], "need --edit"),
                              (["--mode", "mine"], "need --edit")):
            with self.subTest(flags=flags):
                self.assertIn(needle, self.fails("pauses", *flags))
        self.assertEqual(self.stored(), before)
        r = self.timesheet("pauses", "--remove", "p1", "--edit", "p1")
        self.assertEqual(r.returncode, 2)


# ------------------------------------------------------------------ surfaces

class SurfaceTests(Case):
    def pause_acme(self, *flags):
        self.ok("pause", "--client", "acme", "--at", "09:30", *flags)

    def test_status_shows_the_pause_and_the_unbilled_hours(self):
        self.setup()
        plain = {c: self.ok("status", "--client", c) for c in ("acme", "beta")}
        self.assertNotIn("PAUSED", plain["acme"] + plain["beta"])
        self.assertNotIn("paused", plain["acme"] + plain["beta"])
        self.pause_acme()
        self.assertEqual(self.ok("status", "--cwd", self.dirs["acme"]).splitlines(), [
            "Acme Co",
            "PAUSED since 2026-11-12 09:30 UTC",
            "Current period: Nov 9 – Nov 15, 2026 (ends Nov 15): 0.58 h",
            "Last period: Nov 2 – Nov 8, 2026: nothing to bill",
            NOTE % ("0.75", "2026-11-12")])
        self.assertEqual(self.ok("status", "--client", "beta"), plain["beta"])
        self.ok("resume", "--client", "acme", "--at", "09:45")
        out = self.ok("status", "--client", "acme").splitlines()
        self.assertEqual(out[1], "Current period: Nov 9 – Nov 15, 2026 (ends Nov 15): 1.08 h")
        self.assertEqual(out[-1], NOTE % ("0.25", "2026-11-12"))

    def test_status_until_and_all_clients_blocks(self):
        self.setup()
        self.ok("pause", "--client", "beta", "--at", "10:00", "--until", "17:00")
        blocks = self.ok("status", "--cwd", self.dirs["elsewhere"]).split("\n\n")
        self.assertNotIn("PAUSED", blocks[0])
        self.assertNotIn("paused", blocks[0])
        self.assertIn("1.33 h", blocks[0])                     # acme's numbers are untouched
        self.assertEqual(blocks[1].splitlines()[:3], [
            "Beta", "PAUSED since 2026-11-12 10:00 UTC until 2026-11-12 17:00 UTC",
            "Current period: Nov 1 – Nov 30, 2026 (ends Nov 30): 0.00 h"])
        self.assertIn(NOTE % ("1.08", "2026-11-12"), blocks[1])

    def test_status_line(self):
        self.setup()
        plain = self.ok("status", "--client", "acme", "--line")
        self.assertEqual(plain, "Current hours: 1.33 h\n")
        self.pause_acme()
        self.assertEqual(self.ok("status", "--client", "acme", "--line"),
                         "PAUSED • Current hours: 0.58 h\n")
        self.assertEqual(self.ok("status", "--client", "beta", "--line"),
                         "Current hours: 1.25 h\n")
        self.setup(self.cfg(components=[]))
        self.assertEqual(self.ok("status", "--client", "acme", "--line"), "PAUSED\n")
        self.assertEqual(self.ok("status", "--client", "beta", "--line"), "")
        self.write_pauses([])
        self.pause_acme("--until", "17:00")
        self.assertEqual(self.ok("status", "--client", "acme", "--line"), "PAUSED until 17:00\n")
        self.assertEqual(self.ok("status", "--client", "acme", "--line",
                                 now="2026-11-12T17:00:00Z"), "")       # over, file untouched
        self.assertIsNone(self.stored()[0]["end"])

    def test_status_line_until_is_local_time(self):
        self.setup(dict(self.cfg(), timezone="America/Los_Angeles"))
        self.ok("pause", "--client", "acme", "--at", "2026-11-12T09:30:00Z",
                "--until", "2026-11-12T22:30:00Z")
        self.assertEqual(self.ok("status", "--client", "acme", "--line"),
                         "PAUSED until 14:30 • Current hours: 0.58 h\n")
        self.assertIn("PAUSED since 2026-11-12 01:30 PST until 2026-11-12 14:30 PST",
                      self.ok("status", "--client", "acme"))

    def test_status_line_script_shows_the_pause_at_once(self):
        self.setup()
        stdin = json.dumps({"workspace": {"current_dir": self.dirs["acme"]}})
        first = self.run_script(ts.STATUSLINE, "--now", NOW, stdin=stdin)
        self.assertEqual((first.returncode, first.stdout), (0, "Current hours: 1.33 h\n"))
        self.pause_acme()
        r = self.run_script(ts.STATUSLINE, "--now", NOW, stdin=stdin)    # within the cache minute
        self.assertEqual((r.returncode, r.stdout, r.stderr),
                         (0, "PAUSED • Current hours: 0.58 h\n", ""))
        self.write_pauses("{broken")
        r = self.run_script(ts.STATUSLINE, "--now", NOW, stdin=stdin)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_status_json(self):
        self.setup()
        doc = json.loads(self.ok("status", "--client", "acme", "--json"))
        self.assertIsNone(doc["paused"])
        self.assertEqual(doc["paused_notes"], [])
        self.pause_acme("--until", "17:00")
        doc = json.loads(self.ok("status", "--client", "acme", "--json"))
        self.assertEqual(doc["paused"], {"id": "p1", "mode": "all",
                                         "start": "2026-11-12T09:30:00Z",
                                         "until": "2026-11-12T17:00:00Z"})
        self.assertEqual(doc["paused_notes"], [NOTE % ("0.75", "2026-11-12")])
        self.assertIsNone(json.loads(self.ok("status", "--client", "beta", "--json"))["paused"])
        doc = json.loads(self.ok("status", "--cwd", self.dirs["elsewhere"], "--json"))
        self.assertEqual([c["paused"] is None for c in doc["clients"]], [False, True])

    def test_compute_warns_and_reports_paused_hours(self):
        self.setup()
        doc = self.compute("acme", "this")
        self.assertEqual((doc["paused_hours"], doc["paused_dates"]), ("0.00", []))
        self.assertFalse([w for w in doc["warnings"] if "paused" in w])
        self.pause_acme()
        doc = self.compute("acme", "this")
        self.assertEqual((doc["total_hours"], doc["paused_hours"]), ("0.58", "0.75"))
        self.assertEqual(doc["paused_dates"], ["2026-11-12"])
        self.assertIn(NOTE % ("0.75", "2026-11-12"), doc["warnings"])
        other = self.compute("beta", "this")
        self.assertEqual((other["total_hours"], other["paused_hours"]), ("1.25", "0.00"))
        self.assertFalse([w for w in other["warnings"] if "paused" in w])
        last = self.compute("acme", "last")                    # the pause is not in that period
        self.assertEqual(last["paused_hours"], "0.00")

    def test_report_footer_and_json(self):
        self.setup()
        plain = self.ok("report", "--client", "beta")
        self.assertNotIn("paused", self.ok("report", "--client", "acme"))
        self.pause_acme()
        out = self.ok("report", "--client", "acme")
        self.assertEqual(out.splitlines()[-1], NOTE % ("0.75", "2026-11-12"))
        self.assertIn("0.58 h", out)
        self.assertEqual(self.ok("report", "--client", "beta"), plain)
        self.assertEqual(json.loads(self.ok("report", "--client", "acme", "--json"))
                         ["paused_hours"], "0.75")
        self.assertEqual(json.loads(self.ok("report", "--client", "beta", "--json"))
                         ["paused_hours"], "0.00")
        self.assertNotIn("paused", self.ok("report", "--client", "acme", "--period", "last"))
        self.ok("pauses", "--edit", "p1", "--start", "2026-11-12T08:00:00Z")   # everything paused
        out = self.ok("report", "--client", "acme")
        self.assertIn("Client: Acme Co", out)
        self.assertEqual(out.splitlines()[-1], NOTE % ("1.33", "2026-11-12"))

    def test_invalid_file_stops_every_command_that_bills(self):
        self.setup()
        for text in ("{broken", '[{"id": "p1", "client": "acme"}]'):
            self.write_pauses(text)
            for script, args in ((ts.INVOICE, ["compute", "--client", "acme", "--now", NOW]),
                                 (ts.INVOICE, ["render", "--client", "acme", "--now", NOW]),
                                 (ts.TIMESHEET, ["report", "--now", NOW]),
                                 (ts.TIMESHEET, ["report", "--json", "--now", NOW]),
                                 (ts.TIMESHEET, ["status", "--client", "acme", "--now", NOW]),
                                 (ts.TIMESHEET, ["status", "--line", "--client", "acme",
                                                 "--now", NOW])):
                r = self.run_script(script, *args)
                with self.subTest(text=text, args=args):
                    self.assertEqual((r.returncode, r.stdout), (2, ""))
                    self.assertIn("pauses.json", r.stderr)

    def test_approved_draft_goes_stale_when_a_pause_is_edited(self):
        self.setup(events=ts.EVENTS)                           # acme worked Nov 3, 09:00-09:40
        self.ok("pause", "--client", "acme", "--at", "2026-11-03T09:10:00Z",
                "--until", "2026-11-03T09:20:00Z")
        self.approve("acme", "last")
        doc = self.compute("acme", "last")
        self.assertFalse(doc["stale"])
        self.assertEqual(doc["paused_hours"], "0.17")
        approved = doc["total"]
        self.ok("pauses", "--edit", "p1", "--end", "2026-11-03T09:30:00Z")
        doc = self.compute("acme", "last")
        self.assertTrue(doc["stale"])
        self.assertNotEqual(doc["total"], approved)
        self.assertEqual(doc["paused_hours"], "0.33")
        self.assertTrue([w for w in doc["warnings"] if "numbers changed since approval" in w])
        self.assertIn("Last period: Nov 2 – Nov 8, 2026: stale",
                      self.ok("status", "--client", "acme"))


class RemindTests(Case):
    def remind(self, cwd, now=NOW):
        stdin = json.dumps({"hook_event_name": "SessionStart", "source": "startup",
                            "session_id": "s", "cwd": cwd, "transcript_path": ""})
        r = self.run_script(ts.REMIND, "--now", now, stdin=stdin)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return r.stdout

    PAUSED = ("timesheet: PAUSED for Acme Co since 2026-11-12 09:30 UTC. "
              "Run /timesheet:resume to start billing again.")

    def test_paused_notice_does_not_depend_on_reminders(self):
        for block in ({}, {"reminders": "off"}, {"reminders": "daily"}):
            self.setup(self.cfg(**block))
            self.write_pauses([])
            self.assertEqual(self.remind(self.dirs["acme"]), "")
            self.ok("pause", "--client", "acme", "--at", "09:30")
            out = self.remind(self.dirs["acme"])
            with self.subTest(block=block):
                self.assertEqual(out.count("\n"), 1)
                doc = json.loads(out)
                self.assertEqual(doc["systemMessage"], self.PAUSED)
                self.assertEqual(doc["hookSpecificOutput"]["hookEventName"], "SessionStart")
                ctx = doc["hookSpecificOutput"]["additionalContext"]
                self.assertIn("paused for Acme Co since 2026-11-12 09:30 UTC", ctx)
                self.assertIn("do not resume unless the user asks", ctx)
                self.assertNotIn("Beta", out)
                self.assertEqual(self.remind(self.dirs["acme"]), out)       # every session
                self.assertEqual(self.remind(self.dirs["beta"]), "")
                self.assertEqual(self.remind(self.dirs["elsewhere"]), "")
                self.assertFalse(os.path.exists(os.path.join(self.home, "reminders.json")))

    def test_combined_with_a_due_invoice_in_one_object(self):
        self.setup(self.cfg(reminders="session_start"), events=ts.EVENTS + TODAY)
        hours = self.hours("acme", "last")
        self.ok("pause", "--all", "--at", "09:30")
        out = self.remind(self.dirs["acme"])
        self.assertEqual(out.count("\n"), 1)
        doc = json.loads(out)
        self.assertEqual(doc["systemMessage"],
                         self.PAUSED + " timesheet: Invoice due: Nov 2 – Nov 8 (%s h). "
                                       "Run /timesheet:invoice acme" % hours)
        ctx = doc["hookSpecificOutput"]["additionalContext"]
        self.assertIn("billing is paused for Acme Co", ctx)
        self.assertIn("an invoice is due for Acme Co", ctx)
        self.assertNotIn("Beta", out)
        self.assertIn("PAUSED for Beta since", json.loads(self.remind(self.dirs["beta"]))
                      ["systemMessage"])

    def test_over_or_invalid_is_silent(self):
        self.setup()
        self.ok("pause", "--client", "acme", "--at", "09:30", "--for", "1h")
        self.assertEqual(self.remind(self.dirs["acme"]), "")              # ended at 10:30
        self.assertIn("PAUSED", self.remind(self.dirs["acme"], now="2026-11-12T10:00:00Z"))
        self.write_pauses("{broken")
        self.assertEqual(self.remind(self.dirs["acme"]), "")


# ------------------------------------------------------------------ away ("mine") windows

class AwayStoreTests(unittest.TestCase):
    def test_missing_mode_means_all_and_other_values_are_invalid(self):
        plain = window("p1", "acme", "09:00")
        self.assertNotIn("mode", plain)
        self.assertEqual(pauses.mode_of(plain), "all")
        self.assertEqual(pauses.mode_of(dict(plain, mode="mine")), "mine")
        for good in ("all", "mine"):
            with helpers.temp_home() as home:
                with open(os.path.join(home, "pauses.json"), "w") as f:
                    json.dump([dict(plain, mode=good)], f)
                self.assertEqual(pauses.mode_of(pauses.load()[0]), good)
        for bad in ("away", "", "ALL", None, 1, ["mine"]):
            with helpers.temp_home() as home:
                with open(os.path.join(home, "pauses.json"), "w") as f:
                    json.dump([dict(plain, mode=bad)], f)
                with self.subTest(mode=bad):
                    with self.assertRaises(pauses.PauseError) as cm:
                        pauses.load()
                    self.assertIn("mode", str(cm.exception))
                    with self.assertRaises(pauses.PauseError):
                        engine.timeline([], make_cfg(), at("12:00"))

    def test_new_windows_store_their_mode(self):
        items = []
        a = pauses.pause(items, "acme", at("09:00"), None, at("09:00"), mode="mine")
        b = pauses.pause(items, "acme", at("10:00"), None, at("10:00"))
        self.assertEqual((a["mode"], b["mode"]), ("mine", "all"))
        with self.assertRaises(pauses.PauseError):
            pauses.pause(items, "acme", at("10:00"), None, at("10:00"), mode="half")

    def test_windows_can_be_filtered_by_mode_and_a_full_pause_wins_as_the_active_one(self):
        items = [window("p1", "acme", "08:00", "09:00", mode="mine"),
                 window("p2", "acme", "08:30", "10:00"),
                 window("p3", "*", "11:00", "12:00", mode="mine")]
        now = at("23:00")
        self.assertEqual(pauses.windows_for(items, "acme", now, "mine"),
                         [(at("08:00"), at("09:00")), (at("11:00"), at("12:00"))])
        self.assertEqual(pauses.windows_for(items, "acme", now, "all"),
                         [(at("08:30"), at("10:00"))])
        self.assertEqual(pauses.windows_for(items, "acme", now),
                         [(at("08:00"), at("10:00")), (at("11:00"), at("12:00"))])
        self.assertEqual(pauses.windows_for(items, "beta", now, "all"), [])
        self.assertEqual(pauses.active(items, "acme", at("08:10"))["id"], "p1")
        self.assertEqual(pauses.active(items, "acme", at("08:40"))["id"], "p2")   # all wins
        self.assertEqual(pauses.active(items, "beta", at("11:30"))["id"], "p3")


class AwayClippingTests(unittest.TestCase):
    A = ("acme", "project-a")
    B = ("beta", "project-b")

    def base(self):
        return eturn("09:00", "10:00", "s1", "project-a")

    def test_mine_clips_only_your_time(self):
        plain = run(self.base(), [])
        tl = run(self.base(), [window("p1", "acme", "08:00", "11:00", mode="mine")])
        got = secs(tl)
        self.assertEqual(got[self.A, "agent"], 60 * 60)
        self.assertEqual(got[self.A, "you"], 0)
        self.assertEqual([s for s in tl.segs if s.kind == "agent"],
                         [s for s in plain.segs if s.kind == "agent"])
        self.assertEqual(tl.paused, {"acme": 10 * 60.0})
        self.assertEqual({s.kind for s in tl.paused_pieces}, {"you"})
        self.assertEqual(tl.away_pieces, tl.paused_pieces)
        self.assertEqual(got[("acme", NON_BILLABLE), "you"], 10 * 60)
        self.assertEqual(got[("acme", NON_BILLABLE), "agent"], 0)

    def test_mine_cuts_your_time_at_the_window_edges(self):
        tl = run(self.base(), [window("p1", "acme", "10:02", "11:00", mode="mine")])
        self.assertEqual(secs(tl)[self.A, "you"], 5 * 60 + 2 * 60)
        self.assertEqual(secs(tl)[self.A, "agent"], 60 * 60)
        self.assertEqual(tl.paused, {"acme": 3 * 60.0})
        tl = run(self.base(), [window("p1", "acme", "08:00", "08:57", mode="mine")])
        self.assertEqual(secs(tl)[self.A, "you"], 3 * 60 + 5 * 60)
        tl = run(self.base(), [window("p1", "acme", "09:10", "09:50", mode="mine")])
        self.assertEqual(tl.paused, {})                       # only agent time in there
        self.assertEqual(tl.segs, run(self.base(), []).segs)

    def test_all_wins_where_both_kinds_overlap(self):
        tl = run(self.base(), [window("p1", "acme", "08:00", "11:00", mode="mine"),
                               window("p2", "acme", "09:58", "10:02")])
        got = secs(tl)
        self.assertEqual(got[self.A, "agent"], 58 * 60)
        self.assertEqual(got[self.A, "you"], 0)
        self.assertEqual(tl.paused, {"acme": (2 + 2 + 5 + 3) * 60.0})
        away = sum((s.end - s.start).total_seconds() for s in tl.away_pieces)
        self.assertEqual(away, (5 + 3) * 60)                  # ramp-up + read after 10:02
        cfg = make_cfg()
        tz = timezone.utc
        self.assertEqual(health.paused_time(cfg, tl, "acme", None, None, tz)[0], 12 * 60)
        self.assertEqual(health.paused_time(cfg, tl, "acme", None, None, tz, mode="all")[0],
                         4 * 60)
        self.assertEqual(health.paused_time(cfg, tl, "acme", None, None, tz, mode="mine")[0],
                         8 * 60)
        same = run(self.base(), [window("p2", "acme", "09:58", "10:02"),
                                 window("p1", "acme", "08:00", "11:00", mode="mine")])
        self.assertEqual(same, tl)                            # the order in the file is irrelevant

    def test_missing_mode_behaves_as_all(self):
        a = run(self.base(), [window("p1", "acme", "09:30", "10:02")])
        b = run(self.base(), [window("p1", "acme", "09:30", "10:02", mode="all")])
        self.assertEqual(a, b)
        self.assertEqual(a.away_pieces, [])
        self.assertEqual(run(self.base(), []).away_pieces, [])

    def test_two_clients_are_isolated(self):
        events = self.base() + eturn("10:20", "11:00", "s2", "project-b", client="beta")
        plain = run(events, [])
        tl = run(events, [window("p1", "beta", "08:00", "12:00", mode="mine")])
        self.assertEqual([s for s in tl.segs if s.key[0] == "acme"],
                         [s for s in plain.segs if s.key[0] == "acme"])
        self.assertEqual(set(tl.paused), {"beta"})
        self.assertEqual(secs(tl)[self.B, "agent"], secs(plain)[self.B, "agent"])
        self.assertEqual(secs(tl)[self.B, "you"], 0)
        tl = run(events, [window("p1", "acme", "08:00", "12:00", mode="mine")])
        self.assertEqual([s for s in tl.segs if s.key[0] == "beta"],
                         [s for s in plain.segs if s.key[0] == "beta"])
        self.assertEqual(secs(tl)[self.A, "you"], 0)          # incl. the gap credited to acme
        tl = run(events, [window("p1", "*", "08:00", "12:00", mode="mine")])
        got = secs(tl)
        self.assertEqual((got[self.A, "you"], got[self.B, "you"]), (0, 0))
        self.assertEqual((got[self.A, "agent"], got[self.B, "agent"]), (60 * 60, 40 * 60))

    def test_summary_names_the_kind_of_window(self):
        cfg = make_cfg()
        tz = timezone.utc
        tl = run(self.base(), [window("p1", "acme", "08:00", "11:00", mode="mine")])
        self.assertEqual(health.paused_summary(cfg, tl, "acme", None, None, tz),
                         ("0.17", ["2026-11-10"], "away"))
        self.assertEqual(health.paused_summary(cfg, tl, "beta", None, None, tz),
                         ("0.00", [], None))
        both = run(self.base(), [window("p1", "acme", "08:00", "09:30", mode="mine"),
                                 window("p2", "acme", "09:30", "11:00")])
        self.assertEqual(health.paused_summary(cfg, both, "acme", None, None, tz)[2], "paused")
        self.assertEqual(health.paused_summary(cfg, both, "acme", at("08:00"), at("09:10"),
                                               tz)[2], "away")
        self.assertEqual(health.paused_warning("0.17", ["2026-11-10"], "away"),
                         AWAY_NOTE % ("0.17", "2026-11-10"))
        self.assertEqual(health.paused_warning("0.17", ["2026-11-10"], "paused"),
                         NOTE % ("0.17", "2026-11-10"))
        self.assertIsNone(health.paused_warning("0.00", [], "away"))


class AwayCommandTests(Case):
    def test_away_creates_a_mine_window_with_the_pause_flags(self):
        self.setup()
        out = self.ok("away", "--cwd", self.dirs["acme"])
        self.assertEqual(out.splitlines(), ["Away for Acme Co from 2026-11-12 12:00 UTC",
                                            AWAY_STILL])
        self.assertEqual(self.stored(), [{"id": "p1", "client": "acme", "mode": "mine",
                                          "start": "2026-11-12T12:00:00Z", "end": None,
                                          "until": None, "created": "2026-11-12T12:00:00Z"}])
        cases = [(["--client", "beta", "--for", "90m"], "beta", "2026-11-12T12:00:00Z",
                  "2026-11-12T13:30:00Z", "Beta"),
                 (["--all", "--at", "11:00", "--until", "17:00"], "*", "2026-11-12T11:00:00Z",
                  "2026-11-12T17:00:00Z", "all clients"),
                 (["--client", "acme", "--at", "2026-11-11", "--until", "09:00"], "acme",
                  "2026-11-11T00:00:00Z", "2026-11-13T09:00:00Z", "Acme Co")]
        for flags, client, start, until, name in cases:
            self.write_pauses([])
            with self.subTest(flags=flags):
                out = self.ok("away", *flags)
                p = self.stored()[0]
                self.assertEqual((p["client"], p["mode"], p["start"], p["until"], p["end"]),
                                 (client, "mine", start, until, None))
                self.assertEqual(out.splitlines(), [
                    "Away for %s from %s UTC until %s UTC" % (
                        name, start[:16].replace("T", " "), until[:16].replace("T", " ")),
                    AWAY_STILL])

    def test_away_errors_match_pause(self):
        self.setup()
        for flags, needle in ((["--client", "acme", "--for", "abc"], "invalid duration"),
                              (["--client", "acme", "--at", "13:00"], "future"),
                              (["--client", "gamma"], "unknown client"),
                              (["--cwd", self.dirs["elsewhere"]], "--client KEY")):
            with self.subTest(flags=flags):
                self.assertIn(needle, self.fails("away", *flags))
        for flags in (["--client", "acme", "--all"], ["--for", "1h", "--until", "17:00"],
                      ["--mode", "all"]):
            self.assertEqual(self.timesheet("away", *flags).returncode, 2)
        self.assertEqual(self.timesheet("pause", "--mode", "mine").returncode, 2)
        self.assertIsNone(self.stored())
        self.write_pauses("{broken")
        self.assertIn("pauses.json", self.fails("away", "--client", "acme"))

    def test_already_away_or_already_paused_changes_nothing(self):
        self.setup()
        self.ok("away", "--client", "acme", "--at", "09:30", "--until", "17:00")
        before = self.stored()
        self.assertEqual(self.ok("away", "--client", "acme"),
                         "Already away: Acme Co since 2026-11-12 09:30 UTC until "
                         "2026-11-12 17:00 UTC. Nothing changed.\n")
        self.assertEqual(self.stored(), before)
        self.ok("away", "--client", "beta")                    # the other client is free
        self.write_pauses([])
        self.ok("pause", "--all", "--at", "10:00")
        before = self.stored()
        for who, name in ((["--client", "acme"], "Acme Co"), (["--all"], "all clients")):
            self.assertEqual(self.ok("away", *who),
                             "Already paused: %s since 2026-11-12 10:00 UTC (a full pause "
                             "covers away). Nothing changed.\n" % name)
        self.assertEqual(self.stored(), before)

    def test_away_bills_agent_time_but_not_yours(self):
        self.setup()
        self.assertEqual((self.hours("acme"), self.hours("beta")), ("1.33", "1.25"))
        self.ok("away", "--client", "acme", "--at", "08:00")
        doc = self.compute("acme", "this")
        self.assertEqual((doc["total_hours"], doc["paused_hours"], doc["paused_kind"]),
                         ("1.00", "0.33", "away"))
        self.assertIn(AWAY_NOTE % ("0.33", "2026-11-12"), doc["warnings"])
        self.assertFalse([w for w in doc["warnings"] if "while paused" in w])
        other = self.compute("beta", "this")
        self.assertEqual((other["total_hours"], other["paused_hours"], other["paused_kind"]),
                         ("1.25", "0.00", None))
        self.assertFalse([w for w in other["warnings"] if "away" in w or "paused" in w])
        self.write_pauses([])
        self.ok("away", "--client", "beta", "--at", "08:00")
        self.assertEqual((self.hours("acme"), self.hours("beta")), ("1.33", "1.00"))

    def test_pause_while_away_closes_the_away_window(self):
        self.setup()
        self.ok("away", "--client", "acme", "--at", "08:00")
        out = self.ok("pause", "--client", "acme", "--at", "09:30")
        self.assertEqual(out.splitlines(), [
            "Ended away p1 at 2026-11-12 09:30 UTC",
            "Paused Acme Co at 2026-11-12 09:30 UTC", STILL])
        p1, p2 = self.stored()
        self.assertEqual((p1["mode"], p1["end"]), ("mine", "2026-11-12T09:30:00Z"))
        self.assertEqual((p2["mode"], p2["start"], p2["end"]),
                         ("all", "2026-11-12T09:30:00Z", None))
        doc = self.compute("acme", "this")
        self.assertEqual((doc["total_hours"], doc["paused_hours"], doc["paused_kind"]),
                         ("0.50", "0.83", "paused"))
        self.assertIn(NOTE % ("0.83", "2026-11-12"), doc["warnings"])
        self.assertEqual(self.ok("pause", "--client", "acme"),
                         "Already paused: Acme Co since 2026-11-12 09:30 UTC. "
                         "Nothing changed.\n")

    def test_pause_before_the_away_began_empties_the_away_window(self):
        self.setup()
        self.ok("away", "--client", "acme", "--at", "09:30")
        out = self.ok("pause", "--client", "acme", "--at", "08:00")
        self.assertEqual(out.splitlines()[:2], ["Ended away p1 at 2026-11-12 09:30 UTC",
                                                "Paused Acme Co at 2026-11-12 08:00 UTC"])
        p1 = self.stored()[0]
        self.assertEqual((p1["start"], p1["end"]), ("2026-11-12T09:30:00Z",) * 2)
        self.assertEqual(self.hours("acme"), "0.00")

    def test_pause_for_one_client_splits_an_away_of_all_clients(self):
        self.setup()
        self.ok("away", "--all", "--at", "08:00")
        out = self.ok("pause", "--client", "acme", "--at", "09:30")
        self.assertEqual(out.splitlines(), [
            "Ended away p1 at 2026-11-12 09:30 UTC",
            "That away covered all clients: the other 1 client(s) stay away (p2).",
            "Paused Acme Co at 2026-11-12 09:30 UTC", STILL])
        self.assertNotIn("Beta", out)
        self.assertNotIn("beta", out)
        self.assertEqual([(p["id"], p["client"], p["mode"], p["end"]) for p in self.stored()],
                         [("p1", "*", "mine", "2026-11-12T09:30:00Z"),
                          ("p2", "beta", "mine", None), ("p3", "acme", "all", None)])
        self.assertEqual(self.hours("beta"), "1.00")           # still away: agent time only
        self.write_pauses([])
        self.ok("away", "--client", "acme", "--at", "08:00")
        self.ok("away", "--client", "beta", "--at", "08:00")
        out = self.ok("pause", "--all")
        self.assertEqual(out.splitlines()[:3], ["Ended away p1 at 2026-11-12 12:00 UTC",
                                                "Ended away p2 at 2026-11-12 12:00 UTC",
                                                "Paused all clients at 2026-11-12 12:00 UTC"])

    def test_resume_says_which_kind_it_closed(self):
        self.setup()
        self.ok("away", "--client", "acme", "--at", "08:00")
        out = self.ok("resume", "--cwd", self.dirs["acme"])
        self.assertEqual(out.splitlines(), [
            "Resumed Acme Co at 2026-11-12 12:00 UTC",
            "Closed away p1: 2026-11-12 08:00 UTC to 2026-11-12 12:00 UTC (4h 00m)",
            "0.33 h of logged time in that window are not billed for Acme Co."])
        self.assertEqual(self.stored()[0]["end"], "2026-11-12T12:00:00Z")
        self.ok("pause", "--client", "acme", "--at", "12:10", now="2026-11-12T12:30:00Z")
        out = self.ok("resume", "--client", "acme", "--at", "12:20", now="2026-11-12T12:30:00Z")
        self.assertIn("Closed pause p2: 2026-11-12 12:10 UTC to 2026-11-12 12:20 UTC (0h 10m)",
                      out)
        self.write_pauses([])
        self.ok("away", "--all", "--at", "08:00")
        out = self.ok("resume", "--client", "acme", "--at", "09:00")
        self.assertIn("Closed away p1:", out)
        self.assertIn("That away covered all clients: the other 1 client(s) stay away (p2).",
                      out)
        self.assertNotIn("eta", out)
        self.assertEqual([(p["client"], p["mode"]) for p in self.stored()],
                         [("*", "mine"), ("beta", "mine")])
        # acme lost only the 5 min ramp-up before 09:00; beta is still away
        self.assertEqual((self.hours("acme"), self.hours("beta")), ("1.25", "1.00"))

    def test_list_shows_the_mode_and_edit_can_change_it(self):
        self.setup()
        self.ok("away", "--client", "acme", "--at", "08:00")
        self.ok("pause", "--client", "beta", "--at", "11:00")
        self.assertEqual(self.ok("pauses").splitlines(), [
            "p2  beta  paused  2026-11-12 11:00 UTC, still paused  0.58 h unbilled",
            "p1  acme  away  2026-11-12 08:00 UTC, still away  0.33 h unbilled"])
        doc = json.loads(self.ok("pauses", "--json"))
        self.assertEqual([(d["id"], d["mode"], d["unbilled_hours"]) for d in doc],
                         [("p2", "all", "0.58"), ("p1", "mine", "0.33")])
        out = self.ok("pauses", "--edit", "p1", "--mode", "all")
        self.assertEqual(out.splitlines()[:2], [
            "Before: p1 (Acme Co, away): 2026-11-12 08:00 UTC, still away, 0.33 h unbilled",
            "After:  p1 (Acme Co, paused): 2026-11-12 08:00 UTC, still paused, 1.33 h unbilled"])
        self.assertIn("stale", out.splitlines()[2])
        self.assertEqual(self.stored()[0]["mode"], "all")
        self.assertEqual(self.hours("acme"), "0.00")
        out = self.ok("pauses", "--edit", "p1", "--mode", "mine", "--end", "09:30")
        self.assertIn("After:  p1 (Acme Co, away): 2026-11-12 08:00 UTC to 2026-11-12 09:30 UTC, "
                      "0.08 h unbilled", out)
        self.assertEqual(self.hours("acme"), "1.25")
        self.assertEqual(self.timesheet("pauses", "--edit", "p1", "--mode", "half").returncode, 2)
        self.assertIn("Removed away p1 (Acme Co, away):", self.ok("pauses", "--remove", "p1"))


class AwaySurfaceTests(Case):
    def away_acme(self, *flags):
        self.ok("away", "--client", "acme", "--at", "08:00", *flags)

    def test_status_human_line_and_json(self):
        self.setup()
        plain = {c: self.ok("status", "--client", c) for c in ("acme", "beta")}
        self.away_acme("--until", "17:00")
        self.assertEqual(self.ok("status", "--cwd", self.dirs["acme"]).splitlines(), [
            "Acme Co",
            "AWAY since 2026-11-12 08:00 UTC until 2026-11-12 17:00 UTC",
            "Current period: Nov 9 – Nov 15, 2026 (ends Nov 15): 1.00 h",
            "Last period: Nov 2 – Nov 8, 2026: nothing to bill",
            AWAY_NOTE % ("0.33", "2026-11-12")])
        self.assertEqual(self.ok("status", "--client", "beta"), plain["beta"])
        self.assertEqual(self.ok("status", "--client", "acme", "--line"),
                         "AWAY until 17:00 • Current hours: 1.00 h\n")
        self.assertEqual(self.ok("status", "--client", "beta", "--line"),
                         "Current hours: 1.25 h\n")
        doc = json.loads(self.ok("status", "--client", "acme", "--json"))
        self.assertEqual(doc["paused"], {"id": "p1", "mode": "mine",
                                         "start": "2026-11-12T08:00:00Z",
                                         "until": "2026-11-12T17:00:00Z"})
        self.assertEqual(doc["paused_notes"], [AWAY_NOTE % ("0.33", "2026-11-12")])
        self.assertIsNone(json.loads(self.ok("status", "--client", "beta", "--json"))["paused"])
        self.setup(self.cfg(components=[]))
        self.write_pauses([])
        self.away_acme()
        self.assertEqual(self.ok("status", "--client", "acme", "--line"), "AWAY\n")
        self.assertIn("\nAWAY since 2026-11-12 08:00 UTC\n",
                      self.ok("status", "--client", "acme"))
        self.ok("pause", "--client", "acme", "--at", "09:30")
        self.assertEqual(self.ok("status", "--client", "acme", "--line"), "PAUSED\n")
        out = self.ok("status", "--client", "acme")
        self.assertIn("\nPAUSED since 2026-11-12 09:30 UTC\n", out)
        self.assertEqual(out.splitlines()[-1], NOTE % ("0.83", "2026-11-12"))

    def test_report_footer(self):
        self.setup()
        plain = self.ok("report", "--client", "beta")
        self.away_acme()
        out = self.ok("report", "--client", "acme")
        self.assertEqual(out.splitlines()[-1], AWAY_NOTE % ("0.33", "2026-11-12"))
        self.assertIn("1.00 h", out)
        self.assertEqual(self.ok("report", "--client", "beta"), plain)
        doc = json.loads(self.ok("report", "--client", "acme", "--json"))
        self.assertEqual((doc["paused_hours"], doc["paused_kind"]), ("0.33", "away"))

    def test_session_start_notice(self):
        want = ("timesheet: AWAY for Acme Co since 2026-11-12 08:00 UTC; your own time is not "
                "being counted. Run /timesheet:resume when you are back.")

        def remind(cwd):
            stdin = json.dumps({"hook_event_name": "SessionStart", "source": "startup",
                                "session_id": "s", "cwd": cwd, "transcript_path": ""})
            r = self.run_script(ts.REMIND, "--now", NOW, stdin=stdin)
            self.assertEqual((r.returncode, r.stderr), (0, ""))
            return r.stdout

        self.setup(self.cfg(reminders="off"))
        self.away_acme()
        out = remind(self.dirs["acme"])
        self.assertEqual(out.count("\n"), 1)
        doc = json.loads(out)
        self.assertEqual(doc["systemMessage"], want)
        ctx = doc["hookSpecificOutput"]["additionalContext"]
        self.assertIn("away", ctx)
        self.assertIn("do not resume unless the user asks", ctx)
        self.assertNotIn("Beta", out)
        self.assertEqual(remind(self.dirs["beta"]), "")
        self.setup(self.cfg(reminders="session_start"), events=ts.EVENTS + TODAY)
        hours = self.hours("acme", "last")
        self.assertEqual(json.loads(remind(self.dirs["acme"]))["systemMessage"],
                         want + " timesheet: Invoice due: Nov 2 – Nov 8 (%s h). "
                                "Run /timesheet:invoice acme" % hours)


# ------------------------------------------------------------------ no overlapping windows

class OverlapStoreTests(unittest.TestCase):
    def test_conflicts_and_overlaps(self):
        items = [window("p1", "acme", "09:00", "10:00"), window("p2", "beta", "09:30", "10:30"),
                 window("p3", "*", "10:00", "11:00"), window("p4", "acme", "10:30", "10:30"),
                 window("p5", "acme", "11:00"), window("p6", "beta", "12:00", until="13:00")]
        self.assertEqual(pauses.overlaps(items), [("p2", "p3")])
        ids = lambda w: [p["id"] for p in pauses.conflicts(items, w)]
        self.assertEqual(ids(window("x", "acme", "09:59", "10:01")), ["p1", "p3"])
        self.assertEqual(ids(window("x", "acme", "08:00", "09:00")), [])       # touching is fine
        self.assertEqual(ids(window("x", "acme", "08:00")), ["p1", "p3", "p5"])
        self.assertEqual(ids(window("x", "beta", "11:00")), ["p6"])
        self.assertEqual(ids(window("x", "beta", "11:00", until="12:00")), [])
        self.assertEqual(ids(window("x", "*", "12:30", "12:40")), ["p5", "p6"])
        self.assertEqual(ids(window("x", "*", "10:30", "10:30")), [])          # empty window
        self.assertEqual(ids(dict(items[0])), [])                              # not with itself
        self.assertEqual(pauses.overlaps(items[:1] + items[3:]), [])


class OverlapCommandTests(Case):
    def clean(self):
        self.assertEqual(pauses.overlaps(self.stored()), [])
        return [(p["id"], p["client"], pauses.mode_of(p), p["start"][11:16],
                 p["end"] and p["end"][11:16]) for p in self.stored()]

    def test_pause_all_over_an_individual_pause(self):
        self.setup()
        self.ok("pause", "--client", "acme", "--at", "09:30")
        before = (self.hours("acme"), self.hours("beta"))
        self.assertEqual(before, ("0.58", "1.25"))
        out = self.ok("pause", "--all")
        self.assertEqual(out.splitlines(), ["Ended pause p1 at 2026-11-12 12:00 UTC",
                                            "Paused all clients at 2026-11-12 12:00 UTC", STILL])
        self.assertEqual(self.clean(), [("p1", "acme", "all", "09:30", "12:00"),
                                        ("p2", "*", "all", "12:00", None)])
        self.assertEqual((self.hours("acme"), self.hours("beta")), before)
        self.assertEqual(self.ok("pauses").splitlines(), [
            "p2  all  paused  2026-11-12 12:00 UTC, still paused  0.00 h unbilled",
            "p1  acme  paused  2026-11-12 09:30 UTC to 2026-11-12 12:00 UTC  0.75 h unbilled"])
        self.assertEqual(self.ok("pause", "--all"),
                         "Already paused: all clients since 2026-11-12 12:00 UTC. "
                         "Nothing changed.\n")
        self.assertIn('"detail": "paused since 2026-11-12 12:00 UTC for all clients"',
                      self.timesheet("doctor", "--json").stdout)

    def test_away_all_over_individual_away_windows(self):
        self.setup()
        self.ok("away", "--client", "beta", "--at", "08:00")
        before = (self.hours("acme"), self.hours("beta"))
        out = self.ok("away", "--all")
        self.assertEqual(out.splitlines(), ["Ended away p1 at 2026-11-12 12:00 UTC",
                                            "Away for all clients from 2026-11-12 12:00 UTC",
                                            AWAY_STILL])
        self.assertEqual(self.clean(), [("p1", "beta", "mine", "08:00", "12:00"),
                                        ("p2", "*", "mine", "12:00", None)])
        self.assertEqual((self.hours("acme"), self.hours("beta")), before)
        self.assertEqual(self.ok("away", "--client", "acme"),
                         "Already away: Acme Co since 2026-11-12 12:00 UTC. Nothing changed.\n")
        out = self.ok("pause", "--all", "--until", "17:00")       # a full pause is stronger
        self.assertEqual(out.splitlines()[:2], [
            "Ended away p2 at 2026-11-12 12:00 UTC",
            "Paused all clients at 2026-11-12 12:00 UTC until 2026-11-12 17:00 UTC"])
        self.assertEqual(self.clean()[1:], [("p2", "*", "mine", "12:00", "12:00"),
                                            ("p3", "*", "all", "12:00", None)])

    def test_away_all_leaves_a_fully_paused_client_alone(self):
        self.setup()
        self.ok("pause", "--client", "acme", "--at", "09:30")
        out = self.ok("away", "--all", "--until", "17:00")
        self.assertEqual(out.splitlines(), [
            "Left pause p1 as it is (a full pause covers away).",
            "Away for Beta from 2026-11-12 12:00 UTC until 2026-11-12 17:00 UTC", AWAY_STILL])
        self.assertEqual(self.clean(), [("p1", "acme", "all", "09:30", None),
                                        ("p2", "beta", "mine", "12:00", None)])
        self.assertEqual(self.stored()[1]["until"], "2026-11-12T17:00:00Z")
        self.assertEqual((self.hours("acme"), self.hours("beta")), ("0.58", "1.25"))
        before = self.stored()
        out = self.ok("away", "--all")
        self.assertEqual(out.splitlines(), [
            "Left pause p1 as it is (a full pause covers away).",
            "Left away p2 as it is.", "Nothing changed."])
        self.assertEqual(self.stored(), before)

    def test_backdated_windows_may_not_overlap_history(self):
        self.setup()
        self.ok("pause", "--client", "acme", "--at", "09:30")
        self.ok("resume", "--client", "acme", "--at", "10:00")
        before = self.stored()
        for sub in (["pause", "--client", "acme", "--at", "09:45"],
                    ["away", "--client", "acme", "--at", "09:45"],
                    ["pause", "--all", "--at", "09:45"],
                    ["away", "--all", "--at", "09:00"],
                    ["pause", "--client", "acme", "--at", "09:00",
                     "--until", "2026-11-12T09:45:00Z"]):
            with self.subTest(sub=sub):
                err = self.fails(*sub)
                self.assertIn("overlap", err)
                self.assertIn("p1", err)
        self.assertEqual(self.stored(), before)
        self.ok("away", "--client", "beta", "--at", "09:45")       # another client is free
        self.ok("pause", "--client", "acme", "--at", "09:00", "--until", "2026-11-12T09:30:00Z")
        self.ok("pause", "--client", "acme", "--at", "10:00")      # touching the old end
        self.assertEqual(self.clean(), [("p1", "acme", "all", "09:30", "10:00"),
                                        ("p2", "beta", "mine", "09:45", None),
                                        ("p3", "acme", "all", "09:00", None),
                                        ("p4", "acme", "all", "10:00", None)])

    def test_edit_may_not_create_an_overlap(self):
        self.setup()
        self.ok("pause", "--client", "acme", "--at", "09:30", "--until", "2026-11-12T10:00:00Z")
        self.ok("pause", "--client", "acme", "--at", "10:30", "--until", "2026-11-12T11:00:00Z")
        self.ok("away", "--all", "--at", "11:30")
        before = self.stored()
        for flags, other in ((["--edit", "p2", "--start", "09:45"], "p1"),
                             (["--edit", "p1", "--end", "10:45"], "p2"),
                             (["--edit", "p3", "--start", "10:50"], "p2"),
                             (["--edit", "p2", "--end", "11:45"], "p3")):
            with self.subTest(flags=flags):
                err = self.fails("pauses", *flags)
                self.assertIn("overlap", err)
                self.assertIn(other, err)
        self.assertEqual(self.stored(), before)
        self.ok("pauses", "--edit", "p1", "--end", "10:30")
        self.ok("pauses", "--edit", "p2", "--mode", "mine")
        self.assertEqual(pauses.overlaps(self.stored()), [])

    def test_hand_edited_overlap_is_merged_and_reported(self):
        self.setup()
        self.write_pauses([{"id": "p1", "client": "acme", "start": "2026-11-12T09:30:00Z",
                            "end": "2026-11-12T09:50:00Z", "until": None,
                            "created": "2026-11-12T09:30:00Z"},
                           {"id": "p2", "client": "*", "start": "2026-11-12T09:40:00Z",
                            "end": "2026-11-12T10:00:00Z", "until": None,
                            "created": "2026-11-12T09:40:00Z"}])
        self.assertEqual(self.compute("acme", "this")["paused_hours"], "0.50")
        r = self.timesheet("doctor", "--json")
        check = {c["name"]: c for c in json.loads(r.stdout)["checks"]}["pauses"]
        self.assertEqual((r.returncode, check["status"], check["detail"]),
                         (0, "WARN", "overlapping windows: p1 and p2"))


class DoctorTests(Case):
    def checks(self):
        r = self.timesheet("doctor", "--json")
        doc = json.loads(r.stdout)
        return r.returncode, {c["name"]: c for c in doc["checks"]}["pauses"]

    def test_pauses_line(self):
        self.setup()
        self.assertEqual(self.checks(), (0, {"name": "pauses", "status": "OK", "detail": "none"}))
        self.write_pauses([{"id": "p1", "client": "acme", "start": "2020-01-01T10:00:00Z",
                            "end": "2020-01-01T11:00:00Z", "until": None,
                            "created": "2020-01-01T10:00:00Z"}])
        self.assertEqual(self.checks()[1]["detail"], "1 window(s), none active")
        self.write_pauses([{"id": "p1", "client": "acme", "start": "2020-01-01T10:00:00Z",
                            "end": "2020-01-02T10:00:00Z", "until": None,
                            "created": "2020-01-01T10:00:00Z"},
                           {"id": "p2", "client": "*", "start": "2020-01-02T10:00:00Z",
                            "end": None, "until": None, "created": "2020-01-02T10:00:00Z"}])
        code, check = self.checks()
        self.assertEqual((code, check["status"]), (0, "WARN"))
        self.assertEqual(check["detail"], "paused since 2020-01-02 10:00 UTC for all clients")
        self.write_pauses([{"id": "p1", "client": "acme", "start": "2020-01-01T10:00:00Z",
                            "end": None, "until": None, "created": "2020-01-01T10:00:00Z"},
                           {"id": "p2", "client": "*", "start": "2020-01-02T10:00:00Z",
                            "end": "2020-01-03T10:00:00Z", "until": None,
                            "created": "2020-01-02T10:00:00Z"},
                           {"id": "p3", "client": "beta", "start": "2020-01-05T10:00:00Z",
                            "end": "2020-01-06T10:00:00Z", "until": None,
                            "created": "2020-01-05T10:00:00Z"}])
        code, check = self.checks()
        self.assertEqual((code, check["status"]), (0, "WARN"))
        self.assertEqual(check["detail"], "paused since 2020-01-01 10:00 UTC for acme; "
                                          "overlapping windows: p1 and p2")
        self.write_pauses([{"id": "p1", "client": "beta", "mode": "mine",
                            "start": "2020-01-01T10:00:00Z", "end": None, "until": None,
                            "created": "2020-01-01T10:00:00Z"}])
        code, check = self.checks()
        self.assertEqual((code, check["status"], check["detail"]),
                         (0, "WARN", "away since 2020-01-01 10:00 UTC for beta"))
        self.assertIn("WARN  pauses: away since 2020-01-01 10:00 UTC for beta",
                      self.timesheet("doctor").stdout)
        self.write_pauses("{broken")
        code, check = self.checks()
        self.assertEqual((code, check["status"]), (1, "FAIL"))
        self.assertIn("pauses.json", check["detail"])


class DocsTests(unittest.TestCase):
    def read(self, *parts):
        with open(os.path.join(helpers.REPO, *parts), encoding="utf-8") as f:
            return f.read()

    def test_commands_exist_and_docs_cover_the_feature(self):
        for name in ("pause", "resume", "away"):
            text = self.read("commands", name + ".md")
            self.assertIn('scripts/timesheet.py" %s' % name, text)
            self.assertIn("output unchanged", text)
            self.assertIn("/timesheet:" + name, self.read("README.md"))
            self.assertIn("/timesheet:" + name, self.read("skills", "timesheet", "README.md"))
        skill = self.read("skills", "timesheet", "SKILL.md")
        section = skill[skill.index("## Pause and resume"):skill.index("## Editing config")]
        for needle in ("resume --at", "pauses --edit", "pauses --remove", "get a yes",
                       "unless the user asks", "Ignore", "timesheet.py\" away", "--mode",
                       "agent"):
            self.assertIn(needle, section)
        ref = self.read("docs", "reference.md")
        section = ref[ref.index("## Pause and resume"):ref.index("## The invoice flow")]
        for needle in ("pauses.json", "--for", "--until", "--at", "--remove", "--edit", '"*"',
                       "PAUSED", "paused_hours", "doctor", "cut at the window's edges",
                       '"mode"', '"mine"', "AWAY", "away", "--mode", "paused_kind"):
            self.assertIn(needle, section)
        self.assertIn("## Pause and resume", self.read("README.md"))

    def test_hook_script_does_not_know_about_pauses(self):
        self.assertNotIn("pause", self.read("scripts", "hook.py").lower())


if __name__ == "__main__":
    unittest.main()
