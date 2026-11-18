import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tests import helpers
from ats import rollup
from ats.engine import NON_BILLABLE, Seg, Turn

LA = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
ACME = ("acme", "project-a")
BETA = ("beta", "project-b")


def z(day, hh, mm=0, month=11):
    return datetime(2026, month, day, hh, mm, tzinfo=UTC)


def seg(start, end, key=ACME, weight=1.0, kind="agent"):
    return Seg(start, end, key, weight, kind)


def secs(s):
    return (s.end - s.start).total_seconds() * s.weight


class ClipTests(unittest.TestCase):
    def test_clip_trims_and_drops(self):
        segs = [seg(z(10, 8), z(10, 9)),     # entirely before
                seg(z(10, 9), z(10, 11)),    # straddles start
                seg(z(10, 12), z(10, 13)),   # inside
                seg(z(10, 14), z(10, 16)),   # straddles end
                seg(z(10, 17), z(10, 18))]   # entirely after
        out = rollup.clip(segs, z(10, 10), z(10, 15))
        self.assertEqual([(s.start, s.end) for s in out],
                         [(z(10, 10), z(10, 11)), (z(10, 12), z(10, 13)), (z(10, 14), z(10, 15))])

    def test_clip_keeps_weight_kind_key_and_none_is_unbounded(self):
        s = seg(z(10, 9), z(10, 11), key=BETA, weight=0.5, kind="you")
        (out,) = rollup.clip([s], z(10, 10), None)
        self.assertEqual((out.key, out.weight, out.kind), (BETA, 0.5, "you"))
        self.assertEqual(out.start, z(10, 10))
        self.assertEqual(rollup.clip([s], None, None), [s])
        (out,) = rollup.clip([s], None, z(10, 10))
        self.assertEqual(out.end, z(10, 10))


class SplitDaysTests(unittest.TestCase):
    def test_split_at_local_midnight(self):
        pieces = list(rollup.split_days(z(10, 7, 30), z(10, 8, 30), LA))
        self.assertEqual([p[0] for p in pieces], [date(2026, 11, 9), date(2026, 11, 10)])
        self.assertEqual([(p[2] - p[1]).total_seconds() for p in pieces], [1800, 1800])
        self.assertEqual(pieces[0][2], pieces[1][1])

    def test_dst_day_has_25_hours(self):
        pieces = list(rollup.split_days(z(1, 7), z(2, 8), LA))
        self.assertEqual(len(pieces), 1)
        d, a, b = pieces[0]
        self.assertEqual(d, date(2026, 11, 1))
        self.assertEqual((b.astimezone(UTC) - a.astimezone(UTC)).total_seconds(), 90000)

    def test_dst_day_has_23_hours(self):
        pieces = list(rollup.split_days(z(8, 8, month=3), z(9, 7, month=3), LA))
        self.assertEqual(len(pieces), 1)
        d, a, b = pieces[0]
        self.assertEqual(d, date(2026, 3, 8))
        self.assertEqual((b.astimezone(UTC) - a.astimezone(UTC)).total_seconds(), 82800)

    def test_start_inside_the_repeated_hour_stays_on_one_date(self):
        # 2026-11-01 01:00-02:00 happens twice in Los Angeles: 08:00Z (PDT) and 09:00Z (PST)
        for a in (z(1, 8, 30), z(1, 9, 30)):
            b = z(1, 12)
            pieces = list(rollup.split_days(a, b, LA))
            self.assertEqual([p[0] for p in pieces], [date(2026, 11, 1)], a)
            self.assertEqual(sum((p[2] - p[1]).total_seconds() for p in pieces),
                             (b - a).total_seconds(), a)
            self.assertEqual((pieces[0][1], pieces[-1][2]), (a, b))

    def test_empty_range_yields_nothing(self):
        self.assertEqual(list(rollup.split_days(z(10, 8), z(10, 8), LA)), [])


