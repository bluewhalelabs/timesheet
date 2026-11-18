import json
import os
import re
import subprocess
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

from tests import helpers
from tests.test_invoice_cli import InvoiceCase, NOW, WEEK, EVENTS
from tests.test_timesheet_cli import two_clients

from ats import render  # noqa: E402


def slurp(path, mode="r"):
    with open(path, mode) as f:
        return f.read()


GOLDEN = os.path.join(helpers.REPO, "tests", "golden", "timesheet.html")


def fixed_cfg():
    return {
        "you": {"name": "Pat Example", "business": "Example Studio",
                "email": "pat@example.com", "address": "1 Main Street\nSuite 2\nSpringfield",
                "logo": ""},
        "clients": {"acme": {"name": "Acme Co", "contact": "Ann Acme",
                             "address": "9 Side Road\nTownsville"}},
    }


def fixed_payload():
    return {
        "client": {"key": "acme", "name": "Acme Co"},
        "period": {"id": "2026-11-02_2026-11-08", "start": "2026-11-02", "end": "2026-11-08",
                   "label": "Nov 2 – Nov 8, 2026"},
        "reference": "acme-2026-11-02_2026-11-08",
        "sessions": [
            {"key": "S1", "start": "2026-11-03T08:55:00+00:00", "end": "2026-11-03T10:05:00+00:00",
             "date": "2026-11-03", "days": ["2026-11-03"], "you_seconds": 1800.0,
             "agent_seconds": 2400.0, "projects": ["project-a", "project-b"], "summary": "",
             "threads": []},
            {"key": "S2", "start": "2026-11-04T13:30:00+00:00", "end": "2026-11-04T15:10:00+00:00",
             "date": "2026-11-04", "days": ["2026-11-04"], "you_seconds": 3000.0,
             "agent_seconds": 3000.0, "projects": ["project-a"], "summary": "", "threads": []},
        ],
        "lines": [
            {"key": "k1", "date": "2026-11-03", "label": "Tue, Nov 3, 2026",
             "description": "Built the export & reports page", "hours": "1.25", "rate": "150",
             "amount": "187.50", "manual": False},
            {"key": "k2", "date": "2026-11-04", "label": "Wed, Nov 4, 2026",
             "description": "API review\nand fixes", "hours": "1.50", "rate": "150",
             "amount": "225.00", "manual": False},
        ],
        "total_hours": "2.75", "total": "412.50", "currency": "USD",
        "subtotals": [
            {"project": "project-a", "label": "project-a", "hours": "1.75", "amount": "262.50"},
            {"project": "project-b", "label": "project-b", "hours": "1.00", "amount": "150.00"}],
        "text": {"title": "Timesheet", "period_label": "Professional services",
                 "counting_note": "Time is agent run time plus engaged time.",
                 "footer": "Thank you for your business."},
        "show_session_detail": False, "show_project_subtotals": True, "line_items": "day",
        "approved_at": "2026-11-12T12:00:00Z",
    }


