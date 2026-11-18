import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from tests import helpers  # noqa: F401  (puts scripts/ on sys.path)
from ats import transcripts

UTC = timezone.utc


def ts(h, m, s=0):
    return "2026-03-02T%02d:%02d:%02d.000Z" % (h, m, s)


def user(t, content, **kw):
    e = {"type": "user", "timestamp": t, "isSidechain": False,
         "message": {"role": "user", "content": content}}
    e.update(kw)
    return e


def assistant(t, blocks, **kw):
    e = {"type": "assistant", "timestamp": t, "isSidechain": False,
         "message": {"role": "assistant", "content": blocks}}
    e.update(kw)
    return e


def text(s):
    return {"type": "text", "text": s}


TOOL_USE = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
TOOL_RESULT = {"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": False}


def write_jsonl(path, entries, extra_lines=()):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
        for line in extra_lines:
            f.write(line + "\n")
    return path


def dt(h, m, s=0):
    return datetime(2026, 3, 2, h, m, s, tzinfo=UTC)


class TranscriptCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


class LocateTests(TranscriptCase):
    def setUp(self):
        super().setUp()
        self.home = self.path("home")
        os.makedirs(self.home)
        env = {"HOME": self.home}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def ev(self, session, path, minute):
        return {"session": session, "transcript_path": path, "_t": dt(9, minute)}

    def test_locate_prefers_logged_path(self):
        old = write_jsonl(self.path("a", "old.jsonl"), [])
        new = write_jsonl(self.path("a", "new.jsonl"), [])
        evs = [self.ev("s1", new, 5), self.ev("s1", old, 1), self.ev("s1", "", 9),
               self.ev("other", old, 7)]
        self.assertEqual(transcripts.locate("s1", evs, {"clients": {}}),
                         os.path.realpath(new))

    def test_locate_tolerates_events_without_t(self):
        old = write_jsonl(self.path("a", "old.jsonl"), [])
        new = write_jsonl(self.path("a", "new.jsonl"), [])
        no_t = {"session": "s1", "transcript_path": old}
        evs = [no_t, self.ev("s1", new, 1)]
        self.assertEqual(transcripts.locate("s1", evs, {"clients": {}}), os.path.realpath(new))
        self.assertEqual(transcripts.locate("s1", [no_t], {"clients": {}}),
                         os.path.realpath(old))

    def test_locate_searches_client_account_before_env_and_default(self):
        acct = self.path("acct-clientx")
        envdir = self.path("envdir")
        client_hit = write_jsonl(os.path.join(acct, "projects", "p", "s4.jsonl"), [])
        write_jsonl(os.path.join(envdir, "projects", "p", "s4.jsonl"), [])
        write_jsonl(os.path.join(self.home, ".claude", "projects", "p", "s4.jsonl"), [])
        cfg = {"clients": {"acme": {"scope": {"accounts": [acct]}}}}
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": envdir}):
            self.assertEqual(transcripts.locate("s4", [], cfg), os.path.realpath(client_hit))

    def test_locate_falls_back_to_search_by_session_id(self):
        acct = self.path("acct-clientx")
        found = write_jsonl(os.path.join(acct, "projects", "project-a", "s2.jsonl"), [])
        cfg = {"clients": {"acme": {"scope": {"accounts": [acct]}}}}
        # logged path no longer exists -> fall back to the search
        evs = [self.ev("s2", self.path("gone.jsonl"), 1)]
        self.assertEqual(transcripts.locate("s2", evs, cfg), os.path.realpath(found))

    def test_locate_searches_env_dir_then_default(self):
        envdir = self.path("envdir")
        default = write_jsonl(os.path.join(self.home, ".claude", "projects", "p", "s3.jsonl"), [])
        self.assertEqual(transcripts.locate("s3", [], {"clients": {}}), os.path.realpath(default))
        viaenv = write_jsonl(os.path.join(envdir, "projects", "p", "s3.jsonl"), [])
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": envdir}):
            self.assertEqual(transcripts.locate("s3", [], {"clients": {}}),
                             os.path.realpath(viaenv))

    def test_locate_returns_none_when_missing(self):
        cfg = {"clients": {"acme": {"scope": {"accounts": [self.path("nowhere")]}}}}
        self.assertIsNone(transcripts.locate("nosuch", [self.ev("nosuch", "", 1)], cfg))

    def test_locate_rejects_session_id_with_path_separator(self):
        acct = self.path("acct")
        write_jsonl(os.path.join(acct, "projects", "p", "x.jsonl"), [])
        write_jsonl(os.path.join(acct, "secret.jsonl"), [])
        cfg = {"clients": {"acme": {"scope": {"accounts": [acct]}}}}
        for bad in ("../secret", "p/x", "..", "", "a/../b"):
            self.assertIsNone(transcripts.locate(bad, [], cfg), bad)


