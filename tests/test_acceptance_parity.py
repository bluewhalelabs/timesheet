"""The acceptance script, run on a synthetic prototype log (skipped without the prototype)."""
import json
import os
import random
import subprocess
import tempfile
import unittest
from datetime import timezone

from tests import helpers
from tests.test_parity import PREFIX, REFERENCE, random_log, thresholds

from ats import events as ev

SCRIPT = os.path.join(helpers.REPO, "tests", "acceptance_parity.py")
PY = "/usr/bin/python3"


def unique(events):
    """The import skips repeats of (session, event, instant); keep the generated log free of them."""
    seen, out = set(), []
    for e in events:
        key = (e["session"], e["event"], e["_t"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def write_log(path, events):
    with open(path, "w") as f:
        for e in events:
            row = {k: v for k, v in e.items() if k not in ("_t", "client")}
            f.write(json.dumps(row) + "\n")


@unittest.skipUnless(os.path.exists(REFERENCE), "private prototype not present")
class AcceptanceParityTests(unittest.TestCase):
    def build(self, seed):
        rng = random.Random(seed)
        th = thresholds(rng)
        events, now = random_log(rng)
        events = unique(events)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.realpath(tmp.name)
        self.old_log = os.path.join(root, "old-events.jsonl")
        self.old_config = os.path.join(root, "old-config.json")
        write_log(self.old_log, events)
        with open(self.old_config, "w") as f:
            json.dump(dict(th, exclude_sessions=[PREFIX]), f)
        self.home = os.path.join(root, "home")
        os.mkdir(self.home)
        helpers.write_config(self.home, {
            "defaults": th,
            "clients": {"acme": {"overrides": {"non_billable_sessions": [PREFIX]}}}})
        self.log = os.path.join(self.home, "events.jsonl")
        ev.import_legacy(self.old_log, "acme", self.log)
        self.now = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.events = events

    def run_script(self, *extra, **over):
        args = {"--reference": REFERENCE, "--old-log": self.old_log,
                "--old-config": self.old_config, "--home": self.home,
                "--client": "acme", "--now": self.now}
        args.update(over)
        argv = [PY, SCRIPT]
        for k, v in args.items():
            if v is not None:
                argv += [k, v]
        env = {k: v for k, v in os.environ.items() if k != "TIMESHEET_HOME"}
        return subprocess.run(argv + list(extra), capture_output=True, text=True,
                              env=env, timeout=60)

    def test_agrees_on_a_synthetic_log(self):
        for seed in (0, 1, 2):
            self.build(seed)
            r = self.run_script()
            with self.subTest(seed=seed):
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(r.stderr, "")
                self.assertIn("agent ref", r.stdout)
                self.assertIn("TOTAL", r.stdout)
                self.assertIn("capped turns:", r.stdout)
                self.assertIn("events loaded: reference %d, plugin %d"
                              % (len(self.events), len(self.events)), r.stdout)

    def test_a_dropped_event_is_reported_with_the_project(self):
        self.build(0)
        with open(self.log) as f:
            rows = [json.loads(line) for line in f]
        # drop the first end event: its turn runs on to the cap instead
        victim = next(i for i, e in enumerate(rows) if e["event"] == "end")
        project = rows[victim]["project"]
        del rows[victim]
        with open(self.log, "w") as f:
            for e in rows:
                f.write(json.dumps(e) + "\n")
        r = self.run_script()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("MISMATCH", r.stdout)
        self.assertIn(project + ":", r.stdout.split("MISMATCH")[1])

    def test_bad_arguments_and_missing_files_exit_2(self):
        self.build(0)
        for over in ({"--old-log": os.path.join(self.home, "missing.jsonl")},
                     {"--reference": os.path.join(self.home, "missing.py")},
                     {"--home": os.path.join(self.home, "missing")},
                     {"--now": "not a time"},
                     {"--client": "nobody"}):
            with self.subTest(over=over):
                r = self.run_script(**over)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.run_script(**{"--client": None}).returncode, 2)

    def test_importing_the_script_does_nothing(self):
        r = subprocess.run([PY, "-c", "import importlib.util as u; "
                            "s = u.spec_from_file_location('x', %r); "
                            "m = u.module_from_spec(s); s.loader.exec_module(m)" % SCRIPT],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
