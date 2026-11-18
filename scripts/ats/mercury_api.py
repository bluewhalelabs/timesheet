"""Minimal client for Mercury's Accounts Receivable API (stdlib only).

Creates invoices that are never emailed: every invoice body carries
sendEmailOption "DontSend". The token comes only from the environment variable
named in the config and is never part of any message, repr or file."""
import http.client
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from ats import VERSION
from ats.cli import flat

__all__ = ["MercuryError", "ConfigProblem", "NetworkError", "NotReady", "Client",
           "client_from_config", "resolve_base_url", "describe_host", "invoice_body",
           "check_ready", "DEFAULT_BASE_URL", "pay_url", "target_label", "is_ambiguous",
           "memo", "money_text"]

DEFAULT_BASE_URL = "https://api.mercury.com/api/v1"
_TIMEOUT = 30
_BODY_LIMIT = 500
_MAX_PAGES = 1000
_NAME_LIMIT = 299
_CENTS = Decimal("0.01")
_MERCURY_HOSTS = ("api.mercury.com", "api-sandbox.mercury.com")
_LOOPBACK = ("127.0.0.1", "localhost")


class MercuryError(Exception):
    """A failed Mercury request. status is None when no HTTP response arrived."""

    def __init__(self, status, body=""):
        self.status = status
        self.body = body
        if status is None:
            text = "Mercury request failed: %s" % body
        else:
            text = "Mercury returned HTTP %s: %s" % (status, body)
        super().__init__(text)


class NetworkError(MercuryError):
    """No usable HTTP response. ambiguous is True when the request may have been
    received (timeout or reset after sending), so a write may have happened."""

    def __init__(self, body, ambiguous):
        super().__init__(None, body)
        self.ambiguous = ambiguous


class ConfigProblem(MercuryError):
    """Local configuration is unusable (token, base URL); nothing was sent."""


class NotReady(ValueError):
    """The draft or the client's Mercury settings cannot produce an invoice."""


def _cut(text, limit=_BODY_LIMIT):
    return text if len(text) <= limit else text[:limit]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):      # never forward the Authorization header
        return None


class Client:
    def __init__(self, base_url: str, token: str):
        if not token or token != token.strip() or not re.fullmatch(r"[\x21-\x7e]+", token):
            raise ConfigProblem(None, "the API token is empty or contains whitespace or "
                                      "non-printable characters")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __repr__(self):
        return "Client(base_url=%r, token=<hidden>)" % self.base_url

    __str__ = __repr__

    def _scrub(self, text):
        return text.replace(self._token, "<token>")

    def _request(self, method, path, params=None, body=None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json",
                   "User-Agent": "timesheet/%s" % VERSION}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=_TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            try:
                text = e.read().decode("utf-8", "replace")
            except Exception:
                text = ""
            raise MercuryError(e.code, _cut(self._scrub(text))) from None
        except urllib.error.URLError as e:
            reason = e.reason
            refused = isinstance(reason, (ConnectionRefusedError, socket.gaierror))
            raise NetworkError(self._scrub("%s" % (reason,)), not refused) from None
        except (socket.timeout, ConnectionError, http.client.HTTPException, OSError) as e:
            raise NetworkError(self._scrub("%s: %s" % (type(e).__name__, e)), True) from None
        try:
            return json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except ValueError:
            raise MercuryError(200, "response was not valid JSON: %s"
                               % _cut(self._scrub(raw.decode("utf-8", "replace")))) from None

    def get(self, path, params=None):
        return self._request("GET", path, params=params)

    def post(self, path, body):
        return self._request("POST", path, body=body)

    def paged(self, path, key):
        """All items of a paginated list, following page.nextPage."""
        items, after = [], None
        for _ in range(_MAX_PAGES):
            params = {"limit": 500}
            if after:
                params["start_after"] = after
            data = self.get(path, params)
            items.extend(data.get(key) or [])
            after = (data.get("page") or {}).get("nextPage")
            if not after:
                return items
        raise MercuryError(None, "too many pages while listing %s" % path)


