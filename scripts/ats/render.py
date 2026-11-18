"""Timesheet rendering: fill the HTML template from a draft payload and turn it into
a PDF with a headless Chrome/Edge/Chromium.

Test/diagnostic switch: TIMESHEET_NO_BROWSER=1 makes find_browser return None."""
import base64
import html as _html
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

from ats import config, lines

__all__ = ["RenderError", "html", "logo_warning", "find_browser", "pdf"]

_TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "templates", "timesheet.html")
_SLOT = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_LOGO_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp"}
_LOGO_MAX = 2 * 1024 * 1024
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Lookup tables, module-level so tests can patch them.
_MAC_APPS = ["Google Chrome", "Chromium", "Microsoft Edge", "Brave Browser"]
_MAC_DIRS = ["/Applications", "~/Applications"]
_PATH_NAMES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
               "microsoft-edge"]


class RenderError(ValueError):
    pass


def _e(value) -> str:
    return _html.escape("" if value is None else str(value), quote=True)


def _multiline(text) -> str:
    return "<br>".join(_e(l.strip()) for l in str(text or "").splitlines() if l.strip())


def _div(cls: str, inner: str) -> str:
    return '<p class="%s">%s</p>' % (cls, inner) if inner else ""


# ---------------------------------------------------------------- logo

def _logo(cfg: dict) -> Tuple[str, Optional[str]]:
    """(img tag or "", reason the configured logo was left out or None)."""
    raw = ((cfg.get("you") or {}).get("logo") or "").strip()
    if not raw:
        return "", None
    path = config.expand(raw)
    mime = _LOGO_TYPES.get(os.path.splitext(path)[1].lower())
    if mime is None:
        return "", "logo %s: unsupported type (use png, jpg, gif, svg or webp)" % raw
    try:
        if os.path.getsize(path) > _LOGO_MAX:
            return "", "logo %s: larger than 2 MB" % raw
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return "", "logo %s: cannot read (%s)" % (raw, e.strerror or e)
    uri = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
    return '<img class="logo" alt="" src="%s">' % uri, None


def logo_warning(cfg: dict) -> Optional[str]:
    """Why the configured logo will be omitted, or None."""
    return _logo(cfg)[1]


# ---------------------------------------------------------------- fragments

def _you(cfg: dict) -> Tuple[str, str]:
    you = cfg.get("you") or {}
    name, business = (you.get("name") or "").strip(), (you.get("business") or "").strip()
    head = business or name
    parts = []
    if business and name and name != business:
        parts.append(_div("meta", _e(name)))
    parts.append(_div("meta", _e((you.get("email") or "").strip())))
    parts.append(_div("meta", _multiline(you.get("address"))))
    return _e(head), "".join(parts)


def _client(payload: dict, cfg: dict) -> str:
    key = payload["client"]["key"]
    c = (cfg.get("clients") or {}).get(key) or {}
    return "".join([_div("name", _e(payload["client"]["name"])),
                    _div("meta", _multiline(c.get("contact"))),
                    _div("meta", _multiline(c.get("address")))])


def _money(value, currency: str) -> str:
    return lines.money(Decimal(str(value)), currency)


def _rows(payload: dict) -> str:
    cur = payload["currency"]
    out = []
    for l in payload["lines"]:
        out.append(
            '    <tr><td class="date">%s</td><td class="desc">%s</td>'
            '<td class="num hours">%s</td><td class="num rate">%s</td>'
            '<td class="num amt">%s</td></tr>'
            % (_e(l["label"]), _e(l.get("description", "")), _e(l["hours"]),
               _e(_money(l["rate"], cur)), _e(_money(l["amount"], cur))))
    return "\n".join(out)


def _subtotals(payload: dict) -> str:
    subs = payload.get("subtotals") or []
    if not payload.get("show_project_subtotals") or len(subs) < 2:
        return ""
    cur = payload["currency"]
    rows = "".join(
        '<tr><td>%s</td><td class="num">%s</td><td class="num">%s</td></tr>'
        % (_e(s["label"]), _e(s["hours"]), _e(_money(s["amount"], cur))) for s in subs)
    return ('<section class="subtotals"><h3>By project (before per-line rounding)</h3>'
            '<p class="subnote">Project figures are rounded once per project and may differ '
            'slightly from the line total above.</p><table><thead><tr><th>Project</th>'
            '<th class="num">Hours</th><th class="num">Amount</th></tr></thead>'
            '<tbody>%s</tbody></table></section>' % rows)