class TotalsTests(unittest.TestCase):
    def _segs(self):
        return [seg(z(9, 22), z(10, 3)),                    
                seg(z(10, 7, 30), z(10, 8, 30), kind="you"),  # crosses LA midnight
                seg(z(15, 6), z(16, 12), key=BETA)]           # crosses a week boundary

    def test_totals_conserve_seconds_across_boundaries(self):
        segs = self._segs()
        by_day = rollup.totals(segs, LA, "day")
        by_proj = rollup.totals(segs, LA, "project")
        by_week = rollup.totals(segs, LA, "week")

        def grand(t):
            return sum(v["agent"] + v["you"] for v in t.values())
        expected = sum(secs(s) for s in segs)
        self.assertEqual(grand(by_day), expected)
        self.assertEqual(grand(by_proj), expected)
        self.assertEqual(grand(by_week), expected)
        self.assertEqual(set(by_proj), {("", ACME), ("", BETA)})
        self.assertIn(("2026-11-09", ACME), by_day)
        self.assertEqual(by_day[("2026-11-09", ACME)]["you"], 1800)
        self.assertEqual(by_day[("2026-11-10", ACME)]["you"], 1800)
        # 2026-11-15 is a Sunday (ISO week 46); the 16th starts week 47.
        weeks = {b for b, k in by_week if k == BETA}
        self.assertEqual(weeks, {"2026-W46", "2026-W47"})

    def test_weighted_agent_seconds_survive_clipping_and_splitting(self):
        s = seg(z(10, 7), z(10, 9), weight=0.5)  # LA midnight at 08:00Z
        (c,) = rollup.clip([s], z(10, 6), z(10, 12))
        t = rollup.totals([c], LA, "day")
        self.assertEqual(t[("2026-11-09", ACME)]["agent"], 1800)
        self.assertEqual(t[("2026-11-10", ACME)]["agent"], 1800)
        self.assertEqual(t[("2026-11-09", ACME)]["you"], 0)
        # clip at the midnight itself: half the weighted seconds on each side
        left = rollup.clip([s], None, z(10, 8))
        right = rollup.clip([s], z(10, 8), None)
        self.assertEqual(rollup.totals(left, LA, "project")[("", ACME)]["agent"], 1800)
        self.assertEqual(rollup.totals(right, LA, "project")[("", ACME)]["agent"], 1800)

    def test_week_bucket_follows_the_iso_year(self):
        # 2026-12-31 is a Thursday in ISO week 53 of 2026; 2027-01-04 is Monday of 2027-W01
        t = rollup.totals([seg(z(31, 18, month=12), z(31, 19, month=12))], UTC, "week")
        self.assertEqual(list(t), [("2026-W53", ACME)])
        s = seg(datetime(2027, 1, 4, 18, tzinfo=UTC), datetime(2027, 1, 4, 19, tzinfo=UTC))
        self.assertEqual(list(rollup.totals([s], UTC, "week")), [("2027-W01", ACME)])

    def test_spring_forward_day_sums_to_23_hours(self):
        start = datetime(2026, 3, 8, 8, tzinfo=UTC)    # local midnight (PST)
        end = datetime(2026, 3, 9, 7, tzinfo=UTC)      # next local midnight (PDT)
        t = rollup.totals([seg(start, end)], LA, "day")
        self.assertEqual(list(t), [("2026-03-08", ACME)])
        self.assertEqual(t[("2026-03-08", ACME)]["agent"], 82800)

    def test_period_edge_clips_a_turn_in_progress(self):
        # local 23:50 on the 9th to 00:20 on the 10th; period ends at local midnight (08:00Z)
        s = seg(z(10, 7, 50), z(10, 8, 20))
        out = rollup.clip([s], None, z(10, 8))
        t = rollup.totals(out, LA, "project")
        self.assertEqual(t[("", ACME)]["agent"], 600)

    def test_unknown_by_raises(self):
        with self.assertRaises(ValueError):
            rollup.totals([seg(z(10, 9), z(10, 10))], LA, "month")


class BillableTests(unittest.TestCase):
    def test_billable_drops_non_billable_and_filters_client(self):
        segs = [seg(z(10, 9), z(10, 10), key=("acme", "project-a")),
                seg(z(10, 9), z(10, 10), key=("acme", "project-c")),
                seg(z(10, 9), z(10, 10), key=("acme", NON_BILLABLE)),
                seg(z(10, 9), z(10, 10), key=("beta", "project-b")),
                seg(z(10, 9), z(10, 10), key=("beta", NON_BILLABLE))]
        self.assertEqual([s.key for s in rollup.billable(segs)],
                         [("acme", "project-a"), ("acme", "project-c"), ("beta", "project-b")])
        self.assertEqual([s.key for s in rollup.billable(segs, client="acme")],
                         [("acme", "project-a"), ("acme", "project-c")])
        self.assertEqual([s.key for s in rollup.billable(segs, client="acme", project="project-c")],
                         [("acme", "project-c")])
        self.assertEqual(rollup.billable(segs, client="beta", project=NON_BILLABLE), [])


