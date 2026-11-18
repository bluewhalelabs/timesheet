import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from tests import helpers
from ats import events
from ats.events import load_events, parse_ts


class ParseTsTests(unittest.TestCase):
    def test_parse_ts_accepts_z_offset_and_naive(self):
        want = datetime(2026, 11, 10, 9, 0, 0, 123000, tzinfo=timezone.utc)
        for s in ("2026-11-10T09:00:00.123Z",
                  "2026-11-10T09:00:00.123+00:00",
                  "2026-11-10T04:00:00.123-05:00",
                  "2026-11-10T09:00:00.123"):
            t = parse_ts(s)
            self.assertEqual(t, want, s)
            self.assertEqual(t.utcoffset(), timedelta(0), s)
        for bad in ("nope", "", None, 12):
            with self.assertRaises(ValueError):
                parse_ts(bad)

    def test_extreme_timestamp_is_a_value_error(self):
        for s in ("0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"):
            with self.assertRaises(ValueError):
                parse_ts(s)


def _line(ts, event="start", client="acme", **extra):
    rec = {"ts": ts, "event": event, "session": "s1", "client": client, "project": "project-a"}
    rec.update(extra)
    return json.dumps(rec)


class AppendEventTests(unittest.TestCase):
    def test_bare_filename_path_works(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.getcwd()
            os.chdir(d)
            try:
                events.append_event({"a": 1}, "events.jsonl")
                with open("events.jsonl") as f:
                    self.assertEqual(json.loads(f.read()), {"a": 1})
            finally:
                os.chdir(old)

    def test_torn_last_line_is_not_glued_to_the_new_record(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "events.jsonl")
            with open(path, "w") as f:
                f.write('{"a": 1}\n{"torn": ')
            events.append_event({"b": 2}, path)
            events.append_event({"c": 3}, path)
            with open(path) as f:
                lines = f.read().split("\n")
        self.assertEqual(lines, ['{"a": 1}', '{"torn": ', '{"b": 2}', '{"c": 3}', ""])

    def test_empty_file_gets_no_leading_newline(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "events.jsonl")
            open(path, "w").close()
            events.append_event({"a": 1}, path)
            with open(path) as f:
                self.assertEqual(f.read(), '{"a": 1}\n')


class LoadEventsTests(unittest.TestCase):
    def _write(self, home, lines):
        path = os.path.join(home, "events.jsonl")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def test_load_skips_and_counts_bad_lines(self):
        with helpers.temp_home() as home:
            no_client = json.loads(_line("2026-11-10T09:05:00Z"))
            del no_client["client"]
            self._write(home, [
                _line("2026-11-10T09:00:00Z"),
                "",
                "garbage",
                json.dumps({"ts": "nope", "client": "acme"}),
                json.dumps(no_client),
                _line("2026-11-10T09:10:00Z", event="end"),
            ])
            evs, skipped = load_events()
        self.assertEqual(skipped, 3)
        self.assertEqual([e["event"] for e in evs], ["start", "end"])
        self.assertEqual(evs[0]["_t"], datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc))

    def test_extreme_timestamp_line_is_skipped(self):
        with helpers.temp_home() as home:
            path = self._write(home, [
                _line("0001-01-01T00:00:00+14:00"),
                _line("2026-11-10T09:00:00Z"),
            ])
            evs, skipped = load_events(path)
        self.assertEqual((len(evs), skipped), (1, 1))

    def test_events_sorted_by_time(self):
        with helpers.temp_home() as home:
            path = self._write(home, [
                _line("2026-11-10T09:10:00Z", session="b"),
                _line("2026-11-10T04:05:00-05:00", session="a"),
                _line("2026-11-10T09:10:00Z", session="c"),
                _line("2026-11-10T08:00:00", session="z"),
            ])
            evs, skipped = load_events(path)
        self.assertEqual(skipped, 0)
        self.assertEqual([e["session"] for e in evs], ["z", "a", "b", "c"])
        self.assertTrue(all(e["_t"].utcoffset() == timedelta(0) for e in evs))

    def test_session_and_project_are_normalised_to_strings(self):
        with helpers.temp_home() as home:
            base = {"event": "start", "client": "acme"}
            recs = [
                dict(base, ts="2026-11-10T09:00:00Z"),                                  # both missing
                dict(base, ts="2026-11-10T09:01:00Z", session=None, project=None),
                dict(base, ts="2026-11-10T09:02:00Z", session=42, project=7),
                dict(base, ts="2026-11-10T09:03:00Z", session="s1", project="project-a"),
            ]
            path = self._write(home, [json.dumps(r) for r in recs])
            evs, skipped = load_events(path)
        self.assertEqual(skipped, 0)
        self.assertEqual([(e["session"], e["project"]) for e in evs],
                         [("", "(unknown)"), ("", "(unknown)"), ("42", "7"), ("s1", "project-a")])

    def test_non_string_or_empty_client_is_skipped_and_counted(self):
        with helpers.temp_home() as home:
            path = self._write(home, [
                json.dumps({"ts": "2026-11-10T09:00:00Z", "client": None}),
                json.dumps({"ts": "2026-11-10T09:00:00Z", "client": 5}),
                json.dumps({"ts": "2026-11-10T09:00:00Z", "client": ""}),
                _line("2026-11-10T09:00:00Z"),
            ])
            evs, skipped = load_events(path)
        self.assertEqual((len(evs), skipped), (1, 3))

    def test_missing_file_is_empty(self):
        with helpers.temp_home():
            self.assertEqual(load_events(), ([], 0))


if __name__ == "__main__":
    unittest.main()
