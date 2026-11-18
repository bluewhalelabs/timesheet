import json
import os
import unittest

from tests import helpers
from tests.test_timesheet_cli import CliCase, two_clients, event

from ats import events as ev


def legacy(ts, kind, session, project="project-a", hook="stop", **extra):
    e = {"ts": ts, "event": kind, "hook": hook, "session": session,
         "project": project, "cwd": "/nowhere"}
    e.update(extra)
    return e


class ImportCase(CliCase):
    def src_path(self, lines):
        p = os.path.join(self.home, "legacy.jsonl")
        with open(p, "w") as f:
            for line in lines:
                f.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
        return p

    def dest_lines(self):
        with open(os.path.join(self.home, "events.jsonl")) as f:
            return [l for l in f.read().splitlines() if l.strip()]

    def imp(self, src, client="acme", *extra):
        return self.run_cli("import", "--from", src, "--client", client, *extra)


class ImportTests(ImportCase):
    def test_import_adds_client_and_keeps_backfill_marker(self):
        self.setup(two_clients())
        src = self.src_path([
            legacy("2026-11-10T09:00:00+00:00", "start", "s1"),
            legacy("2026-11-10T09:10:00Z", "end", "s1", hook="backfill", extra="x")])
        r = self.imp(src)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "read 2, imported 2, duplicates 0, skipped 0")
        recs = [json.loads(l) for l in self.dest_lines()]
        self.assertEqual([x["client"] for x in recs], ["acme", "acme"])
        self.assertEqual(recs[1]["hook"], "backfill")
        self.assertEqual(recs[0]["transcript_path"], "")
        self.assertEqual(list(recs[1]), ["ts", "event", "hook", "session", "client",
                                         "project", "cwd", "transcript_path", "extra"])
        self.assertEqual(recs[1]["ts"], "2026-11-10T09:10:00Z")

    def test_import_twice_adds_nothing(self):
        self.setup(two_clients())
        src = self.src_path([legacy("2026-11-10T09:00:00Z", "start", "s1"),
                             legacy("2026-11-10T09:10:00Z", "end", "s1")])
        self.imp(src)
        r = self.imp(src)
        self.assertEqual(r.stdout.strip(), "read 2, imported 0, duplicates 2, skipped 0")
        self.assertEqual(len(self.dest_lines()), 2)

    def test_import_after_new_events_adds_only_new(self):
        self.setup(two_clients())
        rows = [legacy("2026-11-10T09:00:00Z", "start", "s1"),
                legacy("2026-11-10T09:10:00Z", "end", "s1")]
        src = self.src_path(rows)
        self.imp(src)
        rows.append(legacy("2026-11-11T09:00:00Z", "start", "s2"))
        src = self.src_path(rows)
        r = self.imp(src)
        self.assertEqual(r.stdout.strip(), "read 3, imported 1, duplicates 2, skipped 0")
        self.assertEqual(len(self.dest_lines()), 3)

    def test_dry_run_writes_nothing(self):
        self.setup(two_clients())
        src = self.src_path([legacy("2026-11-10T09:00:00Z", "start", "s1")])
        r = self.imp(src, "acme", "--dry-run")
        self.assertEqual(r.stdout.strip(),
                         "dry run: read 1, imported 1, duplicates 0, skipped 0")
        self.assertEqual(self.dest_lines(), [])

    def test_garbage_source_lines_are_counted_as_skipped(self):
        self.setup(two_clients())
        src = self.src_path([
            "not json", "[1, 2]", "", "   ",
            {"event": "start", "session": "s1"},
            legacy("not a time", "start", "s1"),
            legacy("2026-11-10T09:00:00Z", "pause", "s1"),
            legacy("2026-11-10T09:00:00Z", "start", "s1")])
        r = self.imp(src)
        self.assertEqual(r.stdout.strip(), "read 6, imported 1, duplicates 0, skipped 5")

    def test_z_and_offset_are_the_same_instant(self):
        self.setup(two_clients())
        src = self.src_path([legacy("2026-11-10T09:00:00Z", "start", "s1"),
                             legacy("2026-11-10T09:00:00+00:00", "start", "s1")])
        r = self.imp(src)
        self.assertEqual(r.stdout.strip(), "read 2, imported 1, duplicates 1, skipped 0")

    def test_other_clients_events_are_never_changed_or_duplicated(self):
        beta = event("2026-11-10T09:00:00Z", "start", "s1", "project-a", client="beta")
        self.setup(two_clients(), [beta])
        before = self.dest_lines()
        src = self.src_path([legacy("2026-11-10T09:00:00Z", "start", "s1"),
                             legacy("2026-11-10T09:10:00Z", "end", "s1")])
        r = self.imp(src, "acme")
        self.assertEqual(r.stdout.strip(), "read 2, imported 1, duplicates 1, skipped 0")
        after = self.dest_lines()
        self.assertEqual(after[:1], before)
        self.assertEqual(len(after), 2)
        self.assertEqual(sum(1 for l in after if json.loads(l)["client"] == "beta"), 1)

    def test_existing_client_is_replaced_and_transcript_path_kept(self):
        self.setup(two_clients())
        src = self.src_path([legacy("2026-11-10T09:00:00Z", "start", "s1",
                                    client="beta", transcript_path="/t.jsonl")])
        self.imp(src, "acme")
        rec = json.loads(self.dest_lines()[0])
        self.assertEqual((rec["client"], rec["transcript_path"]), ("acme", "/t.jsonl"))

    def test_unknown_client_and_missing_source_exit_2(self):
        self.setup(two_clients())
        src = self.src_path([legacy("2026-11-10T09:00:00Z", "start", "s1")])
        r = self.imp(src, "nope")
        self.assertEqual(r.returncode, 2)
        self.assertIn("acme", r.stderr)
        self.assertIn("beta", r.stderr)
        r = self.imp(os.path.join(self.home, "missing.jsonl"))
        self.assertEqual(r.returncode, 2)

    def test_source_equal_to_destination_is_refused(self):
        self.setup(two_clients(), [event("2026-11-10T09:00:00Z", "start", "s1", "p")])
        r = self.imp(os.path.join(self.home, "events.jsonl"))
        self.assertEqual(r.returncode, 2)
        link = os.path.join(self.home, "link.jsonl")
        os.symlink(os.path.join(self.home, "events.jsonl"), link)
        with self.assertRaises(ValueError):
            ev.import_legacy(link, "acme", dest=os.path.join(self.home, "events.jsonl"))

    def test_non_string_event_in_destination_or_source_does_not_crash(self):
        dest = os.path.join(self.home, "events.jsonl")
        self.setup(two_clients())
        with open(dest, "w") as f:
            f.write(json.dumps({"ts": "2026-11-10T09:00:00Z", "event": ["x"],
                                "session": "s"}) + "\n")
            f.write(json.dumps({"ts": "2026-11-10T09:00:00Z", "event": {"a": 1},
                                "session": ["s"]}) + "\n")
        src = self.src_path([
            legacy("2026-11-10T09:00:00Z", ["start"], "s1"),
            legacy("2026-11-10T09:00:00Z", "start", ["s1"]),
            legacy("2026-11-10T09:01:00Z", "start", {"k": 1})])
        r = self.imp(src)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "read 3, imported 2, duplicates 0, skipped 1")

    def test_duplicate_inside_the_source_is_counted(self):
        self.setup(two_clients())
        row = legacy("2026-11-10T09:00:00Z", "start", "s1")
        src = self.src_path([row, dict(row), legacy("2026-11-10T09:10:00Z", "end", "s1")])
        r = self.imp(src)
        self.assertEqual(r.stdout.strip(), "read 3, imported 2, duplicates 1, skipped 0")
        self.assertEqual(len(self.dest_lines()), 2)

    def test_resume_after_partial_import_imports_only_the_rest(self):
        self.setup(two_clients())
        rows = [legacy("2026-11-10T09:%02d:00Z" % i, "start", "s%d" % i) for i in range(6)]
        first = self.src_path(rows[:3])
        self.assertEqual(self.imp(first).returncode, 0)
        src = self.src_path(rows)
        r = self.imp(src)
        self.assertEqual(r.stdout.strip(), "read 6, imported 3, duplicates 3, skipped 0")
        self.assertEqual(len(self.dest_lines()), 6)


if __name__ == "__main__":
    unittest.main()
