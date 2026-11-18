import os
import unittest

from tests import helpers
from ats import health


class UnknownClientTests(unittest.TestCase):
    def test_counts_and_warnings_per_unknown_key(self):
        cfg = {"clients": {"acme": {}, "beta": {}}}
        evs = [{"client": "acme"}, {"client": "old"}, {"client": "old"}, {"client": "gone"}]
        self.assertEqual(health.unknown_clients(cfg, evs), {"old": 2, "gone": 1})
        self.assertEqual(health.unknown_clients(cfg, [{"client": "beta"}]), {})
        self.assertEqual(health.unknown_client_warnings(cfg, evs, [{"client": "old"}]),
                         ['2 event(s) logged for unknown client "old" (renamed or removed '
                          'from config?)',
                          '1 event(s) logged for unknown client "gone" (renamed or removed '
                          'from config?)',
                          '1 manual entry for unknown client "old"'])
        self.assertEqual(health.unknown_client_warnings(cfg, [], [{"client": "x"}] * 3),
                         ['3 manual entries for unknown client "x"'])


class HealthTests(unittest.TestCase):
    def run_in_home(self, text=None):
        with helpers.temp_home() as home:
            os.environ["TIMESHEET_HOME"] = home
            self.addCleanup(os.environ.pop, "TIMESHEET_HOME", None)
            if text is not None:
                with open(os.path.join(home, "hook-errors.log"), "wb") as f:
                    f.write(text)
            return health.hook_errors(), health.hook_error_warning(True), \
                health.hook_error_warning(False)

    def test_missing_and_empty_log(self):
        self.assertEqual(self.run_in_home(), ((0, ""), None, None))
        self.assertEqual(self.run_in_home(b"\n  \n"), ((0, ""), None, None))

    def test_counts_lines_and_reports_last(self):
        errs, with_last, without = self.run_in_home(b"first\n\nlast \xff end\n")
        self.assertEqual(errs[0], 2)
        self.assertTrue(errs[1].startswith("last "))
        self.assertTrue(with_last.startswith("hook errors logged: 2; last: last "))
        self.assertEqual(without, 'hook errors logged: 2 (run "timesheet doctor" for details)')

    def test_skipped_warning(self):
        self.assertIsNone(health.skipped_warning(0, "events.jsonl"))
        self.assertEqual(health.skipped_warning(3, "manual.jsonl"),
                         "3 unreadable line(s) skipped in manual.jsonl")


if __name__ == "__main__":
    unittest.main()
