"""Invoicing status: the summary, the one-line form, the status command, the status
line script, the session-start reminder and the status line installer."""
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone

from tests import helpers
from tests.test_timesheet_cli import turn, two_clients

from ats import config, drafts, periods, status

PY = "/usr/bin/python3"
TIMESHEET = os.path.join(helpers.SCRIPTS, "timesheet.py")
INVOICE = os.path.join(helpers.SCRIPTS, "invoice.py")
STATUSLINE = os.path.join(helpers.SCRIPTS, "statusline.py")
REMIND = os.path.join(helpers.SCRIPTS, "remind.py")

NOW = "2026-11-12T12:00:00Z"      # Thursday: acme's week is Nov 9-15, beta's month November
NOW_DT = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
WEEK = "2026-11-02_2026-11-08"    # acme's last complete period
OLDER = "2026-10-26_2026-11-01"
THIS_WEEK = "2026-11-09_2026-11-15"
OCTOBER = "2026-10-01_2026-10-31"

EVENTS = (turn("2026-11-03", "09:00", "09:40", "s1", "project-a")
          + turn("2026-11-03", "13:00", "13:50", "s2", "project-b", client="beta")
          + turn("2026-11-10", "09:00", "09:20", "s3", "project-a")
          + turn("2026-10-20", "14:00", "15:00", "s5", "project-b", client="beta"))
OLDER_TURN = turn("2026-10-27", "10:00", "11:00", "s6", "project-a")


def sample(due=1):
    """A summary dict with the figures of the documented examples."""
    rows = [{"period_id": "2026-11-09_2026-11-15", "label": "Nov 9 – Nov 15, 2026",
             "short_label": "Nov 9 – Nov 15", "hours": "12.40", "amount": "1860.00",
             "state": "not drafted"},
            {"period_id": "2026-11-16_2026-11-22", "label": "Nov 16 – Nov 22, 2026",
             "short_label": "Nov 16 – Nov 22", "hours": "7.75", "amount": "1162.50",
             "state": "stale"}]
    return {"client": {"key": "acme", "name": "Acme Co"},
            "current": {"period_id": "2026-11-23_2026-11-29", "label": "Nov 23 – Nov 29, 2026",
                        "short_label": "Nov 23 – Nov 29", "end": "2026-11-29",
                        "hours": "5.06", "amount": "759.00", "currency": "USD"},
            "due": rows[:due], "last": "not drafted" if due else "nothing to bill",
            "last_period": {"period_id": "2026-11-16_2026-11-22",
                            "label": "Nov 16 – Nov 22, 2026"}}


