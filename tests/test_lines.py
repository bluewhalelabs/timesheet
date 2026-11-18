import json
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from tests import helpers
from ats import config, events, periods
from ats.engine import Seg
from ats.lines import build_lines, money, round_hours
from ats.rollup import WorkSession

UTC = timezone.utc
ACME_P1 = ("acme", "p1")
ACME_P2 = ("acme", "p2")
BETA_P = ("beta", "project-b")
WEEK = periods.Period(date(2026, 11, 9), date(2026, 11, 15), "2026-11-09_2026-11-15")
MONTH = periods.Period(date(2026, 11, 1), date(2026, 11, 30), "2026-11-01_2026-11-30")


def z(day, hh, mm=0):
    return datetime(2026, 11, day, hh, mm, tzinfo=UTC)


def seg(start, end, key=ACME_P1, kind="agent", weight=1.0):
    return Seg(start, end, key, weight, kind)


def session(segs, key=None):
    """A WorkSession built from segs (UTC calendar days)."""
    start, end = min(s.start for s in segs), max(s.end for s in segs)
    you = agent = 0.0
    by = {}
    for s in segs:
        n = (s.end - s.start).total_seconds() * s.weight
        cell = by.setdefault(s.key[1], {"you": 0.0, "agent": 0.0})
        cell[s.kind] += n
        if s.kind == "you":
            you += n
        else:
            agent += n
    days = sorted({start.date(), (end - timedelta(seconds=1)).date()})
    key = key or "S" + start.strftime("%Y%m%dT%H%M%SZ")
    return WorkSession(key, start, end, you, agent, by, [], days)


def la_session(segs):
    """A WorkSession whose days are America/Los_Angeles dates."""
    from zoneinfo import ZoneInfo
    la = ZoneInfo("America/Los_Angeles")
    base = session(segs)
    days = sorted({base.start.astimezone(la).date(),
                   (base.end - timedelta(seconds=1)).astimezone(la).date()})
    return base._replace(days=days)


def manual(day, hours, client="acme", project="p1", description=""):
    return {"date": day, "client": client, "project": project,
            "hours": Decimal(str(hours)), "description": description}


class Base(unittest.TestCase):
    def setUp(self):
        cm = helpers.temp_home()
        self.home = cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)

    def cfg(self, acme=None, beta=None, tz="UTC"):
        a = {"overrides": {"hourly_rate": 150,
                           "billing_period": {"type": "weekly", "week_starts": "monday"},
                           "rounding": {"increment_hours": 0.01, "mode": "nearest"}},
             "projects": {}}
        a.update(acme or {})
        b = {"overrides": {"hourly_rate": 200, "billing_period": {"type": "monthly"},
                           "rounding": {"increment_hours": 0.25, "mode": "up"}},
             "projects": {}}
        b.update(beta or {})
        helpers.write_config(self.home, {"timezone": tz, "defaults": {"line_items": "day"},
                                         "clients": {"acme": a, "beta": b}})
        return config.load()

    def build(self, cfg, segs, client="acme", period=WEEK, man=(), summaries=None,
              descriptions=None, sessions=None):
        if sessions is None:
            sessions = [session(segs)] if segs else []
        return build_lines(cfg, client, period, segs, sessions, list(man),
                           summaries or {}, descriptions or {})