def _hm(seconds) -> str:
    minutes = int(round(float(seconds) / 60.0))
    return "%dh %02dm" % (minutes // 60, minutes % 60)


def _day_label(d) -> str:
    return "%s, %s %d, %d" % (_DAYS[d.weekday()], _MONTHS[d.month - 1], d.day, d.year)


def _session_day(s: dict) -> str:
    try:
        return _day_label(date.fromisoformat(s["date"]))
    except (KeyError, TypeError, ValueError):
        raise RenderError("session %r has no valid date" % (s.get("key"),))


def _local_ts(text) -> datetime:
    """An ISO timestamp kept in its own offset (the payload carries the config zone's).
    A trailing Z means UTC; a naive value is taken as written."""
    if not isinstance(text, str):
        raise RenderError("session time must be a string: %r" % (text,))
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
    except ValueError:
        raise RenderError("invalid session time: %r" % (text,))


def _when(s: dict) -> str:
    start, end = _local_ts(s["start"]), _local_ts(s["end"])
    text = "%s–%s" % (start.strftime("%H:%M"), end.strftime("%H:%M"))
    gap = (end.date() - start.date()).days
    return text + (" (+%dd)" % gap if gap > 0 else "")


def _detail(payload: dict) -> str:
    """The session page. One Hours column (billed time) unless the client opted in to
    show_agent_time, which splits it into your time and agent time."""
    if not payload.get("show_session_detail"):
        return ""
    split = bool(payload.get("show_agent_time"))
    rows = []
    for s in payload["sessions"]:
        if split:
            cells = [_hm(s["you_seconds"]), _hm(s["agent_seconds"])]
        else:
            billed = s.get("billable_seconds")
            if billed is None:
                billed = s["you_seconds"] + s["agent_seconds"]
            cells = [_hm(billed)]
        rows.append(
            '<tr><td class="date">%s</td><td class="when">%s</td>%s<td>%s</td></tr>'
            % (_e(_session_day(s)), _e(_when(s)),
               "".join('<td class="num">%s</td>' % _e(c) for c in cells),
               _e(", ".join(s.get("projects") or []))))
    heads = ["Your time", "Agent time"] if split else ["Hours"]
    return ('<section class="detail"><h2>Session detail</h2><table><thead><tr><th>Date</th>'
            '<th>Time</th>%s<th>Projects</th></tr></thead><tbody>%s</tbody></table>'
            '%s</section>'
            % ("".join('<th class="num">%s</th>' % h for h in heads), "".join(rows),
               _div("note", _e(str(payload["text"].get("counting_note") or "").strip()))))


# ---------------------------------------------------------------- html

def html(payload: dict, cfg: dict, template_path: Optional[str] = None) -> str:
    """The client-facing timesheet page for a draft payload."""
    with open(template_path or _TEMPLATE, encoding="utf-8") as f:
        template = f.read()
    text = payload["text"]
    business, you_block = _you(cfg)
    values = {
        "title": _e(text["title"]),
        "business": business,
        "you_block": you_block,
        "logo": _logo(cfg)[0],
        "client_block": _client(payload, cfg),
        "period_label": _e(payload["period"]["label"]),
        "reference": _e(payload["reference"]),
        "item_header": "Item" if payload.get("line_items") in ("project", "period") else "Date",
        "rows": _rows(payload),
        "total_hours": _e(payload["total_hours"]),
        "total": _e(_money(payload["total"], payload["currency"])),
        "subtotals": _subtotals(payload),
        "detail_page": _detail(payload),
        "footer": _div("foot", _e(str(text.get("footer") or "").strip())),
    }

    def fill(m):
        name = m.group(1)
        if name not in values:
            raise RenderError("unknown template slot {{%s}}" % name)
        return values[name]

    return _SLOT.sub(fill, template)


# ---------------------------------------------------------------- browser and PDF

def _executable(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def find_browser(cfg: dict) -> Optional[str]:
    """pdf.browser_path, then macOS app bundles, then browsers on PATH."""
    if os.environ.get("TIMESHEET_NO_BROWSER") == "1":
        return None
    configured = ((cfg.get("pdf") or {}).get("browser_path") or "").strip()
    if configured:
        p = os.path.expanduser(configured)
        if _executable(p):
            return p
    for d in _MAC_DIRS:
        for app in _MAC_APPS:
            p = os.path.join(os.path.expanduser(d), "%s.app" % app, "Contents", "MacOS", app)
            if _executable(p):
                return p
    for name in _PATH_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _is_pdf(path: str) -> bool:
    try:
        if os.path.getsize(path) == 0:
            return False
        with open(path, "rb") as f:
            return f.read(4) == b"%PDF"
    except OSError:
        return False


def pdf(html_path: str, pdf_path: str, browser: str, timeout: int = 60) -> bool:
    """Print html_path to pdf_path with a headless browser. Never raises."""
    try:
        url = Path(html_path).resolve().as_uri()
        for mode in ("--headless=new", "--headless"):
            try:
                os.unlink(pdf_path)
            except OSError:
                pass
            with tempfile.TemporaryDirectory() as profile:
                cmd = [browser, mode, "--disable-gpu", "--no-pdf-header-footer",
                       "--no-first-run", "--no-default-browser-check",
                       "--user-data-dir=%s" % profile,
                       "--print-to-pdf=%s" % pdf_path, url]
                try:
                    subprocess.run(cmd, capture_output=True, timeout=timeout)
                except Exception:       # timeout or cannot start: no retry
                    return False
            if _is_pdf(pdf_path):
                return True
        return False
    except Exception:
        return False
