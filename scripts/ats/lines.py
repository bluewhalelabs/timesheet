"""Invoice line items: group credited time, round each line, price it.

All arithmetic that reaches a printed number is decimal.Decimal. Floats (seconds)
enter through Decimal(str(x)) and are rounded to 6 places first so float dust
cannot push an exact multiple of the increment into the next one."""
from datetime import date
from decimal import (Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP)
from typing import Dict, List

from ats import config, periods
from ats.rollup import WorkSession, split_days

__all__ = ["round_hours", "money", "build_lines"]

_CENTS = Decimal("0.01")
_MODES = {"nearest": ROUND_HALF_UP, "up": ROUND_CEILING, "down": ROUND_FLOOR}
_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def round_hours(seconds: float, increment: float, mode: str) -> Decimal:
    """Seconds -> hours rounded to a multiple of increment (nearest = half up, up,
    down), quantised to the increment's decimal places. Zero or negative -> 0."""
    q = Decimal(str(increment))
    if mode not in _MODES:
        raise ValueError("rounding mode must be nearest, up or down, got %r" % (mode,))
    secs = Decimal(str(round(float(seconds), 6)))
    if secs <= 0:
        return Decimal(0).quantize(q)
    steps = (secs / 3600 / q).quantize(Decimal(1), rounding=_MODES[mode])
    return (steps * q).quantize(q)


def money(amount: Decimal, currency: str) -> str:
    text = format(amount.quantize(_CENTS, ROUND_HALF_UP), ",.2f")
    symbol = _SYMBOLS.get(currency)
    return symbol + text if symbol else "%s %s" % (text, currency)


def _fmt(d: Decimal) -> str:
    return format(d, "f")


def _rate(value) -> Decimal:
    d = Decimal(str(value))
    return Decimal(0) if d == 0 else d.normalize()


def _date_label(d: date) -> str:
    return "%s, %s %d, %d" % (_DAYS[d.weekday()], _MONTHS[d.month - 1], d.day, d.year)


class _Acc:
    """Running totals of one line."""
    def __init__(self, key, date_, label, rate):
        self.key, self.date, self.label, self.rate = key, date_, label, rate
        self.you = self.agent = self.billable = 0.0
        self.projects = set()
        self.session_keys = []
        self.manual = False
        self.manual_text = []
        self.order = 0

    def add(self, project, you, agent, fraction):
        self.you += you
        self.agent += agent
        self.billable += you + agent * fraction
        self.projects.add(project)