class RoundingTests(unittest.TestCase):
    def test_round_hours_modes(self):
        self.assertEqual(round_hours(4800, 0.01, "nearest"), Decimal("1.33"))
        self.assertEqual(round_hours(3618, 0.01, "nearest"), Decimal("1.01"))
        self.assertEqual(round_hours(4800, 0.25, "up"), Decimal("1.50"))
        self.assertEqual(round_hours(4800, 0.1, "down"), Decimal("1.3"))
        self.assertEqual(round_hours(5400, 0.25, "up"), Decimal("1.50"))

    def test_round_hours_formats_to_increment_places(self):
        self.assertEqual(str(round_hours(4800, 0.25, "up")), "1.50")
        self.assertEqual(str(round_hours(4800, 0.1, "down")), "1.3")
        self.assertEqual(str(round_hours(7200, 1, "nearest")), "2")

    def test_float_dust_does_not_round_up_an_exact_multiple(self):
        self.assertEqual(round_hours(5400.000000001, 0.25, "up"), Decimal("1.50"))
        self.assertEqual(round_hours(5400.001, 0.25, "up"), Decimal("1.75"))

    def test_zero_and_negative_are_zero(self):
        self.assertEqual(round_hours(0, 0.25, "up"), Decimal("0.00"))
        self.assertEqual(round_hours(-50, 0.25, "up"), Decimal("0.00"))

    def test_money_formats(self):
        self.assertEqual(money(Decimal("1234.5"), "USD"), "$1,234.50")
        self.assertEqual(money(Decimal("1234.5"), "EUR"), "€1,234.50")
        self.assertEqual(money(Decimal("1234.5"), "GBP"), "£1,234.50")
        self.assertEqual(money(Decimal("1234.5"), "CAD"), "1,234.50 CAD")
        self.assertEqual(money(Decimal("0"), "USD"), "$0.00")


class LoadManualTests(unittest.TestCase):
    def test_missing_file(self):
        with helpers.temp_home():
            self.assertEqual(events.load_manual(), ([], 0))

    def test_parses_defaults_and_skips_invalid(self):
        with helpers.temp_home() as home:
            good = [{"date": "2026-11-10", "client": "acme", "hours": 1.5},
                    {"date": "2026-11-11", "client": "acme", "project": "p1",
                     "hours": 0.1, "description": "call"}]
            bad = ["not json", "[]", json.dumps({"date": "2026-11-10", "hours": 1}),
                   json.dumps({"date": "2026-13-10", "client": "acme", "hours": 1}),
                   json.dumps({"date": "2026-11-10", "client": "acme", "hours": 0}),
                   json.dumps({"date": "2026-11-10", "client": "acme", "hours": -1}),
                   json.dumps({"date": "2026-11-10", "client": "acme", "hours": "2"}),
                   json.dumps({"date": "2026-11-10", "client": "acme", "hours": True})]
            lines = [json.dumps(good[0]), "", "   "] + bad + [json.dumps(good[1])]
            with open(os.path.join(home, "manual.jsonl"), "w") as f:
                f.write("\n".join(lines) + "\n")
            out, skipped = events.load_manual()
        self.assertEqual(skipped, len(bad))
        self.assertEqual(out[0], {"date": date(2026, 11, 10), "client": "acme",
                                  "project": "(root)", "hours": Decimal("1.5"),
                                  "description": ""})
        self.assertEqual(out[1]["hours"], Decimal("0.1"))
        self.assertEqual(out[1]["description"], "call")