BRK = timedelta(minutes=45)


class WorkSessionTests(unittest.TestCase):
    def test_work_sessions_split_on_break(self):
        segs = [seg(z(10, 9), z(10, 10)),
                seg(z(10, 10, 30), z(10, 11), kind="you"),
                seg(z(10, 12), z(10, 12, 30), key=("acme", "project-c"), weight=0.5)]
        ws = rollup.work_sessions(segs, [], "acme", BRK, LA)
        self.assertEqual([w.key for w in ws], ["S20261110T090000Z", "S20261110T120000Z"])
        first, second = ws
        self.assertEqual((first.start, first.end), (z(10, 9), z(10, 11)))
        self.assertEqual((first.agent, first.you), (3600, 1800))
        self.assertEqual(first.by_project, {"project-a": {"you": 1800, "agent": 3600}})
        self.assertEqual(second.by_project, {"project-c": {"you": 0, "agent": 900}})
        self.assertEqual((second.agent, second.you), (900, 0))
        self.assertEqual(rollup.WorkSession._fields,
                         ("key", "start", "end", "you", "agent", "by_project", "threads", "days"))
        self.assertEqual(len({w.key for w in ws}), len(ws))

    def test_gap_equal_to_break_still_merges(self):
        segs = [seg(z(10, 9), z(10, 10)), seg(z(10, 10, 45), z(10, 11))]
        self.assertEqual(len(rollup.work_sessions(segs, [], "acme", BRK, LA)), 1)

    def test_contained_segment_does_not_shrink_session_end(self):
        segs = [seg(z(10, 9), z(10, 12)), seg(z(10, 10), z(10, 11)), seg(z(10, 12, 30), z(10, 13))]
        ws = rollup.work_sessions(segs, [], "acme", BRK, LA)
        self.assertEqual(len(ws), 1)
        self.assertEqual(ws[0].end, z(10, 13))

    def test_other_clients_time_does_not_join_sessions(self):
        segs = [seg(z(10, 9), z(10, 10)),
                seg(z(10, 10), z(10, 11), key=BETA),
                seg(z(10, 11), z(10, 12))]
        # break shorter than the 1 h of beta time between the acme segments
        ws = rollup.work_sessions(segs, [], "acme", timedelta(minutes=30), LA)
        self.assertEqual([w.key for w in ws], ["S20261110T090000Z", "S20261110T110000Z"])
        self.assertEqual(sum(w.agent for w in ws), 7200)
        wb = rollup.work_sessions(segs, [], "beta", timedelta(minutes=30), LA)
        self.assertEqual([w.key for w in wb], ["S20261110T100000Z"])

    def test_non_billable_segments_are_skipped(self):
        segs = [seg(z(10, 9), z(10, 10), key=("acme", NON_BILLABLE))]
        self.assertEqual(rollup.work_sessions(segs, [], "acme", BRK, LA), [])

    def test_session_threads_and_days(self):
        # LA midnight on the 10th is 08:00Z; session runs 07:30Z-08:30Z
        segs = [seg(z(10, 7, 30), z(10, 8, 30))]
        turns = [Turn(z(10, 7, 20), z(10, 7, 40), ACME, "sess-b"),
                 Turn(z(10, 8), z(10, 8, 10), ("acme", "project-c"), "sess-a"),
                 Turn(z(10, 8), z(10, 8, 10), BETA, "sess-other-client"),
                 Turn(z(10, 8), z(10, 8, 10), ("acme", NON_BILLABLE), "sess-nb"),
                 Turn(z(10, 6), z(10, 7, 30), ACME, "sess-touching-start"),
                 Turn(z(10, 8, 30), z(10, 9), ACME, "sess-touching-end")]
        (w,) = rollup.work_sessions(segs, turns, "acme", BRK, LA)
        self.assertEqual(w.threads, ["sess-a", "sess-b"])
        self.assertEqual(w.days, [date(2026, 11, 9), date(2026, 11, 10)])

    def test_empty_input(self):
        self.assertEqual(rollup.work_sessions([], [], "acme", BRK, LA), [])


if __name__ == "__main__":
    unittest.main()
