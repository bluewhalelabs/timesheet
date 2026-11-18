import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tests import helpers

CLI = os.path.join(helpers.SCRIPTS, "timesheet.py")
PY = "/usr/bin/python3"
NOW = "2026-11-12T12:00:00Z"   # a Thursday; week = Nov 9 - 15, month = November


def two_clients(**top):
    # these suites were written against one line per day; the shipped default is per session
    cfg = {"timezone": "UTC", "defaults": {"line_items": "day"}, "clients": {
        "acme": {"name": "Acme Co", "overrides": {
            "hourly_rate": 150, "billing_period": {"type": "weekly"},
            "rounding": {"increment_hours": 0.01, "mode": "nearest"}}},
        "beta": {"name": "Beta", "overrides": {
            "hourly_rate": 200, "billing_period": {"type": "monthly"},
            "rounding": {"increment_hours": 0.25, "mode": "up"}}},
    }}
    cfg.update(top)
    return cfg


def one_client():
    cfg = two_clients()
    del cfg["clients"]["beta"]
    return cfg


def event(ts, kind, session, project, client="acme", cwd="/nowhere"):
    return {"ts": ts, "event": kind, "hook": "x", "session": session,
            "client": client, "project": project, "cwd": cwd,
            "transcript_path": ""}


def turn(day, start, end, session, project, client="acme", cwd="/nowhere"):
    return [event("%sT%s:00Z" % (day, start), "start", session, project, client, cwd),
            event("%sT%s:00Z" % (day, end), "end", session, project, client, cwd)]


DAY = "2026-11-10"
RUBRIC = (turn(DAY, "09:00", "09:10", "s1", "p1") + turn(DAY, "09:20", "09:30", "s2", "p2")
          + turn(DAY, "10:00", "10:05", "s3", "p1") + turn(DAY, "12:00", "12:10", "s4", "p2"))


class CliCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.realpath(self._tmp.name)

    def setup(self, cfg, events=(), raw_lines=()):
        helpers.write_config(self.home, cfg)
        with open(os.path.join(self.home, "events.jsonl"), "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
            for line in raw_lines:
                f.write(line + "\n")

    def run_cli(self, *args, **env_extra):
        env = dict(os.environ)
        env["TIMESHEET_HOME"] = self.home
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.update(env_extra)
        return subprocess.run([PY, CLI] + list(args), capture_output=True, text=True,
                              env=env, timeout=30)

    def report(self, *args, **env_extra):
        return self.run_cli("report", "--now", NOW, *args, **env_extra)

    def ok(self, *args, **env_extra):
        r = self.report(*args, **env_extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def config(self):
        with open(os.path.join(self.home, "config.json")) as f:
            return json.load(f)


class ReportTests(CliCase):
    def test_report_by_project_matches_engine_numbers(self):
        self.setup(two_clients(), RUBRIC)
        r = self.ok("--client", "acme", "--week", "2026-11-10")
        out = r.stdout
        self.assertIn("Client: Acme Co", out)
        self.assertIn("Project | Your time | Agent time | Billable | Rate | Amount",
                      " | ".join(c.strip() for c in out.split("\n")[3].split("|")))
        rows = {}
        for line in out.splitlines():
            cells = [c.strip() for c in line.split("|")]
            if cells[0] in ("p1", "p2", "TOTAL"):
                rows[cells[0]] = cells[1:]
        self.assertEqual(rows["p1"], ["0h 10m", "0h 15m", "0.42 h", "$150.00/h", "$62.50"])
        self.assertEqual(rows["p2"], ["0h 35m", "0h 20m", "0.92 h", "$150.00/h", "$137.50"])
        self.assertEqual(rows["TOTAL"][0], "0h 45m")
        self.assertEqual(rows["TOTAL"][1], "0h 35m")
        self.assertEqual(rows["TOTAL"][4], "$200.00")
        self.assertIn("Agent time billed at 100%. Your time: gaps <= 15m in full, "
                      "<= 45m capped at 15m, longer = 5m read + 5m ramp-up.", out)
        self.assertNotIn("Beta", out)
        self.assertNotIn("$200.00/h", out)

    def test_json_report_has_unrounded_seconds(self):
        self.setup(two_clients(), RUBRIC)
        r = self.ok("--client", "acme", "--period", "this", "--json")
        data = json.loads(r.stdout)
        self.assertEqual(data["client"], "acme")
        self.assertEqual(data["range"]["label"], "Nov 9 – Nov 15, 2026")
        self.assertEqual(data["range"]["start"], "2026-11-09")
        self.assertEqual(data["range"]["end"], "2026-11-15")
        rows = {x["project"]: x for x in data["rows"]}
        self.assertEqual(rows["p1"]["you_seconds"], 600)
        self.assertEqual(rows["p1"]["agent_seconds"], 900)
        self.assertEqual(rows["p1"]["billable_seconds"], 1500)
        self.assertEqual(rows["p1"]["hourly_rate"], 150)
        self.assertEqual(rows["p1"]["amount"], "62.50")
        self.assertEqual(rows["p1"]["period"], "")
        self.assertEqual(rows["p2"]["billable_seconds"], 3300)
        self.assertEqual(data["capped"], 0)
        self.assertEqual(data["warnings"], [])

    def test_agent_fraction_applies_to_billable_seconds(self):
        cfg = two_clients()
        cfg["clients"]["acme"]["overrides"]["agent_billable_fraction"] = 0.5
        self.setup(cfg, turn(DAY, "09:00", "09:10", "s1", "p1"))
        data = json.loads(self.ok("--client", "acme", "--period", "this", "--json").stdout)
        row = data["rows"][0]
        self.assertEqual(row["agent_seconds"], 600)
        self.assertEqual(row["you_seconds"], 600)
        self.assertEqual(row["billable_seconds"], 900)
        r = self.ok("--client", "acme", "--period", "this")
        self.assertIn("Agent time billed at 50%.", r.stdout)

    def test_period_since_until_all_ranges(self):
        evs = (turn("2026-11-02", "09:00", "10:00", "s1", "p1")
               + turn("2026-11-10", "09:00", "10:00", "s2", "p1"))
        self.setup(one_client(), evs)
        by_range = {}
        for name, args in (("this", ["--period", "this"]),
                           ("last", ["--period", "last"]),
                           ("week", ["--week", "this"]),
                           ("since_until", ["--since", "2026-11-10", "--until", "2026-11-10"]),
                           ("since", ["--since", "2026-11-03"]),
                           ("until", ["--until", "2026-11-09"]),
                           ("all", ["--all"])):
            data = json.loads(self.ok(*args, "--json").stdout)
            by_range[name] = data
            self.assertEqual(data["client"], "acme", name)
        total = lambda d: sum(x["agent_seconds"] for x in d["rows"])
        self.assertEqual(total(by_range["this"]), 3600)
        self.assertEqual(total(by_range["last"]), 3600)
        self.assertEqual(total(by_range["week"]), 3600)
        self.assertEqual(total(by_range["since_until"]), 3600)
        self.assertEqual(total(by_range["since"]), 3600)
        self.assertEqual(total(by_range["until"]), 3600)
        self.assertEqual(total(by_range["all"]), 7200)
        self.assertEqual(by_range["all"]["range"]["label"], "All time")
        self.assertIsNone(by_range["all"]["range"]["start"])
        self.assertEqual(by_range["since"]["range"]["label"], "Since Nov 3, 2026")
        self.assertEqual(by_range["until"]["range"]["label"], "Until Nov 9, 2026")
        self.assertEqual(by_range["since_until"]["range"]["label"], "Nov 10, 2026")
        # until is inclusive of that whole day
        data = json.loads(self.ok("--since", "2026-11-10", "--until", "today", "--json").stdout)
        self.assertEqual(data["range"]["end"], "2026-11-12")
        # ranges are mutually exclusive
        self.assertEqual(self.report("--all", "--week", "this").returncode, 2)
        self.assertEqual(self.report("--period", "this", "--since", "2026-11-01").returncode, 2)

    def test_week_is_monday_sunday_regardless_of_billing_period(self):
        evs = turn("2026-11-02", "09:00", "10:00", "s1", "p1", "beta")
        self.setup(two_clients(), evs)
        data = json.loads(self.ok("--client", "beta", "--week", "2026-11-04", "--json").stdout)
        self.assertEqual(data["range"]["start"], "2026-11-02")
        self.assertEqual(data["range"]["end"], "2026-11-08")
        self.assertEqual(data["rows"][0]["agent_seconds"], 3600)

    def test_by_day_and_week_buckets(self):
        evs = turn(DAY, "09:00", "10:00", "s1", "p1") + turn("2026-11-11", "09:00", "10:00", "s2", "p1")
        self.setup(one_client(), evs)
        r = self.ok("--by", "day")
        self.assertEqual([c.strip() for c in r.stdout.splitlines()[3].split("|")][:3],
                         ["Period", "Project", "Your time"])
        self.assertIn("2026-11-10 Tue", r.stdout)
        self.assertIn("2026-11-11 Wed", r.stdout)
        r = self.ok("--by", "week", "--csv")
        lines = r.stdout.splitlines()
        self.assertEqual(lines[1].split(",")[1], "2026-W46")
        self.assertEqual(len(lines), 2)

    def test_project_filter(self):
        self.setup(two_clients(), RUBRIC)
        data = json.loads(self.ok("--client", "acme", "--week", "this", "--project", "p2", "--json").stdout)
        self.assertEqual([x["project"] for x in data["rows"]], ["p2"])
        r = self.report("--client", "acme", "--project", "nope")
        self.assertEqual(r.returncode, 2)
        self.assertIn("nope", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_csv_and_md_shapes(self):
        self.setup(two_clients(), RUBRIC)
        r = self.ok("--client", "acme", "--week", "this", "--csv")
        lines = r.stdout.strip().splitlines()
        self.assertEqual(lines[0], "client,period,project,you_hours,agent_hours,"
                                   "agent_billed_pct,billable_hours,hourly_rate,amount")
        self.assertEqual(lines[1], "acme,,p1,0.17,0.25,100,0.42,150,62.50")
        self.assertEqual(lines[2], "acme,,p2,0.58,0.33,100,0.92,150,137.50")
        self.assertEqual(len(lines), 3)
        r = self.ok("--client", "acme", "--week", "this", "--md")
        md = r.stdout.splitlines()
        table = [l for l in md if l.startswith("|")]
        self.assertEqual(table[0], "| Project | Your time | Agent time | Billable | Rate | Amount |")
        self.assertEqual(table[1], "| --- | ---: | ---: | ---: | ---: | ---: |")
        self.assertEqual(len(table), 5)
        self.assertEqual(table[-1].split("|")[1].strip(), "**TOTAL**")
        self.assertIn("**$200.00**", table[-1])

    def test_no_tracked_time_message(self):
        self.setup(one_client(), [])
        r = self.ok("--period", "this")
        self.assertEqual(r.stdout.strip(), "Nov 9 – Nov 15, 2026: no tracked time.")
        self.assertEqual(r.stderr, "")

    def test_warns_when_hook_errors_log_is_non_empty(self):
        self.setup(one_client(), RUBRIC)
        with open(os.path.join(self.home, "hook-errors.log"), "w") as f:
            f.write("first problem\nlast problem \xe9\n")
        r = self.ok("--week", "this")
        self.assertIn("hook errors logged: 2; last: last problem", r.stderr)
        self.assertNotIn("hook errors", r.stdout)
        data = json.loads(self.ok("--week", "this", "--json").stdout)
        self.assertTrue(any("hook errors logged: 2" in w for w in data["warnings"]))

    def test_warns_about_skipped_lines_and_capped_turns(self):
        evs = [event("2026-11-10T09:00:00Z", "start", "s1", "p1"),
               event("2026-11-10T09:30:00Z", "start", "s1", "p1"),
               event("2026-11-10T09:31:00Z", "end", "s1", "p1")]
        self.setup(one_client(), evs, raw_lines=["not json", "{\"x\": 1}"])
        r = self.ok("--week", "this")
        self.assertIn("2 unreadable line(s) skipped in events.jsonl", r.stderr)
        self.assertIn("Note: 1 interrupted turn(s) capped at 10 min.", r.stdout)
        data = json.loads(self.ok("--week", "this", "--json").stdout)
        self.assertEqual(data["capped"], 1)
        self.assertIn("2 unreadable line(s) skipped in events.jsonl", data["warnings"])

    def test_capped_count_is_scoped_to_range_and_billable_sessions(self):
        def capped(day, session, project):
            return [event("%sT09:00:00Z" % day, "start", session, project),
                    event("%sT09:30:00Z" % day, "start", session, project),
                    event("%sT09:31:00Z" % day, "end", session, project)]
        evs = (capped("2026-11-10", "in1", "p1")
               + capped("2026-10-20", "out1", "p1")
               + capped("2026-11-11", "nb1", "p1"))
        cfg = one_client()
        cfg["clients"]["acme"]["non_billable_sessions"] = ["nb1"]
        self.setup(cfg, evs)
        data = json.loads(self.ok("--week", "this", "--json").stdout)
        self.assertEqual(data["capped"], 1)
        r = self.ok("--week", "this")
        self.assertIn("Note: 1 interrupted turn(s)", r.stdout)
        self.assertEqual(json.loads(self.ok("--all", "--json").stdout)["capped"], 2)
        # the --project filter applies too
        self.setup(cfg, evs + capped("2026-11-12", "in2", "p2"))
        self.assertEqual(json.loads(self.ok("--week", "this", "--json").stdout)["capped"], 2)
        self.assertEqual(json.loads(
            self.ok("--week", "this", "--project", "p2", "--json").stdout)["capped"], 1)

    def test_capped_count_is_per_client(self):
        evs = [event("2026-11-10T09:00:00Z", "start", "a1", "p1"),
               event("2026-11-10T09:30:00Z", "start", "a1", "p1"),
               event("2026-11-10T09:31:00Z", "end", "a1", "p1")]
        evs += turn(DAY, "11:00", "12:00", "b1", "p9", "beta")
        self.setup(two_clients(), evs)
        data = json.loads(self.ok("--json").stdout)
        by = {c["client"]: c["capped"] for c in data["clients"]}
        self.assertEqual(by, {"acme": 1, "beta": 0})
        self.assertEqual(data["capped"], 1)

    def test_non_utc_timezone_buckets_by_local_day(self):
        cfg = one_client()
        cfg["timezone"] = "America/Los_Angeles"
        self.setup(cfg, turn(DAY, "07:30", "08:30", "s1", "p1"))   # 23:30 Nov 9 - 00:30 Nov 10 PST
        data = json.loads(self.ok("--week", "this", "--by", "day", "--json").stdout)
        agent = {x["period"]: x["agent_seconds"] for x in data["rows"]}
        self.assertEqual(agent, {"2026-11-09": 1800, "2026-11-10": 1800})
        data = json.loads(self.ok("--since", "2026-11-10", "--until", "2026-11-10",
                                  "--by", "day", "--json").stdout)
        agent = {x["period"]: x["agent_seconds"] for x in data["rows"]}
        self.assertEqual(agent, {"2026-11-10": 1800})

    def test_bad_config_exits_2_without_traceback(self):
        self.setup({"timezone": "Not/AZone", "clients": {}})
        r = self.report()
        self.assertEqual(r.returncode, 2)
        self.assertIn("Not/AZone", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        os.unlink(os.path.join(self.home, "config.json"))
        r = self.report()
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)
        self.setup(two_clients())
        for args in (["--client", "zzz"], ["--period", "garbage", "--client", "acme"],
                     ["--since", "yesterday"], ["--now", "bogus"]):
            r = self.run_cli("report", *(["--now", NOW] + args if args[0] != "--now" else args))
            self.assertEqual(r.returncode, 2, args)
            self.assertNotIn("Traceback", r.stderr)

    def test_two_clients_report_separately_at_their_own_rates(self):
        evs = (turn(DAY, "09:00", "10:00", "s1", "p1", "acme")
               + turn(DAY, "11:00", "12:00", "s2", "p9", "beta"))
        self.setup(two_clients(), evs)
        # 1 h of agent time plus 10 m of your time (ramp-up and read allowance) each
        a = json.loads(self.ok("--client", "acme", "--week", "this", "--json").stdout)
        b = json.loads(self.ok("--client", "beta", "--week", "this", "--json").stdout)
        self.assertEqual(a["rows"][0]["hourly_rate"], 150)
        self.assertEqual(b["rows"][0]["hourly_rate"], 200)
        self.assertEqual(a["rows"][0]["agent_seconds"], 3600)
        self.assertEqual(b["rows"][0]["agent_seconds"], 3600)
        self.assertEqual(a["rows"][0]["amount"], "175.00")   # 1 h agent + 10 m you
        self.assertEqual(b["rows"][0]["amount"], "233.33")
        ta = self.ok("--client", "acme", "--week", "this").stdout
        tb = self.ok("--client", "beta", "--week", "this").stdout
        self.assertIn("$150.00/h", ta)
        self.assertNotIn("$200.00/h", ta)
        self.assertNotIn("p9", ta)
        self.assertNotIn("Beta", ta)
        self.assertIn("$200.00/h", tb)
        self.assertNotIn("p1", tb.split("Project", 1)[1])
        self.assertIn("p9", tb)

    def test_all_clients_report_has_a_section_per_client(self):
        evs = (turn(DAY, "09:00", "10:00", "s1", "p1", "acme")
               + turn(DAY, "11:00", "12:00", "s2", "p9", "beta"))
        self.setup(two_clients(), evs)
        r = self.ok()
        out = r.stdout
        self.assertIn("Client: Acme Co", out)
        self.assertIn("Client: Beta", out)
        self.assertLess(out.index("Client: Acme Co"), out.index("Client: Beta"))
        self.assertIn("Grand total USD: $408.33", out)
        self.assertIn("$175.00", out)
        self.assertIn("$233.33", out)
        self.assertEqual(out.count("Nov 9 – Nov 15, 2026"), 2)
        data = json.loads(self.ok("--client", "all", "--json").stdout)
        self.assertEqual([c["client"] for c in data["clients"]], ["acme", "beta"])
        self.assertEqual(data["capped"], 0)
        self.assertEqual(data["warnings"], [])
        csv_out = self.ok("--csv").stdout.strip().splitlines()
        self.assertEqual(len(csv_out), 3)
        self.assertTrue(csv_out[1].startswith("acme,"))
        self.assertTrue(csv_out[2].startswith("beta,"))
        # a client without rows gets no section
        self.setup(two_clients(), turn(DAY, "09:00", "10:00", "s1", "p1", "acme"))
        out = self.ok().stdout
        self.assertNotIn("Beta", out)

    def test_period_with_several_clients_requires_client(self):
        self.setup(two_clients(), RUBRIC)
        r = self.report("--period", "this")
        self.assertEqual(r.returncode, 2)
        self.assertIn("acme, beta", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        r = self.report("--period", "this", "--client", "all")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.report("--period", "this", "--client", "acme").returncode, 0)

    def test_single_client_defaults_to_that_clients_period(self):
        cfg = one_client()
        cfg["clients"]["acme"]["overrides"]["billing_period"] = {"type": "monthly"}
        self.setup(cfg, RUBRIC)
        data = json.loads(self.ok("--json").stdout)
        self.assertEqual(data["range"]["start"], "2026-11-01")
        self.assertEqual(data["range"]["end"], "2026-11-30")

    def test_help_mentions_no_per_line_rounding(self):
        r = self.run_cli("report", "--help")
        self.assertEqual(r.returncode, 0)
        text = " ".join(r.stdout.split())
        self.assertIn("rounding", text)
        self.assertIn("manual", text)


class SilentLossTests(CliCase):
    UNCLOSED = ("1 turn(s) never closed (no Stop or SessionEnd logged) and were not counted: "
                "2026-11-10; they are counted, capped at 10 min, once the session ends")

    def test_unclosed_final_turn_is_reported_per_client_and_range(self):
        evs = (turn(DAY, "08:00", "08:30", "a0", "p1") + turn(DAY, "13:00", "13:10", "a3", "p2")
               + [event("2026-11-10T09:00:00Z", "start", "a1", "p1"),
                  event("2026-11-03T09:00:00Z", "start", "a2", "p1"),     # last week
                  event("2026-11-10T09:00:00Z", "start", "b1", "p9", "beta")]
               + turn(DAY, "11:00", "12:00", "b2", "p9", "beta"))
        self.setup(two_clients(), evs)
        out = self.ok("--client", "acme", "--week", "this").stdout
        self.assertIn(self.UNCLOSED, out)
        data = json.loads(self.ok("--client", "acme", "--week", "this", "--json").stdout)
        self.assertEqual(data["unclosed"], 1)
        data = json.loads(self.ok("--client", "acme", "--all", "--json").stdout)
        self.assertEqual(data["unclosed"], 2)
        data = json.loads(self.ok("--json").stdout)
        self.assertEqual({c["client"]: c["unclosed"] for c in data["clients"]},
                         {"acme": 1, "beta": 1})
        self.assertEqual(data["unclosed"], 2)
        r = self.ok("--client", "acme", "--week", "this", "--project", "p2")
        self.assertNotIn("never closed", r.stdout)

    def test_unknown_client_events_and_manual_entries_warn(self):
        evs = (turn(DAY, "09:00", "09:30", "a1", "p1")
               + turn(DAY, "10:00", "10:30", "o1", "p1", "oldname"))
        self.setup(two_clients(), evs)
        with open(os.path.join(self.home, "manual.jsonl"), "w") as f:
            f.write(json.dumps({"date": DAY, "client": "gone", "hours": 1}) + "\n")
        want = ['2 event(s) logged for unknown client "oldname" (renamed or removed from config?)',
                '1 manual entry for unknown client "gone"']
        for args in (["--client", "acme"], ["--md"], ["--csv"]):
            r = self.ok(*args)
            for w in want:
                self.assertIn("warning: " + w, r.stderr, args)
        data = json.loads(self.ok("--client", "acme", "--json").stdout)
        for w in want:
            self.assertIn(w, data["warnings"])


class ReattributeTests(CliCase):
    def park(self, *records):
        with open(os.path.join(self.home, "unattributed.jsonl"), "w") as f:
            for r in records:
                f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")

    def parked(self, ts, kind, session, cwd, account="/no/account"):
        return {"ts": ts, "event": kind, "hook": "x", "session": session, "cwd": cwd,
                "transcript_path": "", "account": account}

    def setUp(self):
        super().setUp()
        self.acme_dir = os.path.join(self.home, "acme-code")
        self.beta_dir = os.path.join(self.home, "beta-code")
        os.makedirs(os.path.join(self.acme_dir, "project-a"))
        os.makedirs(os.path.join(self.beta_dir, "project-b"))
        cfg = two_clients()
        cfg["clients"]["acme"]["scope"] = {"directories": [self.acme_dir]}
        cfg["clients"]["beta"]["scope"] = {"directories": [self.beta_dir]}
        self.cfg = cfg

    def test_reattribute_moves_matched_events_into_the_log(self):
        existing = event("2026-11-10T09:00:00Z", "start", "s1", "project-a",
                         cwd=os.path.join(self.acme_dir, "project-a"))
        self.setup(self.cfg, [existing])
        a = os.path.join(self.acme_dir, "project-a")
        b = os.path.join(self.beta_dir, "project-b")
        self.park(self.parked("2026-11-10T09:00:00Z", "start", "s1", a),       # duplicate
                  self.parked("2026-11-10T09:10:00Z", "end", "s1", a),
                  self.parked("2026-11-10T10:00:00Z", "start", "s2", b),
                  self.parked("2026-11-10T11:00:00Z", "start", "s3", "/elsewhere"),
                  "not json")
        r = self.run_cli("reattribute", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(),
                         "dry run: read 5, attributed 2, duplicates 1, unmatched 2")
        self.assertTrue(os.path.exists(os.path.join(self.home, "unattributed.jsonl")))
        with open(os.path.join(self.home, "events.jsonl")) as f:
            self.assertEqual(len(f.read().splitlines()), 1)
        r = self.run_cli("reattribute")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "read 5, attributed 2, duplicates 1, unmatched 2")
        self.assertFalse(os.path.exists(os.path.join(self.home, "unattributed.jsonl")))
        with open(os.path.join(self.home, "events.jsonl")) as f:
            logged = [json.loads(l) for l in f if l.strip()]
        self.assertEqual([(e["session"], e["event"], e["client"], e["project"]) for e in logged],
                         [("s1", "start", "acme", "project-a"), ("s1", "end", "acme", "project-a"),
                          ("s2", "start", "beta", "project-b")])
        self.assertEqual(list(logged[1]), ["ts", "event", "hook", "session", "client", "project",
                                           "cwd", "transcript_path"])
        r = self.run_cli("reattribute")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "read 0, attributed 0, duplicates 0, unmatched 0")

    def test_reattribute_needs_a_valid_config(self):
        self.setup({"timezone": "Not/AZone", "clients": {}})
        self.park(self.parked("2026-11-10T09:00:00Z", "start", "s1", "/x"))
        r = self.run_cli("reattribute")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.home, "unattributed.jsonl")))

    def test_report_compute_and_doctor_warn_about_parked_events(self):
        self.setup(self.cfg, turn(DAY, "09:00", "09:30", "a1", "p1"))
        self.park(self.parked("2026-11-10T09:00:00Z", "start", "s1", "/x"),
                  self.parked("2026-11-10T09:05:00Z", "end", "s1", "/x"))
        want = ('2 event(s) were logged while the config was unreadable; run '
                '"timesheet reattribute"')
        self.assertIn("warning: " + want, self.ok("--client", "acme").stderr)
        self.assertIn(want, json.loads(self.ok("--client", "acme", "--json").stdout)["warnings"])
        r = self.run_cli("doctor")
        self.assertIn("WARN  unattributed events: " + want, r.stdout)
        inv = subprocess.run([PY, os.path.join(helpers.SCRIPTS, "invoice.py"), "compute",
                              "--now", NOW, "--client", "acme", "--period", "this"],
                             capture_output=True, text=True, timeout=30,
                             env=dict(os.environ, TIMESHEET_HOME=self.home))
        self.assertEqual(inv.returncode, 0, inv.stderr)
        self.assertIn(want, json.loads(inv.stdout)["warnings"])


