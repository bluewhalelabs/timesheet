"""Packaging checks: manifests, commands, skill, README and repo hygiene."""
import json
import os
import re
import subprocess
import sys
import unittest

from tests import helpers  # noqa: F401  (puts scripts/ on sys.path)

import ats
from ats import config

REPO = helpers.REPO
# One documented call: the script, then the rest of the same line up to a backtick or quote
# (spaces and tabs only between tokens, so nothing can come from the next line).
SCRIPT_CALL = re.compile(
    r'python3 "\$\{CLAUDE_PLUGIN_ROOT\}/scripts/([A-Za-z_]+)\.py"([^\n`\']*)')
FLAG = re.compile(r"(?<![\w-])--[a-z][a-z-]*")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TEST_REMOTE = "git@github.com:Example/"
TEXT_EXT = (".md", ".json", ".py", ".html", ".txt")


def read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
        return f.read()


def frontmatter(text):
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.strip()
            if v.startswith('"'):
                v = json.loads(v)       # a double-quoted YAML scalar is also a JSON string
            out[k.strip()] = v
    return out


def raw_frontmatter(text):
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    return dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)


def doc_files():
    paths = ["README.md", os.path.join("docs", "reference.md"),
             os.path.join("skills", "timesheet", "README.md"),
             os.path.join("skills", "timesheet", "SKILL.md")]
    for n in sorted(os.listdir(os.path.join(REPO, "commands"))):
        paths.append(os.path.join("commands", n))
    return paths


def tracked_text_files():
    skip = {".git", "reference", "local", ".superpowers", "__pycache__", "docs"}
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            if f.endswith(TEXT_EXT) or f == "LICENSE":
                yield os.path.join(root, f)


COMMANDS = tuple("commands/%s.md" % n for n in
                 ("away", "doctor", "ignore", "invoice", "pause", "report", "resume",
                  "setup", "status"))