class DayModeTests(Base):
    def test_one_line_per_day(self):
        cfg = self.cfg()
        segs = [seg(z(10, 9), z(10, 9, 35)), seg(z(10, 9, 35), z(10, 10, 20), kind="you"),
                seg(z(11, 9), z(11, 9, 30))]
        r = self.build(cfg, segs, sessions=[session(segs[:2]), session(segs[2:])])
        self.assertEqual([l["key"] for l in r["lines"]], ["2026-11-10@150", "2026-11-11@150"])
        first = r["lines"][0]
        self.assertEqual((first["date"], first["label"]), ("2026-11-10", "Tue, Nov 10, 2026"))
        self.assertEqual((first["hours"], first["rate"], first["amount"]), ("1.33", "150", "199.50"))
        self.assertEqual((first["seconds"], first["you_seconds"], first["agent_seconds"]),
                         (4800.0, 2700.0, 2100.0))
        self.assertEqual(first["projects"], ["p1"])
        self.assertEqual(first["session_keys"], ["S20261110T090000Z"])
        self.assertFalse(first["manual"])
        self.assertEqual(r["currency"], "USD")
        self.assertEqual(r["warnings"], [])

    def test_mixed_rate_day_gets_one_line_per_rate(self):
        cfg = self.cfg(acme={"projects": {"p2": {"hourly_rate": 200}}})
        segs = [seg(z(10, 9), z(10, 10)), seg(z(10, 10), z(10, 11), key=ACME_P2)]
        r = self.build(cfg, segs)
        self.assertEqual([(l["key"], l["projects"], l["hours"], l["amount"]) for l in r["lines"]],
                         [("2026-11-10@150", ["p1"], "1.00", "150.00"),
                          ("2026-11-10@200", ["p2"], "1.00", "200.00")])
        self.assertEqual(r["total"], "350.00")
        self.assertEqual(r["lines"][1]["session_keys"], ["S20261110T090000Z"])

    def test_fractional_rate_key(self):
        cfg = self.cfg(acme={"overrides": {"hourly_rate": 137.5}})
        r = self.build(cfg, [seg(z(10, 9), z(10, 10))])
        self.assertEqual((r["lines"][0]["key"], r["lines"][0]["rate"], r["lines"][0]["amount"]),
                         ("2026-11-10@137.5", "137.5", "137.50"))

    def test_total_is_sum_of_rounded_lines(self):
        cfg = self.cfg()
        segs = [seg(z(d, 9), z(d, 10, 20)) for d in (10, 11, 12)]
        r = self.build(cfg, segs, sessions=[session([s]) for s in segs])
        self.assertEqual([l["amount"] for l in r["lines"]], ["199.50"] * 3)
        self.assertEqual(r["total"], "598.50")
        self.assertEqual(r["total_hours"], "3.99")

    def test_agent_fraction_applies_per_project(self):
        cfg = self.cfg(acme={"projects": {"p1": {"agent_billable_fraction": 0.5}}})
        segs = [seg(z(10, 9), z(10, 10)), seg(z(10, 10), z(10, 10, 30), kind="you")]
        (line,) = self.build(cfg, segs)["lines"]
        self.assertEqual(line["seconds"], 3600.0)
        self.assertEqual((line["you_seconds"], line["agent_seconds"]), (1800.0, 3600.0))
        self.assertEqual(line["hours"], "1.00")

    def test_segment_weight_and_midnight_split(self):
        cfg = self.cfg()
        segs = [seg(z(10, 23), z(11, 1), weight=0.5)]
        r = self.build(cfg, segs)
        self.assertEqual([(l["date"], l["seconds"]) for l in r["lines"]],
                         [("2026-11-10", 1800.0), ("2026-11-11", 1800.0)])

    def test_root_project_uses_label(self):
        cfg = self.cfg()
        r = self.build(cfg, [seg(z(10, 9), z(10, 10), key=("acme", "(root)"))])
        self.assertEqual(r["lines"][0]["projects"], ["(root)"])
        self.assertEqual(r["subtotals"][0]["project"], "(root)")
        self.assertEqual(r["subtotals"][0]["label"], "General / cross-project")

    def test_subtotals(self):
        cfg = self.cfg()
        segs = [seg(z(10, 9), z(10, 9, 25)), seg(z(10, 10), z(10, 11), key=ACME_P2)]
        r = self.build(cfg, segs)
        self.assertEqual(r["subtotals"],
                         [{"project": "p1", "label": "p1", "hours": "0.42", "amount": "63.00"},
                          {"project": "p2", "label": "p2", "hours": "1.00", "amount": "150.00"}])

    def test_zero_hour_line_is_dropped_with_warning(self):
        cfg = self.cfg()
        segs = [seg(z(10, 9), z(10, 9, 0) + timedelta(seconds=10)), seg(z(11, 9), z(11, 10))]
        r = self.build(cfg, segs, sessions=[session(segs[:1]), session(segs[1:])])
        self.assertEqual([l["date"] for l in r["lines"]], ["2026-11-11"])
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn("2026-11-10", r["warnings"][0])
        self.assertIn("10 s", r["warnings"][0])
        self.assertEqual(r["total"], "150.00")

    def test_empty_period_returns_no_lines_and_zero_total(self):
        cfg = self.cfg()
        r = self.build(cfg, [])
        self.assertEqual((r["lines"], r["total"], r["total_hours"], r["subtotals"]),
                         ([], "0.00", "0.00", []))
        r = self.build(cfg, [], client="beta", period=MONTH)
        self.assertEqual((r["lines"], r["total"], r["total_hours"]), ([], "0.00", "0.00"))
        cfg = self.cfg(acme={"overrides": {"rounding": {"increment_hours": 0.1, "mode": "up"}}})
        self.assertEqual(self.build(cfg, [])["total_hours"], "0.0")

    def test_description_prefers_edited_text_then_summaries(self):
        cfg = self.cfg()
        s1 = [seg(z(10, 9), z(10, 10))]
        s2 = [seg(z(10, 14), z(10, 15))]
        ss = [session(s1), session(s2)]
        k1, k2 = ss[0].key, ss[1].key
        man = [manual(date(2026, 11, 10), 0.5, description="phone call")]
        r = self.build(cfg, s1 + s2, sessions=ss, man=man,
                       summaries={k1: "Built parser", k2: "Fixed tests"})
        self.assertEqual(r["lines"][0]["description"], "Built parser; Fixed tests; phone call")
        r = self.build(cfg, s1 + s2, sessions=ss, man=man, summaries={k1: "Built parser"},
                       descriptions={"2026-11-10@150": "Edited text"})
        self.assertEqual(r["lines"][0]["description"], "Edited text")
        r = self.build(cfg, s1 + s2, sessions=ss)
        self.assertEqual(r["lines"][0]["description"], "")

    def test_manual_entry_adds_hours_and_flags_line(self):
        cfg = self.cfg()
        segs = [seg(z(10, 9), z(10, 10))]
        man = [manual(date(2026, 11, 10), 0.5), manual(date(2026, 11, 12), 1.5)]
        r = self.build(cfg, segs, man=man)
        a, b = r["lines"]
        self.assertEqual((a["hours"], a["manual"], a["you_seconds"], a["agent_seconds"]),
                         ("1.50", True, 1800.0, 3600.0))
        self.assertEqual((b["key"], b["hours"], b["amount"], b["manual"], b["you_seconds"]),
                         ("2026-11-12@150", "1.50", "225.00", True, 5400.0))
        self.assertEqual(b["session_keys"], [])
        self.assertEqual(r["total"], "450.00")

    def test_each_client_rounds_by_its_own_rule(self):
        cfg = self.cfg()
        for client, period, hours, amount, rate in (("acme", WEEK, "1.33", "199.50", "150"),
                                                    ("beta", MONTH, "1.50", "300.00", "200")):
            segs = [seg(z(10, 9), z(10, 10, 20), key=(client, "p1"))]
            (line,) = self.build(cfg, segs, client=client, period=period)["lines"]
            self.assertEqual((line["hours"], line["amount"], line["rate"]), (hours, amount, rate))

    def test_manual_entries_of_other_clients_and_other_periods_are_ignored(self):
        cfg = self.cfg()
        segs = [seg(z(10, 9), z(10, 10))]
        man = [manual(date(2026, 11, 10), 5, client="beta", project="project-b"),
               manual(date(2026, 11, 8), 5), manual(date(2026, 11, 16), 5),
               manual(date(2026, 11, 15), 1), manual(date(2026, 11, 9), 1)]
        r = self.build(cfg, segs, man=man)
        self.assertEqual([(l["date"], l["hours"]) for l in r["lines"]],
                         [("2026-11-09", "1.00"), ("2026-11-10", "1.00"), ("2026-11-15", "1.00")])
        self.assertEqual(r["total"], "450.00")


