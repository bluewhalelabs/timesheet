"""Config loading, validation and the defaults -> client -> project cascade."""
import copy
import datetime
import json
import os
import re
import tempfile
from typing import List, Optional
from zoneinfo import ZoneInfo

__all__ = ["ConfigError", "DEFAULTS", "home", "expand", "load", "save",
           "settings", "client_keys", "tz", "local_zone_name", "counting_note"]


_CLIENT_KEY = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class ConfigError(Exception):
    pass


DEFAULTS = {
    "hourly_rate": 150,
    "currency": "USD",
    "agent_billable_fraction": 1.0,
    "engaged_gap_minutes": 15,
    "break_gap_minutes": 45,
    "session_break_minutes": 45,
    "read_allowance_minutes": 5,
    "rampup_allowance_minutes": 5,
    "unclosed_cap_minutes": 10,
    "rounding": {"increment_hours": 0.01, "mode": "nearest"},
    "billing_period": {"type": "weekly", "week_starts": "monday"},
    "line_items": "session",
    "show_session_detail": False,
    "show_project_subtotals": False,
    "show_agent_time": False,
    "project_labels": {"(root)": "General / cross-project"},
    "non_billable_sessions": [],
    "text": {
        "title": "Timesheet",
        "period_label": "Professional services",
        "counting_note": "",
        "footer": "",
    },
}

_TOP_DEFAULTS = {
    "you": {"name": "", "business": "", "email": "", "address": "", "logo": ""},
    "timezone": "",
    "clients": {},
    "pdf": {"browser_path": "", "output_dir": "~/.timesheet/invoices"},
    "mercury": {"api_key_env": "MERCURY_API_KEY", "base_url": "", "allow_custom_host": False},
    "status": {"components": ["invoice_due", "current_hours"], "show_amounts": False,
               "reminders": "off"},
}

# What the status line and the reminder may show, and when the reminder fires.
STATUS_COMPONENTS = ("client", "invoice_due", "current_hours")
STATUS_REMINDERS = ("off", "session_start", "daily")

_CLIENT_DEFAULTS = {
    "contact": "",
    "address": "",
    "scope": {"accounts": [], "directories": [], "repos": []},
    "overrides": {},
    "non_billable_sessions": [],
    "projects": {},
    "mercury": {"enabled": False, "customer_id": "",
                "destination_account_id": "", "due_in_days": 15,
                "cc_emails": [], "ach_debit_enabled": False,
                "credit_card_enabled": False, "use_real_account_number": False},
}

# The only settings a project entry may override; everything else is per client.
PROJECT_KEYS = ("hourly_rate", "agent_billable_fraction", "engaged_gap_minutes",
                "break_gap_minutes", "read_allowance_minutes", "rampup_allowance_minutes",
                "unclosed_cap_minutes")

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")
_MINUTE_KEYS = ("engaged_gap_minutes", "break_gap_minutes",
                "session_break_minutes", "read_allowance_minutes",
                "rampup_allowance_minutes", "unclosed_cap_minutes")


def home() -> str:
    h = os.environ.get("TIMESHEET_HOME") or "~/.timesheet"
    return os.path.expanduser(h)


def expand(path: str) -> str:
    if not path:
        return ""
    return os.path.realpath(os.path.expanduser(path))


def _default_path() -> str:
    return os.path.join(home(), "config.json")