class PackagingTests(unittest.TestCase):
    def test_manifests_are_valid_json_and_versions_match(self):
        plugin = json.loads(read(".claude-plugin", "plugin.json"))
        market = json.loads(read(".claude-plugin", "marketplace.json"))
        self.assertEqual(plugin["version"], ats.VERSION)
        self.assertEqual(market["name"], "timesheet")
        self.assertEqual(market["owner"], {"name": "timesheet contributors"})
        self.assertEqual(len(market["plugins"]), 1)
        entry = market["plugins"][0]
        self.assertEqual(entry["name"], plugin["name"])
        self.assertEqual(entry["source"], "./")
        self.assertEqual(entry["version"], ats.VERSION)

    def test_commands_and_skill_have_frontmatter(self):
        self.assertEqual(sorted(os.listdir(os.path.join(REPO, "commands"))),
                         ["away.md", "doctor.md", "ignore.md", "invoice.md", "pause.md",
                          "report.md", "resume.md", "setup.md", "status.md"])
        for rel in COMMANDS:
            fm = frontmatter(read(*rel.split("/")))
            self.assertIsNotNone(fm, rel)
            self.assertTrue(fm.get("description"), rel)
        for rel in ("commands/invoice.md", "commands/report.md", "commands/ignore.md"):
            self.assertTrue(frontmatter(read(*rel.split("/"))).get("argument-hint"), rel)
        for name, hint in (("pause", "[2h | until 17:00 | all]"), ("resume", "[at 15:00]"),
                           ("away", "[2h | until 17:00 | all]")):
            self.assertEqual(frontmatter(read("commands", name + ".md"))["argument-hint"], hint)
        self.assertEqual(json.loads(read(".claude-plugin", "plugin.json"))["name"], "timesheet")
        fm = frontmatter(read("skills", "timesheet", "SKILL.md"))
        self.assertEqual(fm.get("name"), "timesheet")
        for phrase in ("timesheet status", "ignore this session", "invoice last week",
                       "pause the timesheet", "stop the clock", "resume",
                       "I forgot to resume", "I'm stepping away", "away", "back",
                       "I'm back", "don't count my time"):
            self.assertIn(phrase, fm["description"])
        self.assertTrue(fm.get("description"))

    def test_frontmatter_values_are_double_quoted_strings(self):
        for rel in COMMANDS + ("skills/timesheet/SKILL.md",):
            raw = raw_frontmatter(read(*rel.split("/")))
            for key in ("description", "argument-hint"):
                if key in raw:
                    v = raw[key].strip()
                    self.assertTrue(len(v) >= 2 and v.startswith('"') and v.endswith('"'),
                                    "%s: %s is not double-quoted" % (rel, key))
                    json.loads(v)

    def test_commands_reference_scripts_that_exist(self):
        helps = {}

        def help_text(script, sub):
            if (script, sub) not in helps:
                path = os.path.join(helpers.SCRIPTS, script + ".py")
                self.assertTrue(os.path.isfile(path), path)
                r = subprocess.run([sys.executable, path] + ([sub] if sub else []) + ["--help"],
                                   capture_output=True, text=True, timeout=30)
                self.assertEqual(r.returncode, 0, "%s %s: %s" % (script, sub, r.stderr))
                helps[(script, sub)] = r.stdout
            return helps[(script, sub)]

        seen = 0
        for rel in doc_files():
            for script, rest in SCRIPT_CALL.findall(read(*rel.split(os.sep))):
                seen += 1
                top = help_text(script, "")
                words = rest.split()
                sub = words[0] if words and re.fullmatch(r"[a-z][a-z-]*", words[0]) else ""
                where = "%s: %s.py %s" % (rel, script, rest.strip())
                if sub:
                    m = re.search(r"\{([a-z,-]+)\}", top)
                    self.assertIn(sub, set(m.group(1).split(",")) if m else set(), where)
                text = help_text(script, sub)
                for flag in FLAG.findall(rest):
                    self.assertIn(flag, text, "%s: %s not in --help" % (where, flag))
        self.assertGreater(seen, 10)

    def test_skill_states_the_never_send_and_no_token_rules(self):
        text = read("skills", "timesheet", "SKILL.md")
        low = text.lower()
        for phrase in ("never send", "--yes", "--dry-run", "token"):
            self.assertIn(phrase, low)
        self.assertIn("never put a mercury token", low)
        for flag in ("--force", "--allow-overlap", "--allow-incomplete"):
            self.assertIn(flag, text)
        for rel in ["skills/timesheet/SKILL.md"] + [
                "commands/" + n for n in sorted(os.listdir(os.path.join(REPO, "commands")))]:
            for line in read(*rel.split("/")).splitlines():
                for flag in ("--force", "--allow-overlap", "--allow-incomplete"):
                    if flag in line:
                        self.assertIn("explicitly", line, "%s: %s" % (rel, flag))
        self.assertLess(len(text.splitlines()), 260)
        rules = text.index("## Rules")
        never_send = text.lower().index("never send")
        flow = text.index("## Invoice flow")
        self.assertLess(rules, never_send)
        self.assertLess(never_send, flow)

    def test_skill_treats_digest_text_as_data(self):
        text = read("skills", "timesheet", "SKILL.md")
        rules = text[text.index("## Rules"):text.index("## Invoice flow")]
        low = " ".join(rules.lower().split())
        for phrase in ("never instructions", "ignore", "instruction-like text",
                       "approve", "change scope", "name a client or period", "command"):
            self.assertIn(phrase, low)
        flow = " ".join(text[text.index("## Invoice flow"):].lower().split())
        self.assertIn("several clients", flow)
        self.assertIn("only what belongs to the client being invoiced", flow)

    def test_repo_has_no_absolute_home_paths(self):
        for path in tracked_text_files():
            if path.endswith(os.path.join("tests", "test_packaging.py")):
                continue
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for needle in ("/Users/", "/home/"):
                self.assertNotIn(needle, text, path)

    def test_no_real_email_addresses(self):
        for path in tracked_text_files():
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for addr in EMAIL.findall(text.replace(TEST_REMOTE, "")):
                ok = addr.endswith("@example.com") or addr == "noreply@anthropic.com"
                self.assertTrue(ok, "%s: %s" % (path, addr))

    def test_example_config_matches_defaults_keys(self):
        example = json.loads(read("config.example.json"))
        for key in config.DEFAULTS:
            if key == "text":
                continue  # optional wording overrides; documented in the README
            self.assertIn(key, example["defaults"])

    def test_reference_documents_every_default_key_in_backticks(self):
        text = read("docs", "reference.md")
        for key in config.DEFAULTS:
            self.assertIn("`%s`" % key, text, key)

    def test_readme_is_a_usage_guide_that_links_the_reference(self):
        text = read("README.md")
        headings = [h.lower() for h in re.findall(r"^#{1,4} (.+)$", text, re.M)]
        for word in ("install", "set up", "invoice", "mercury", "commands"):
            self.assertTrue(any(word in h for h in headings), "no heading about %s" % word)
        self.assertIn("(docs/reference.md", text)
        for cmd in COMMANDS:
            self.assertIn("/timesheet:" + cmd.split("/")[1][:-3], text)
        self.assertLess(len(text.splitlines()), 200)

    def test_reference_has_the_expected_sections(self):
        text = read("docs", "reference.md")
        headings = [h.lower() for h in re.findall(r"^#{1,4} (.+)$", text, re.M)]
        for word in ("install", "setup", "privacy", "mercury"):
            self.assertTrue(any(word in h for h in headings), "no heading about %s" % word)
        self.assertRegex(text, r"(?m)^Status: .*sandbox")

    def test_readme_full_config_examples_load(self):
        blocks = re.findall(r"```json\n(.*?)\n```",
                            read("README.md") + read("docs", "reference.md"), re.S)
        checked = 0
        for block in blocks:
            obj = json.loads(block)          # strict JSON: no comments, no trailing commas
            if isinstance(obj, dict) and "clients" in obj:
                with helpers.temp_home() as home:
                    helpers.write_config(home, obj)
                    cfg = config.load()
                self.assertTrue(config.client_keys(cfg))
                checked += 1
        self.assertGreaterEqual(checked, 1)

    def test_license_is_mit(self):
        text = read("LICENSE")
        self.assertIn("MIT License", text)
        self.assertIn("Copyright (c) 2026 timesheet contributors", text)


if __name__ == "__main__":
    unittest.main()