def resolve_base_url(cfg: dict) -> str:
    url = (cfg.get("mercury", {}).get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    if not host or parts.scheme not in ("http", "https"):
        raise ConfigProblem(None, "mercury.base_url must be an https URL, got %r" % url)
    if parts.scheme != "https" and host not in _LOOPBACK:
        raise ConfigProblem(None, "mercury.base_url must use https (http is only allowed "
                                  "for 127.0.0.1 and localhost), got %r" % url)
    allow = cfg.get("mercury", {}).get("allow_custom_host") is True
    if host not in _MERCURY_HOSTS and host not in _LOOPBACK and not allow:
        raise ConfigProblem(None, "mercury.base_url host %s is not a Mercury API host "
                                  "(api.mercury.com, api-sandbox.mercury.com); set "
                                  "mercury.allow_custom_host to true only if you really "
                                  "mean to send invoices there" % host)
    return url


def target_label(base_url: str) -> str:
    """SANDBOX, PRODUCTION or CUSTOM, by exact host."""
    host = urllib.parse.urlsplit(base_url).hostname or ""
    if host == "api-sandbox.mercury.com":
        return "SANDBOX"
    if host == "api.mercury.com":
        return "PRODUCTION"
    return "CUSTOM"


def is_ambiguous(error) -> bool:
    """True when a failed write may nevertheless have reached Mercury: timeouts and
    resets, any 5xx, a redirect, or an unreadable 2xx. A 4xx, a refused connection
    and local configuration problems are unambiguous (nothing was created)."""
    if isinstance(error, ConfigProblem):
        return False
    if isinstance(error, NetworkError):
        return error.ambiguous
    status = error.status
    if status is None:
        return False
    return status >= 500 or 300 <= status < 400 or 200 <= status < 300


def describe_host(base_url: str) -> str:
    parts = urllib.parse.urlsplit(base_url)
    host = parts.hostname or ""
    return "%s %s" % (target_label(base_url), parts.netloc)


def client_from_config(cfg: dict) -> Client:
    base = resolve_base_url(cfg)
    env = cfg.get("mercury", {}).get("api_key_env") or "MERCURY_API_KEY"
    token = os.environ.get(env, "")
    if not token.strip():
        raise ConfigProblem(None, "environment variable %s is not set (it must hold the "
                                  "Mercury API token)" % env)
    return Client(base, token)


def pay_url(slug) -> str:
    return "https://app.mercury.com/pay/%s" % slug


def _line_name(line: dict) -> str:
    label, desc = flat(line.get("label", "")), flat(line.get("description", ""))
    name = "%s: %s" % (label, desc) if desc else label
    if len(name) > _NAME_LIMIT:
        name = name[:_NAME_LIMIT - 1] + "…"
    return name or "Professional services"


def _mercury_block(cfg: dict, client: str) -> dict:
    return cfg["clients"][client]["mercury"]


def check_ready(payload: dict, cfg: dict, client: str, is_approved) -> None:
    """Raise NotReady, with a user-facing reason, unless this draft may be invoiced."""
    m = _mercury_block(cfg, client)
    if not m.get("enabled"):
        raise NotReady("Mercury is not enabled for client %s (set clients.%s.mercury.enabled)"
                       % (client, client))
    for k in ("customer_id", "destination_account_id"):
        if not m.get(k):
            raise NotReady("clients.%s.mercury.%s is empty (see mercury.py customers / accounts)"
                           % (client, k))
    if not payload["lines"]:
        raise NotReady("no tracked time for %s in %s; nothing to invoice"
                       % (payload["client"]["name"], payload["period"]["label"]))
    if not is_approved(payload):
        why = "numbers changed since approval" if payload.get("stale") else "the draft is not approved"
        raise NotReady("%s: review it and run invoice.py save before creating the invoice "
                       "(%s, %s)" % (why, client, payload["period"]["id"]))
    if m.get("ach_debit_enabled") and payload["currency"] != "USD":
        raise NotReady("ACH debit is USD only but this client bills in %s; turn off "
                       "ach_debit_enabled" % payload["currency"])
    for l in payload["lines"]:
        if Decimal(l["rate"]) != Decimal(l["rate"]).quantize(_CENTS):
            raise NotReady("rate %s is not a multiple of 0.01, which Mercury requires"
                           % l["rate"])


def invoice_body(payload: dict, cfg: dict, client: str) -> dict:
    m = _mercury_block(cfg, client)
    start = date.fromisoformat(payload["period"]["start"])
    end = date.fromisoformat(payload["period"]["end"])
    due = end + timedelta(days=m.get("due_in_days", 15))
    # Decimal strings with at most two places survive float() and json.dumps exactly.
    for l in payload["lines"]:       # last line of defence: build_lines already drops zero hours
        if Decimal(l["hours"]) <= 0 or Decimal(l["rate"]) <= 0:
            raise NotReady("refusing to send a line with zero or negative quantity or unit price")
    items = [{"name": _line_name(l), "quantity": float(l["hours"]), "unitPrice": float(l["rate"])}
             for l in payload["lines"]]
    return {
        "customerId": m["customer_id"],
        "destinationAccountId": m["destination_account_id"],
        "invoiceDate": end.isoformat(),
        "dueDate": due.isoformat(),
        "servicePeriodStartDate": start.isoformat(),
        "servicePeriodEndDate": end.isoformat(),
        "lineItems": items,
        "ccEmails": list(m.get("cc_emails", [])),
        "achDebitEnabled": bool(m.get("ach_debit_enabled", False)),
        "creditCardEnabled": bool(m.get("credit_card_enabled", False)),
        "useRealAccountNumber": bool(m.get("use_real_account_number", False)),
        "payerMemo": memo(payload),
        "internalNote": "Created by timesheet for %s" % payload["period"]["label"],
        "currencyCode": payload["currency"],
        "sendEmailOption": "DontSend",
    }


def memo(payload: dict) -> str:
    return "Timesheet %s attached." % payload["reference"]


def money_text(value) -> Optional[str]:
    """value as a 2-place decimal string, or None when it is not a number."""
    try:
        return str(Decimal(str(value)).quantize(_CENTS))
    except (InvalidOperation, ValueError, TypeError):
        return None
