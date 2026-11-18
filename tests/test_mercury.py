import http.server
import json
import os
import socket
import subprocess
import threading
import unittest
from unittest import mock

from tests import helpers
from tests.test_invoice_cli import EVENTS, NOW, WEEK, InvoiceCase
from tests.test_timesheet_cli import two_clients, turn

from ats import mercury_api

CLI = os.path.join(helpers.SCRIPTS, "mercury.py")
PY = "/usr/bin/python3"
TOKEN = "secret-token:TESTONLY123"
CUSTOMER = "11111111-1111-4111-8111-111111111111"
ACCOUNT = "22222222-2222-4222-8222-222222222222"
INVOICE_ID = "33333333-3333-4333-8333-333333333333"
CREATED = {"id": INVOICE_ID, "status": "Unpaid", "slug": "abc123slug", "amount": 63.0,
           "invoiceNumber": "INV-7", "currencyCode": "USD",
           "createdAt": "2026-11-12T12:00:00Z", "lineItems": []}


class FakeMercury:
    """Loopback HTTP server that records every request and returns canned JSON."""

    def __init__(self):
        self.requests = []
        self.routes = {}       # (method, path) -> (status, body) or list of them
        self.drop_posts = False
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8") if n else ""
                path, _, query = self.path.partition("?")
                outer.requests.append({
                    "method": self.command, "path": path, "query": query,
                    "headers": dict(self.headers), "body": json.loads(raw) if raw else None,
                    "raw": raw})
                if self.command == "POST" and outer.drop_posts:
                    self.request.shutdown(socket.SHUT_RDWR)
                    return
                route = outer.routes.get((self.command, path))
                if route is None:
                    status, body = 404, {"error": "no route"}
                else:
                    if isinstance(route, list):
                        route = route.pop(0) if len(route) > 1 else route[0]
                    status, body = route
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _handle

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:%d/api/v1" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]


def mercury_cfg(base_url, **acme_mercury):
    cfg = two_clients()
    block = {"enabled": True, "customer_id": CUSTOMER, "destination_account_id": ACCOUNT,
             "due_in_days": 15}
    block.update(acme_mercury)
    cfg["clients"]["acme"]["mercury"] = block
    cfg["clients"]["beta"]["mercury"] = {"enabled": True, "customer_id": CUSTOMER,
                                         "destination_account_id": ACCOUNT}
    cfg["mercury"] = {"api_key_env": "MERCURY_API_KEY", "base_url": base_url}
    return cfg


