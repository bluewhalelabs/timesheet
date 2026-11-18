import json
import os
import unittest

from tests import helpers
from tests.test_timesheet_cli import CliCase, two_clients, event


class DoctorTests(CliCase):
    def account(self, name, settings=None):
        d = os.path.join(self.home, name)
        os.makedirs(d)
        if settings is not None:
            with open(os.path.join(d, "settings.json"), "w") as f:
                f.write(settings if isinstance(settings, str) else json.dumps(settings))
        return d

    def cfg(self, acme_scope, beta_scope=None, **top):
        cfg = two_clients(**top)
        cfg["clients"]["acme"]["scope"] = acme_scope
        if beta_scope is not None:
            cfg["clients"]["beta"]["scope"] = beta_scope
        return cfg

    def doctor(self, *args, **env):
        return self.run_cli("doctor", *args, **env)

    def checks(self, r):
        return {c["name"]: c for c in json.loads(r.stdout)["checks"]}

    def test_doctor_reports_missing_scope_directory_and_hook_errors(self):
        self.setup(self.cfg({"directories": [os.path.join(self.home, "gone")]},
                            {"directories": [self.home]}))
        with open(os.path.join(self.home, "hook-errors.log"), "w") as f:
            f.write("first\nboom happened\n")
        r = self.doctor()
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        self.assertIn("WARN  client acme scope", out)
        self.assertIn("gone", out)
        self.assertIn("OK  client beta scope", out)
        self.assertIn("WARN  hook errors: 2", out)
        self.assertIn("boom happened", out)
        self.assertIn("OK  python", out)
        self.assertIn("OK  config", out)

    def test_clear_hook_errors_prints_the_count_then_truncates(self):
        self.setup(self.cfg({"directories": [self.home]}, {"directories": [self.home]}))
        path = os.path.join(self.home, "hook-errors.log")
        with open(path, "w") as f:
            f.write("first\nboom happened\n")
        r = self.doctor("--clear-hook-errors")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARN  hook errors: 2", r.stdout)
        self.assertIn("cleared 2 hook error(s)", r.stdout)
        self.assertEqual(os.path.getsize(path), 0)
        self.assertIn("OK  hook errors: none", self.doctor().stdout)
        with open(path, "w") as f:                # without the flag nothing is cleared
            f.write("again\n")
        self.doctor()
        self.assertEqual(os.path.getsize(path), 6)

    def test_empty_scope_fails_with_exit_1(self):
        self.setup(self.cfg({}, {"directories": [self.home]}))
        r = self.doctor()
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL  client acme scope", r.stdout)

    def test_config_failure_skips_client_checks(self):
        r = self.doctor()      # no config.json
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL  config", r.stdout)
        self.assertNotIn("scope", r.stdout)

    def test_unknown_clients_warn(self):
        self.setup(self.cfg({"directories": [self.home]}, {"directories": [self.home]}),
                   [event("2026-11-10T09:00:00Z", "start", "s1", "p1", client="oldname")])
        with open(os.path.join(self.home, "manual.jsonl"), "w") as f:
            f.write(json.dumps({"date": "2026-11-10", "client": "gone", "hours": 1}) + "\n")
        r = self.doctor()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('WARN  unknown clients: 1 event(s) logged for unknown client "oldname" '
                      '(renamed or removed from config?)', r.stdout)
        self.assertIn('1 manual entry for unknown client "gone"', r.stdout)
        self.setup(self.cfg({"directories": [self.home]}, {"directories": [self.home]}),
                   [event("2026-11-10T09:00:00Z", "start", "s1", "p1")])
        os.unlink(os.path.join(self.home, "manual.jsonl"))
        self.assertEqual(self.checks(self.doctor("--json"))["unknown clients"]["status"], "OK")

    def test_json_shape(self):
        self.setup(self.cfg({"directories": [self.home]}, {"directories": [self.home]}))
        r = self.doctor("--json")
        doc = json.loads(r.stdout)
        self.assertEqual(sorted(doc), ["checks", "ok"])
        self.assertIs(doc["ok"], True)
        for c in doc["checks"]:
            self.assertEqual(sorted(c), ["detail", "name", "status"])
            self.assertIn(c["status"], ("OK", "WARN", "FAIL"))
        names = [c["name"] for c in doc["checks"]]
        for n in ("python", "config", "client acme scope", "client beta scope",
                  "pdf browser", "hook errors", "events log"):
            self.assertIn(n, names)

    def test_plugin_enabled_check(self):
        on = self.account("on", {"enabledPlugins": {"timesheet@market": True}})
        off = self.account("off", {"enabledPlugins": {"timesheet@market": False,
                                                      "other@market": True}})
        bad = self.account("bad", "{not json")
        none = self.account("none")
        self.setup(self.cfg({"accounts": [on, off]}, {"accounts": [bad, none]}))
        c = self.checks(self.doctor("--json"))
        self.assertEqual(c["plugin enabled in " + on]["status"], "OK")
        self.assertEqual(c["plugin enabled in " + off]["status"], "WARN")
        self.assertEqual(c["plugin enabled in " + off]["detail"], "not enabled")
        self.assertEqual(c["plugin enabled in " + bad]["detail"], "unknown")
        self.assertEqual(c["plugin enabled in " + none]["detail"], "unknown")

    def test_events_log_check(self):
        self.setup(self.cfg({"directories": [self.home]}, {"directories": [self.home]}))
        self.assertEqual(self.checks(self.doctor("--json"))["events log"]["status"], "WARN")
        self.setup(self.cfg({"directories": [self.home]}, {"directories": [self.home]}),
                   [event("2026-11-10T09:00:00Z", "start", "s1", "p"),
                    event("2026-11-10T09:10:00Z", "end", "s1", "p")])
        c = self.checks(self.doctor("--json"))["events log"]
        self.assertEqual(c["status"], "OK")
        self.assertIn("2", c["detail"])
        self.assertIn("2026-11-10T09:10:00", c["detail"])

    def test_doctor_never_prints_the_token(self):
        cfg = self.cfg({"directories": [self.home]}, {"directories": [self.home]})
        cfg["clients"]["acme"]["mercury"] = {"enabled": True}
        cfg["clients"]["beta"]["mercury"] = {"enabled": True}
        cfg["mercury"] = {"api_key_env": "ATS_TEST_TOKEN"}
        self.setup(cfg)
        r = self.doctor(ATS_TEST_TOKEN="secret-token:abc")
        self.assertNotIn("abc", r.stdout + r.stderr)
        self.assertIn("OK  mercury token", r.stdout)
        r = self.run_cli("doctor")
        self.assertIn("WARN  mercury token", r.stdout)

    def test_doctor_warns_when_no_token_env_var_is_configured(self):
        cfg = self.cfg({"directories": [self.home]}, {"directories": [self.home]})
        cfg["clients"]["acme"]["mercury"] = {"enabled": True}
        cfg["mercury"] = {"api_key_env": ""}
        self.setup(cfg)
        r = self.doctor("--json", ATS_TEST_TOKEN="x")
        c = self.checks(r)["mercury token acme"]
        self.assertEqual(c["status"], "WARN")
        self.assertEqual(c["detail"], "no token env var configured (mercury.api_key_env)")


if __name__ == "__main__":
    unittest.main()
