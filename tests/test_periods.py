import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tests import helpers  # noqa: F401
from ats import periods
from ats.periods import Period, containing, last_complete, resolve, bounds, label, next_period, previous_period

TODAY = date(2026, 11, 17)  # a Tuesday
MON = {"type": "weekly", "week_starts": "monday"}
SUN = {"type": "weekly", "week_starts": "sunday"}
BI = {"type": "biweekly", "anchor": "2026-01-05"}
SEMI = {"type": "semimonthly"}
MONTH = {"type": "monthly"}


def rng(p):
    return (str(p.start), str(p.end))


class PeriodTests(unittest.TestCase):
    def test_period_id(self):
        p = Period(date(2026, 11, 9), date(2026, 11, 15), "2026-11-09_2026-11-15")
        self.assertEqual(p.id, "{}_{}".format(p.start, p.end))
        self.assertEqual(containing(TODAY, MON).id, "2026-11-16_2026-11-22")

    def test_parse_id(self):
        p = periods.parse_id("2026-11-01_2026-11-30")
        self.assertEqual((p.start, p.end, p.id), (date(2026, 11, 1), date(2026, 11, 30),
                                                  "2026-11-01_2026-11-30"))
        for bad in ("junk", "2026-11-31_2026-12-01", "2026-11-30_2026-11-01", "", None,
                    "2026-11-01_2026-11-30x"):
            self.assertIsNone(periods.parse_id(bad), bad)

    def test_weekly_monday(self):
        self.assertEqual(rng(last_complete(TODAY, MON)), ("2026-11-09", "2026-11-15"))
        self.assertEqual(rng(containing(TODAY, MON)), ("2026-11-16", "2026-11-22"))

    def test_weekly_default_week_starts(self):
        self.assertEqual(rng(containing(TODAY, {"type": "weekly"})), ("2026-11-16", "2026-11-22"))

    def test_weekly_sunday(self):
        self.assertEqual(rng(containing(TODAY, SUN)), ("2026-11-15", "2026-11-21"))
        self.assertEqual(rng(last_complete(TODAY, SUN)), ("2026-11-08", "2026-11-14"))

    def test_weekly_year_boundary(self):
        self.assertEqual(rng(containing(date(2026, 1, 1), MON)), ("2025-12-29", "2026-01-04"))

    def test_biweekly_anchor(self):
        self.assertEqual(rng(containing(TODAY, BI)), ("2026-11-09", "2026-11-22"))
        self.assertEqual(rng(last_complete(TODAY, BI)), ("2026-10-26", "2026-11-08"))
        self.assertEqual(rng(containing(date(2025, 12, 30), BI)), ("2025-12-22", "2026-01-04"))

    def test_semimonthly(self):
        self.assertEqual(rng(containing(TODAY, SEMI)), ("2026-11-16", "2026-11-30"))
        self.assertEqual(rng(last_complete(TODAY, SEMI)), ("2026-11-01", "2026-11-15"))
        self.assertEqual(rng(containing(date(2026, 2, 20), SEMI)), ("2026-02-16", "2026-02-28"))

    def test_semimonthly_leap_february(self):
        self.assertEqual(rng(containing(date(2028, 2, 20), SEMI)), ("2028-02-16", "2028-02-29"))

    def test_monthly(self):
        self.assertEqual(rng(containing(TODAY, MONTH)), ("2026-11-01", "2026-11-30"))
        self.assertEqual(rng(last_complete(TODAY, MONTH)), ("2026-10-01", "2026-10-31"))

    def test_monthly_december(self):
        self.assertEqual(rng(containing(date(2026, 12, 31), MONTH)), ("2026-12-01", "2026-12-31"))
        self.assertEqual(rng(last_complete(date(2027, 1, 10), MONTH)), ("2026-12-01", "2026-12-31"))

    def test_last_complete_on_the_day_after_a_period_ends(self):
        self.assertEqual(rng(last_complete(date(2026, 11, 16), MON)), ("2026-11-09", "2026-11-15"))
        # on the last day itself, that period is not yet complete
        self.assertEqual(rng(last_complete(date(2026, 11, 15), MON)), ("2026-11-02", "2026-11-08"))

    def test_next_and_previous(self):
        mar = containing(date(2026, 3, 10), MONTH)
        self.assertEqual(rng(previous_period(mar, MONTH)), ("2026-02-01", "2026-02-28"))
        feb2 = containing(date(2026, 2, 20), SEMI)
        self.assertEqual(rng(next_period(feb2, SEMI)), ("2026-03-01", "2026-03-15"))
        self.assertEqual(rng(next_period(containing(TODAY, BI), BI)), ("2026-11-23", "2026-12-06"))
        self.assertEqual(rng(previous_period(containing(TODAY, MON), MON)), ("2026-11-09", "2026-11-15"))

    def test_resolve_forms(self):
        self.assertEqual(rng(resolve(None, MON, TODAY)), ("2026-11-09", "2026-11-15"))
        self.assertEqual(rng(resolve("last", MON, TODAY)), ("2026-11-09", "2026-11-15"))
        self.assertEqual(rng(resolve("this", MON, TODAY)), ("2026-11-16", "2026-11-22"))
        self.assertEqual(rng(resolve("2026-11-12", MON, TODAY)), ("2026-11-09", "2026-11-15"))
        self.assertEqual(rng(resolve("2026-11-09_2026-11-15", MON, TODAY)), ("2026-11-09", "2026-11-15"))
        self.assertEqual(rng(resolve("2026-W46", MON, TODAY)), ("2026-11-09", "2026-11-15"))
        self.assertEqual(rng(resolve("2026-W46", MONTH, TODAY)), ("2026-11-01", "2026-11-30"))
        with self.assertRaises(ValueError):
            resolve("soon", MON, TODAY)

    def test_resolve_odd_range_kept_and_bad_ranges(self):
        p = resolve("2026-11-10_2026-11-12", MONTH, TODAY)
        self.assertEqual((rng(p), p.id), (("2026-11-10", "2026-11-12"), "2026-11-10_2026-11-12"))
        with self.assertRaises(ValueError):
            resolve("2026-11-15_2026-11-09", MON, TODAY)
        with self.assertRaises(ValueError):
            resolve("2026-13-01", MON, TODAY)
        with self.assertRaises(ValueError):
            resolve("2026-W60", MON, TODAY)

    def test_bad_specs(self):
        with self.assertRaises(ValueError) as cm:
            containing(TODAY, {"type": "yearly"})
        self.assertIn("yearly", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            containing(TODAY, {"type": "biweekly"})
        self.assertIn("anchor", str(cm.exception))

    def test_resolve_bad_spec_is_not_reported_as_bad_date(self):
        with self.assertRaises(ValueError) as cm:
            resolve("2026-11-12", {"type": "yearly"}, TODAY)
        self.assertIn("yearly", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            resolve("2026-11-12", {"type": "biweekly"}, TODAY)
        self.assertIn("anchor", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            resolve("2026-W46", {"type": "yearly"}, TODAY)
        self.assertIn("yearly", str(cm.exception))

    def test_invalid_biweekly_anchor(self):
        with self.assertRaises(ValueError) as cm:
            containing(TODAY, {"type": "biweekly", "anchor": "2026-1-5"})
        self.assertIn("2026-1-5", str(cm.exception))
        self.assertIn("YYYY-MM-DD", str(cm.exception))

    def test_impossible_dates_in_ids_and_weeks_are_friendly(self):
        for arg in ("2026-13-01_2026-13-05", "2026-W60", "2026-13-01"):
            with self.assertRaises(ValueError) as cm:
                resolve(arg, MON, TODAY)
            self.assertIn("unrecognised period", str(cm.exception))

    def test_monthly_leap_february(self):
        self.assertEqual(rng(containing(date(2028, 2, 10), MONTH)), ("2028-02-01", "2028-02-29"))

    def test_bounds_are_local_midnights(self):
        la = ZoneInfo("America/Los_Angeles")
        s, e = bounds(containing(date(2026, 11, 12), MON), la)
        self.assertEqual(s.astimezone(timezone.utc), datetime(2026, 11, 9, 8, tzinfo=timezone.utc))
        self.assertEqual(e.astimezone(timezone.utc), datetime(2026, 11, 16, 8, tzinfo=timezone.utc))
        self.assertIsNotNone(s.tzinfo)

    def test_bounds_across_dst_change(self):
        la = ZoneInfo("America/Los_Angeles")
        s, e = bounds(resolve("2026-10-26_2026-11-01", MON, TODAY), la)
        self.assertEqual(e.astimezone(timezone.utc) - s.astimezone(timezone.utc), timedelta(days=7, hours=1))
        self.assertEqual((s.hour, e.hour, e.day, e.month), (0, 0, 2, 11))

    def test_label(self):
        self.assertEqual(label(containing(date(2026, 11, 12), MON)), "Nov 9 – Nov 15, 2026")
        self.assertEqual(label(resolve("2026-11-09_2026-11-09", MON, TODAY)), "Nov 9, 2026")
        self.assertEqual(label(resolve("2026-10-26_2026-11-01", MON, TODAY)), "Oct 26 – Nov 1, 2026")
        self.assertEqual(label(containing(date(2026, 1, 1), MON)), "Dec 29, 2025 – Jan 4, 2026")


if __name__ == "__main__":
    unittest.main()