def build_lines(cfg: dict, client: str, period: periods.Period, segs, sessions: List[WorkSession],
                manual: List[dict], summaries: Dict[str, str],
                descriptions: Dict[str, str]) -> dict:
    top = config.settings(cfg, client)
    zone = config.tz(cfg)
    mode_items = top["line_items"]
    inc = top["rounding"]["increment_hours"]
    rmode = top["rounding"]["mode"]
    labels = top.get("project_labels", {})
    q = Decimal(str(inc))
    cache = {}  # type: Dict[str, dict]

    def proj(p):
        if p not in cache:
            s = config.settings(cfg, client, p)
            cache[p] = {"rate": _rate(s["hourly_rate"]),
                        "fraction": float(s["agent_billable_fraction"])}
        return cache[p]

    def plabel(p):
        return labels.get(p, p)

    mine = [m for m in manual
            if m["client"] == client and period.start <= m["date"] <= period.end]
    sessions = sorted(sessions, key=lambda s: s.start)
    per_project = {}  # type: Dict[str, float]
    accs = {}  # type: Dict[str, _Acc]

    def acc(key, date_, label, rate):
        if key not in accs:
            accs[key] = _Acc(key, date_, label, rate)
        return accs[key]

    def target(p, d, session_key=None):
        """The accumulator a (project, local date or session) amount belongs to."""
        rate = proj(p)["rate"]
        rs = _fmt(rate)
        if mode_items == "day":
            return acc("%s@%s" % (d.isoformat(), rs), d, _date_label(d), rate)
        if mode_items == "session":
            a = acc("%s@%s" % (session_key, rs), session_dates[session_key][0],
                    session_dates[session_key][1], rate)
            a.order = session_dates[session_key][2]
            return a
        if mode_items == "project":
            return acc("%s@%s" % (p, rs), None, plabel(p), rate)
        return acc("period@%s" % rs, None, top["text"]["period_label"], rate)

    session_dates = {}  # key -> (date, label, order)
    if mode_items == "session":
        seen = {}  # type: Dict[date, int]
        for i, s in enumerate(sessions):
            d = s.days[0] if s.days else s.start.astimezone(zone).date()
            seen[d] = seen.get(d, 0) + 1
            n = seen[d]
            session_dates[s.key] = (d, _date_label(d) + ("" if n == 1 else " (%d)" % n), i)

    # credited time
    if mode_items == "session":
        for s in sessions:
            for p, cell in s.by_project.items():
                target(p, None, s.key).add(p, cell["you"], cell["agent"], proj(p)["fraction"])
                per_project[p] = per_project.get(p, 0.0) + cell["you"] + cell["agent"] * proj(p)["fraction"]
    else:
        for sg in segs:
            p = sg.key[1]
            for d, a, b in split_days(sg.start, sg.end, zone):
                n = (b - a).total_seconds() * sg.weight
                you, agent = (n, 0.0) if sg.kind == "you" else (0.0, n)
                target(p, d).add(p, you, agent, proj(p)["fraction"])
                per_project[p] = per_project.get(p, 0.0) + you + agent * proj(p)["fraction"]

    # manual entries
    manual_n = {}  # type: Dict[date, int]
    for m in mine:
        p, d = m["project"], m["date"]
        secs = float(m["hours"] * 3600)
        per_project[p] = per_project.get(p, 0.0) + secs
        manual_n[d] = manual_n.get(d, 0) + 1
        if mode_items == "session":
            rate = proj(p)["rate"]
            a = acc("manual-%s-%d@%s" % (d.isoformat(), manual_n[d], _fmt(rate)),
                    d, _date_label(d) + " (manual)", rate)
            a.order = len(sessions) + manual_n[d]
        else:
            a = target(p, d)
        a.add(p, secs, 0.0, 1.0)
        a.manual = True
        if m["description"]:
            a.manual_text.append(m["description"])

    # session keys per line
    for a in accs.values():
        if mode_items == "session":
            if not a.manual:
                a.session_keys = [a.key.rsplit("@", 1)[0]]
            continue
        for s in sessions:
            if mode_items == "day" and a.date not in s.days:
                continue
            ps = [p for p in s.by_project if proj(p)["rate"] == a.rate
                  and (mode_items != "project" or p in a.projects)]
            if ps:
                a.session_keys.append(s.key)

    warnings = []  # type: List[str]
    lines = []
    total = Decimal(0)
    total_hours = Decimal(0)
    for a in sorted(accs.values(), key=_sort_key(mode_items)):
        hours = round_hours(a.billable, inc, rmode)
        if hours == 0:
            warnings.append("dropped %s: %.0f s of billable time rounds to 0 hours"
                            % (a.date.isoformat() if a.date else a.label, a.billable))
            continue
        amount = (hours * a.rate).quantize(_CENTS, ROUND_HALF_UP)
        total += amount
        total_hours += hours
        if a.key in descriptions:
            desc = descriptions[a.key]
        else:
            parts = [summaries[k] for k in a.session_keys if summaries.get(k)]
            desc = "; ".join(parts + a.manual_text)
        lines.append({
            "key": a.key, "date": a.date.isoformat() if a.date else None, "label": a.label,
            "description": desc, "hours": _fmt(hours), "rate": _fmt(a.rate),
            "amount": _fmt(amount), "seconds": a.billable, "you_seconds": a.you,
            "agent_seconds": a.agent, "projects": sorted(a.projects),
            "session_keys": a.session_keys, "manual": a.manual})

    subtotals = []
    for p, secs in per_project.items():
        hours = round_hours(secs, inc, rmode)
        subtotals.append({"project": p, "label": plabel(p), "hours": _fmt(hours),
                          "amount": _fmt((hours * proj(p)["rate"]).quantize(_CENTS, ROUND_HALF_UP))})
    subtotals.sort(key=lambda r: (r["label"], r["project"]))

    return {"lines": lines, "total_hours": _fmt(total_hours.quantize(q)),
            "total": _fmt(total.quantize(_CENTS)), "currency": top["currency"],
            "subtotals": subtotals, "warnings": warnings}


def _sort_key(mode):
    if mode in ("project", "period"):
        return lambda a: (a.label, a.rate)
    if mode == "session":
        return lambda a: (a.date, a.order, a.rate)
    return lambda a: (a.date, a.rate)