def _fill(target: dict, defaults: dict) -> None:
    """Add missing keys from defaults into target, recursing into dicts."""
    for k, v in defaults.items():
        if k not in target:
            target[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(target[k], dict):
            _fill(target[k], v)


def _merge(base: dict, over: dict) -> dict:
    """Return base overlaid with over; nested dicts merge key by key."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_tz(name, path: str) -> None:
    if not isinstance(name, str):
        raise ConfigError("%s must be a string" % path)
    if name == "":
        return
    try:
        ZoneInfo(name)
    except Exception:
        raise ConfigError("%s: unknown timezone %r" % (path, name))


def _check_settings(d, path: str) -> None:
    """Validate whichever setting keys are present in d (a defaults-shaped dict)."""
    if not isinstance(d, dict):
        raise ConfigError("%s must be an object" % path)

    def key(k):
        return "%s.%s" % (path, k)

    if "hourly_rate" in d and not (_is_num(d["hourly_rate"]) and d["hourly_rate"] >= 0):
        raise ConfigError("%s must be a number >= 0" % key("hourly_rate"))
    if "agent_billable_fraction" in d:
        f = d["agent_billable_fraction"]
        if not (_is_num(f) and 0 <= f <= 1):
            raise ConfigError("%s must be a number between 0 and 1" % key("agent_billable_fraction"))
    for m in _MINUTE_KEYS:
        if m in d and not (_is_num(d[m]) and d[m] >= 0):
            raise ConfigError("%s must be a number >= 0" % key(m))
    if "non_billable_sessions" in d:
        v = d["non_billable_sessions"]
        if not (isinstance(v, list) and all(isinstance(x, str) and x for x in v)):
            raise ConfigError("%s must be a list of non-empty strings"
                              % key("non_billable_sessions"))
    for b in ("show_session_detail", "show_project_subtotals", "show_agent_time"):
        if b in d and not isinstance(d[b], bool):
            raise ConfigError("%s must be true or false" % key(b))
    if "line_items" in d and d["line_items"] not in ("day", "session", "project", "period"):
        raise ConfigError("%s must be one of day, session, project, period" % key("line_items"))
    if "timezone" in d:
        raise ConfigError("%s is not allowed here: timezone is set once at the top level"
                          % key("timezone"))
    if "status" in d:
        raise ConfigError("%s is not allowed here: status is set once at the top level"
                          % key("status"))
    for sub in ("text", "project_labels"):
        if sub in d and not isinstance(d[sub], dict):
            raise ConfigError("%s must be an object" % key(sub))
    if "rounding" in d:
        r = d["rounding"]
        if not isinstance(r, dict):
            raise ConfigError("%s must be an object" % key("rounding"))
        if "mode" in r and r["mode"] not in ("nearest", "up", "down"):
            raise ConfigError("%s must be one of nearest, up, down" % key("rounding.mode"))
        if "increment_hours" in r and not (_is_num(r["increment_hours"]) and r["increment_hours"] > 0):
            raise ConfigError("%s must be a number > 0" % key("rounding.increment_hours"))
    if "billing_period" in d:
        b = d["billing_period"]
        if not isinstance(b, dict):
            raise ConfigError("%s must be an object" % key("billing_period"))
        if "type" in b and b["type"] not in ("weekly", "biweekly", "semimonthly", "monthly"):
            raise ConfigError("%s must be one of weekly, biweekly, semimonthly, monthly"
                              % key("billing_period.type"))
        if "week_starts" in b and b["week_starts"] not in _WEEKDAYS:
            raise ConfigError("%s must be a lowercase weekday name" % key("billing_period.week_starts"))
        if "anchor" in b:
            _check_anchor(b["anchor"], key("billing_period.anchor"))


_MERCURY_BOOLS = ("enabled", "ach_debit_enabled", "credit_card_enabled",
                  "use_real_account_number")


def _check_mercury(m: dict, path: str) -> None:
    for k in _MERCURY_BOOLS:
        if k in m and not isinstance(m[k], bool):
            raise ConfigError("%s.%s must be true or false" % (path, k))
    for k in ("customer_id", "destination_account_id"):
        if k in m and not isinstance(m[k], str):
            raise ConfigError("%s.%s must be a string" % (path, k))
    if "due_in_days" in m:
        v = m["due_in_days"]
        if not (isinstance(v, int) and not isinstance(v, bool) and v >= 0):
            raise ConfigError("%s.due_in_days must be an integer >= 0" % path)
    if "cc_emails" in m:
        v = m["cc_emails"]
        if not (isinstance(v, list) and all(isinstance(x, str) and x for x in v)):
            raise ConfigError("%s.cc_emails must be a list of non-empty strings" % path)


def _check_anchor(v, path: str) -> None:
    try:
        if not isinstance(v, str) or len(v) != 10:
            raise ValueError(v)
        datetime.datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        raise ConfigError("%s must be a date as YYYY-MM-DD" % path)


def _check_biweekly(d: dict, path: str) -> None:
    b = d.get("billing_period", {})
    if b.get("type") == "biweekly" and "anchor" not in b:
        raise ConfigError("%s.billing_period.anchor is required for biweekly billing" % path)


def _check_gaps(eff: dict, path: str) -> None:
    """The rubric tiers must nest on the effective settings of one level."""
    engaged, brk = eff["engaged_gap_minutes"], eff["break_gap_minutes"]
    read, ramp = eff["read_allowance_minutes"], eff["rampup_allowance_minutes"]
    if engaged > brk:
        raise ConfigError("%s: engaged_gap_minutes (%s) must not exceed break_gap_minutes (%s)"
                          % (path, _fmt_num(engaged), _fmt_num(brk)))
    if read + ramp > brk:
        raise ConfigError("%s: read_allowance_minutes + rampup_allowance_minutes (%s + %s) "
                          "must not exceed break_gap_minutes (%s)"
                          % (path, _fmt_num(read), _fmt_num(ramp), _fmt_num(brk)))


def _check_project_keys(d: dict, path: str) -> None:
    for k in d:
        if k not in PROJECT_KEYS:
            raise ConfigError("%s.%s is not a per-project setting: it can only be set per "
                              "client (in overrides) or in defaults" % (path, k))


def _check_status(s: dict) -> None:
    comps = s["components"]
    if not (isinstance(comps, list)
            and all(isinstance(c, str) and c in STATUS_COMPONENTS for c in comps)):
        raise ConfigError("status.components must be a list of: %s"
                          % ", ".join(STATUS_COMPONENTS))
    if len(set(comps)) != len(comps):
        raise ConfigError("status.components must not repeat a component")
    if not isinstance(s["show_amounts"], bool):
        raise ConfigError("status.show_amounts must be true or false")
    if not (isinstance(s["reminders"], str) and s["reminders"] in STATUS_REMINDERS):
        raise ConfigError("status.reminders must be one of %s" % ", ".join(STATUS_REMINDERS))


def _validate(cfg: dict) -> None:
    _check_tz(cfg["timezone"], "timezone")
    for sec in ("you", "pdf", "mercury", "status"):
        if not isinstance(cfg[sec], dict):
            raise ConfigError("%s must be an object" % sec)
    _check_status(cfg["status"])
    if not isinstance(cfg["mercury"]["allow_custom_host"], bool):
        raise ConfigError("mercury.allow_custom_host must be true or false")
    _check_settings(cfg["defaults"], "defaults")
    _check_biweekly(cfg["defaults"], "defaults")
    _check_gaps(cfg["defaults"], "defaults")
    if not isinstance(cfg["clients"], dict):
        raise ConfigError("clients must be an object")
    for ck, client in cfg["clients"].items():
        if not _CLIENT_KEY.match(ck) or ck in (".", ".."):
            raise ConfigError("client key %r is not allowed: use letters, digits, '.', '_' "
                              "and '-', starting with a letter or digit" % ck)
        cpath = "clients.%s" % ck
        if not isinstance(client, dict):
            raise ConfigError("%s must be an object" % cpath)
        for sub in ("overrides", "projects", "scope", "mercury"):
            if not isinstance(client[sub], dict):
                raise ConfigError("%s.%s must be an object" % (cpath, sub))
        _check_mercury(client["mercury"], cpath + ".mercury")
        for sk in ("accounts", "directories", "repos"):
            v = client["scope"].get(sk)
            if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                raise ConfigError("%s.scope.%s must be a list of strings" % (cpath, sk))
        v = client["non_billable_sessions"]
        if not (isinstance(v, list) and all(isinstance(x, str) and x for x in v)):
            raise ConfigError("%s.non_billable_sessions must be a list of non-empty strings"
                              % cpath)
        _check_settings(client["overrides"], cpath + ".overrides")
        for pk, pdict in client["projects"].items():
            ppath = "%s.projects.%s" % (cpath, pk)
            _check_settings(pdict, ppath)
            _check_project_keys(pdict, ppath)
        # biweekly needs an anchor somewhere in the effective cascade (projects cannot
        # set billing_period)
        eff = _merge(cfg["defaults"], client["overrides"])
        _check_biweekly(eff, cpath + ".overrides")
        _check_gaps(eff, cpath + ".overrides")
        for pk, pdict in client["projects"].items():
            _check_gaps(_merge(eff, pdict), "%s.projects.%s" % (cpath, pk))


def load(path: Optional[str] = None) -> dict:
    path = path or _default_path()
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise ConfigError("config file not found: %s" % path)
    except ValueError as e:
        raise ConfigError("invalid JSON in %s: %s" % (path, e))
    except OSError as e:
        raise ConfigError("cannot read %s: %s" % (path, e))
    if not isinstance(cfg, dict):
        raise ConfigError("config root must be an object")
    _fill(cfg, _TOP_DEFAULTS)
    cfg.setdefault("defaults", {})
    if not isinstance(cfg["defaults"], dict):
        raise ConfigError("defaults must be an object")
    _fill(cfg["defaults"], DEFAULTS)
    if isinstance(cfg["clients"], dict):
        for ck, client in cfg["clients"].items():
            if isinstance(client, dict):
                client.setdefault("name", ck)
                _fill(client, _CLIENT_DEFAULTS)
    _validate(cfg)
    return cfg


def save(cfg: dict, path: Optional[str] = None) -> None:
    path = path or _default_path()
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def settings(cfg: dict, client: str, project: Optional[str] = None) -> dict:
    clients = cfg.get("clients", {})
    if client not in clients:
        raise ConfigError("unknown client: %s" % client)
    c = clients[client]
    out = _merge(cfg["defaults"], c.get("overrides", {}))
    nb = list(cfg["defaults"].get("non_billable_sessions", []))
    nb += c.get("non_billable_sessions", [])
    nb += c.get("overrides", {}).get("non_billable_sessions", [])
    if project is not None and project in c.get("projects", {}):
        out = _merge(out, c["projects"][project])
    out["non_billable_sessions"] = list(dict.fromkeys(nb))   # union, order kept
    return out


def client_keys(cfg: dict) -> List[str]:
    return list(cfg.get("clients", {}).keys())


def local_zone_name() -> Optional[str]:
    """IANA key of the system zone: TZ env var, else /etc/localtime; None if unknown."""
    def valid(name):
        try:
            ZoneInfo(name)
            return True
        except Exception:
            return False

    env = os.environ.get("TZ", "").lstrip(":")
    if env and valid(env):
        return env
    try:
        real = os.path.realpath("/etc/localtime")
    except OSError:
        return None
    marker = "zoneinfo/"
    if marker in real:
        name = real.split(marker, 1)[1]
        if name and valid(name):
            return name
    return None


def tz(cfg: dict) -> datetime.tzinfo:
    name = cfg.get("timezone", "") or local_zone_name()
    if name:
        return ZoneInfo(name)
    return datetime.datetime.now().astimezone().tzinfo


def _fmt_num(v):
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


_NOTE_KEYS = ("engaged_gap_minutes", "break_gap_minutes",
              "read_allowance_minutes", "rampup_allowance_minutes")


def counting_note(settings: dict) -> str:
    """The text.counting_note template filled from a cascaded settings dict."""
    note = settings["text"]["counting_note"]
    values = {k: _fmt_num(settings[k]) for k in _NOTE_KEYS if k in settings}
    try:
        return note.format(**values)
    except (KeyError, IndexError, ValueError):
        return note