class HtmlTests(unittest.TestCase):
    def test_golden_html_for_fixed_dataset(self):
        got = render.html(fixed_payload(), fixed_cfg())
        if os.environ.get("UPDATE_GOLDEN") == "1":
            with open(GOLDEN, "w", encoding="utf-8", newline="") as f:
                f.write(got)
        with open(GOLDEN, encoding="utf-8", newline="") as f:
            self.assertEqual(got, f.read(),
                             "regenerate with UPDATE_GOLDEN=1 /usr/bin/python3 -m unittest tests.test_render")

    def test_special_characters_are_escaped(self):
        p = fixed_payload()
        p["lines"][0]["description"] = 'Fixed <Table> & "quotes"'
        p["client"]["name"] = "A <b>&</b> Co"
        out = render.html(p, fixed_cfg())
        self.assertIn("Fixed &lt;Table&gt; &amp; &quot;quotes&quot;", out)
        self.assertNotIn("<Table>", out)
        self.assertNotIn("<b>", out)

    def test_values_are_not_expanded_twice_and_unknown_slots_fail(self):
        p = fixed_payload()
        p["lines"][0]["description"] = "{{total}} {{nope}}"
        out = render.html(p, fixed_cfg())
        self.assertIn("{{total}} {{nope}}", out)
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
            f.write("<p>{{title}} {{bogus}}</p>")
        self.addCleanup(os.unlink, f.name)
        with self.assertRaises(render.RenderError) as cm:
            render.html(p, fixed_cfg(), f.name)
        self.assertIn("bogus", str(cm.exception))

    def test_empty_fields_are_omitted(self):
        cfg = {"you": {"name": "Pat Example"}, "clients": {"acme": {"name": "Acme Co"}}}
        p = fixed_payload()
        p["text"]["footer"] = ""
        p["subtotals"] = p["subtotals"][:1]
        out = render.html(p, cfg)
        self.assertNotIn("<br>", out)
        self.assertNotIn('class="meta"></p>', out)
        self.assertNotIn("By project", out)
        self.assertNotIn("Session detail", out)
        self.assertNotIn('class="foot"', out)
        self.assertNotIn("<p></p>", out)

    def test_hostile_everything_is_escaped_and_never_expanded(self):
        import html as htmllib

        def h(tag):
            return "<script>%s\"'&\\1\\g<0>$1{{total}}" % tag

        cfg = {
            "you": {"name": h("yname"), "business": h("biz"), "email": h("email"),
                    "address": h("youaddr1") + "\n" + h("youaddr2"), "logo": ""},
            "clients": {"acme": {"name": h("cfgname"), "contact": h("contact"),
                                 "address": h("cliaddr1") + "\n" + h("cliaddr2")}},
        }
        p = fixed_payload()
        p["client"]["name"] = h("clientname")
        p["period"]["label"] = h("period")
        p["reference"] = h("ref")
        p["lines"][0]["label"] = h("label")
        p["lines"][0]["description"] = h("desc")
        p["subtotals"][0]["label"] = h("subtotal")
        p["sessions"][0]["projects"] = [h("proj1"), h("proj2")]
        p["show_session_detail"] = True
        p["text"] = {"title": h("title"), "period_label": h("textperiod"),
                     "counting_note": h("note"), "footer": h("footer")}
        out = render.html(p, cfg)
        self.assertNotIn("<script", out)
        for tag in ("yname", "biz", "email", "youaddr1", "youaddr2", "clientname", "contact",
                    "cliaddr1", "cliaddr2", "period", "ref", "label", "desc", "subtotal",
                    "proj1", "proj2", "title", "note", "footer"):
            self.assertIn(htmllib.escape(h(tag), quote=True), out, tag)
        self.assertIn("{{total}}", out)          # literal text, never expanded
        self.assertEqual(len(re.findall(r'<td class="num">\$412\.50</td></tr>', out)), 1)

    def test_project_subtotals_only_when_enabled_and_several_projects(self):
        out = render.html(fixed_payload(), fixed_cfg())
        self.assertIn("<h3>By project (before per-line rounding)</h3>", out)
        self.assertIn("Project figures are rounded once per project and may differ slightly "
                      "from the line total above.", out)
        p = fixed_payload()
        p["show_project_subtotals"] = False
        self.assertNotIn("By project", render.html(p, fixed_cfg()))
        del p["show_project_subtotals"]
        self.assertNotIn("By project", render.html(p, fixed_cfg()))
        p = fixed_payload()
        p["subtotals"] = p["subtotals"][:1]
        self.assertNotIn("By project", render.html(p, fixed_cfg()))

    def test_counting_note_is_not_a_main_page_slot(self):
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
            f.write("<p>{{counting_note}}</p>")
        self.addCleanup(os.unlink, f.name)
        with self.assertRaises(render.RenderError):
            render.html(fixed_payload(), fixed_cfg(), f.name)

    def test_address_lines_are_joined_with_br(self):
        out = render.html(fixed_payload(), fixed_cfg())
        self.assertIn("1 Main Street<br>Suite 2<br>Springfield", out)
        self.assertIn("9 Side Road<br>Townsville", out)

    def test_money_formatting_and_item_header(self):
        out = render.html(fixed_payload(), fixed_cfg())
        self.assertIn("$150.00", out)
        self.assertIn("$412.50", out)
        self.assertIn("<th>Date</th>", out)
        p = fixed_payload()
        p["line_items"] = "project"
        self.assertIn("<th>Item</th>", render.html(p, fixed_cfg()))

    def test_no_clock_times_without_detail_page(self):
        out = render.html(fixed_payload(), fixed_cfg())
        self.assertIsNone(re.search(r"\b\d{1,2}:\d{2}\b", out))

    def test_detail_page_lists_sessions_when_enabled(self):
        p = fixed_payload()
        p["show_session_detail"] = True
        p["show_agent_time"] = True
        out = render.html(p, fixed_cfg())
        self.assertIn("Session detail", out)
        self.assertIn("08:55–10:05", out)
        self.assertIn("13:30–15:10", out)
        self.assertIn("0h 30m", out)      # your time
        self.assertIn("0h 40m", out)      # agent time
        self.assertIn("0h 50m", out)
        self.assertIn(">Your time</th>", out)
        self.assertIn(">Agent time</th>", out)
        self.assertIn("project-a, project-b", out)
        self.assertIn("Time is agent run time plus engaged time.", out)
        self.assertIn('<td class="date">Tue, Nov 3, 2026</td><td class="when">08:55', out)

    def test_detail_page_hides_agent_time_by_default(self):
        p = fixed_payload()
        p["show_session_detail"] = True
        p["sessions"][0]["billable_seconds"] = 3000.0   # 30 m + 40 m of agent time at 50%
        out = render.html(p, fixed_cfg())
        self.assertNotIn("Agent time", out)
        self.assertNotIn("Your time", out)
        self.assertIn('<th class="num">Hours</th><th>Projects</th>', out)
        self.assertIn('<td class="num">0h 50m</td><td>project-a, project-b</td>', out)
        # without billable_seconds (older payload) the two parts are added
        self.assertIn('<td class="num">1h 40m</td><td>project-a</td>', out)
        self.assertNotIn("0h 30m", out)

    def test_detail_clock_times_keep_the_payload_offset(self):
        p = fixed_payload()
        p["show_session_detail"] = True
        p["sessions"][0]["start"] = "2026-11-10T08:55:00-07:00"
        p["sessions"][0]["end"] = "2026-11-10T10:05:00-07:00"
        p["sessions"][0]["date"] = "2026-11-10"
        out = render.html(p, fixed_cfg())
        self.assertIn('<td class="when">08:55–10:05</td>', out)
        self.assertNotIn("15:55", out)

    def test_detail_session_crossing_local_midnight_marks_the_end(self):
        p = fixed_payload()
        p["show_session_detail"] = True
        # 23:30-00:40 local; in UTC both ends are on the same day
        p["sessions"][0]["start"] = "2026-11-10T23:30:00-07:00"
        p["sessions"][0]["end"] = "2026-11-11T00:40:00-07:00"
        out = render.html(p, fixed_cfg())
        self.assertIn('<td class="when">23:30–00:40 (+1d)</td>', out)
        # the reverse: same local day, different UTC days -> no marker
        p["sessions"][0]["start"] = "2026-11-10T16:30:00-07:00"
        p["sessions"][0]["end"] = "2026-11-10T17:40:00-07:00"
        out = render.html(p, fixed_cfg())
        self.assertIn('<td class="when">16:30–17:40</td>', out)

    def test_detail_date_comes_from_the_session_date_field(self):
        p = fixed_payload()
        p["show_session_detail"] = True
        p["sessions"][0]["date"] = "2026-11-02"      # differs from the start's UTC day
        out = render.html(p, fixed_cfg())
        self.assertIn('<td class="date">Mon, Nov 2, 2026</td><td class="when">08:55', out)
        self.assertNotIn("Tue, Nov 3, 2026</td><td", out.split("Session detail")[1])

    def test_empty_counting_note_omits_the_note_element(self):
        p = fixed_payload()
        p["show_session_detail"] = True
        p["text"]["counting_note"] = ""
        out = render.html(p, fixed_cfg())
        self.assertIn("Session detail", out)
        self.assertNotIn('class="note"', out)

    def test_total_equals_sum_of_line_amounts_in_html(self):
        out = render.html(fixed_payload(), fixed_cfg())
        amounts = re.findall(r'<td class="num amt">\$([\d,.]+)</td>', out)
        self.assertEqual(len(amounts), 2)
        total = re.search(r'class="total".*?<td class="num">\$([\d,.]+)</td></tr>', out, re.S)
        self.assertEqual(sum(Decimal(a.replace(",", "")) for a in amounts),
                         Decimal(total.group(1).replace(",", "")))

    def test_logo_inlined_and_missing_logo_omitted(self):
        with tempfile.TemporaryDirectory() as d:
            png = os.path.join(d, "logo.png")
            with open(png, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\nxx")
            cfg = fixed_cfg()
            cfg["you"]["logo"] = png
            out = render.html(fixed_payload(), cfg)
            self.assertIn('src="data:image/png;base64,', out)
            self.assertIsNone(render.logo_warning(cfg))
            for bad in (os.path.join(d, "missing.png"), os.path.join(d, "x.bmp")):
                cfg["you"]["logo"] = bad
                out = render.html(fixed_payload(), cfg)
                self.assertNotIn("<img", out)
                self.assertIsNotNone(render.logo_warning(cfg))
            big = os.path.join(d, "big.jpg")
            with open(big, "wb") as f:
                f.write(b"0" * (2 * 1024 * 1024 + 1))
            cfg["you"]["logo"] = big
            self.assertNotIn("<img", render.html(fixed_payload(), cfg))
            self.assertIn("2 MB", render.logo_warning(cfg))

    def test_missing_logo_is_omitted_not_an_error(self):
        cfg = fixed_cfg()
        cfg["you"]["logo"] = "/nonexistent/logo.png"
        self.assertNotIn("<img", render.html(fixed_payload(), cfg))


def _fake_browser(directory, name="fakechrome"):
    path = os.path.join(directory, name)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n")
    os.chmod(path, 0o755)
    return path


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TIMESHEET_NO_BROWSER", None)

    def test_find_browser_prefers_config_path(self):
        cfg_browser = _fake_browser(self.dir, "configured")
        other = _fake_browser(self.dir, "other")
        with mock.patch.object(render, "_PATH_NAMES", ["other"]), \
                mock.patch.object(render, "_MAC_APPS", []), \
                mock.patch.dict(os.environ, {"PATH": self.dir}):
            self.assertEqual(render.find_browser({"pdf": {"browser_path": cfg_browser}}),
                             cfg_browser)
            self.assertEqual(render.find_browser({"pdf": {"browser_path": "/nope"}}), other)
            self.assertEqual(render.find_browser({"pdf": {}}), other)

    def test_find_browser_checks_mac_bundles_then_path(self):
        app = os.path.join(self.dir, "Chromium.app", "Contents", "MacOS")
        os.makedirs(app)
        exe = _fake_browser(app, "Chromium")
        with mock.patch.object(render, "_MAC_DIRS", [self.dir]), \
                mock.patch.object(render, "_MAC_APPS", ["Google Chrome", "Chromium"]), \
                mock.patch.object(render, "_PATH_NAMES", []):
            self.assertEqual(render.find_browser({}), exe)
        with mock.patch.object(render, "_MAC_DIRS", [self.dir]), \
                mock.patch.object(render, "_MAC_APPS", ["Brave Browser"]), \
                mock.patch.object(render, "_PATH_NAMES", []):
            self.assertIsNone(render.find_browser({}))

    def test_non_executable_is_ignored_and_switch_disables(self):
        plain = os.path.join(self.dir, "plain")
        open(plain, "w").close()
        with mock.patch.object(render, "_MAC_APPS", []), mock.patch.object(render, "_PATH_NAMES", []):
            self.assertIsNone(render.find_browser({"pdf": {"browser_path": plain}}))
            exe = _fake_browser(self.dir)
            os.environ["TIMESHEET_NO_BROWSER"] = "1"
            self.assertIsNone(render.find_browser({"pdf": {"browser_path": exe}}))

    def test_pdf_never_raises_and_retries_old_headless(self):
        page = os.path.join(self.dir, "p.html")
        with open(page, "w") as f:
            f.write("<p>x</p>")
        out = os.path.join(self.dir, "p.pdf")
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[1] == "--headless":
                with open(out, "wb") as f:
                    f.write(b"%PDF-1.4 x")
            return mock.Mock(returncode=1 if cmd[1] != "--headless" else 0)

        with mock.patch.object(render.subprocess, "run", side_effect=run):
            self.assertTrue(render.pdf(page, out, "/b"))
        self.assertEqual([c[1] for c in calls], ["--headless=new", "--headless"])
        self.assertIn("--no-first-run", calls[0])
        self.assertTrue(any(a.startswith("--user-data-dir=") for a in calls[0]))
        self.assertTrue(calls[0][-1].startswith("file://"))
        os.unlink(out)
        with mock.patch.object(render.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("b", 1)):
            self.assertFalse(render.pdf(page, out, "/b"))
        self.assertFalse(render.pdf(page, out, "/nonexistent/browser"))

    @unittest.skipUnless(render.find_browser({"pdf": {}}), "no browser")
    def test_pdf_is_created_with_a_real_browser(self):
        page = os.path.join(self.dir, "t.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write(render.html(fixed_payload(), fixed_cfg()))
        out = os.path.join(self.dir, "t.pdf")
        self.assertTrue(render.pdf(page, out, render.find_browser({"pdf": {}})))
        with open(out, "rb") as f:
            self.assertEqual(f.read(4), b"%PDF")


NEW_EVENTS = [
    {"ts": "2026-11-04T09:00:00Z", "event": "start", "hook": "x", "session": "s6",
     "client": "acme", "project": "project-a", "cwd": "/nowhere", "transcript_path": ""},
    {"ts": "2026-11-04T10:00:00Z", "event": "end", "hook": "x", "session": "s6",
     "client": "acme", "project": "project-a", "cwd": "/nowhere", "transcript_path": ""}]


class RenderCliTests(InvoiceCase):
    def render(self, *args, **kw):
        return self.run_cli("render", "--now", NOW, "--no-open", *args, **kw)

    def approve(self, client="acme", desc="Built the <export> page"):
        r = self.run_cli("compute", "--now", NOW, "--client", client)
        d = json.loads(r.stdout)
        r = self.save({"descriptions": {d["lines"][0]["key"]: desc}}, "--client", client)
        self.assertEqual(r.returncode, 0, r.stderr)
        return d

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"TIMESHEET_NO_BROWSER": "1"})
        env.start()
        self.addCleanup(env.stop)

    def lines_json(self):
        with open(os.path.join(self.draft_dir("acme", WEEK), "lines.json")) as f:
            return json.load(f)

    def test_render_requires_approval(self):
        self.setup()
        r = self.render("--client", "acme")
        self.assertEqual(r.returncode, 2)
        self.assertIn("approve the draft first", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.draft_dir("acme", WEEK), "timesheet.html")))

    def test_render_falls_back_to_html_when_no_browser(self):
        self.setup()
        self.approve()
        r = self.render("--client", "acme")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = self.draft_dir("acme", WEEK)
        self.assertTrue(os.path.exists(os.path.join(d, "timesheet.html")))
        self.assertFalse(os.path.exists(os.path.join(d, "timesheet.pdf")))
        out = r.stdout.split("\n")
        self.assertEqual(out[0], "Item\tDescription\tHours\tRate\tAmount")
        self.assertEqual(out[1], ("Tue, Nov 3, 2026\tBuilt the <export> page\t0.42\t$150.00\t$63.00"))
        self.assertRegex(out[2], r"^TOTAL\t\t0\.42\t\t\$63\.00$")
        self.assertEqual(out[3], "")
        self.assertEqual(out[4], "HTML: " + os.path.join(d, "timesheet.html"))
        self.assertEqual(out[5], "PDF: not created (no browser found). "
                                 "Open the HTML and use Print → Save as PDF.")

    def test_render_makes_a_pdf_when_a_browser_exists(self):
        self.setup()
        self.approve()
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "chrome")
            with open(fake, "w") as f:
                f.write('#!/bin/sh\nfor a in "$@"; do case "$a" in --print-to-pdf=*) '
                        'printf "%%PDF-1.4 fake" > "${a#--print-to-pdf=}";; esac; done\n')
            os.chmod(fake, 0o755)
            cfg = json.loads(slurp(os.path.join(self.home, "config.json")))
            cfg["pdf"]["browser_path"] = fake
            helpers.write_config(self.home, cfg)
            with mock.patch.dict(os.environ):
                os.environ.pop("TIMESHEET_NO_BROWSER")
                r = self.render("--client", "acme")
        self.assertEqual(r.returncode, 0, r.stderr)
        pdf = os.path.join(self.draft_dir("acme", WEEK), "timesheet.pdf")
        self.assertIn("PDF: " + pdf, r.stdout)
        self.assertTrue(slurp(pdf, "rb").startswith(b"%PDF"))

    def test_render_refuses_a_stale_draft_and_keeps_earlier_output(self):
        self.setup()
        self.approve()
        self.assertEqual(self.render("--client", "acme").returncode, 0)
        d = self.draft_dir("acme", WEEK)
        html_path = os.path.join(d, "timesheet.html")
        pdf_path = os.path.join(d, "timesheet.pdf")
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-old")
        old_html, old_lines = slurp(html_path), slurp(os.path.join(d, "lines.json"))
        self.write_events(EVENTS + NEW_EVENTS)
        r = self.render("--client", "acme")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        now_total = json.loads(self.run_cli("compute", "--now", NOW, "--client", "acme").stdout)["total"]
        self.assertIn("numbers changed since approval", r.stderr)
        self.assertIn("now $%s" % now_total, r.stderr)
        self.assertIn("save again to re-approve", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(slurp(html_path), old_html)
        self.assertEqual(slurp(pdf_path, "rb"), b"%PDF-old")
        self.assertEqual(slurp(os.path.join(d, "lines.json")), old_lines)

    def stale_pdf(self):
        d = self.draft_dir("acme", WEEK)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "timesheet.pdf")
        with open(path, "wb") as f:
            f.write(b"%PDF-old")
        return path

    def test_render_without_a_browser_removes_an_older_pdf(self):
        self.setup()
        self.approve()
        pdf_path = self.stale_pdf()
        r = self.render("--client", "acme")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(pdf_path))

    def test_failed_browser_run_never_leaves_an_older_pdf(self):
        self.setup()
        self.approve()
        pdf_path = self.stale_pdf()
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "chrome")
            with open(fake, "w") as f:
                f.write("#!/bin/sh\nexit 1\n")
            os.chmod(fake, 0o755)
            cfg = json.loads(slurp(os.path.join(self.home, "config.json")))
            cfg["pdf"]["browser_path"] = fake
            helpers.write_config(self.home, cfg)
            with mock.patch.dict(os.environ):
                os.environ.pop("TIMESHEET_NO_BROWSER")
                r = self.render("--client", "acme")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PDF: not created (browser failed)", r.stdout)
        self.assertFalse(os.path.exists(pdf_path))

    def test_unapproved_and_empty_period_refusals_keep_an_existing_pdf(self):
        self.setup()
        pdf_path = self.stale_pdf()
        self.assertEqual(self.render("--client", "acme").returncode, 2)
        self.assertEqual(slurp(pdf_path, "rb"), b"%PDF-old")
        self.write_events([])
        self.assertEqual(self.render("--client", "acme").returncode, 0)
        self.assertEqual(slurp(pdf_path, "rb"), b"%PDF-old")

    def test_render_succeeds_after_re_approval(self):
        self.setup()
        self.approve()
        self.write_events(EVENTS + NEW_EVENTS)
        self.assertEqual(self.render("--client", "acme").returncode, 2)
        r = self.save({}, "--client", "acme")
        self.assertEqual(r.returncode, 0, r.stderr)
        now_total = json.loads(r.stdout)["total"]
        r = self.render("--client", "acme")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertIn("$" + now_total, slurp(os.path.join(self.draft_dir("acme", WEEK), "timesheet.html")))

    def test_empty_period_writes_no_files(self):
        self.setup(events=[])
        r = self.render("--client", "acme")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nothing to render", r.stdout)
        self.assertFalse(os.path.exists(self.draft_dir("acme", WEEK)))

    def test_two_clients_do_not_leak_into_each_others_timesheet(self):
        self.setup()
        self.approve("acme", "Acme work")
        self.approve("beta", "Beta secret work")
        r = self.render("--client", "acme")
        self.assertEqual(r.returncode, 0, r.stderr)
        page = slurp(os.path.join(self.draft_dir("acme", WEEK), "timesheet.html"))
        self.assertIn("Acme Co", page)
        self.assertIn("Acme work", page)
        self.assertNotIn("Beta", page)
        self.assertNotIn("$200.00", page)
        self.assertNotIn("project-b", page)
        self.assertNotIn("Beta", r.stdout)

    def test_logo_problem_warns_on_stderr(self):
        cfg = two_clients()
        cfg["you"] = {"logo": os.path.join(self.home, "missing.png")}
        self.setup(cfg)
        self.approve()
        r = self.render("--client", "acme")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("logo", r.stderr)
        self.assertIn("omitted", r.stderr)


if __name__ == "__main__":
    unittest.main()