class ManualExcludedTests(CliCase):
    def manual(self, *entries):
        with open(os.path.join(self.home, "manual.jsonl"), "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_report_notes_manual_entries_it_excludes(self):
        cfg = two_clients()
        cfg["clients"]["acme"]["projects"] = {"p2": {"hourly_rate": 175}}
        self.setup(cfg, turn(DAY, "09:00", "10:00", "s1", "p1")
                   + turn(DAY, "11:00", "12:00", "s2", "p9", "beta"))
        self.manual({"date": DAY, "client": "acme", "project": "p1", "hours": 1.5,
                     "description": "Call"},
                    {"date": "2026-11-11", "client": "acme", "project": "p2", "hours": 0.25},
                    {"date": "2026-11-02", "client": "acme", "hours": 3},      # last week
                    {"date": DAY, "client": "beta", "hours": 2})
        out = self.ok("--client", "acme", "--week", "this").stdout
        # 1.5 h x 150 + 0.25 h x 175 = 225.00 + 43.75
        self.assertIn("Excludes 2 manual entries totalling 1.75 h ($268.75) in this range; "
                      "the invoice includes them.", out)
        self.assertLess(out.index("TOTAL"), out.index("Excludes"))
        data = json.loads(self.ok("--client", "acme", "--week", "this", "--json").stdout)
        self.assertEqual(data["manual_excluded"],
                         {"entries": 2, "hours": "1.75", "amount": "268.75"})
        beta = self.ok("--client", "beta", "--week", "this").stdout
        self.assertIn("Excludes 1 manual entry totalling 2.00 h ($400.00) in this range", beta)
        self.assertNotIn("268.75", beta)
        both = self.ok("--week", "this").stdout
        self.assertEqual(both.count("Excludes"), 2)
        md = self.ok("--client", "acme", "--week", "this", "--md").stdout
        self.assertIn("Excludes 2 manual entries", md)

    def test_no_note_without_manual_entries_in_range(self):
        self.setup(two_clients(), turn(DAY, "09:00", "10:00", "s1", "p1"))
        self.manual({"date": "2026-10-01", "client": "acme", "hours": 1})
        out = self.ok("--client", "acme", "--week", "this").stdout
        self.assertNotIn("Excludes", out)
        data = json.loads(self.ok("--client", "acme", "--week", "this", "--json").stdout)
        self.assertEqual(data["manual_excluded"], {"entries": 0, "hours": "0.00", "amount": "0.00"})

    def test_client_with_only_manual_entries_still_gets_a_note(self):
        self.setup(two_clients(), turn(DAY, "09:00", "10:00", "s1", "p1"))
        self.manual({"date": DAY, "client": "beta", "hours": 1})
        out = self.ok("--week", "this").stdout
        self.assertIn("Client: Beta", out)
        self.assertIn("Excludes 1 manual entry totalling 1.00 h ($200.00)", out)


class NonBillableTests(CliCase):
    def sessions(self):
        evs = (turn(DAY, "09:00", "10:00", "sess-aaa-1", "p1", cwd=self.home)
               + turn(DAY, "11:00", "11:30", "sess-bbb-2", "p2", cwd=self.home)
               + turn(DAY, "11:00", "11:30", "sess-ccc-3", "p3", cwd="/elsewhere"))
        self.setup(two_clients(), evs)

    def listed(self):
        return self.config()["clients"]["acme"].get("non_billable_sessions", [])

    def test_nonbillable_current_session_by_cwd(self):
        self.sessions()
        r = self.run_cli("nonbillable", "--cwd", self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.listed(), ["sess-bbb-2"])     # the latest start in that cwd
        self.assertIn("sess-bbb-2", r.stdout)
        self.assertIn("p2", r.stdout)
        self.assertIn("2026-11-10 11:00", r.stdout)
        self.assertIn("2026-11-10 11:30", r.stdout)
        self.assertIn("now non-billable", r.stdout)
        # defaults were not written back into the user's file
        raw = self.config()
        self.assertEqual(raw["defaults"], {"line_items": "day"})   # only what the fixture set
        self.assertNotIn("pdf", raw)
        r = self.run_cli("nonbillable", "--cwd", self.home)
        self.assertIn("already non-billable", r.stdout)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.listed(), ["sess-bbb-2"])

    def test_nonbillable_env_session_wins_when_logged(self):
        self.sessions()
        r = self.run_cli("nonbillable", "--cwd", self.home, CLAUDE_CODE_SESSION_ID="sess-aaa-1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.listed(), ["sess-aaa-1"])
        # an env var naming an unlogged session never falls back to the cwd lookup
        r = self.run_cli("nonbillable", "--cwd", self.home,
                         CLAUDE_CODE_SESSION_ID="zzz-unlogged-session")
        self.assertEqual(r.returncode, 2)
        self.assertIn("this session (zzz-unl", r.stderr)
        self.assertIn("has no tracked events (it is outside every client's scope)", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.listed(), ["sess-aaa-1"])

    def test_nonbillable_named_session_and_remove(self):
        self.sessions()
        r = self.run_cli("nonbillable", "sess-aaa")      # unique prefix
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.listed(), ["sess-aaa-1"])  # full id stored
        r = self.run_cli("nonbillable", "sess-aaa-1", "--remove")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("billable again", r.stdout)
        self.assertEqual(self.listed(), [])
        r = self.run_cli("nonbillable", "sess-", "--client", "acme")
        self.assertEqual(r.returncode, 2)
        for sid in ("sess-aaa-1", "sess-bbb-2", "sess-ccc-3"):
            self.assertIn(sid, r.stderr)
        r = self.run_cli("nonbillable", "nope")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)
        r = self.run_cli("nonbillable", "--cwd", "/no/such/place")
        self.assertEqual(r.returncode, 2)

    def test_nonbillable_session_spanning_clients_requires_client(self):
        evs = (turn(DAY, "09:00", "09:10", "shared", "p1", "acme")
               + turn(DAY, "10:00", "10:10", "shared", "p9", "beta"))
        self.setup(two_clients(), evs)
        r = self.run_cli("nonbillable", "shared")
        self.assertEqual(r.returncode, 2)
        self.assertIn("acme, beta", r.stderr)
        r = self.run_cli("nonbillable", "shared", "--client", "beta")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.config()["clients"]["beta"]["non_billable_sessions"], ["shared"])
        self.assertEqual(self.run_cli("nonbillable", "shared", "--client", "nobody").returncode, 2)

    def test_nonbillable_excludes_time_from_next_report(self):
        self.setup(two_clients(), RUBRIC)
        before = json.loads(self.ok("--client", "acme", "--week", "this", "--json").stdout)
        self.assertEqual(len(before["rows"]), 2)
        r = self.run_cli("nonbillable", "s4")
        self.assertEqual(r.returncode, 0, r.stderr)
        after = json.loads(self.ok("--client", "acme", "--week", "this", "--json").stdout)
        by = {x["project"]: x for x in after["rows"]}
        self.assertEqual(by["p2"]["agent_seconds"], 600)
        self.assertLess(sum(x["billable_seconds"] for x in after["rows"]),
                        sum(x["billable_seconds"] for x in before["rows"]))
        self.run_cli("nonbillable", "s4", "--remove")
        again = json.loads(self.ok("--client", "acme", "--week", "this", "--json").stdout)
        self.assertEqual(again["rows"], before["rows"])

    def test_nonbillable_appends_to_an_existing_list(self):
        self.sessions()
        cfg = two_clients()
        cfg["clients"]["acme"]["non_billable_sessions"] = ["x"]
        helpers.write_config(self.home, cfg)
        r = self.run_cli("nonbillable", "sess-aaa-1", "--client", "acme")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.listed(), ["x", "sess-aaa-1"])

    def test_nonbillable_summary_uses_only_the_chosen_clients_events(self):
        evs = (turn(DAY, "09:00", "09:10", "shared", "p1", "acme")
               + turn(DAY, "10:00", "10:20", "shared", "p9", "beta"))
        self.setup(two_clients(), evs)
        r = self.run_cli("nonbillable", "shared", "--client", "acme")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Project: p1\n", r.stdout)
        self.assertNotIn("p9", r.stdout)
        self.assertIn("First event: 2026-11-10 09:00", r.stdout)
        self.assertIn("Last event: 2026-11-10 09:10", r.stdout)
        self.assertNotIn("10:", r.stdout)

    def test_nonbillable_remove_clears_override_and_reports_remaining_prefix(self):
        self.sessions()
        cfg = two_clients()
        cfg["defaults"] = {"non_billable_sessions": ["sess-aa"]}
        cfg["clients"]["acme"]["non_billable_sessions"] = ["sess-aaa-1"]
        cfg["clients"]["acme"]["overrides"]["non_billable_sessions"] = ["sess-aaa-1", "keep"]
        helpers.write_config(self.home, cfg)
        r = self.run_cli("nonbillable", "sess-aaa-1", "--remove")
        self.assertEqual(r.returncode, 0, r.stderr)
        raw = self.config()["clients"]["acme"]
        self.assertEqual(raw["non_billable_sessions"], [])
        self.assertEqual(raw["overrides"]["non_billable_sessions"], ["keep"])
        self.assertIn('still non-billable: matched by "sess-aa"', r.stdout)
        # a session only covered by a prefix elsewhere: nothing to remove, still reported
        r = self.run_cli("nonbillable", "sess-aaa-1", "--remove")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('still non-billable: matched by "sess-aa"', r.stdout)
        # adding it again is a no-op because the prefix already covers it
        r = self.run_cli("nonbillable", "sess-aaa-1")
        self.assertIn("already non-billable", r.stdout)
        self.assertEqual(self.listed(), [])

    def test_nonbillable_remove_of_unlisted_session_is_already_billable(self):
        self.sessions()
        r = self.run_cli("nonbillable", "sess-aaa-1", "--remove")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already billable", r.stdout)
        self.assertNotIn("non_billable_sessions", json.dumps(self.config()))

    def test_nonbillable_preserves_the_users_config_file(self):
        evs = turn(DAY, "09:00", "10:00", "sess-aaa-1", "p1")
        cfg = {"you": {"business": "Caf\u00e9 \u00dcn\u00efcode GmbH"},
               "timezone": "UTC", "notes": "keep me",
               "clients": {"beta": {"name": "Beta"}, "acme": {"name": "Acme Co"}}}
        self.setup(cfg, evs)
        r = self.run_cli("nonbillable", "sess-aaa-1")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.home, "config.json"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Caf\u00e9 \u00dcn\u00efcode GmbH", text)
        raw = json.loads(text)
        self.assertEqual(raw["notes"], "keep me")
        self.assertEqual(list(raw), ["you", "timezone", "notes", "clients"])
        self.assertEqual(list(raw["clients"]), ["beta", "acme"])
        self.assertEqual(raw["clients"]["beta"], {"name": "Beta"})
        self.assertEqual(raw["clients"]["acme"],
                         {"name": "Acme Co", "non_billable_sessions": ["sess-aaa-1"]})
        for filled in ("defaults", "pdf", "mercury"):
            self.assertNotIn(filled, raw)

    def test_nonbillable_bad_config_exits_2(self):
        self.setup({"timezone": "Not/AZone"})
        r = self.run_cli("nonbillable", "abc")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)


