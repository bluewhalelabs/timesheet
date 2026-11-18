import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest

from tests import helpers

HOOK = os.path.join(helpers.SCRIPTS, "hook.py")
PY = "/usr/bin/python3"
KEYS = ["ts", "event", "hook", "session", "client", "project", "cwd", "transcript_path"]


def make_cfg(acme_dir, beta_dir, **extra):
    cfg = {"clients": {
        "acme": {"name": "Acme Co", "scope": {"directories": [acme_dir]}},
        "beta": {"name": "Beta", "scope": {"directories": [beta_dir]}},
    }}
    cfg.update(extra)
    return cfg


class HookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self.acme = os.path.join(self.root, "acme-code")
        self.beta = os.path.join(self.root, "beta-code")
        os.makedirs(os.path.join(self.acme, "project-a"))
        os.makedirs(os.path.join(self.beta, "project-b"))
        self.cwd_a = os.path.join(self.acme, "project-a")
        self.cwd_b = os.path.join(self.beta, "project-b")
        helpers.write_config(self.home, make_cfg(self.acme, self.beta))

    def env(self, home=None, config_dir=None, **extra):
        env = dict(os.environ)
        env["TIMESHEET_HOME"] = home or self.home
        env.pop("CLAUDE_CONFIG_DIR", None)
        if config_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = config_dir
        env.update(extra)
        return env

    def run_hook(self, payload, argv=(), env=None, raw=None):
        data = raw if raw is not None else json.dumps(payload).encode()
        return subprocess.run([PY, HOOK] + list(argv), input=data,
                              capture_output=True, env=env or self.env(), timeout=30)

    def events(self, home=None):
        path = os.path.join(home or self.home, "events.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def error_log(self):
        path = os.path.join(self.home, "hook-errors.log")
        if not os.path.exists(path):
            return ""
        with open(path) as f:
            return f.read()

    def payload(self, name, cwd=None, **kw):
        p = {"hook_event_name": name, "session_id": "s1",
             "cwd": cwd or self.cwd_a, "transcript_path": "/x/projects/p/s1.jsonl"}
        p.update(kw)
        return p

    def test_start_end_session_end_are_logged(self):
        for name in ("UserPromptSubmit", "Stop", "StopFailure", "SessionEnd"):
            r = self.run_hook(self.payload(name))
            self.assertEqual(r.returncode, 0)
        evs = self.events()
        self.assertEqual([e["event"] for e in evs], ["start", "end", "end", "session_end"])
        self.assertEqual([e["hook"] for e in evs],
                         ["UserPromptSubmit", "Stop", "StopFailure", "SessionEnd"])
        for e in evs:
            self.assertEqual(list(e.keys()), KEYS)
            self.assertEqual(e["client"], "acme")
            self.assertEqual(e["project"], "project-a")
            self.assertEqual(e["session"], "s1")
            self.assertEqual(e["cwd"], self.cwd_a)
            self.assertEqual(e["transcript_path"], "/x/projects/p/s1.jsonl")
            self.assertRegex(e["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00$")

    def test_events_carry_the_right_client(self):
        self.run_hook(self.payload("Stop", cwd=self.cwd_b))
        self.run_hook(self.payload("Stop", cwd=self.cwd_a))
        evs = self.events()
        self.assertEqual([(e["client"], e["project"]) for e in evs],
                         [("beta", "project-b"), ("acme", "project-a")])

    def test_hook_name_falls_back_to_argv(self):
        p = self.payload("x")
        del p["hook_event_name"]
        self.run_hook(p, argv=["Stop"])
        self.assertEqual([e["event"] for e in self.events()], ["end"])

    def test_cwd_falls_back_to_process_cwd(self):
        p = self.payload("Stop")
        del p["cwd"]
        r = subprocess.run([PY, HOOK], input=json.dumps(p).encode(), cwd=self.cwd_b,
                           capture_output=True, env=self.env(), timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertEqual([e["client"] for e in self.events()], ["beta"])

    def test_unmatched_cwd_logs_nothing(self):
        r = self.run_hook(self.payload("Stop", cwd=self.root))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.error_log(), "")

    def test_unknown_hook_event_logs_nothing(self):
        r = self.run_hook(self.payload("PreToolUse"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.error_log(), "")

    def test_garbage_stdin_exits_zero_and_logs_nothing(self):
        for raw in (b"not json", b"", b"[]", b'"str"', b"null"):
            r = self.run_hook(None, raw=raw)
            self.assertEqual(r.returncode, 0, raw)
            self.assertEqual(r.stdout, b"")
        self.assertEqual(self.events(), [])
        self.assertEqual(self.error_log(), "")

    def test_account_from_env_and_from_transcript_path(self):
        cfg = {"clients": {
            "acme": {"name": "Acme Co", "scope": {"accounts": ["~/.claude-acme"]}},
            "beta": {"name": "Beta", "scope": {"accounts": ["~/.claude-beta"]}},
        }}
        helpers.write_config(self.home, cfg)
        home_dir = os.path.expanduser("~")
        # from CLAUDE_CONFIG_DIR
        self.run_hook(self.payload("Stop", cwd=self.root),
                      env=self.env(config_dir=os.path.join(home_dir, ".claude-acme")))
        # from the transcript path (no env var)
        self.run_hook(self.payload(
            "Stop", cwd=self.root,
            transcript_path=os.path.join(home_dir, ".claude-beta", "projects", "p", "s.jsonl")))
        # default account matches nobody
        self.run_hook(self.payload("Stop", cwd=self.root,
                                   transcript_path="/elsewhere/s.jsonl"))
        self.assertEqual([e["client"] for e in self.events()], ["acme", "beta"])

    def test_account_uses_the_last_projects_segment_of_the_transcript_path(self):
        acct = os.path.join(self.root, "projects", ".claude-clientx")
        helpers.write_config(self.home, {"clients": {
            "acme": {"name": "Acme Co", "scope": {"accounts": [acct]}},
            "beta": {"name": "Beta", "scope": {"accounts": [os.path.join(self.root, "projects")]}},
        }})
        self.run_hook(self.payload(
            "Stop", cwd=self.root,
            transcript_path=os.path.join(acct, "projects", "-some-slug", "abc.jsonl")))
        self.assertEqual([e["client"] for e in self.events()], ["acme"])

    def test_error_log_is_private(self):
        os.unlink(os.path.join(self.home, "config.json"))
        self.run_hook(self.payload("Stop"))
        path = os.path.join(self.home, "hook-errors.log")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_error_log_writes_non_ascii_text_as_utf8(self):
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"timezone": "Caf\u00e9/\u2713"}, f)
        for env in (self.env(LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0"),
                    self.env()):
            r = self.run_hook(self.payload("Stop"), env=env)
            self.assertEqual((r.returncode, r.stdout), (0, b""))
        with open(os.path.join(self.home, "hook-errors.log"), "rb") as f:
            data = f.read()
        self.assertEqual(data.count("Caf\u00e9/\u2713".encode("utf-8")), 2)

    def test_missing_config_exits_zero_and_writes_error_log(self):
        os.unlink(os.path.join(self.home, "config.json"))
        r = self.run_hook(self.payload("Stop"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, b"")
        self.assertEqual(self.events(), [])
        lines = self.error_log().splitlines()
        self.assertEqual(len(lines), 1)
        ts, msg = lines[0].split("\t", 1)
        self.assertRegex(ts, r"^\d{4}-\d\d-\d\dT")
        self.assertTrue(msg.startswith("ConfigError: "), msg)

    def test_invalid_config_exits_zero_and_writes_error_log(self):
        with open(os.path.join(self.home, "config.json"), "w") as f:
            f.write("{not json")
        r = self.run_hook(self.payload("Stop"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.events(), [])
        self.assertIn("ConfigError: ", self.error_log())

    def unattributed(self):
        path = os.path.join(self.home, "unattributed.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_unreadable_config_parks_the_event_without_prompt_text(self):
        with open(os.path.join(self.home, "config.json"), "w") as f:
            f.write("{not json")
        acct = os.path.join(self.root, ".claude-x")
        r = self.run_hook(self.payload("UserPromptSubmit", prompt="SECRET PROMPT"),
                          env=self.env(config_dir=acct))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        r = self.run_hook(self.payload("Stop"))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        parked = self.unattributed()
        self.assertEqual([list(e) for e in parked],
                         [["ts", "event", "hook", "session", "cwd", "transcript_path",
                           "account"]] * 2)
        self.assertEqual([(e["event"], e["hook"], e["session"], e["cwd"], e["account"])
                          for e in parked],
                         [("start", "UserPromptSubmit", "s1", self.cwd_a, acct),
                          ("end", "Stop", "s1", self.cwd_a, "/x")])   # from transcript_path
        with open(os.path.join(self.home, "unattributed.jsonl")) as f:
            self.assertNotIn("SECRET", f.read())
        mode = stat.S_IMODE(os.stat(os.path.join(self.home, "unattributed.jsonl")).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.error_log().count("ConfigError: "), 2)

    def test_missing_config_parks_nothing(self):
        os.unlink(os.path.join(self.home, "config.json"))
        self.assertEqual(self.run_hook(self.payload("Stop")).returncode, 0)
        self.assertEqual(self.unattributed(), [])

    def test_unwritable_park_file_still_exits_zero_and_logs(self):
        with open(os.path.join(self.home, "config.json"), "w") as f:
            f.write("{not json")
        os.mkdir(os.path.join(self.home, "unattributed.jsonl"))       # cannot be appended to
        r = self.run_hook(self.payload("Stop"))
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertIn("ConfigError: ", self.error_log())

    def test_error_log_is_truncated_to_last_200_lines(self):
        os.unlink(os.path.join(self.home, "config.json"))
        path = os.path.join(self.home, "hook-errors.log")
        with open(path, "w") as f:
            for i in range(2000):
                f.write("2000-01-01T00:00:00\tOld: line %05d padding padding padding\n" % i)
        self.assertGreater(os.path.getsize(path), 64 * 1024)
        self.run_hook(self.payload("Stop"))
        lines = self.error_log().splitlines()
        self.assertLessEqual(len(lines), 201)
        self.assertGreaterEqual(len(lines), 200)
        self.assertIn("ConfigError", lines[-1])
        self.assertIn("line 01999", lines[-2])

    def test_no_stdout_ever(self):
        for payload in (self.payload("Stop"), self.payload("Stop", cwd=self.root),
                        self.payload("Nope")):
            self.assertEqual(self.run_hook(payload).stdout, b"")
        os.unlink(os.path.join(self.home, "config.json"))
        self.assertEqual(self.run_hook(self.payload("Stop")).stdout, b"")

    def test_unwritable_home_still_exits_zero(self):
        blocker = os.path.join(self.root, "file")
        with open(blocker, "w") as f:
            f.write("x")
        r = self.run_hook(self.payload("Stop"), env=self.env(home=os.path.join(blocker, "x")))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, b"")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file modes")
    def test_readonly_home_with_valid_config_still_exits_zero(self):
        os.chmod(self.home, 0o500)
        self.addCleanup(os.chmod, self.home, 0o700)
        r = self.run_hook(self.payload("Stop"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, b"")
        self.assertEqual(self.events(), [])

    def test_no_prompt_text_in_record(self):
        self.run_hook(self.payload("UserPromptSubmit", prompt="SECRET"))
        with open(os.path.join(self.home, "events.jsonl")) as f:
            content = f.read()
        self.assertTrue(content.strip())
        self.assertNotIn("SECRET", content)

    def test_events_file_is_private(self):
        self.run_hook(self.payload("Stop"))
        mode = stat.S_IMODE(os.stat(os.path.join(self.home, "events.jsonl")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_directory_scope_makes_no_git_call(self):
        bindir = os.path.join(self.root, "bin")
        os.makedirs(bindir)
        marker = os.path.join(self.root, "git-called")
        fake = os.path.join(bindir, "git")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\ntouch '%s'\nexit 1\n" % marker)
        os.chmod(fake, 0o755)
        env = self.env(PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
        r = self.run_hook(self.payload("Stop"), env=env)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual([e["client"] for e in self.events()], ["acme"])

    def test_concurrent_appends_do_not_interleave(self):
        procs = [subprocess.Popen([PY, HOOK], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=self.env()) for _ in range(20)]
        data = json.dumps(self.payload("Stop")).encode()
        outs = []
        threads = [threading.Thread(target=lambda p=p: outs.append(p.communicate(data)))
                   for p in procs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([p.returncode for p in procs], [0] * 20)
        self.assertEqual([o[0] for o in outs], [b""] * 20)
        with open(os.path.join(self.home, "events.jsonl")) as f:
            lines = f.read().split("\n")
        self.assertEqual(lines[-1], "")
        lines = lines[:-1]
        self.assertEqual(len(lines), 20)
        for line in lines:
            self.assertEqual(json.loads(line)["client"], "acme")

    def test_hooks_json_wires_four_events_to_hook_py(self):
        with open(os.path.join(helpers.REPO, "hooks", "hooks.json")) as f:
            data = json.load(f)
        hooks = data["hooks"]
        tracking = ["SessionEnd", "Stop", "StopFailure", "UserPromptSubmit"]
        # SessionStart runs the reminder script, not the tracking hook (see test_status.py)
        self.assertEqual(sorted(n for n in hooks if n != "SessionStart"), tracking)
        for name in tracking:
            groups = hooks[name]
            cmds = [h for g in groups for h in g["hooks"]]
            self.assertEqual(cmds, [{"type": "command",
                                     "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hook.py"'}], name)


if __name__ == "__main__":
    unittest.main()