class MercuryCase(InvoiceCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeMercury()
        self.addCleanup(self.fake.close)
        self.fake.routes[("POST", "/api/v1/ar/invoices")] = (200, dict(CREATED))
        env = mock.patch.dict(os.environ, {"TIMESHEET_NO_BROWSER": "1"})
        env.start()
        self.addCleanup(env.stop)

    def setup(self, cfg=None, events=EVENTS):
        super().setup(cfg if cfg is not None else mercury_cfg(self.fake.base_url), events)

    def mercury(self, *args, token=TOKEN, env=None):
        e = {k: v for k, v in os.environ.items() if k != "MERCURY_API_KEY"}
        e["TIMESHEET_HOME"] = self.home
        if token is not None:
            e["MERCURY_API_KEY"] = token
        e.update(env or {})
        return subprocess.run([PY, CLI] + list(args), capture_output=True, text=True,
                              env=e, timeout=30)

    def approve(self, client="acme", period=None, descriptions=None, render=True):
        """Save the draft and (by default) render it, as the invoice flow does."""
        args = ["--client", client] + (["--period", period] if period else [])
        data = {}
        if descriptions:
            r = self.run_cli("compute", "--now", NOW, *args)
            key = json.loads(r.stdout)["lines"][0]["key"]
            data = {"descriptions": {key: descriptions}}
        r = self.save(data, *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        if render:
            r = self.run_cli("render", "--now", NOW, "--no-open", *args)
            self.assertEqual(r.returncode, 0, r.stderr)

    def create(self, *extra, client="acme", period=None, **kw):
        args = ["create-invoice", "--now", NOW, "--client", client]
        if period:
            args += ["--period", period]
        return self.mercury(*(args + list(extra)), **kw)

    def record_path(self, client="acme", period=WEEK):
        return os.path.join(self.draft_dir(client, period), "mercury.json")

    def read_record(self, client="acme", period=WEEK):
        with open(self.record_path(client, period)) as f:
            return json.load(f)

    def all_text_under_home(self):
        out = []
        for root, _, files in os.walk(self.home):
            for name in files:
                with open(os.path.join(root, name), "rb") as f:
                    out.append(f.read().decode("utf-8", "replace"))
        return "\n".join(out)


class BodyTests(MercuryCase):
    def payload(self, client="acme", period=None, description=None):
        self.setup()
        self.approve(client, period, description)
        r = self.run_cli("compute", "--now", NOW, "--client", client,
                         *(["--period", period] if period else []))
        return json.loads(r.stdout), mercury_cfg(self.fake.base_url)

    def test_invoice_body_fields_and_dates(self):
        payload, cfg = self.payload()
        b = mercury_api.invoice_body(payload, cfg, "acme")
        self.assertEqual(b["invoiceDate"], "2026-11-08")
        self.assertEqual(b["dueDate"], "2026-11-23")
        self.assertEqual(b["servicePeriodStartDate"], "2026-11-02")
        self.assertEqual(b["servicePeriodEndDate"], "2026-11-08")
        self.assertEqual(b["customerId"], CUSTOMER)
        self.assertEqual(b["destinationAccountId"], ACCOUNT)
        self.assertEqual(b["ccEmails"], [])
        self.assertIs(b["achDebitEnabled"], False)
        self.assertIs(b["creditCardEnabled"], False)
        self.assertIs(b["useRealAccountNumber"], False)
        self.assertEqual(b["payerMemo"], "Timesheet acme-%s attached." % WEEK)
        self.assertEqual(b["internalNote"], "Created by timesheet for " + payload["period"]["label"])
        self.assertEqual(b["currencyCode"], "USD")
        self.assertNotIn("invoiceNumber", b)
        self.assertEqual(len(b["lineItems"]), 1)
        li = b["lineItems"][0]
        self.assertEqual(li["quantity"], float(payload["lines"][0]["hours"]))
        self.assertEqual(li["unitPrice"], 150.0)

    def test_due_date_uses_the_clients_due_in_days(self):
        payload, cfg = self.payload()
        cfg["clients"]["acme"]["mercury"]["due_in_days"] = 0
        self.assertEqual(mercury_api.invoice_body(payload, cfg, "acme")["dueDate"], "2026-11-08")
        cfg["clients"]["acme"]["mercury"]["due_in_days"] = 30
        self.assertEqual(mercury_api.invoice_body(payload, cfg, "acme")["dueDate"], "2026-12-08")

    def test_body_always_says_dont_send(self):
        payload, cfg = self.payload()
        self.assertEqual(mercury_api.invoice_body(payload, cfg, "acme")["sendEmailOption"],
                         "DontSend")

    def test_source_never_mentions_send_now(self):
        for p in (os.path.join(helpers.SCRIPTS, "ats", "mercury_api.py"), CLI):
            with open(p, encoding="utf-8") as f:
                self.assertNotIn("SendNow", f.read(), p)

    def test_long_line_name_is_cut_to_299(self):
        payload, cfg = self.payload()
        payload["lines"][0]["description"] = "word  \n" * 200
        name = mercury_api.invoice_body(payload, cfg, "acme")["lineItems"][0]["name"]
        self.assertEqual(len(name), 299)
        self.assertTrue(name.endswith("…"))
        self.assertNotIn("  ", name)
        self.assertNotIn("\n", name)

    def test_line_name_forms(self):
        payload, cfg = self.payload()
        line = payload["lines"][0]
        line["label"], line["description"] = "Tue,  Nov 3", " did\tthings "
        self.assertEqual(mercury_api.invoice_body(payload, cfg, "acme")["lineItems"][0]["name"],
                         "Tue, Nov 3: did things")
        line["description"] = ""
        self.assertEqual(mercury_api.invoice_body(payload, cfg, "acme")["lineItems"][0]["name"],
                         "Tue, Nov 3")

    def test_optional_flags_come_from_config(self):
        payload, cfg = self.payload()
        m = cfg["clients"]["acme"]["mercury"]
        m.update(cc_emails=["x@example.com"], ach_debit_enabled=True,
                 credit_card_enabled=True, use_real_account_number=True)
        b = mercury_api.invoice_body(payload, cfg, "acme")
        self.assertEqual(b["ccEmails"], ["x@example.com"])
        self.assertIs(b["achDebitEnabled"], True)
        self.assertIs(b["creditCardEnabled"], True)
        self.assertIs(b["useRealAccountNumber"], True)

    def test_zero_or_negative_quantity_or_price_is_never_built(self):
        payload, cfg = self.payload()
        for field, value in (("hours", "0.00"), ("hours", "-1.00"),
                             ("rate", "0.00"), ("rate", "-150.00")):
            with self.subTest(field=field, value=value):
                bad = json.loads(json.dumps(payload))
                bad["lines"][0][field] = value
                with self.assertRaises(mercury_api.NotReady):
                    mercury_api.invoice_body(bad, cfg, "acme")

    def test_body_numbers_serialise_without_drift(self):
        payload, cfg = self.payload()
        payload["lines"][0]["hours"] = "1.33"
        payload["lines"][0]["rate"] = "149.99"
        text = json.dumps(mercury_api.invoice_body(payload, cfg, "acme"))
        self.assertIn('"quantity": 1.33', text)
        self.assertIn('"unitPrice": 149.99', text)


class ClientTests(unittest.TestCase):
    def test_http_base_url_refused_unless_loopback(self):
        def cfg(url):
            return {"mercury": {"api_key_env": "MERCURY_TEST_TOKEN", "base_url": url}}
        os.environ["MERCURY_TEST_TOKEN"] = "secret-token:TESTONLY123"
        self.addCleanup(os.environ.pop, "MERCURY_TEST_TOKEN", None)
        for url in ("http://api.mercury.com/api/v1", "http://example.com/api/v1",
                    "ftp://127.0.0.1/x", "https://", "api.mercury.com/api/v1"):
            with self.subTest(url=url):
                with self.assertRaises(mercury_api.MercuryError):
                    mercury_api.client_from_config(cfg(url))
        for url in ("http://127.0.0.1:9/api/v1/", "http://localhost:9/api/v1",
                    "https://api-sandbox.mercury.com/api/v1"):
            with self.subTest(url=url):
                c = mercury_api.client_from_config(cfg(url))
                self.assertFalse(c.base_url.endswith("/"))

    def test_only_mercury_hosts_and_loopback_without_the_override(self):
        os.environ["MERCURY_TEST_TOKEN"] = "secret-token:TESTONLY123"
        self.addCleanup(os.environ.pop, "MERCURY_TEST_TOKEN", None)

        def cfg(url, allow=None):
            m = {"api_key_env": "MERCURY_TEST_TOKEN", "base_url": url}
            if allow is not None:
                m["allow_custom_host"] = allow
            return {"mercury": m}
        for url in ("https://api.mercury.com/api/v1", "https://api-sandbox.mercury.com/api/v1",
                    "http://127.0.0.1:9/api/v1", "http://localhost:9/api/v1"):
            mercury_api.client_from_config(cfg(url))
        for url in ("https://example.com/api/v1", "https://api.mercury.com.evil.example/x",
                    "https://x.api.mercury.com/x"):
            with self.subTest(url=url):
                with self.assertRaises(mercury_api.MercuryError) as cm:
                    mercury_api.client_from_config(cfg(url))
                msg = str(cm.exception)
                self.assertIn(url.split("/")[2], msg)
                self.assertIn("mercury.allow_custom_host", msg)
                with self.assertRaises(mercury_api.MercuryError):
                    mercury_api.client_from_config(cfg(url, False))
                c = mercury_api.client_from_config(cfg(url, True))
                self.assertTrue(mercury_api.describe_host(c.base_url).startswith("CUSTOM "))
        with self.assertRaises(mercury_api.MercuryError):     # http stays loopback-only
            mercury_api.client_from_config(cfg("http://example.com/api/v1", True))

    def test_default_base_url_and_host_labels(self):
        os.environ["MERCURY_TEST_TOKEN"] = "secret-token:TESTONLY123"
        self.addCleanup(os.environ.pop, "MERCURY_TEST_TOKEN", None)
        c = mercury_api.client_from_config({"mercury": {"api_key_env": "MERCURY_TEST_TOKEN",
                                                         "base_url": ""}})
        self.assertEqual(c.base_url, "https://api.mercury.com/api/v1")
        self.assertEqual(mercury_api.describe_host(c.base_url), "PRODUCTION api.mercury.com")
        self.assertEqual(mercury_api.describe_host("https://api-sandbox.mercury.com/api/v1"),
                         "SANDBOX api-sandbox.mercury.com")
        self.assertEqual(mercury_api.describe_host("http://127.0.0.1:5/api/v1"),
                         "CUSTOM 127.0.0.1:5")
        for url in ("https://api-sandbox.evil.example/api/v1", "https://api.mercury.com.evil.example/x",
                    "https://api-sandbox.mercury.com.evil.example/x", "https://x.api.mercury.com/x"):
            self.assertTrue(mercury_api.describe_host(url).startswith("CUSTOM "), url)

    def test_repr_and_errors_hide_the_token(self):
        c = mercury_api.Client("http://127.0.0.1:9/api/v1", "secret-token:TESTONLY123")
        self.assertNotIn("TESTONLY123", repr(c))
        self.assertNotIn("TESTONLY123", str(c))
        with self.assertRaises(mercury_api.MercuryError) as cm:
            c.get("/accounts")                      # nothing listens on port 9
        self.assertNotIn("TESTONLY123", str(cm.exception))

    def test_malformed_token_is_rejected_without_echo(self):
        with self.assertRaises(mercury_api.MercuryError) as cm:
            mercury_api.Client("https://api.mercury.com/api/v1", "secret-token:TESTONLY123\nX: y")
        self.assertNotIn("TESTONLY123", str(cm.exception))


class CreateTests(MercuryCase):
    def test_create_posts_to_ar_invoices_with_bearer_token(self):
        self.setup()
        self.approve()
        r = self.create("--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        posts = self.fake.posts()
        self.assertEqual(len(posts), 1)
        req = posts[0]
        self.assertEqual(req["path"], "/api/v1/ar/invoices")
        self.assertEqual(req["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertTrue(req["headers"]["User-Agent"].startswith("timesheet/"))
        self.assertEqual(req["headers"]["Content-Type"], "application/json")
        self.assertEqual(req["body"]["sendEmailOption"], "DontSend")
        self.assertEqual(req["body"]["customerId"], CUSTOMER)
        self.assertIn(INVOICE_ID, r.stdout)
        self.assertIn("abc123slug", r.stdout)
        self.assertNotIn("https://app.mercury.com/pay/", r.stdout)     # not a production target
        self.assertIn("web app", r.stdout)
        self.assertIn("Unpaid", r.stdout)
        self.assertIn("The invoice was created but NOT sent. Open it in Mercury, attach the "
                      "timesheet PDF, and send it from there.", r.stdout)
        self.assertEqual(r.stderr, "")

    def test_create_writes_mercury_json_and_second_run_is_refused(self):
        self.setup()
        self.approve()
        self.assertEqual(self.create("--yes").returncode, 0)
        path = self.record_path()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path) as f:
            rec = json.load(f)
        self.assertEqual(rec["invoice_id"], INVOICE_ID)
        self.assertEqual(rec["slug"], "abc123slug")
        self.assertEqual(rec["status"], "Unpaid")
        self.assertEqual(rec["amount"], 63.0)
        self.assertEqual(rec["invoice_number"], "INV-7")
        self.assertEqual(rec["created_at"], "2026-11-12T12:00:00Z")
        self.assertEqual(rec["base_url"], self.fake.base_url)
        self.assertEqual(rec["reference"], "acme-" + WEEK)
        self.assertEqual(rec["total_sent"], "63.00")
        self.assertNotIn("amount_mismatch", rec)
        self.assertEqual(os.listdir(os.path.dirname(path)).count("mercury.json"), 1)
        self.assertFalse([n for n in os.listdir(os.path.dirname(path)) if n.endswith(".tmp")])
        r = self.create("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("already created in CUSTOM Mercury (%s) on 2026-11-12T12:00:00Z: id %s"
                      % (self.fake.base_url, INVOICE_ID), r.stderr)
        self.assertIn("--force", r.stderr)
        self.assertEqual(len(self.fake.posts()), 1)

    def test_force_allows_second_invoice(self):
        self.setup()
        self.approve()
        self.assertEqual(self.create("--yes").returncode, 0)
        self.fake.routes[("POST", "/api/v1/ar/invoices")] = (200, dict(CREATED, id="second-id"))
        r = self.create("--yes", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.fake.posts()), 2)
        with open(self.record_path()) as f:
            rec = json.load(f)
        self.assertEqual(rec["invoice_id"], "second-id")
        self.assertEqual([p["invoice_id"] for p in rec["previous"]], [INVOICE_ID])
        self.fake.routes[("POST", "/api/v1/ar/invoices")] = (200, dict(CREATED, id="third-id"))
        self.assertEqual(self.create("--yes", "--force").returncode, 0)
        with open(self.record_path()) as f:
            rec = json.load(f)
        self.assertEqual([p["invoice_id"] for p in rec["previous"]], [INVOICE_ID, "second-id"])
        self.assertTrue(all("previous" not in p for p in rec["previous"]))

    def test_without_yes_nothing_is_sent(self):
        self.setup()
        self.approve()
        r = self.create()
        self.assertEqual(r.returncode, 4)
        self.assertEqual(self.fake.requests, [])
        self.assertIn("re-run with --yes to create the invoice (it will NOT be emailed)",
                      r.stdout + r.stderr)
        for needle in ("CUSTOM 127.0.0.1", "Acme Co", CUSTOMER, "Nov 2", "$"):
            self.assertIn(needle, r.stdout)
        self.assertFalse(os.path.exists(self.record_path()))

    def test_dry_run_sends_nothing(self):
        self.setup()
        self.approve()
        r = self.create("--dry-run", token=None)       # no token needed
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.fake.requests, [])
        self.assertIn("127.0.0.1", r.stdout)
        start = r.stdout.index("{")
        body = json.loads(r.stdout[start:])
        self.assertEqual(body["sendEmailOption"], "DontSend")
        self.assertEqual(body["customerId"], CUSTOMER)
        self.assertFalse(os.path.exists(self.record_path()))

    def test_dry_run_with_yes_still_sends_nothing(self):
        self.setup()
        self.approve()
        self.assertEqual(self.create("--dry-run", "--yes").returncode, 0)
        self.assertEqual(self.fake.requests, [])

    def test_missing_token_exits_2_and_names_the_env_var(self):
        self.setup()
        self.approve()
        r = self.create("--yes", token=None)
        self.assertEqual(r.returncode, 2)
        self.assertIn("MERCURY_API_KEY", r.stderr)
        self.assertEqual(self.fake.requests, [])
        r = self.create("--yes", token="   ")
        self.assertEqual(r.returncode, 2)

    def test_custom_env_var_name_is_used(self):
        cfg = mercury_cfg(self.fake.base_url)
        cfg["mercury"]["api_key_env"] = "MERCURY_OTHER_VAR"
        self.setup(cfg)
        self.approve()
        r = self.create("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("MERCURY_OTHER_VAR", r.stderr)
        r = self.create("--yes", env={"MERCURY_OTHER_VAR": TOKEN})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_token_never_printed_or_stored(self):
        self.setup()
        self.approve()
        outputs = []
        r = self.create("--yes")
        outputs.append(r)
        self.fake.routes[("POST", "/api/v1/ar/invoices")] = (400, {"error": "bad"})
        outputs.append(self.create("--yes", "--force"))
        self.assertEqual(outputs[1].returncode, 5)
        self.fake.drop_posts = True
        outputs.append(self.create("--yes", "--force"))
        self.assertEqual(outputs[2].returncode, 5)
        outputs.append(self.create("--dry-run"))
        outputs.append(self.mercury("accounts"))
        for r in outputs:
            self.assertNotIn("TESTONLY123", r.stdout)
            self.assertNotIn("TESTONLY123", r.stderr)
        self.assertNotIn("TESTONLY123", self.all_text_under_home())

    def test_token_echoed_by_server_is_scrubbed(self):
        self.setup()
        self.approve()
        self.fake.routes[("POST", "/api/v1/ar/invoices")] = (401, {"error": "bad " + TOKEN})
        r = self.create("--yes")
        self.assertEqual(r.returncode, 5)
        self.assertNotIn("TESTONLY123", r.stderr)

    def test_failed_post_exits_5_and_writes_nothing(self):
        self.setup()
        self.approve()
        self.fake.routes[("POST", "/api/v1/ar/invoices")] = (400, {"error": "x" * 900})
        r = self.create("--yes")
        self.assertEqual(r.returncode, 5)
        self.assertIn("400", r.stderr)
        self.assertLess(len(r.stderr), 900)
        self.assertNotIn("may have reached", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(os.path.exists(self.record_path()))

    def test_dropped_connection_is_reported_as_ambiguous(self):
        self.setup()
        self.approve()
        self.fake.drop_posts = True
        r = self.create("--yes")
        self.assertEqual(r.returncode, 5)
        self.assertIn('The request may have reached Mercury. Check CUSTOM Mercury for an '
                      'invoice with memo "Timesheet acme-%s attached." If none exists, re-run '
                      'with --force.' % WEEK, r.stderr)
        self.assertEqual(self.read_record()["status"], "pending")

    def test_refused_connection_is_not_ambiguous(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.setup(mercury_cfg("http://127.0.0.1:%d/api/v1" % port))
        self.approve()
        r = self.create("--yes")
        self.assertEqual(r.returncode, 5)
        self.assertNotIn("may have reached", r.stderr)

    def test_amount_mismatch_is_reported(self):
        self.setup()
        self.approve()
        self.fake.routes[("POST", "/api/v1/ar/invoices")] = (200, dict(CREATED, amount=99.5))
        r = self.create("--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING: Mercury computed 99.50 but the timesheet total is 63.00. "
                      "Do not send the invoice until this is resolved.", r.stderr)
        with open(self.record_path()) as f:
            rec = json.load(f)
        self.assertIs(rec["amount_mismatch"], True)

    def test_matching_amount_prints_no_warning(self):
        self.setup()
        self.approve()
        self.fake.routes[("POST", "/api/v1/ar/invoices")] = (200, dict(CREATED, amount=63))
        self.assertNotIn("WARNING", self.create("--yes").stderr)

    def test_disabled_or_unconfigured_client_is_refused(self):
        for tweak, word in (({"enabled": False}, "not enabled"),
                            ({"customer_id": ""}, "customer_id"),
                            ({"destination_account_id": ""}, "destination_account_id")):
            with self.subTest(tweak=tweak):
                self.setup(mercury_cfg(self.fake.base_url, **tweak))
                self.approve()
                r = self.create("--yes")
                self.assertEqual(r.returncode, 2)
                self.assertIn(word, r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                self.assertEqual(self.fake.requests, [])

    def test_default_client_block_means_disabled(self):
        cfg = two_clients()
        cfg["mercury"] = {"api_key_env": "MERCURY_API_KEY", "base_url": self.fake.base_url}
        self.setup(cfg)
        self.approve()
        r = self.create("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not enabled", r.stderr)

    def test_empty_period_makes_no_request(self):
        self.setup()
        r = self.create("--yes", period="2026-09-07_2026-09-13")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no", r.stderr.lower())
        self.assertEqual(self.fake.requests, [])

    def test_stale_or_unapproved_draft_is_refused(self):
        self.setup()
        r = self.create("--yes")                              # never approved
        self.assertEqual(r.returncode, 2)
        self.assertIn("invoice.py save", r.stderr)
        self.approve()
        self.write_events(list(EVENTS) + turn("2026-11-04", "09:00", "09:30", "s9",
                                              "project-a"))   # numbers change after approval
        for extra in (("--yes",), ("--dry-run",), ()):
            r = self.create(*extra)
            self.assertEqual(r.returncode, 2, extra)
            self.assertIn("invoice.py save", r.stderr)
        self.assertEqual(self.fake.requests, [])

    def test_ach_with_non_usd_currency_is_refused(self):
        cfg = mercury_cfg(self.fake.base_url, ach_debit_enabled=True)
        cfg["clients"]["acme"]["overrides"]["currency"] = "EUR"
        self.setup(cfg)
        self.approve()
        r = self.create("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("ACH", r.stderr)
        self.assertEqual(self.fake.requests, [])

    def test_rate_with_sub_cent_precision_is_refused(self):
        cfg = mercury_cfg(self.fake.base_url)
        cfg["clients"]["acme"]["overrides"]["hourly_rate"] = 150.005
        self.setup(cfg)
        self.approve()
        r = self.create("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.fake.requests, [])

    def test_other_clients_lines_never_sent(self):
        self.setup()
        self.approve("acme", descriptions="ACME-ONLY-WORK")
        self.approve("beta", "2026-10-01_2026-10-31", descriptions="BETA-SECRET-WORK")
        r = self.create("--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        raw = self.fake.posts()[0]["raw"]
        self.assertIn("ACME-ONLY-WORK", raw)
        self.assertNotIn("BETA-SECRET-WORK", raw)
        self.assertNotIn("200", raw)
        self.assertNotIn("Beta", raw)
        body = self.fake.posts()[0]["body"]
        self.assertEqual({li["unitPrice"] for li in body["lineItems"]}, {150.0})
        self.assertNotIn("BETA-SECRET-WORK", r.stdout + r.stderr)

    def test_unknown_client_and_missing_client_flag(self):
        self.setup()
        r = self.mercury("create-invoice", "--now", NOW)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--client", r.stderr)
        r = self.create("--yes", client="nope")
        self.assertEqual(r.returncode, 2)


RENDER_FIRST = ("render the timesheet first (invoice.py render): the invoice must match the "
                "PDF you attach")


class GuardTests(MercuryCase):
    def test_unrendered_or_re_approved_draft_is_refused(self):
        self.setup()
        self.approve(render=False)
        for extra in (("--dry-run",), ("--yes",), ()):
            r = self.create(*extra)
            self.assertEqual(r.returncode, 2, extra)
            self.assertIn(RENDER_FIRST, r.stderr)
        self.assertEqual(self.run_cli("render", "--now", NOW, "--no-open", "--client",
                                      "acme").returncode, 0)
        self.assertEqual(self.create("--dry-run").returncode, 0)
        # re-approval at new numbers removes the rendered files: render again first
        self.write_events(list(EVENTS) + turn("2026-11-04", "09:00", "09:30", "s9", "project-a"))
        self.assertEqual(self.save({}, "--client", "acme").returncode, 0)
        d = self.draft_dir("acme", WEEK)
        for name in ("timesheet.html", "render.json"):
            self.assertFalse(os.path.exists(os.path.join(d, name)), name)
        r = self.create("--dry-run")
        self.assertEqual(r.returncode, 2)
        self.assertIn(RENDER_FIRST, r.stderr)
        self.assertEqual(self.fake.requests, [])

    def test_render_record_with_other_numbers_is_refused(self):
        self.setup()
        self.approve()
        path = os.path.join(self.draft_dir("acme", WEEK), "render.json")
        with open(path) as f:
            rec = json.load(f)
        rec["total"] = "1.00"
        with open(path, "w") as f:
            json.dump(rec, f)
        r = self.create("--dry-run")
        self.assertEqual(r.returncode, 2)
        self.assertIn(RENDER_FIRST, r.stderr)

    def test_overlapping_approved_period_is_refused_unless_allowed(self):
        self.setup()
        self.approve()                                    # acme, Nov 2-8
        month = "2026-11-01_2026-11-30"
        r = self.save({}, "--client", "acme", "--period", month, "--allow-overlap")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_cli("render", "--now", NOW, "--no-open", "--client", "acme",
                         "--period", month)
        self.assertEqual(r.returncode, 0, r.stderr)
        for extra in (("--dry-run",), ("--yes",)):
            r = self.create(*extra, period=month)
            self.assertEqual(r.returncode, 2, extra)
            self.assertIn("overlaps already approved period(s)", r.stderr)
            self.assertIn(WEEK, r.stderr)
            self.assertIn("$63.00", r.stderr)
            self.assertIn("--allow-overlap", r.stderr)
        self.assertEqual(self.fake.requests, [])
        r = self.create("--dry-run", "--allow-overlap", "--allow-incomplete", period=month)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.create("--yes", "--allow-overlap", "--allow-incomplete", period=month)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.fake.posts()), 1)
        # the week now overlaps a period with a Mercury record
        r = self.create("--dry-run")
        self.assertEqual(r.returncode, 2)
        self.assertIn(month, r.stderr)
        self.assertIn("Mercury", r.stderr)

    def test_incomplete_period_is_refused_unless_allowed(self):
        self.setup(events=list(EVENTS) + turn("2026-11-10", "09:00", "09:30", "s8", "project-a"))
        self.approve(period="this")
        for extra in (("--dry-run",), ("--yes",)):
            r = self.create(*extra, period="this")
            self.assertEqual(r.returncode, 2, extra)
            self.assertIn("not complete", r.stderr)
            self.assertIn("--allow-incomplete", r.stderr)
        self.assertEqual(self.fake.requests, [])
        r = self.create("--dry-run", "--allow-incomplete", period="this")
        self.assertEqual(r.returncode, 0, r.stderr)


class PendingRecordTests(MercuryCase):
    POST = ("POST", "/api/v1/ar/invoices")

    def ready(self, cfg=None):
        self.setup(cfg)
        self.approve()

    def test_500_keeps_pending_and_blocks_next_run_until_force(self):
        self.ready()
        self.fake.routes[self.POST] = (500, {"error": "boom"})
        r = self.create("--yes")
        self.assertEqual(r.returncode, 5)
        self.assertIn("500", r.stderr)
        self.assertIn('The request may have reached Mercury. Check CUSTOM Mercury for an '
                      'invoice with memo "Timesheet acme-%s attached." If none exists, re-run '
                      'with --force.' % WEEK, r.stderr)
        rec = self.read_record()
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(rec["reference"], "acme-" + WEEK)
        self.assertEqual(rec["base_url"], self.fake.base_url)
        self.assertEqual(rec["target"], "CUSTOM")
        self.assertEqual(rec["total_sent"], "63.00")
        self.assertIn("attempted_at", rec)
        self.assertEqual(os.stat(self.record_path()).st_mode & 0o777, 0o600)
        r = self.create("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("an earlier attempt on %s against CUSTOM Mercury (%s) did not finish "
                      "and may have created an invoice" % (rec["attempted_at"],
                                                           self.fake.base_url), r.stderr)
        self.assertIn("--force", r.stderr)
        self.assertEqual(len(self.fake.posts()), 1)
        self.fake.routes[self.POST] = (200, dict(CREATED))
        r = self.create("--yes", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.fake.posts()), 2)
        rec = self.read_record()
        self.assertEqual(rec["invoice_id"], INVOICE_ID)
        self.assertEqual([p["status"] for p in rec["previous"]], ["pending"])

    def test_closed_socket_keeps_pending_and_blocks(self):
        self.ready()
        self.fake.drop_posts = True
        self.assertEqual(self.create("--yes").returncode, 5)
        self.assertEqual(self.read_record()["status"], "pending")
        self.fake.drop_posts = False
        r = self.create("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(len(self.fake.posts()), 1)
        self.assertEqual(self.create("--yes", "--force").returncode, 0)
        self.assertEqual(len(self.fake.posts()), 2)

    def test_400_leaves_no_record_and_allows_retry(self):
        self.ready()
        self.fake.routes[self.POST] = (400, {"error": "bad"})
        r = self.create("--yes")
        self.assertEqual(r.returncode, 5)
        self.assertNotIn("may have reached", r.stderr)
        self.assertFalse(os.path.exists(self.record_path()))
        self.fake.routes[self.POST] = (200, dict(CREATED))
        self.assertEqual(self.create("--yes").returncode, 0)
        self.assertEqual(len(self.fake.posts()), 2)

    def test_refused_connection_leaves_no_record(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.ready(mercury_cfg("http://127.0.0.1:%d/api/v1" % port))
        self.assertEqual(self.create("--yes").returncode, 5)
        self.assertFalse(os.path.exists(self.record_path()))

    def test_unambiguous_failure_under_force_restores_the_earlier_record(self):
        self.ready()
        self.assertEqual(self.create("--yes").returncode, 0)
        with open(self.record_path()) as f:
            before = f.read()
        self.fake.routes[self.POST] = (400, {"error": "bad"})
        self.assertEqual(self.create("--yes", "--force").returncode, 5)
        with open(self.record_path()) as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(os.stat(self.record_path()).st_mode & 0o777, 0o600)

    def test_record_that_is_not_utf8_refuses_without_a_traceback(self):
        self.ready()
        with open(self.record_path(), "wb") as f:
            f.write(b"\xff\xfe")
        r = self.create("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("a record exists but cannot be read", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.fake.posts(), [])
        with open(self.record_path(), "rb") as f:
            self.assertEqual(f.read(), b"\xff\xfe")

    def test_force_restores_the_earlier_record_byte_for_byte(self):
        self.ready()
        raw = (b'{\r\n  "invoice_id": "inv-old",\r\n  "status": "Unpaid"\r\n}\r\n')
        with open(self.record_path(), "wb") as f:
            f.write(raw)
        self.fake.routes[self.POST] = (400, {"error": "bad"})
        self.assertEqual(self.create("--yes", "--force").returncode, 5)
        with open(self.record_path(), "rb") as f:
            self.assertEqual(f.read(), raw)

    def test_redirect_is_not_followed_and_keeps_pending(self):
        self.ready()
        self.fake.routes[self.POST] = (302, {})
        r = self.create("--yes")
        self.assertEqual(r.returncode, 5)
        self.assertIn("The request may have reached Mercury", r.stderr)
        self.assertEqual(len(self.fake.posts()), 1)
        self.assertEqual(self.read_record()["status"], "pending")

    def test_2xx_without_an_invoice_id_keeps_pending(self):
        self.ready()
        self.fake.routes[self.POST] = (200, {"unexpected": True})
        r = self.create("--yes")
        self.assertEqual(r.returncode, 5)
        self.assertIn("The request may have reached Mercury", r.stderr)
        self.assertEqual(self.read_record()["status"], "pending")

    def test_pending_record_exists_while_the_post_is_in_flight(self):
        self.ready()
        seen = {}
        path = self.record_path()

        class Requests(list):
            def append(self, item):
                with open(path) as f:
                    seen["rec"] = json.load(f)
                super().append(item)
        self.fake.requests = Requests()
        self.assertEqual(self.create("--yes").returncode, 0)
        self.assertEqual(seen["rec"]["status"], "pending")
        self.assertEqual(self.read_record()["status"], "Unpaid")

    def test_complete_record_blocks_and_names_what_is_on_file(self):
        self.ready()
        self.assertEqual(self.create("--yes").returncode, 0)
        r = self.create("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("id " + INVOICE_ID, r.stderr)
        self.assertNotIn("note: that record is for", r.stderr)

    def test_record_from_another_target_still_blocks_and_says_so(self):
        self.ready()
        self.assertEqual(self.create("--yes").returncode, 0)
        rec = self.read_record()
        rec["target"] = "SANDBOX"
        with open(self.record_path(), "w") as f:
            json.dump(rec, f)
        other = FakeMercury()
        self.addCleanup(other.close)
        other.routes[self.POST] = (200, dict(CREATED))
        self.setup(mercury_cfg(other.base_url))
        self.approve()
        r = self.create("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("note: that record is for SANDBOX; this run targets CUSTOM", r.stderr)
        self.assertEqual(other.requests, [])

    def test_dry_run_and_summary_note_an_existing_record(self):
        self.ready()
        self.assertEqual(self.create("--yes").returncode, 0)
        line = "NOTE: a Mercury record already exists for this period (%s, CUSTOM)" % INVOICE_ID
        for extra in (("--dry-run",), ()):
            r = self.create(*extra)
            self.assertIn(line, r.stdout, extra)
        os.unlink(self.record_path())
        self.fake.routes[self.POST] = (500, {})
        self.create("--yes")
        r = self.create("--dry-run")
        self.assertIn("NOTE: a Mercury record already exists for this period (pending, CUSTOM)",
                      r.stdout)
        body = json.loads(r.stdout[r.stdout.index("{"):])
        self.assertEqual(body["sendEmailOption"], "DontSend")

    def test_compute_shows_a_pending_record_as_is(self):
        self.ready()
        self.fake.routes[self.POST] = (500, {})
        self.create("--yes")
        r = self.run_cli("compute", "--now", NOW, "--client", "acme")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["mercury"]["status"], "pending")

    def test_created_at_falls_back_to_the_run_time(self):
        self.ready()
        created = dict(CREATED)
        del created["createdAt"]
        self.fake.routes[self.POST] = (200, created)
        self.assertEqual(self.create("--yes").returncode, 0)
        self.assertEqual(self.read_record()["created_at"], NOW)


class FinaliseTests(unittest.TestCase):
    def test_failed_final_write_prints_the_record_and_exits_5(self):
        import contextlib
        import io
        from unittest import mock
        import mercury
        record = {"invoice_id": INVOICE_ID, "slug": "abc123slug", "status": "Unpaid"}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(mercury, "_write_record", side_effect=OSError("disk full")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mercury._finalise("/nonexistent", record)
        self.assertEqual(code, 5)
        self.assertIn(INVOICE_ID, out.getvalue())
        self.assertIn("abc123slug", out.getvalue())
        self.assertEqual(json.loads(out.getvalue()[out.getvalue().index("{"):]), record)
        self.assertIn("disk full", err.getvalue())


class ReadCommandTests(MercuryCase):
    def test_accounts_lists_id_name_kind_status(self):
        self.setup()
        self.fake.routes[("GET", "/api/v1/accounts")] = (200, {"accounts": [
            {"id": ACCOUNT, "name": "Ops Checking", "nickname": "Ops", "kind": "checking",
             "type": "mercury", "status": "active"}], "page": {"nextPage": None}})
        r = self.mercury("accounts")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(ACCOUNT, r.stdout)
        self.assertIn("checking", r.stdout)
        self.assertIn("active", r.stdout)
        self.assertEqual(self.fake.requests[0]["headers"]["Authorization"], "Bearer " + TOKEN)

    def test_customers_follows_pagination(self):
        self.setup()
        self.fake.routes[("GET", "/api/v1/ar/customers")] = [
            (200, {"customers": [{"id": "c1", "name": "First Co", "email": "a@example.com",
                                  "deletedAt": None}],
                   "page": {"nextPage": "c1"}}),
            (200, {"customers": [{"id": "c2", "name": "Second Co", "email": "b@example.com",
                                  "deletedAt": None},
                                 {"id": "c3", "name": "Gone Co", "email": "c@example.com",
                                  "deletedAt": "2026-01-01T00:00:00Z"}],
                   "page": {"nextPage": None}})]
        r = self.mercury("customers")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.fake.requests), 2)
        self.assertNotIn("start_after", self.fake.requests[0]["query"])
        self.assertIn("start_after=c1", self.fake.requests[1]["query"])
        self.assertIn("First Co", r.stdout)
        self.assertIn("Second Co", r.stdout)
        self.assertNotIn("Gone Co", r.stdout)

    def test_customers_find_is_case_insensitive_on_name_and_email(self):
        self.setup()
        self.fake.routes[("GET", "/api/v1/ar/customers")] = (200, {"customers": [
            {"id": "c1", "name": "First Co", "email": "a@example.com"},
            {"id": "c2", "name": "Second Co", "email": "FIRSTLY@example.com"},
            {"id": "c3", "name": "Third", "email": "t@example.com"}],
            "page": {"nextPage": None}})
        r = self.mercury("customers", "--find", "first")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("c1", r.stdout)
        self.assertIn("c2", r.stdout)
        self.assertNotIn("c3", r.stdout)

    def test_create_customer_needs_yes(self):
        self.setup()
        r = self.mercury("create-customer", "--name", "Placeholder Co", "--email", "x@example.com")
        self.assertEqual(r.returncode, 4)
        self.assertIn("Placeholder Co", r.stdout)
        self.assertEqual(self.fake.requests, [])
        self.fake.routes[("POST", "/api/v1/ar/customers")] = (200, {"id": "new-id"})
        r = self.mercury("create-customer", "--name", "Placeholder Co",
                         "--email", "x@example.com", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("new-id", r.stdout)
        self.assertEqual(self.fake.posts()[0]["body"],
                         {"name": "Placeholder Co", "email": "x@example.com"})

    def test_api_error_exits_5_without_traceback(self):
        self.setup()
        self.fake.routes[("GET", "/api/v1/accounts")] = (403, {"error": "forbidden"})
        r = self.mercury("accounts")
        self.assertEqual(r.returncode, 5)
        self.assertIn("403", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_accounts_without_token_exits_2(self):
        self.setup()
        r = self.mercury("accounts", token=None)
        self.assertEqual(r.returncode, 2)
        self.assertIn("MERCURY_API_KEY", r.stderr)


if __name__ == "__main__":
    unittest.main()