class DigestTests(TranscriptCase):
    def digest(self, entries, start=None, end=None, extra_lines=(), **kw):
        p = write_jsonl(self.path("t.jsonl"), entries, extra_lines)
        return transcripts.digest(p, start or dt(9, 0), end or dt(10, 0), **kw)

    def test_digest_keeps_prompts_and_final_replies_in_window(self):
        out = self.digest([
            user(ts(9, 5), "Add a CSV export"),
            assistant(ts(9, 6), [text("Looking at the code")]),
            assistant(ts(9, 9), [text("Done: export added"), text("Tests pass")]),
            user(ts(9, 20), [text("Now   rename\n the flag")]),
            assistant(ts(9, 21), [{"type": "thinking", "thinking": "secret"}, text("Renamed")]),
        ])
        self.assertEqual(out.splitlines(), [
            "[09:05] USER: Add a CSV export",
            "[09:09] AGENT: Done: export added Tests pass",
            "[09:20] USER: Now rename the flag",
            "[09:21] AGENT: Renamed",
        ])

    def test_digest_skips_tool_calls_sidechains_and_meta(self):
        out = self.digest([
            user(ts(9, 5), "Add a CSV export"),
            assistant(ts(9, 6), [text("Earlier text reply")]),
            assistant(ts(9, 7), [TOOL_USE]),
            user(ts(9, 8), [TOOL_RESULT]),
            assistant(ts(9, 9), [{"type": "thinking", "thinking": "hm"}, TOOL_USE]),
            assistant(ts(9, 10), [text("sidechain reply")], isSidechain=True),
            user(ts(9, 11), "sidechain prompt", isSidechain=True),
            user(ts(9, 12), "meta prompt", isMeta=True),
            assistant(ts(9, 13), [text("meta reply")], isMeta=True),
        ])
        self.assertEqual(out.splitlines(), [
            "[09:05] USER: Add a CSV export",
            "[09:06] AGENT: Earlier text reply",
        ])

    def test_tool_result_only_user_entries_are_not_prompts(self):
        out = self.digest([
            user(ts(9, 5), "Add a CSV export"),
            assistant(ts(9, 6), [text("First")]),
            user(ts(9, 7), [TOOL_RESULT]),
            assistant(ts(9, 8), [text("Second")]),
        ])
        self.assertEqual(out.splitlines(), [
            "[09:05] USER: Add a CSV export", "[09:08] AGENT: Second"])

    def test_wrapper_tag_user_entries_are_skipped(self):
        out = self.digest([
            user(ts(9, 5), "<command-name>/clear</command-name>"),
            user(ts(9, 6), "  <local-command-stdout>x</local-command-stdout>"),
            user(ts(9, 7), [text("<task-notification>done</task-notification>")]),
            user(ts(9, 8), "<system-reminder>be nice</system-reminder>"),
            user(ts(9, 9), "<b>bold</b> is a real prompt"),
            assistant(ts(9, 10), [text("ok")]),
        ])
        self.assertEqual(out.splitlines(), [
            "[09:09] USER: <b>bold</b> is a real prompt", "[09:10] AGENT: ok"])

    def test_window_boundaries_are_inclusive(self):
        entries = [
            user(ts(8, 59, 59), "before"),
            assistant(ts(8, 59, 59), [text("before reply")]),
            user(ts(9, 0), "at start"),
            assistant(ts(9, 30), [text("at end reply")]),
            user(ts(9, 30, 1), "after"),
            assistant(ts(9, 30, 1), [text("after reply")]),
        ]
        out = self.digest(entries, dt(9, 0), dt(9, 30))
        self.assertEqual(out.splitlines(), [
            "[09:00] USER: at start", "[09:30] AGENT: at end reply"])

    def test_time_is_printed_in_given_timezone(self):
        entries = [user(ts(14, 5), "Add a CSV export"), assistant(ts(14, 6), [text("Done")])]
        s, e = dt(14, 0), dt(15, 0)
        self.assertEqual(self.digest(entries, s, e).splitlines()[0],
                         "[14:05] USER: Add a CSV export")
        out = self.digest(entries, s, e, tz=ZoneInfo("America/New_York"))
        self.assertEqual(out.splitlines(), [
            "[09:05] USER: Add a CSV export", "[09:06] AGENT: Done"])  # EST in early March

    def test_digest_cuts_long_text(self):
        out = self.digest([user(ts(9, 5), "p" * 600), assistant(ts(9, 6), [text("r" * 900)])])
        u, a = out.splitlines()
        self.assertEqual(u, "[09:05] USER: " + "p" * 500 + "…")
        self.assertEqual(a, "[09:06] AGENT: " + "r" * 800 + "…")

    def test_digest_respects_max_chars(self):
        entries = []
        for i in range(30):
            entries.append(user(ts(9, i), "Prompt number %d" % i))
            entries.append(assistant(ts(9, i, 30), [text("Reply %d" % i)]))
        out = self.digest(entries, max_chars=200)
        self.assertLessEqual(len(out), 200)
        lines = out.splitlines()
        self.assertEqual(lines[-1], "[…truncated]")
        self.assertTrue(all(l.startswith("[09:") for l in lines[:-1]))
        self.assertEqual(lines[0], "[09:00] USER: Prompt number 0")
        full = self.digest(entries, max_chars=100000)
        full_lines = full.splitlines()
        self.assertEqual(len(full_lines), 60)
        self.assertEqual(full_lines[0], "[09:00] USER: Prompt number 0")
        self.assertEqual(full_lines[-1], "[09:29] AGENT: Reply 29")

    def test_deeply_nested_json_line_is_skipped(self):
        out = self.digest(
            [user(ts(9, 5), "Add a CSV export")],
            extra_lines=["[" * 100000, '{"a": ' * 100000,
                         json.dumps(assistant(ts(9, 7), [text("Done")]))])
        self.assertEqual(out.splitlines(), [
            "[09:05] USER: Add a CSV export", "[09:07] AGENT: Done"])

    def test_tiny_max_chars_is_raised_to_100(self):
        entries = []
        for i in range(10):
            entries.append(user(ts(9, i), "Prompt number %d" % i))
        out = self.digest(entries, max_chars=10)
        self.assertLessEqual(len(out), 100)
        self.assertGreater(len(out), 10)
        self.assertEqual(out.splitlines()[0], "[09:00] USER: Prompt number 0")
        self.assertEqual(out.splitlines()[-1], "[…truncated]")

    def test_digest_survives_garbage_lines(self):
        out = self.digest(
            [user(ts(9, 5), "Add a CSV export")],
            extra_lines=["not json", "[1, 2]", "{\"type\": \"user\"}", "",
                         json.dumps({"type": "user", "timestamp": "nope",
                                     "message": {"content": "x"}}),
                         json.dumps({"type": "assistant", "timestamp": ts(9, 6),
                                     "message": "oops"}),
                         json.dumps(assistant(ts(9, 7), [text("Done: export added")]))])
        self.assertEqual(out.splitlines(), [
            "[09:05] USER: Add a CSV export", "[09:07] AGENT: Done: export added"])

    def test_no_matching_entries_or_missing_file_gives_empty_string(self):
        self.assertEqual(self.digest([user(ts(12, 0), "late")]), "")
        self.assertEqual(transcripts.digest(self.path("absent.jsonl"), dt(9, 0), dt(10, 0)), "")

    def test_agent_reply_before_first_prompt_in_window_is_dropped(self):
        out = self.digest([assistant(ts(9, 1), [text("stray")]), user(ts(9, 2), "Hi there")])
        self.assertEqual(out, "[09:02] USER: Hi there")


if __name__ == "__main__":
    unittest.main()
