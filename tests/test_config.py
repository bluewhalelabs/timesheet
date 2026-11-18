import copy
import json
import os
import re
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, tzinfo
from unittest import mock

from tests import helpers
from ats import config
from ats.config import ConfigError, load, save, settings, client_keys, tz, counting_note, local_zone_name


def base_cfg():
    cfg = {
        "defaults": {"hourly_rate": 150},
        "clients": {"acme": {"name": "Acme Co"}},
    }
    with helpers.temp_home() as home:
        path = helpers.write_config(home, cfg)
        return load(path)


def load_raw(cfg):
    with helpers.temp_home() as home:
        return load(helpers.write_config(home, cfg))


class ConfigTests(unittest.TestCase):
    def test_non_ascii_round_trip_under_ascii_locale(self):
        cfg = base_cfg()
        cfg["you"]["business"] = "Café Études"
        with helpers.temp_home() as home:
            path = os.path.join(home, "config.json")
            save(cfg, path)
            with open(path, "rb") as f:
                self.assertIn("Café Études".encode("utf-8"), f.read())
            self.assertEqual(load(path)["you"]["business"], "Café Études")
            code = ("import sys; sys.path.insert(0, %r); from ats import config; "
                    "print(ascii(config.load(%r)['you']['business']))" % (helpers.SCRIPTS, path))
            env = dict(os.environ, LC_ALL="C", PYTHONUTF8="0")
            env.pop("LANG", None)
            r = subprocess.run(["/usr/bin/python3", "-c", code], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), ascii("Café Études"))

    def test_cascade_defaults_client_project(self):
        cfg = base_cfg()
        cfg["clients"]["acme"]["overrides"] = {"hourly_rate": 175, "rounding": {"increment_hours": 0.25}}
        cfg["clients"]["acme"]["projects"] = {"project-a": {"hourly_rate": 200}}
        self.assertEqual(settings(cfg, "acme")["hourly_rate"], 175)
        self.assertEqual(settings(cfg, "acme", "project-a")["hourly_rate"], 200)
        self.assertEqual(settings(cfg, "acme", "other")["hourly_rate"], 175)
        r = settings(cfg, "acme", "project-a")["rounding"]
        self.assertEqual((r["increment_hours"], r["mode"]), (0.25, "nearest"))

    def test_client_level_non_billable_sessions_default_and_union(self):
        cfg = base_cfg()
        self.assertEqual(cfg["clients"]["acme"]["non_billable_sessions"], [])
        self.assertEqual(settings(cfg, "acme")["non_billable_sessions"], [])
        cfg = load_raw({
            "defaults": {"non_billable_sessions": ["d", "x"]},
            "clients": {"acme": {
                "non_billable_sessions": ["c", "x"],
                "overrides": {"non_billable_sessions": ["o", "c"]},
                "projects": {"project-a": {"hourly_rate": 200}}}}})
        self.assertEqual(settings(cfg, "acme")["non_billable_sessions"], ["d", "x", "c", "o"])
        self.assertEqual(settings(cfg, "acme", "project-a")["non_billable_sessions"],
                         ["d", "x", "c", "o"])
        self.assertEqual(settings(cfg, "acme", "other")["non_billable_sessions"],
                         ["d", "x", "c", "o"])

    def test_settings_returns_deep_copy_and_unknown_client_raises(self):
        cfg = base_cfg()
        s = settings(cfg, "acme")
        s["rounding"]["mode"] = "up"
        s["project_labels"]["x"] = "y"
        self.assertEqual(cfg["defaults"]["rounding"]["mode"], "nearest")
        self.assertNotIn("x", cfg["defaults"]["project_labels"])
        with self.assertRaises(ConfigError):
            settings(cfg, "nope")

    def test_missing_defaults_are_filled(self):
        cfg = load_raw({"clients": {"acme": {}}})
        self.assertEqual(cfg["defaults"]["engaged_gap_minutes"], 15)
        self.assertEqual(cfg["defaults"]["non_billable_sessions"], [])
        self.assertEqual(cfg["defaults"]["text"]["title"], "Timesheet")
        self.assertEqual(cfg["pdf"]["output_dir"], "~/.timesheet/invoices")
        self.assertEqual(cfg["mercury"]["api_key_env"], "MERCURY_API_KEY")
        self.assertEqual(cfg["timezone"], "")
        c = cfg["clients"]["acme"]
        self.assertEqual(c["name"], "acme")
        self.assertEqual(c["scope"], {"accounts": [], "directories": [], "repos": []})
        self.assertEqual((c["overrides"], c["projects"]), ({}, {}))
        self.assertEqual(c["mercury"]["due_in_days"], 15)
        self.assertFalse(c["mercury"]["enabled"])

    def test_partial_nested_defaults_are_filled(self):
        cfg = load_raw({"defaults": {"rounding": {"mode": "up"}}, "clients": {}})
        self.assertEqual(cfg["defaults"]["rounding"], {"increment_hours": 0.01, "mode": "up"})

    def test_invalid_json_raises_config_error(self):
        with helpers.temp_home() as home:
            path = os.path.join(home, "config.json")
            with open(path, "w") as f:
                f.write("{not json")
            with self.assertRaises(ConfigError):
                load(path)

    def test_missing_file_raises_config_error(self):
        with helpers.temp_home():
            with self.assertRaises(ConfigError):
                load()

    def test_load_default_path_uses_home(self):
        with helpers.temp_home() as home:
            helpers.write_config(home, {"clients": {"acme": {}}})
            self.assertEqual(client_keys(load()), ["acme"])
            self.assertEqual(config.home(), home)

    def test_rejects_bad_values(self):
        bad = [
            ({"defaults": {"rounding": {"mode": "sideways"}}}, "rounding.mode"),
            ({"defaults": {"rounding": {"increment_hours": 0}}}, "rounding.increment_hours"),
            ({"defaults": {"billing_period": {"type": "daily"}}}, "billing_period.type"),
            ({"defaults": {"billing_period": {"week_starts": "Monday"}}}, "week_starts"),
            ({"defaults": {"billing_period": {"type": "biweekly"}}}, "anchor"),
            ({"defaults": {"billing_period": {"type": "biweekly", "anchor": "11/17/2026"}}}, "anchor"),
            ({"defaults": {"line_items": "hour"}}, "line_items"),
            ({"defaults": {"hourly_rate": -1}}, "hourly_rate"),
            ({"defaults": {"agent_billable_fraction": 1.5}}, "agent_billable_fraction"),
            ({"defaults": {"engaged_gap_minutes": -5}}, "engaged_gap_minutes"),
            ({"timezone": "Mars/Base"}, "timezone"),
            ({"clients": {"acme": {"overrides": {"line_items": "hour"}}}}, "clients.acme.overrides.line_items"),
            ({"clients": {"acme": {"projects": {"project-a": {"hourly_rate": -2}}}}},
             "clients.acme.projects.project-a.hourly_rate"),
            ({"defaults": {"non_billable_sessions": "nb"}}, "defaults.non_billable_sessions"),
            ({"defaults": {"non_billable_sessions": ["nb", ""]}}, "defaults.non_billable_sessions"),
            ({"defaults": {"non_billable_sessions": ["nb", 3]}}, "defaults.non_billable_sessions"),
            ({"clients": {"acme": {"overrides": {"non_billable_sessions": "nb"}}}},
             "clients.acme.overrides.non_billable_sessions"),
            ({"clients": {"acme": {"non_billable_sessions": "nb"}}},
             "clients.acme.non_billable_sessions"),
            ({"clients": {"acme": {"non_billable_sessions": ["nb", ""]}}},
             "clients.acme.non_billable_sessions"),
            ({"clients": {"acme": {"projects": {"project-a": {"non_billable_sessions": ["nb"]}}}}},
             "clients.acme.projects.project-a.non_billable_sessions"),
        ]
        for cfg, key in bad:
            with self.subTest(key=key):
                with self.assertRaises(ConfigError) as cm:
                    load_raw(cfg)
                self.assertIn(key, str(cm.exception))

    def test_rejects_unsafe_client_keys(self):
        for key in ("..", ".", "a/b", "../x", ".hidden", "-dash", "", "a b", "a\\b"):
            with self.subTest(key=key):
                with self.assertRaises(ConfigError) as cm:
                    load_raw({"clients": {key: {}}})
                self.assertIn("client key", str(cm.exception))
        for key in ("acme", "Acme_1", "a.b-c", "9lives"):
            load_raw({"clients": {key: {}}})

    def test_accepts_valid_variants(self):
        cfg = load_raw({
            "timezone": "America/Los_Angeles",
            "defaults": {"billing_period": {"type": "biweekly", "anchor": "2026-01-05"}},
            "clients": {"beta": {"overrides": {"hourly_rate": 0, "rounding": {"mode": "up", "increment_hours": 0.25}}}},
        })
        self.assertEqual(str(tz(cfg)), "America/Los_Angeles")

    def test_tz_empty_is_system_local(self):
        cfg = load_raw({"clients": {}})
        self.assertIsNotNone(tz(cfg))

    def test_save_is_atomic_and_round_trips(self):
        cfg = load_raw({"clients": {"beta": {}, "acme": {}}})
        with helpers.temp_home() as home:
            path = os.path.join(home, "config.json")
            save(cfg, path)
            self.assertEqual(os.listdir(home), ["config.json"])
            self.assertEqual(load(path), cfg)
            with open(path) as f:
                text = f.read()
            self.assertIn('\n  "', text)
            self.assertEqual(client_keys(json.loads(text)), ["beta", "acme"])
            save(cfg)  # default path under home
            self.assertEqual(load(), cfg)

    def test_expand(self):
        self.assertEqual(config.expand(""), "")
        self.assertEqual(config.expand("~/x"), os.path.realpath(os.path.expanduser("~/x")))

    def test_example_config_uses_placeholders_only(self):
        path = os.path.join(helpers.REPO, "config.example.json")
        with open(path) as f:
            text = f.read()
        data = json.loads(text)  # strict JSON
        emails = re.findall(r"[\w.+-]+@[\w.-]+", text)
        self.assertTrue(emails)
        for e in emails:
            self.assertTrue(e.endswith("@example.com"), e)

        paths = []

        def walk(v):
            if isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
            elif isinstance(v, str) and (v.startswith("~") or v.startswith("/")):
                paths.append(v)
        walk(data)
        self.assertTrue(paths)
        for p in paths:
            self.assertTrue(p.startswith("~/"), p)
        cfg = load(path)
        self.assertEqual(client_keys(cfg), ["acme", "beta"])
        self.assertEqual(settings(cfg, "beta")["hourly_rate"], 200)
        self.assertEqual(settings(cfg, "beta")["billing_period"]["type"], "monthly")
        self.assertEqual(settings(cfg, "beta")["rounding"], {"increment_hours": 0.25, "mode": "up"})
        self.assertEqual(settings(cfg, "acme")["hourly_rate"], 150)

    def test_tz_empty_uses_real_dst_aware_zone(self):
        cfg = load_raw({"clients": {}})
        with mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
            self.assertEqual(local_zone_name(), "America/Los_Angeles")
            z = tz(cfg)
            self.assertEqual(datetime(2026, 1, 15, 12, tzinfo=z).utcoffset(), timedelta(hours=-8))
            self.assertEqual(datetime(2026, 7, 15, 12, tzinfo=z).utcoffset(), timedelta(hours=-7))

    def test_tz_invalid_env_does_not_raise(self):
        cfg = load_raw({"clients": {}})
        with mock.patch.dict(os.environ, {"TZ": "Not/AZone"}):
            self.assertIsNotNone(tz(cfg))
            name = local_zone_name()
            self.assertTrue(name is None or isinstance(name, str))

    def test_tz_falls_back_to_fixed_offset_when_nothing_resolves(self):
        cfg = load_raw({"clients": {}})
        with mock.patch.dict(os.environ, {"TZ": "Not/AZone"}), \
                mock.patch("ats.config.os.path.realpath", return_value="/nowhere"):
            self.assertIsNone(local_zone_name())
            self.assertIsInstance(tz(cfg), tzinfo)

    def test_rejects_non_object_sections(self):
        bad = [
            ({"clients": {"acme": {"scope": None}}}, "clients.acme.scope"),
            ({"clients": {"acme": {"mercury": "x"}}}, "clients.acme.mercury"),
            ({"clients": {"acme": {"overrides": []}}}, "clients.acme.overrides"),
            ({"clients": {"acme": {"projects": "x"}}}, "clients.acme.projects"),
            ({"clients": {"acme": {"projects": {"project-a": 3}}}}, "clients.acme.projects.project-a"),
            ({"clients": {"acme": {"overrides": {"text": "x"}}}}, "clients.acme.overrides.text"),
            ({"clients": {"acme": {"overrides": {"project_labels": []}}}}, "clients.acme.overrides.project_labels"),
            ({"defaults": {"text": "x"}}, "defaults.text"),
            ({"defaults": {"project_labels": None}}, "defaults.project_labels"),
            ({"mercury": None}, "mercury"),
            ({"pdf": 1}, "pdf"),
            ({"clients": {"acme": {"scope": {"accounts": "a"}}}}, "clients.acme.scope.accounts"),
            ({"clients": {"acme": {"scope": {"directories": [1]}}}}, "clients.acme.scope.directories"),
            ({"clients": {"acme": {"scope": {"repos": None}}}}, "clients.acme.scope.repos"),
        ]
        for cfg, key in bad:
            with self.subTest(key=key, cfg=cfg):
                with self.assertRaises(ConfigError) as cm:
                    load_raw(cfg)
                self.assertIn(key, str(cm.exception))

    def test_mercury_block_is_type_checked(self):
        bad = [
            ({"enabled": "yes"}, "enabled"),
            ({"ach_debit_enabled": 1}, "ach_debit_enabled"),
            ({"credit_card_enabled": None}, "credit_card_enabled"),
            ({"use_real_account_number": "no"}, "use_real_account_number"),
            ({"customer_id": 5}, "customer_id"),
            ({"destination_account_id": None}, "destination_account_id"),
            ({"due_in_days": -1}, "due_in_days"),
            ({"due_in_days": 1.5}, "due_in_days"),
            ({"due_in_days": True}, "due_in_days"),
            ({"cc_emails": "a@example.com"}, "cc_emails"),
            ({"cc_emails": [1]}, "cc_emails"),
        ]
        for block, key in bad:
            with self.subTest(block=block):
                with self.assertRaises(ConfigError) as cm:
                    load_raw({"clients": {"acme": {"mercury": block}}})
                self.assertIn("clients.acme.mercury." + key, str(cm.exception))

    def test_mercury_allow_custom_host_defaults_false_and_must_be_bool(self):
        self.assertIs(base_cfg()["mercury"]["allow_custom_host"], False)
        self.assertIs(load_raw({"mercury": {"allow_custom_host": True}})["mercury"]
                      ["allow_custom_host"], True)
        with self.assertRaises(ConfigError) as cm:
            load_raw({"mercury": {"allow_custom_host": "yes"}})
        self.assertIn("mercury.allow_custom_host", str(cm.exception))

    def test_mercury_block_defaults_and_valid_values(self):
        m = base_cfg()["clients"]["acme"]["mercury"]
        self.assertEqual(m["cc_emails"], [])
        for k in ("ach_debit_enabled", "credit_card_enabled", "use_real_account_number"):
            self.assertIs(m[k], False)
        cfg = load_raw({"clients": {"acme": {"mercury": {
            "cc_emails": ["a@example.com"], "due_in_days": 0, "ach_debit_enabled": True}}}})
        self.assertEqual(cfg["clients"]["acme"]["mercury"]["cc_emails"], ["a@example.com"])

    def test_timezone_is_top_level_only(self):
        for cfg, key in [
            ({"clients": {"acme": {"overrides": {"timezone": "America/Denver"}}}}, "clients.acme.overrides.timezone"),
            ({"clients": {"acme": {"projects": {"project-a": {"timezone": "UTC"}}}}},
             "clients.acme.projects.project-a.timezone"),
            ({"defaults": {"timezone": "UTC"}}, "defaults.timezone"),
        ]:
            with self.subTest(key=key):
                with self.assertRaises(ConfigError) as cm:
                    load_raw(cfg)
                self.assertIn(key, str(cm.exception))

    def test_show_project_subtotals_defaults_false_and_must_be_bool(self):
        cfg = base_cfg()
        self.assertIs(settings(cfg, "acme")["show_project_subtotals"], False)
        cfg = load_raw({"clients": {"acme": {"overrides": {"show_project_subtotals": True}}}})
        self.assertIs(settings(cfg, "acme")["show_project_subtotals"], True)
        for raw, key in [({"defaults": {"show_project_subtotals": "yes"}},
                          "defaults.show_project_subtotals"),
                         ({"clients": {"acme": {"overrides": {"show_project_subtotals": 1}}}},
                          "clients.acme.overrides.show_project_subtotals")]:
            with self.subTest(key=key):
                with self.assertRaises(ConfigError) as cm:
                    load_raw(raw)
                self.assertIn(key, str(cm.exception))

    def test_gap_thresholds_must_be_consistent_at_every_level(self):
        bad = [
            ({"defaults": {"engaged_gap_minutes": 50}}, "defaults",
             "engaged_gap_minutes"),
            ({"defaults": {"read_allowance_minutes": 30, "rampup_allowance_minutes": 20}},
             "defaults", "read_allowance_minutes + rampup_allowance_minutes"),
            ({"clients": {"acme": {"overrides": {"break_gap_minutes": 10}}}},
             "clients.acme.overrides", "engaged_gap_minutes"),
            ({"clients": {"acme": {"overrides": {"break_gap_minutes": 15,
                                                 "read_allowance_minutes": 11}}}},
             "clients.acme.overrides", "read_allowance_minutes + rampup_allowance_minutes"),
            ({"clients": {"acme": {"projects": {"project-a": {"rampup_allowance_minutes": 41}}}}},
             "clients.acme.projects.project-a",
             "read_allowance_minutes + rampup_allowance_minutes"),
            ({"clients": {"acme": {"overrides": {"break_gap_minutes": 20},
                                   "projects": {"project-a": {"engaged_gap_minutes": 25}}}}},
             "clients.acme.projects.project-a", "engaged_gap_minutes"),
        ]
        for raw, level, keys in bad:
            with self.subTest(level=level, keys=keys):
                with self.assertRaises(ConfigError) as cm:
                    load_raw(raw)
                msg = str(cm.exception)
                self.assertIn(level, msg)
                self.assertIn(keys, msg)
                self.assertIn("break_gap_minutes", msg)
        # equal values are allowed; a project may relax what the client tightened
        load_raw({"defaults": {"engaged_gap_minutes": 45, "read_allowance_minutes": 40,
                               "rampup_allowance_minutes": 5}})
        load_raw({"clients": {"acme": {
            "overrides": {"break_gap_minutes": 20, "engaged_gap_minutes": 10},
            "projects": {"project-a": {"break_gap_minutes": 60, "engaged_gap_minutes": 50}}}}})

    def test_project_entries_accept_only_per_project_keys(self):
        for key, value in [("currency", "EUR"), ("rounding", {"mode": "up"}),
                           ("billing_period", {"type": "monthly"}), ("line_items", "session"),
                           ("text", {"title": "X"}), ("session_break_minutes", 30),
                           ("show_session_detail", True), ("project_labels", {}),
                           ("show_project_subtotals", True), ("bogus", 1)]:
            with self.subTest(key=key):
                with self.assertRaises(ConfigError) as cm:
                    load_raw({"clients": {"acme": {"projects": {"project-a": {key: value}}}}})
                msg = str(cm.exception)
                self.assertIn("clients.acme.projects.project-a.%s" % key, msg)
                self.assertIn("only be set per client", msg)
                self.assertIn("overrides", msg)
                self.assertIn("defaults", msg)
        ok = {"hourly_rate": 175, "agent_billable_fraction": 0.5, "engaged_gap_minutes": 10,
              "break_gap_minutes": 40, "read_allowance_minutes": 4,
              "rampup_allowance_minutes": 4, "unclosed_cap_minutes": 20}
        cfg = load_raw({"clients": {"acme": {"projects": {"project-a": ok}}}})
        self.assertEqual(settings(cfg, "acme", "project-a")["hourly_rate"], 175)

    def test_counting_note_template(self):
        cfg = base_cfg()
        text = {"counting_note": "Up to {engaged_gap_minutes} minutes, {break_gap_minutes} minutes, "
                                 "{read_allowance_minutes} minutes."}
        cfg["clients"]["acme"]["overrides"] = {"text": text}
        note = counting_note(settings(cfg, "acme"))
        self.assertIn("15 minutes", note)
        self.assertIn("45 minutes", note)
        self.assertNotIn("15.0", note)
        self.assertNotIn("{", note)
        cfg["clients"]["acme"]["overrides"] = {"engaged_gap_minutes": 10, "read_allowance_minutes": 7.5,
                                               "text": text}
        note = counting_note(settings(cfg, "acme"))
        self.assertIn("10 minutes", note)
        self.assertIn("7.5 minutes", note)

    def test_agent_time_is_hidden_by_default_and_the_note_follows(self):
        cfg = base_cfg()
        st = settings(cfg, "acme")
        self.assertIs(st["show_agent_time"], False)
        self.assertEqual(counting_note(st), "")   # no explanation of the rubric by default
        cfg["clients"]["acme"]["overrides"] = {"show_agent_time": True}
        self.assertEqual(counting_note(settings(cfg, "acme")), "")
        cfg["clients"]["acme"]["overrides"]["text"] = {"counting_note": "Mine."}
        self.assertEqual(counting_note(settings(cfg, "acme")), "Mine.")
        with self.assertRaises(ConfigError) as cm:
            load_raw({"clients": {"acme": {"overrides": {"show_agent_time": "yes"}}}})
        self.assertIn("clients.acme.overrides.show_agent_time", str(cm.exception))
        with self.assertRaises(ConfigError):
            load_raw({"clients": {"acme": {"projects": {"p": {"show_agent_time": True}}}}})

    def test_lines_default_to_one_per_session(self):
        self.assertEqual(settings(base_cfg(), "acme")["line_items"], "session")

    def test_counting_note_edge_cases(self):
        s = {"engaged_gap_minutes": 15}
        self.assertEqual(counting_note({"text": {"counting_note": "Plain."}}), "Plain.")
        for text in ("Stray { brace {engaged_gap_minutes}", "Unknown {nope}", "Close }", "Pos {} {0}"):
            with self.subTest(text=text):
                s2 = dict(s, text={"counting_note": text})
                self.assertEqual(counting_note(s2), text)


if __name__ == "__main__":
    unittest.main()