class RestoreTests(unittest.TestCase):
    def test_invalid_result_restores_the_original_file(self):
        import timesheet
        from ats import config
        with helpers.temp_home() as home:
            path = os.path.join(home, "config.json")
            original = '{"clients": {"acme": {"name": "Caf\u00e9"}}}\n'
            with open(path, "w", encoding="utf-8") as f:
                f.write(original)
            raw = json.loads(original)
            raw["clients"]["acme"]["non_billable_sessions"] = ["x"]
            with mock.patch.object(timesheet.config, "load",
                                   side_effect=config.ConfigError("boom")):
                with self.assertRaises(timesheet.UsageError) as cm:
                    timesheet._save_validated(raw, path, original)
            self.assertIn("boom", str(cm.exception))
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), original)


class UsageErrorTests(CliCase):
    def test_unexpected_value_errors_are_not_reported_as_usage_errors(self):
        import timesheet
        with mock.patch.object(timesheet, "cmd_report", side_effect=ValueError("internal")):
            with self.assertRaises(ValueError):
                timesheet.main(["report"])

    def test_bad_dates_periods_and_now_exit_2_with_a_message(self):
        self.setup(two_clients())
        for args in (["--client", "acme", "--period", "garbage"],
                     ["--week", "2026-W99"], ["--week", "nope"],
                     ["--since", "2026-13-40"], ["--until", "x"],
                     ["--since", "2026-11-12", "--until", "2026-11-01"]):
            r = self.report(*args)
            self.assertEqual(r.returncode, 2, args)
            self.assertTrue(r.stderr.startswith("error: "), (args, r.stderr))
            self.assertNotIn("Traceback", r.stderr)
        r = self.run_cli("report", "--now", "bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--now", r.stderr)


if __name__ == "__main__":
    unittest.main()