class Case(unittest.TestCase):
    """A temp data dir with the two clients, each scoped to its own folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = os.path.realpath(self._tmp.name)
        self.home = os.path.join(root, "data")
        self.account = os.path.join(root, "account")
        self.dirs = {k: os.path.join(root, "work", k) for k in ("acme", "beta", "elsewhere")}
        for d in [self.home, self.account] + list(self.dirs.values()):
            os.makedirs(d)
        old = os.environ.get("TIMESHEET_HOME")
        os.environ["TIMESHEET_HOME"] = self.home
        self.addCleanup(lambda: os.environ.pop("TIMESHEET_HOME", None) if old is None
                        else os.environ.__setitem__("TIMESHEET_HOME", old))

    def cfg(self, **status_block):
        cfg = two_clients()
        for k in ("acme", "beta"):
            cfg["clients"][k]["scope"] = {"directories": [self.dirs[k]]}
        cfg["pdf"] = {"output_dir": os.path.join(self.home, "invoices")}
        if status_block:
            cfg["status"] = status_block
        return cfg

    def setup(self, cfg=None, events=EVENTS):
        helpers.write_config(self.home, cfg if cfg is not None else self.cfg())
        self.write_events(events)

    def write_events(self, events):
        with open(os.path.join(self.home, "events.jsonl"), "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def env(self, **extra):
        env = dict(os.environ)
        env["TIMESHEET_HOME"] = self.home
        env["CLAUDE_CONFIG_DIR"] = self.account
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.update(extra)
        return env

    def run_script(self, script, *args, stdin=None, **env):
        return subprocess.run([PY, script] + list(args), capture_output=True, text=True,
                              encoding="utf-8", env=self.env(**env), input=stdin, timeout=60)

    def timesheet(self, *args, **env):
        return self.run_script(TIMESHEET, *args, **env)

    def compute(self, client, period):
        r = self.run_script(INVOICE, "compute", "--now", NOW, "--client", client,
                            "--period", period)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def approve(self, client, period):
        r = self.run_script(INVOICE, "save", "--now", NOW, "--client", client, "--period",
                            period, "--input", "-", stdin='{"summaries": {}, "descriptions": {}}')
        self.assertEqual(r.returncode, 0, r.stderr)

    def draft_dir(self, client, period_id):
        return os.path.join(self.home, "invoices", client, period_id)

    def summary(self, client):
        return status.summary(config.load(), client, NOW_DT)

    def tree(self):
        out = {}
        for root, _, files in os.walk(self.home):
            for f in files:
                p = os.path.join(root, f)
                with open(p, "rb") as fh:
                    out[p] = fh.read()
        return out


# ------------------------------------------------------------------ config

class ConfigTests(Case):
    def load(self, **block):
        helpers.write_config(self.home, self.cfg(**block))
        return config.load()

    def test_defaults(self):
        helpers.write_config(self.home, self.cfg())
        self.assertEqual(config.load()["status"], {
            "components": ["invoice_due", "current_hours"], "show_amounts": False,
            "reminders": "off"})

    def test_partial_block_is_filled(self):
        cfg = self.load(components=["client"])
        self.assertEqual(cfg["status"], {"components": ["client"], "show_amounts": False,
                                         "reminders": "off"})
        self.assertEqual(self.load(components=[])["status"]["components"], [])
        for mode in ("off", "session_start", "daily"):
            self.assertEqual(self.load(reminders=mode)["status"]["reminders"], mode)

    def test_invalid_values_name_the_key(self):
        bad = [({"components": "client"}, "status.components"),
               ({"components": ["client", "rate"]}, "status.components"),
               ({"components": ["client", "client"]}, "status.components"),
               ({"components": [1]}, "status.components"),
               ({"show_amounts": "yes"}, "status.show_amounts"),
               ({"reminders": "weekly"}, "status.reminders"),
               ({"reminders": True}, "status.reminders")]
        for block, key in bad:
            with self.assertRaises(config.ConfigError) as cm:
                self.load(**block)
            self.assertIn(key, str(cm.exception), block)
        cfg = self.cfg()
        cfg["status"] = []
        helpers.write_config(self.home, cfg)
        with self.assertRaises(config.ConfigError) as cm:
            config.load()
        self.assertIn("status", str(cm.exception))

    def test_status_is_top_level_only(self):
        for where in ("defaults", "overrides", "projects"):
            cfg = self.cfg()
            if where == "defaults":
                cfg["defaults"]["status"] = {"show_amounts": True}
            elif where == "overrides":
                cfg["clients"]["acme"]["overrides"]["status"] = {"show_amounts": True}
            else:
                cfg["clients"]["acme"]["projects"] = {"project-a": {"status": {}}}
            helpers.write_config(self.home, cfg)
            with self.assertRaises(config.ConfigError) as cm:
                config.load()
            self.assertIn("status", str(cm.exception), where)

    def test_example_config_has_the_default_status_block(self):
        with open(os.path.join(helpers.REPO, "config.example.json"), encoding="utf-8") as f:
            example = json.load(f)
        self.assertEqual(example["status"], {
            "components": ["invoice_due", "current_hours"], "show_amounts": False,
            "reminders": "off"})


# ------------------------------------------------------------------ summary

class SummaryTests(Case):
    def test_numbers_equal_the_invoice_draft(self):
        self.setup()
        for client, last in (("acme", WEEK), ("beta", OCTOBER)):
            s = self.summary(client)
            cur = self.compute(client, "this")
            self.assertEqual(s["current"]["period_id"], cur["period"]["id"])
            self.assertEqual(s["current"]["label"], cur["period"]["label"])
            self.assertEqual(s["current"]["end"], cur["period"]["end"])
            self.assertEqual(s["current"]["hours"], cur["total_hours"])
            self.assertEqual(s["current"]["amount"], cur["total"])
            self.assertEqual(s["current"]["currency"], "USD")
            prev = self.compute(client, "last")
            self.assertEqual(prev["period"]["id"], last)
            self.assertNotEqual(prev["total"], "0.00")
            self.assertEqual(s["due"], [{
                "period_id": last, "label": prev["period"]["label"],
                "short_label": s["due"][0]["short_label"], "hours": prev["total_hours"],
                "amount": prev["total"], "state": "not drafted"}])
            self.assertEqual(s["last"], "not drafted")
            self.assertEqual(s["last_period"], {"period_id": last,
                                                "label": prev["period"]["label"]})
            self.assertEqual(s["client"]["key"], client)
        self.assertEqual(self.summary("acme")["client"], {"key": "acme", "name": "Acme Co"})
        self.assertEqual(self.summary("acme")["due"][0]["short_label"], "Nov 2 – Nov 8")
        self.assertEqual(self.summary("acme")["current"]["short_label"], "Nov 9 – Nov 15")
        self.assertEqual(self.summary("beta")["due"][0]["short_label"], "Oct 1 – Oct 31")

    def test_summary_writes_nothing(self):
        self.setup()
        before = self.tree()
        self.summary("acme")
        self.summary("beta")
        self.assertEqual(self.tree(), before)

    def test_client_without_events(self):
        self.setup(events=())
        os.unlink(os.path.join(self.home, "events.jsonl"))
        s = self.summary("acme")
        self.assertEqual(s["due"], [])
        self.assertEqual(s["last"], "nothing to bill")
        self.assertEqual(s["current"]["hours"], "0.00")
        self.assertEqual(s["current"]["amount"], "0.00")
        self.assertEqual(status.line(s, ["invoice_due"], True), "")

    def test_current_period_is_never_due_and_empty_periods_are_skipped(self):
        self.setup(events=EVENTS + OLDER_TURN)
        s = self.summary("acme")
        self.assertNotEqual(s["current"]["hours"], "0.00")
        ids = [d["period_id"] for d in s["due"]]
        self.assertEqual(ids, [OLDER, WEEK])           # oldest first; no empty week between
        self.assertNotIn(THIS_WEEK, ids)

    def test_only_the_previous_six_periods_are_looked_at(self):
        self.setup(events=EVENTS + turn("2026-09-22", "10:00", "11:00", "s7", "project-a")
                   + turn("2026-09-29", "10:00", "11:00", "s8", "project-a"))
        ids = [d["period_id"] for d in self.summary("acme")["due"]]
        self.assertEqual(ids, ["2026-09-28_2026-10-04", WEEK])

    def test_approved_is_not_due_and_stale_is_due_again(self):
        self.setup()
        self.approve("acme", "last")
        s = self.summary("acme")
        self.assertEqual(s["due"], [])
        self.assertEqual(s["last"], "approved")
        self.assertEqual(self.summary("beta")["last"], "not drafted")   # beta is untouched
        self.write_events(EVENTS + turn("2026-11-04", "09:00", "09:30", "s9", "project-a"))
        s = self.summary("acme")
        self.assertEqual([(d["period_id"], d["state"]) for d in s["due"]], [(WEEK, "stale")])
        self.assertEqual(s["last"], "stale")
        self.assertEqual(s["due"][0]["amount"], self.compute("acme", "last")["total"])

    def test_rendered_state(self):
        self.setup()
        self.approve("acme", "last")
        cfg = config.load()
        st = config.settings(cfg, "acme")
        period = periods.resolve("last", st["billing_period"], NOW_DT.date())
        payload = drafts.compute(cfg, "acme", period, NOW_DT)
        drafts.write_render_record(cfg, "acme", period, payload, False, NOW_DT)
        s = self.summary("acme")
        self.assertEqual(s["last"], "rendered")
        self.assertEqual(s["due"], [])

    def test_mercury_record_is_not_due(self):
        self.setup()
        os.makedirs(self.draft_dir("acme", WEEK))
        with open(os.path.join(self.draft_dir("acme", WEEK), "mercury.json"), "w") as f:
            json.dump({"status": "pending"}, f)
        s = self.summary("acme")
        self.assertEqual(s["due"], [])
        self.assertEqual(s["last"], "in Mercury")
        self.assertEqual(len(self.summary("beta")["due"]), 1)

    def test_zero_total_last_period(self):
        self.setup(events=turn("2026-11-10", "09:00", "09:20", "s3", "project-a"))
        s = self.summary("acme")
        self.assertEqual(s["due"], [])
        self.assertEqual(s["last"], "nothing to bill")

    def test_detect_client(self):
        self.setup()
        cfg = config.load()
        self.assertEqual(status.detect_client(cfg, self.dirs["acme"], self.account), "acme")
        sub = os.path.join(self.dirs["beta"], "project-b")
        os.makedirs(sub)
        self.assertEqual(status.detect_client(cfg, sub, self.account), "beta")
        self.assertIsNone(status.detect_client(cfg, self.dirs["elsewhere"], self.account))
        self.assertIsNone(status.detect_client(cfg, "", self.account))


# ------------------------------------------------------------------ line

class LineTests(unittest.TestCase):
    DUE = "Invoice due: Nov 9 – Nov 15 (12.40 h)"
    DUE_AMT = "Invoice due: Nov 9 – Nov 15 (12.40 h, $1,860.00)"
    CUR = "Current hours: 5.06 h"
    CUR_AMT = "Current hours: 5.06 h ($759.00)"

    def test_each_component(self):
        s = sample()
        self.assertEqual(status.line(s, ["client"], False), "Acme Co")
        self.assertEqual(status.line(s, ["client"], True), "Acme Co")
        self.assertEqual(status.line(s, ["invoice_due"], False), self.DUE)
        self.assertEqual(status.line(s, ["invoice_due"], True), self.DUE_AMT)
        self.assertEqual(status.line(s, ["current_hours"], False), self.CUR)
        self.assertEqual(status.line(s, ["current_hours"], True), self.CUR_AMT)
        self.assertEqual(status.line(s, [], False), "")
        self.assertEqual(status.line(s, [], True), "")

    def test_every_combination_keeps_the_configured_order(self):
        s = sample()
        text = {False: {"client": "Acme Co", "invoice_due": self.DUE, "current_hours": self.CUR},
                True: {"client": "Acme Co", "invoice_due": self.DUE_AMT,
                       "current_hours": self.CUR_AMT}}
        names = ["client", "invoice_due", "current_hours"]
        combos = [[]]
        for _ in names:
            combos += [c + [n] for c in combos for n in names if n not in c]
        seen = set()
        for combo in combos:
            if tuple(combo) in seen:
                continue
            seen.add(tuple(combo))
            for amounts in (False, True):
                self.assertEqual(status.line(s, combo, amounts),
                                 " • ".join(text[amounts][n] for n in combo), combo)
        self.assertEqual(len(seen), 16)

    def test_no_money_when_amounts_are_off(self):
        for due in (0, 1, 2):
            out = status.line(sample(due), ["client", "invoice_due", "current_hours"], False)
            for symbol in ("$", "€", "£", "USD"):
                self.assertNotIn(symbol, out)
            for amount in ("1,860", "1860", "1,162", "1162", "3,022", "3022", "759"):
                self.assertNotIn(amount, out)
            numbers = re.findall(r"\d+\.\d+", out)
            self.assertEqual(numbers, [["5.06"], ["12.40", "5.06"], ["20.15", "5.06"]][due])

    def test_several_due_periods(self):
        s = sample(2)
        self.assertEqual(status.line(s, ["invoice_due"], False), "Invoice due: 2 periods (20.15 h)")
        self.assertEqual(status.line(s, ["invoice_due"], True),
                         "Invoice due: 2 periods (20.15 h, $3,022.50)")

    def test_nothing_due_omits_the_component(self):
        s = sample(0)
        self.assertEqual(status.line(s, ["invoice_due"], True), "")
        self.assertEqual(status.line(s, ["client", "invoice_due", "current_hours"], False),
                         "Acme Co • " + self.CUR)
        self.assertEqual(status.line(s, ["invoice_due", "current_hours"], False), self.CUR)

    def test_currency_comes_from_the_summary(self):
        s = sample()
        s["current"]["currency"] = "EUR"
        self.assertEqual(status.line(s, ["current_hours"], True), "Current hours: 5.06 h (€759.00)")
        s["current"]["currency"] = "CHF"
        self.assertEqual(status.line(s, ["invoice_due"], True),
                         "Invoice due: Nov 9 – Nov 15 (12.40 h, 1,860.00 CHF)")


# ------------------------------------------------------------------ status command

class StatusCliTests(Case):
    def figures(self):
        """Per client: every hours figure and money string of its own output."""
        out = {}
        for client, name in (("acme", "Acme Co"), ("beta", "Beta")):
            cur, last = self.compute(client, "this"), self.compute(client, "last")
            out[client] = {"name": name, "cur": cur, "last": last,
                           "money": ["$%s" % format(float(d["total"]), ",.2f")
                                     for d in (cur, last)],
                           "hours": [d["total_hours"] + " h" for d in (cur, last)]}
        return out

    def assert_only(self, text, mine, other):
        for needle in [other["name"]] + other["money"] + other["hours"]:
            self.assertNotIn(needle, mine["money"] + mine["hours"] + [mine["name"]])
            self.assertNotIn(needle, text)
        self.assertNotIn("200", text if mine["name"] == "Acme Co" else "")

    def test_human_output_for_a_named_client(self):
        self.setup()
        f = self.figures()
        r = self.timesheet("status", "--now", NOW, "--client", "acme",
                           "--cwd", self.dirs["beta"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        a = f["acme"]
        self.assertEqual(r.stdout.splitlines(), [
            "Acme Co",
            "Current period: Nov 9 – Nov 15, 2026 (ends Nov 15): %s h" % a["cur"]["total_hours"],
            "Last period: Nov 2 – Nov 8, 2026: not drafted",
            "Invoice due: Nov 2 – Nov 8, 2026: %s h (not drafted)" % a["last"]["total_hours"]])
        self.assertNotIn("$", r.stdout)
        self.assert_only(r.stdout, f["acme"], f["beta"])

    def test_human_output_with_amounts(self):
        self.setup(self.cfg(show_amounts=True))
        f = self.figures()
        r = self.timesheet("status", "--now", NOW, "--client", "beta")
        self.assertEqual(r.returncode, 0, r.stderr)
        b = f["beta"]
        self.assertEqual(r.stdout.splitlines(), [
            "Beta",
            "Current period: Nov 1 – Nov 30, 2026 (ends Nov 30): %s h (%s)"
            % (b["cur"]["total_hours"], b["money"][0]),
            "Last period: Oct 1 – Oct 31, 2026: not drafted",
            "Invoice due: Oct 1 – Oct 31, 2026: %s h, %s (not drafted)"
            % (b["last"]["total_hours"], b["money"][1])])
        self.assert_only(r.stdout, f["beta"], f["acme"])
        self.assertNotIn("150", r.stdout)

    def test_client_is_detected_from_cwd(self):
        self.setup(self.cfg(show_amounts=True, components=["client", "invoice_due",
                                                           "current_hours"]))
        f = self.figures()
        for mine, other in (("acme", "beta"), ("beta", "acme")):
            for args in (["--cwd", self.dirs[mine]], []):
                r = subprocess.run(
                    [PY, TIMESHEET, "status", "--now", NOW] + args, capture_output=True,
                    text=True, encoding="utf-8", env=self.env(), timeout=60,
                    cwd=self.dirs[mine])
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.splitlines()[0], f[mine]["name"])
                self.assert_only(r.stdout, f[mine], f[other])
            r = self.timesheet("status", "--now", NOW, "--cwd", self.dirs[mine], "--line")
            self.assertEqual(r.stdout, "%s • Invoice due: %s (%s, %s) • Current hours: %s (%s)\n" % (
                f[mine]["name"], "Nov 2 – Nov 8" if mine == "acme" else "Oct 1 – Oct 31",
                f[mine]["hours"][1], f[mine]["money"][1], f[mine]["hours"][0],
                f[mine]["money"][0]))
            self.assert_only(r.stdout, f[mine], f[other])

    def test_no_match_with_several_clients_prints_a_block_each(self):
        self.setup()
        f = self.figures()
        r = self.timesheet("status", "--now", NOW, "--cwd", self.dirs["elsewhere"])
        self.assertEqual(r.returncode, 0, r.stderr)
        blocks = r.stdout.strip().split("\n\n")
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].splitlines()[0], "Acme Co")
        self.assertEqual(blocks[1].splitlines()[0], "Beta")
        self.assert_only(blocks[0], f["acme"], f["beta"])
        self.assert_only(blocks[1], f["beta"], f["acme"])
        r = self.timesheet("status", "--now", NOW, "--cwd", self.dirs["elsewhere"], "--line")
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_no_match_with_one_client_uses_it(self):
        cfg = self.cfg()
        del cfg["clients"]["beta"]
        self.setup(cfg)
        r = self.timesheet("status", "--now", NOW, "--cwd", self.dirs["elsewhere"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], "Acme Co")

    def test_line_uses_the_configured_components(self):
        self.setup()
        f = self.figures()
        r = self.timesheet("status", "--now", NOW, "--client", "acme", "--line")
        self.assertEqual(r.stdout, "Invoice due: Nov 2 – Nov 8 (%s) • Current hours: %s\n"
                         % (f["acme"]["hours"][1], f["acme"]["hours"][0]))
        self.setup(self.cfg(components=["current_hours"]))
        r = self.timesheet("status", "--now", NOW, "--client", "acme", "--line")
        self.assertEqual(r.stdout, "Current hours: %s\n" % f["acme"]["hours"][0])
        self.setup(self.cfg(components=[]))
        r = self.timesheet("status", "--now", NOW, "--client", "acme", "--line")
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_json_always_has_amounts(self):
        self.setup()
        f = self.figures()
        r = self.timesheet("status", "--now", NOW, "--client", "acme", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc, json.loads(json.dumps(self.summary("acme"))))
        self.assertEqual(doc["current"]["amount"], f["acme"]["cur"]["total"])
        self.assertEqual(doc["due"][0]["amount"], f["acme"]["last"]["total"])
        self.assertNotIn("Beta", r.stdout)
        r = self.timesheet("status", "--now", NOW, "--cwd", self.dirs["elsewhere"], "--json")
        doc = json.loads(r.stdout)
        self.assertEqual([c["client"]["key"] for c in doc["clients"]], ["acme", "beta"])
        self.assertEqual(doc["clients"][1]["due"][0]["amount"], f["beta"]["last"]["total"])
        r = self.timesheet("status", "--help")
        self.assertIn("amounts", " ".join(r.stdout.split()))

    def test_errors(self):
        self.setup()
        r = self.timesheet("status", "--now", NOW, "--client", "nope")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown client", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        r = self.timesheet("status", "--now", "junk")
        self.assertEqual(r.returncode, 2)

    def test_status_writes_nothing(self):
        self.setup()
        before = self.tree()
        self.timesheet("status", "--now", NOW, "--cwd", self.dirs["elsewhere"])
        self.timesheet("status", "--now", NOW, "--client", "acme", "--line")
        self.assertEqual(self.tree(), before)


# ------------------------------------------------------------------ status line script

class StatuslineScriptTests(Case):
    def line(self, cwd, now=NOW, stdin=None, **env):
        if stdin is None:
            stdin = json.dumps({"workspace": {"current_dir": cwd}, "cwd": self.dirs["elsewhere"]})
        r = self.run_script(STATUSLINE, "--now", now, stdin=stdin, **env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")
        return r.stdout

    def expected(self, client):
        cfg = config.load()
        return status.line(self.summary(client), cfg["status"]["components"],
                           cfg["status"]["show_amounts"])

    def test_prints_the_line_of_the_matching_client_only(self):
        self.setup(self.cfg(show_amounts=True, components=["client", "invoice_due",
                                                           "current_hours"]))
        a, b = self.expected("acme"), self.expected("beta")
        self.assertTrue(a.startswith("Acme Co • Invoice due: Nov 2 – Nov 8 ("))
        self.assertEqual(self.line(self.dirs["acme"]), a + "\n")
        self.assertEqual(self.line(self.dirs["beta"]), b + "\n")
        self.assertNotIn("Beta", a)
        self.assertNotIn("Acme", b)

    def test_cwd_field_is_the_fallback(self):
        self.setup()
        out = self.line(None, stdin=json.dumps({"cwd": self.dirs["acme"]}))
        self.assertEqual(out, self.expected("acme") + "\n")

    def test_silent_when_nothing_matches_or_is_broken(self):
        self.setup()
        self.assertEqual(self.line(self.dirs["elsewhere"]), "")
        for garbage in ("", "not json", "[1, 2]", '{"workspace": 3}', '{"cwd": 7}'):
            self.assertEqual(self.line(None, stdin=garbage), "")
        with open(os.path.join(self.home, "config.json"), "w") as f:
            f.write("{broken")
        self.assertEqual(self.line(self.dirs["acme"]), "")
        self.setup()
        with open(os.path.join(self.home, "config.json")) as f:
            cfg = json.load(f)
        cfg["status"] = {"reminders": "sometimes"}
        helpers.write_config(self.home, cfg)
        self.assertEqual(self.line(self.dirs["acme"]), "")
        os.unlink(os.path.join(self.home, "config.json"))
        self.assertEqual(self.line(self.dirs["acme"]), "")
        r = self.run_script(STATUSLINE, "--now", "junk",
                            stdin=json.dumps({"cwd": self.dirs["acme"]}))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_missing_data_dir_is_not_created(self):
        gone = os.path.join(self.home, "nope")
        r = self.run_script(STATUSLINE, stdin=json.dumps({"cwd": self.dirs["acme"]}),
                            TIMESHEET_HOME=gone)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertFalse(os.path.exists(gone))

    def test_cache_is_reused_for_sixty_seconds(self):
        self.setup()
        first = self.line(self.dirs["acme"])
        path = os.path.join(self.home, "status-cache.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        # more time is logged; the cached line is still shown half a minute later
        self.write_events(EVENTS + turn("2026-11-11", "09:00", "10:00", "s9", "project-a"))
        self.assertEqual(self.line(self.dirs["acme"], now="2026-11-12T12:00:30Z"), first)
        fresh = self.line(self.dirs["acme"], now="2026-11-12T12:01:01Z")
        self.assertNotEqual(fresh, first)
        self.assertEqual(fresh, self.expected("acme") + "\n")
        self.assertEqual([n for n in os.listdir(self.home) if n.endswith(".tmp")], [])

    def test_cache_is_keyed_by_client_and_settings(self):
        self.setup()
        a = self.line(self.dirs["acme"])
        b = self.line(self.dirs["beta"])
        self.assertNotEqual(a, b)
        self.assertEqual(self.line(self.dirs["acme"]), a)
        self.setup(self.cfg(show_amounts=True))
        with_amounts = self.line(self.dirs["acme"])
        self.assertIn("$", with_amounts)
        self.setup(self.cfg(components=["client"]))
        self.assertEqual(self.line(self.dirs["acme"]), "Acme Co\n")

    def test_a_broken_cache_is_ignored(self):
        self.setup()
        path = os.path.join(self.home, "status-cache.json")
        for junk in ("{broken", "[]", '{"entries": 5}', '{"entries": {"x": 1}}'):
            with open(path, "w") as f:
                f.write(junk)
            self.assertEqual(self.line(self.dirs["acme"]), self.expected("acme") + "\n")
        os.unlink(path)
        os.mkdir(path)                       # cannot be written: still prints
        self.assertEqual(self.line(self.dirs["acme"]), self.expected("acme") + "\n")

    def test_starts_with_the_sys_path_line(self):
        for script in (STATUSLINE, REMIND):
            with open(script, encoding="utf-8") as f:
                self.assertIn("sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))",
                              f.read())


# ------------------------------------------------------------------ reminder hook

class RemindTests(Case):
    def remind(self, cwd, now=NOW, stdin=None, **env):
        if stdin is None:
            stdin = json.dumps({"hook_event_name": "SessionStart", "source": "startup",
                                "session_id": "s", "cwd": cwd, "transcript_path": ""})
        r = self.run_script(REMIND, "--now", now, stdin=stdin, **env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")
        return r.stdout

    def test_off_by_default(self):
        self.setup()
        self.assertEqual(self.remind(self.dirs["acme"]), "")
        self.assertFalse(os.path.exists(os.path.join(self.home, "reminders.json")))

    def test_session_start_prints_one_json_object_every_time(self):
        self.setup(self.cfg(reminders="session_start"))
        hours = self.compute("acme", "last")["total_hours"]
        out = self.remind(self.dirs["acme"])
        self.assertEqual(out.count("\n"), 1)
        doc = json.loads(out)
        self.assertEqual(doc, {
            "systemMessage": "timesheet: Invoice due: Nov 2 – Nov 8 (%s h). "
                             "Run /timesheet:invoice acme" % hours,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": doc["hookSpecificOutput"]["additionalContext"]}})
        ctx = doc["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Acme Co", ctx)
        self.assertIn("Nov 2 – Nov 8, 2026", ctx)
        self.assertIn("once", ctx)
        self.assertIn("unless the user asks", ctx)
        self.assertNotIn("$", out)
        self.assertNotIn("Beta", out)
        self.assertEqual(self.remind(self.dirs["acme"]), out)          # again, every startup
        self.assertFalse(os.path.exists(os.path.join(self.home, "reminders.json")))

    def test_amounts_follow_show_amounts_and_stay_per_client(self):
        self.setup(self.cfg(reminders="session_start", show_amounts=True))
        a, b = self.compute("acme", "last"), self.compute("beta", "last")
        doc = json.loads(self.remind(self.dirs["beta"]))
        money = "$%s" % format(float(b["total"]), ",.2f")
        self.assertEqual(doc["systemMessage"],
                         "timesheet: Invoice due: Oct 1 – Oct 31 (%s h, %s). "
                         "Run /timesheet:invoice beta" % (b["total_hours"], money))
        text = json.dumps(doc, ensure_ascii=False)
        self.assertNotIn("Acme", text)
        self.assertNotIn("acme", text)
        self.assertNotEqual(a["total"], b["total"])
        self.assertNotIn("$%s" % format(float(a["total"]), ",.2f"), text)
        self.assertNotIn(a["total_hours"] + " h", text)

    def test_several_periods(self):
        self.setup(self.cfg(reminders="session_start"), events=EVENTS + OLDER_TURN)
        doc = json.loads(self.remind(self.dirs["acme"]))
        self.assertRegex(doc["systemMessage"],
                         r"^timesheet: Invoice due: 2 periods \(\d+\.\d\d h\)\. "
                         r"Run /timesheet:invoice acme$")
        ctx = doc["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Oct 26 – Nov 1, 2026", ctx)
        self.assertIn("Nov 2 – Nov 8, 2026", ctx)

    def test_silent_when_nothing_is_due_or_nothing_matches(self):
        self.setup(self.cfg(reminders="session_start"))
        self.assertEqual(self.remind(self.dirs["elsewhere"]), "")
        self.approve("acme", "last")
        self.assertEqual(self.remind(self.dirs["acme"]), "")
        self.assertNotEqual(self.remind(self.dirs["beta"]), "")

    def test_daily_reminds_once_per_day_per_client(self):
        self.setup(self.cfg(reminders="daily"))
        first = self.remind(self.dirs["acme"])
        self.assertIn("Invoice due", json.loads(first)["systemMessage"])
        path = os.path.join(self.home, "reminders.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(self.remind(self.dirs["acme"], now="2026-11-12T20:00:00Z"), "")
        self.assertIn("beta", json.loads(self.remind(self.dirs["beta"]))["systemMessage"])
        self.assertEqual(self.remind(self.dirs["beta"]), "")
        self.assertEqual(self.remind(self.dirs["acme"], now="2026-11-13T08:00:00Z"), first)
        self.assertEqual(self.remind(self.dirs["acme"], now="2026-11-13T09:00:00Z"), "")
        self.assertEqual([n for n in os.listdir(self.home) if n.endswith(".tmp")], [])

    def test_daily_uses_the_config_timezone_day(self):
        cfg = self.cfg(reminders="daily")
        cfg["timezone"] = "America/Los_Angeles"
        self.setup(cfg)
        self.assertNotEqual(self.remind(self.dirs["acme"], now="2026-11-12T20:00:00Z"), "")
        # 06:00 UTC on the 13th is still the 12th in Los Angeles
        self.assertEqual(self.remind(self.dirs["acme"], now="2026-11-13T06:00:00Z"), "")
        self.assertNotEqual(self.remind(self.dirs["acme"], now="2026-11-13T09:00:00Z"), "")

    def test_never_fails(self):
        self.setup(self.cfg(reminders="session_start"))
        for garbage in ("", "not json", "[]", '{"cwd": 5}'):
            self.assertEqual(self.remind(None, stdin=garbage), "")
        with open(os.path.join(self.home, "config.json"), "w") as f:
            f.write("{broken")
        self.assertEqual(self.remind(self.dirs["acme"]), "")
        os.unlink(os.path.join(self.home, "config.json"))
        self.assertEqual(self.remind(self.dirs["acme"]), "")
        r = self.run_script(REMIND, "--now", "junk", stdin=json.dumps({"cwd": self.dirs["acme"]}))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_plugin_root_is_recorded_when_the_data_dir_exists(self):
        self.setup()
        root = os.path.join(self.dirs["elsewhere"], "plugin-1.2.3")
        self.assertEqual(self.remind(self.dirs["elsewhere"], CLAUDE_PLUGIN_ROOT=root), "")
        path = os.path.join(self.home, "plugin-root")
        with open(path) as f:
            self.assertEqual(f.read(), root + "\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        # also with garbage on stdin and a broken config, and it follows plugin updates
        with open(os.path.join(self.home, "config.json"), "w") as f:
            f.write("{broken")
        self.remind(None, stdin="junk", CLAUDE_PLUGIN_ROOT=root + "-next")
        with open(path) as f:
            self.assertEqual(f.read(), root + "-next\n")
        self.remind(self.dirs["acme"])                 # variable unset: file kept
        with open(path) as f:
            self.assertEqual(f.read(), root + "-next\n")

    def test_plugin_root_never_creates_the_data_dir(self):
        gone = os.path.join(self.home, "nope")
        r = self.run_script(REMIND, stdin=json.dumps({"cwd": self.dirs["acme"]}),
                            TIMESHEET_HOME=gone, CLAUDE_PLUGIN_ROOT=self.dirs["elsewhere"])
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertFalse(os.path.exists(gone))


# ------------------------------------------------------------------ installer

class InstallTests(Case):
    def setUp(self):
        super().setUp()
        self.settings = os.path.join(self.account, "settings.json")
        self.launcher = os.path.join(self.home, "statusline.sh")
        self.ours = {"type": "command", "command": self.launcher, "refreshInterval": 60}

    def statusline(self, *args, settings=True, **env):
        extra = ["--settings", self.settings] if settings else []
        return self.timesheet("statusline", *(list(args) + extra), **env)

    def read(self):
        with open(self.settings, encoding="utf-8") as f:
            return json.load(f)

    def write(self, obj):
        with open(self.settings, "w", encoding="utf-8") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj, indent=4))

    def test_print(self):
        r = self.statusline("--print")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(self.launcher, r.stdout.splitlines()[0])
        snippet = json.loads(r.stdout[r.stdout.index("{"):])
        self.assertEqual(snippet, {"statusLine": self.ours})
        self.assertFalse(os.path.exists(self.settings))
        self.assertFalse(os.path.exists(self.launcher))

    def test_install_into_missing_settings(self):
        r = self.statusline("--install")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read(), {"statusLine": self.ours})
        self.assertEqual(stat.S_IMODE(os.stat(self.launcher).st_mode), 0o700)
        with open(os.path.join(self.home, "plugin-root")) as f:
            self.assertEqual(f.read(), helpers.REPO + "\n")
        self.assertFalse(os.path.exists(self.settings + ".bak-timesheet"))
        self.assertEqual([n for n in os.listdir(self.account) if n != "settings.json"], [])

    def test_install_preserves_other_keys_and_keeps_a_backup(self):
        before = {"model": "x", "hooks": {"Stop": []}, "permissions": {"allow": ["a"]},
                  "zeta": 1}
        self.write(before)
        with open(self.settings, encoding="utf-8") as f:
            raw = f.read()
        os.chmod(self.settings, 0o644)
        r = self.statusline("--install")
        self.assertEqual(r.returncode, 0, r.stderr)
        after = self.read()
        self.assertEqual(list(after), ["model", "hooks", "permissions", "zeta", "statusLine"])
        self.assertEqual(after, dict(before, statusLine=self.ours))
        with open(self.settings + ".bak-timesheet", encoding="utf-8") as f:
            self.assertEqual(f.read(), raw)
        self.assertEqual(stat.S_IMODE(os.stat(self.settings).st_mode), 0o644)
        self.assertEqual(sorted(os.listdir(self.account)),
                         ["settings.json", "settings.json.bak-timesheet"])
        r = self.statusline("--install")                     # again: nothing changes
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read(), after)

    def test_install_refuses_to_replace_another_status_line(self):
        theirs = {"type": "command", "command": "~/bin/my-statusline.sh"}
        self.write({"model": "x", "statusLine": theirs})
        with open(self.settings, encoding="utf-8") as f:
            raw = f.read()
        r = self.statusline("--install")
        self.assertEqual(r.returncode, 2)
        self.assertIn("~/bin/my-statusline.sh", r.stderr)
        self.assertIn(self.launcher, r.stderr)
        self.assertIn("--force", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        with open(self.settings, encoding="utf-8") as f:
            self.assertEqual(f.read(), raw)
        self.assertFalse(os.path.exists(self.settings + ".bak-timesheet"))
        self.assertTrue(os.path.exists(self.launcher))      # so their script can call it
        r = self.statusline("--install", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read(), {"model": "x", "statusLine": self.ours})
        with open(self.settings + ".bak-timesheet", encoding="utf-8") as f:
            self.assertEqual(f.read(), raw)

    def test_invalid_settings_json_is_left_untouched(self):
        for junk in ("{broken", "[1, 2]", '"text"'):
            self.write(junk)
            for action in ("--install", "--uninstall"):
                r = self.statusline(action)
                self.assertEqual(r.returncode, 2, junk)
                self.assertNotIn("Traceback", r.stderr)
                with open(self.settings, encoding="utf-8") as f:
                    self.assertEqual(f.read(), junk)
            self.assertEqual(os.listdir(self.account), ["settings.json"])

    def test_uninstall_removes_only_our_status_line(self):
        self.write({"model": "x"})
        self.assertEqual(self.statusline("--install").returncode, 0)
        r = self.statusline("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read(), {"model": "x"})
        self.assertFalse(os.path.exists(self.launcher))
        r = self.statusline("--uninstall")                   # nothing there: fine
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read(), {"model": "x"})

    def test_uninstall_leaves_another_status_line(self):
        theirs = {"type": "command", "command": "~/bin/my-statusline.sh"}
        self.write({"statusLine": theirs, "model": "x"})
        with open(self.settings, encoding="utf-8") as f:
            raw = f.read()
        r = self.statusline("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.settings, encoding="utf-8") as f:
            self.assertEqual(f.read(), raw)
        os.unlink(self.settings)
        r = self.statusline("--uninstall")                   # no settings file: not created
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.settings))

    def test_default_settings_path_honours_claude_config_dir(self):
        other = os.path.join(self.dirs["elsewhere"], "account2")
        os.makedirs(other)
        r = self.statusline("--install", settings=False, CLAUDE_CONFIG_DIR=other)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(other, "settings.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"statusLine": self.ours})
        self.assertFalse(os.path.exists(self.settings))

    def test_a_symlinked_settings_file_stays_a_symlink(self):
        real = os.path.join(self.dirs["elsewhere"], "dotfiles-settings.json")
        with open(real, "w") as f:
            json.dump({"model": "x"}, f)
        os.symlink(real, self.settings)
        self.assertEqual(self.statusline("--install").returncode, 0)
        self.assertTrue(os.path.islink(self.settings))
        with open(real) as f:
            self.assertEqual(json.load(f), {"model": "x", "statusLine": self.ours})

    def test_one_action_is_required(self):
        self.assertEqual(self.statusline().returncode, 2)
        self.assertEqual(self.statusline("--install", "--uninstall").returncode, 2)

    def run_launcher(self, stdin):
        return subprocess.run(["/bin/sh", self.launcher], capture_output=True, text=True,
                              encoding="utf-8", env=self.env(), input=stdin, timeout=60)

    def test_launcher_runs_the_status_line_script(self):
        self.setup()
        self.assertEqual(self.statusline("--install").returncode, 0)
        r = self.run_launcher(json.dumps({"cwd": self.dirs["acme"]}))
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertIn("Current hours: ", r.stdout)
        r = self.run_launcher(json.dumps({"cwd": self.dirs["elsewhere"]}))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_launcher_is_silent_when_the_plugin_is_gone(self):
        self.setup()
        self.assertEqual(self.statusline("--install").returncode, 0)
        root = os.path.join(self.home, "plugin-root")
        with open(root, "w") as f:
            f.write(os.path.join(self.dirs["elsewhere"], "removed") + "\n")
        r = self.run_launcher(json.dumps({"cwd": self.dirs["acme"]}))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        with open(root, "w"):
            pass
        r = self.run_launcher(json.dumps({"cwd": self.dirs["acme"]}))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        os.unlink(root)
        r = self.run_launcher(json.dumps({"cwd": self.dirs["acme"]}))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


# ------------------------------------------------------------------ hooks.json

class HooksJsonTests(unittest.TestCase):
    ORIGINAL = [
        '    "UserPromptSubmit": [\n      {"hooks": [{"type": "command", "command": '
        '"python3 \\"${CLAUDE_PLUGIN_ROOT}/scripts/hook.py\\""}]}\n    ],\n',
        '    "Stop": [\n      {"hooks": [{"type": "command", "command": '
        '"python3 \\"${CLAUDE_PLUGIN_ROOT}/scripts/hook.py\\""}]}\n    ],\n',
        '    "StopFailure": [\n      {"hooks": [{"type": "command", "command": '
        '"python3 \\"${CLAUDE_PLUGIN_ROOT}/scripts/hook.py\\""}]}\n    ],\n',
        '    "SessionEnd": [\n      {"hooks": [{"type": "command", "command": '
        '"python3 \\"${CLAUDE_PLUGIN_ROOT}/scripts/hook.py\\""}]}\n    ]',
    ]

    def test_session_start_entry_and_the_original_four(self):
        with open(os.path.join(helpers.REPO, "hooks", "hooks.json"), encoding="utf-8") as f:
            text = f.read()
        hooks = json.loads(text)["hooks"]
        self.assertEqual(sorted(hooks), ["SessionEnd", "SessionStart", "Stop", "StopFailure",
                                         "UserPromptSubmit"])
        self.assertEqual(hooks["SessionStart"], [{"matcher": "startup", "hooks": [{
            "type": "command", "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/remind.py"',
            "timeout": 10}]}])
        for chunk in self.ORIGINAL:
            self.assertIn(chunk, text)
        tracking = {"type": "command",
                    "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hook.py"'}
        for name in ("UserPromptSubmit", "Stop", "StopFailure", "SessionEnd"):
            self.assertEqual(hooks[name], [{"hooks": [tracking]}])


if __name__ == "__main__":
    unittest.main()