class OtherModeTests(Base):
    def setUp(self):
        super().setUp()
        self.segs = [seg(z(10, 9), z(10, 10)), seg(z(10, 10), z(10, 10, 30), key=ACME_P2),
                     seg(z(10, 14), z(10, 15)), seg(z(11, 9), z(11, 9, 30))]
        self.sessions = [session(self.segs[:2]), session(self.segs[2:3]), session(self.segs[3:])]

    def cfg_mode(self, mode, **kw):
        return self.cfg(acme={"overrides": {"line_items": mode, "hourly_rate": 150,
                                            "project_labels": {"p1": "Project One"},
                                            "text": {"period_label": "Services"}}, **kw})

    def test_session_mode(self):
        man = [manual(date(2026, 11, 10), 0.5, description="call"),
               manual(date(2026, 11, 10), 1.0)]
        r = self.build(self.cfg_mode("session"), self.segs, sessions=self.sessions, man=man,
                       summaries={self.sessions[0].key: "First"})
        self.assertEqual([(l["key"], l["label"], l["hours"], l["manual"]) for l in r["lines"]],
                         [("S20261110T090000Z@150", "Tue, Nov 10, 2026", "1.50", False),
                          ("S20261110T140000Z@150", "Tue, Nov 10, 2026 (2)", "1.00", False),
                          ("manual-2026-11-10-1@150", "Tue, Nov 10, 2026 (manual)", "0.50", True),
                          ("manual-2026-11-10-2@150", "Tue, Nov 10, 2026 (manual)", "1.00", True),
                          ("S20261111T090000Z@150", "Wed, Nov 11, 2026", "0.50", False)])
        self.assertEqual(r["lines"][0]["description"], "First")
        self.assertEqual(r["lines"][0]["session_keys"], ["S20261110T090000Z"])
        self.assertEqual(r["lines"][0]["projects"], ["p1", "p2"])
        self.assertEqual(r["lines"][2]["description"], "call")
        self.assertEqual(r["total"], "675.00")

    def test_day_mode_splits_a_segment_at_local_midnight(self):
        segs = [seg(z(10, 7, 30), z(10, 8, 30))]   # LA midnight is 08:00Z
        cfg = self.cfg(tz="America/Los_Angeles")
        r = self.build(cfg, segs, sessions=[la_session(segs)])
        self.assertEqual([(l["date"], l["seconds"]) for l in r["lines"]],
                         [("2026-11-09", 1800.0), ("2026-11-10", 1800.0)])

    def test_session_mode_crossing_local_midnight_is_one_line_on_first_day(self):
        segs = [seg(z(10, 7, 30), z(10, 8, 30))]
        cfg = self.cfg(tz="America/Los_Angeles",
                       acme={"overrides": {"line_items": "session", "hourly_rate": 150}})
        sess = la_session(segs)
        self.assertEqual(sess.days, [date(2026, 11, 9), date(2026, 11, 10)])
        r = self.build(cfg, segs, sessions=[sess])
        (line,) = r["lines"]
        self.assertEqual((line["date"], line["seconds"], line["label"]),
                         ("2026-11-09", 3600.0, "Mon, Nov 9, 2026"))

    def test_session_mode_splits_rates(self):
        cfg = self.cfg_mode("session", projects={"p2": {"hourly_rate": 200}})
        r = self.build(cfg, self.segs[:2], sessions=self.sessions[:1])
        self.assertEqual([(l["key"], l["hours"], l["amount"]) for l in r["lines"]],
                         [("S20261110T090000Z@150", "1.00", "150.00"),
                          ("S20261110T090000Z@200", "0.50", "100.00")])

    def test_project_mode(self):
        man = [manual(date(2026, 11, 12), 1, project="p2")]
        r = self.build(self.cfg_mode("project"), self.segs, sessions=self.sessions, man=man)
        self.assertEqual([(l["key"], l["label"], l["date"], l["hours"], l["manual"])
                          for l in r["lines"]],
                         [("p1@150", "Project One", None, "2.50", False),
                          ("p2@150", "p2", None, "1.50", True)])
        self.assertEqual(r["lines"][0]["session_keys"], [s.key for s in self.sessions])

    def test_period_mode(self):
        r = self.build(self.cfg_mode("period"), self.segs, sessions=self.sessions,
                       man=[manual(date(2026, 11, 12), 1)])
        (line,) = r["lines"]
        self.assertEqual((line["key"], line["label"], line["date"], line["hours"], line["amount"],
                          line["manual"], line["projects"]),
                         ("period@150", "Services", None, "4.00", "600.00", True, ["p1", "p2"]))
        self.assertEqual(r["total"], "600.00")
