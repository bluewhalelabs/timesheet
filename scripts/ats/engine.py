"""The rubric engine: logged turn events -> agent time and the user's engaged time.

Agent time is every moment at least one agent turn runs; a moment shared by
turns of several (client, project) keys is split evenly between those keys.
The user's time is credited from the idle gaps between busy blocks:

  gap <= engaged_gap_minutes  -> the whole gap, to the key of the prompt ending it
  gap <= break_gap_minutes    -> engaged_gap_minutes, to the key of the response before it
  longer                      -> read_allowance_minutes to the response's key, plus
                                 rampup_allowance_minutes to the next prompt's key

The tier thresholds and the ramp-up come from the settings of the prompt that
ends a gap; the read allowance comes from the settings of the response before it.
"""
from collections import namedtuple
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from ats import config, pauses as pause_store

__all__ = ["NON_BILLABLE", "Key", "Turn", "Seg", "Block", "Timeline",
           "mark_non_billable", "build_turns", "build_turns_capped", "agent_segments",
           "human_segments", "apply_pauses", "timeline"]

NON_BILLABLE = "\0non-billable"
Key = Tuple[str, str]

Turn = namedtuple("Turn", "start end key session")
Seg = namedtuple("Seg", "start end key weight kind")
Block = namedtuple("Block", "start end first_key last_key")
# paused: client -> seconds (weight applied) of otherwise-billable time that the pause
# windows removed. paused_pieces: those removed parts as Segs with their original key.
# away_pieces: the ones among them that only an away ("mine") window removed.
Timeline = namedtuple("Timeline", "segs turns capped capped_turns dropped_turns paused "
                                  "paused_pieces away_pieces")


def _settings_lookup(cfg: dict) -> Callable[[Key], dict]:
    """key -> cascaded settings. Client-level for NON_BILLABLE keys; an unknown client
    (e.g. renamed since) gets the defaults."""
    cache = {}  # type: Dict[Key, dict]

    def settings_for(key: Key) -> dict:
        if key not in cache:
            client, project = key
            if client not in cfg.get("clients", {}):
                cache[key] = cfg["defaults"]
            elif project == NON_BILLABLE:
                cache[key] = config.settings(cfg, client)
            else:
                cache[key] = config.settings(cfg, client, project)
        return cache[key]

    return settings_for


def _minutes(settings: dict, name: str) -> timedelta:
    return timedelta(minutes=float(settings[name]))


def _event_key(e: dict) -> Key:
    return e.get("_key") or (e.get("client", ""), e.get("project", ""))


def mark_non_billable(events: List[dict], cfg: dict) -> None:
    """Set e["_key"]: (client, NON_BILLABLE) for sessions matching that client's
    non_billable_sessions prefixes, else (client, project)."""
    settings_for = _settings_lookup(cfg)
    prefixes = {}  # type: Dict[str, tuple]
    for e in events:
        client = e.get("client", "")
        if client not in prefixes:
            prefixes[client] = tuple(settings_for((client, NON_BILLABLE)).get("non_billable_sessions", []))
        session = e.get("session", "")
        if prefixes[client] and session.startswith(prefixes[client]):
            e["_key"] = (client, NON_BILLABLE)
        else:
            e["_key"] = (client, e.get("project", ""))


def build_turns(events: List[dict], settings_for: Callable[[Key], dict],
                now: datetime) -> Tuple[List[Turn], int]:
    """Pair each session's start with its end. Returns the turns and how many were capped."""
    turns, capped_turns = build_turns_capped(events, settings_for, now)
    return turns, len(capped_turns)


def build_turns_capped(events: List[dict], settings_for: Callable[[Key], dict],
                       now: datetime) -> Tuple[List[Turn], List[Turn]]:
    """Pair each session's start with its end. Returns the turns and the capped ones
    (the same Turn values as in the turns list)."""
    turns, capped, _ = _pair_turns(events, settings_for, now)
    return turns, capped


def _pair_turns(events: List[dict], settings_for: Callable[[Key], dict],
                now: datetime) -> Tuple[List[Turn], List[Turn], List[Turn]]:
    """(turns, capped, dropped).

    A start while a turn is open ends that turn at the new start, but at most
    unclosed_cap_minutes after it began. A session_end closes an open turn under the
    same cap. A turn still open at the end runs to `now` if that is within the cap;
    otherwise it is dropped (no time credited) and returned in dropped as
    Turn(start, start + cap, key, session) so callers can report it.
    """
    open_by_session = {}  # type: Dict[str, dict]
    dropped = []  # type: List[Turn]
    turns = []  # type: List[Turn]
    capped = []  # type: List[Turn]

    def cap_of(e):
        return _minutes(settings_for(_event_key(e)), "unclosed_cap_minutes")

    for e in events:
        sid, kind = e.get("session", ""), e.get("event")
        opened = open_by_session.get(sid)
        if kind == "start":
            if opened is not None:
                cap = cap_of(opened)
                turn = Turn(opened["_t"], min(e["_t"], opened["_t"] + cap),
                            _event_key(opened), sid)
                if e["_t"] - opened["_t"] > cap:
                    capped.append(turn)
                turns.append(turn)
            open_by_session[sid] = e
        elif kind in ("end", "session_end") and opened is not None:
            end, was_capped = e["_t"], False
            if kind == "session_end":
                cap = cap_of(opened)
                if end - opened["_t"] > cap:
                    was_capped = True
                    end = opened["_t"] + cap
            turn = Turn(opened["_t"], end, _event_key(opened), sid)
            if was_capped:
                capped.append(turn)
            turns.append(turn)
            del open_by_session[sid]
    for sid, opened in open_by_session.items():
        cap = cap_of(opened)
        if now - opened["_t"] <= cap:
            turns.append(Turn(opened["_t"], now, _event_key(opened), sid))
        else:
            dropped.append(Turn(opened["_t"], opened["_t"] + cap, _event_key(opened), sid))
    dropped.sort(key=lambda t: (t.start, t.session))
    return [t for t in turns if t.end > t.start], capped, dropped


def agent_segments(turns: List[Turn]) -> Tuple[List[Seg], List[Block]]:
    """Sweep the turns into non-overlapping agent segments, weighted 1 / number of
    distinct keys running at that moment, and merge them into busy blocks."""
    bounds = sorted([(t.start, 1, t.key) for t in turns] + [(t.end, -1, t.key) for t in turns],
                    key=lambda b: b[0])
    active = {}  # type: Dict[Key, int]
    segs = []  # type: List[Seg]
    i, n = 0, len(bounds)
    while i < n:
        here = bounds[i][0]
        while i < n and bounds[i][0] == here:
            _, delta, key = bounds[i]
            count = active.get(key, 0) + delta
            if count:
                active[key] = count
            else:
                del active[key]
            i += 1
        if i < n and active:
            nxt = bounds[i][0]
            weight = 1.0 / len(active)
            for key in sorted(active):
                segs.append(Seg(here, nxt, key, weight, "agent"))

    blocks = []  # type: List[Block]
    for t in sorted(turns, key=lambda t: (t.start, t.end, t.key)):
        if blocks and t.start <= blocks[-1].end:
            b = blocks[-1]
            if t.end >= b.end:
                blocks[-1] = b._replace(end=t.end, last_key=t.key)
        else:
            blocks.append(Block(t.start, t.end, t.key, t.key))
    return segs, blocks


def human_segments(blocks: List[Block], settings_for: Callable[[Key], dict],
                   now: datetime, prototype_rules: bool = False) -> List[Seg]:
    """Credit the user's engaged time in the idle gaps around the busy blocks.

    A medium gap normally credits the engaged allowance to the work just read. When the
    prompt that ends the gap belongs to an ignored (non-billable) session, coming back is
    no evidence of time spent on the earlier work, so only the read allowance is credited,
    as after a long break. prototype_rules=True keeps the original rule (parity tests)."""
    if not blocks:
        return []
    out = []  # type: List[Seg]

    def add(a, b, key):
        out.append(Seg(a, b, key, 1.0, "you"))

    first = blocks[0]
    add(first.start - _minutes(settings_for(first.first_key), "rampup_allowance_minutes"),
        first.start, first.first_key)
    for prev, nxt in zip(blocks, blocks[1:]):
        ending = settings_for(nxt.first_key)
        engaged = _minutes(ending, "engaged_gap_minutes")
        gap = nxt.start - prev.end
        if gap <= engaged:
            add(prev.end, nxt.start, nxt.first_key)
        elif gap <= _minutes(ending, "break_gap_minutes"):
            credit = engaged
            if nxt.first_key[1] == NON_BILLABLE and not prototype_rules:
                credit = min(engaged, _minutes(settings_for(prev.last_key),
                                               "read_allowance_minutes"))
            add(prev.end, prev.end + min(credit, gap), prev.last_key)
        else:
            # The two sides can have different settings: never credit more than the gap.
            read = min(_minutes(settings_for(prev.last_key), "read_allowance_minutes"), gap)
            ramp = min(_minutes(ending, "rampup_allowance_minutes"), gap - read)
            add(prev.end, prev.end + read, prev.last_key)
            add(nxt.start - ramp, nxt.start, nxt.first_key)
    last = blocks[-1]
    read = _minutes(settings_for(last.last_key), "read_allowance_minutes")
    add(last.end, min(last.end + read, max(now, last.end)), last.last_key)
    return [s for s in out if s.end > s.start]


def _cut(s: Seg, windows: list) -> Tuple[List[Seg], List[Seg]]:
    """(the parts of s outside the sorted, merged windows, the parts inside them)."""
    outside, inside = [], []
    cursor = s.start
    for a, b in windows:
        if b <= cursor:
            continue
        if a >= s.end:
            break
        a, b = max(a, cursor), min(b, s.end)
        if a > cursor:
            outside.append(s._replace(start=cursor, end=a))
        inside.append(s._replace(start=a, end=b))
        cursor = b
    if cursor < s.end:
        outside.append(s._replace(start=cursor))
    return outside, inside


def apply_pauses(segs: List[Seg], pauses: List[dict],
                 now: datetime) -> Tuple[List[Seg], Dict[str, float], List[Seg], List[Seg]]:
    """Cut every billable segment at the edges of its client's pause windows and move
    the part inside a window to (client, NON_BILLABLE). A full pause ("all") takes
    agent time and the user's time; an away window ("mine") takes only the user's time,
    and where both cover a moment the full pause counts. Returns the segments, the
    removed seconds per client, the removed parts (with their original key) and those
    of them that only an away window removed. Segments that are already NON_BILLABLE
    are left alone and not counted."""
    if not pauses:
        return segs, {}, [], []
    windows = {}  # type: Dict[str, tuple]
    out = []  # type: List[Seg]
    pieces = []  # type: List[Seg]
    away = []  # type: List[Seg]
    totals = {}  # type: Dict[str, float]
    for s in segs:
        client, project = s.key
        if project == NON_BILLABLE:
            out.append(s)
            continue
        if client not in windows:
            windows[client] = (pause_store.windows_for(pauses, client, now, "all"),
                               pause_store.windows_for(pauses, client, now, "mine"))
        full, mine = windows[client]
        kept, removed = _cut(s, full)
        if s.kind == "you" and mine:
            parts, kept = kept, []
            for part in parts:
                outside, inside = _cut(part, mine)
                kept += outside
                removed += inside
                away += inside
        out += kept
        for r in removed:
            out.append(r._replace(key=(client, NON_BILLABLE)))
            pieces.append(r)
            totals[client] = totals.get(client, 0.0) + \
                (r.end - r.start).total_seconds() * s.weight
    return out, totals, pieces, away


def timeline(events: List[dict], cfg: dict, now: datetime,
             prototype_rules: bool = False, pauses: Optional[List[dict]] = None) -> Timeline:
    """The whole pipeline. Segments of NON_BILLABLE keys are kept; callers drop them.
    pauses=None reads pauses.json from the data dir (PauseError when it is invalid);
    pass [] to apply none."""
    if pauses is None:
        pauses = pause_store.load()
    events = sorted(events, key=lambda e: e["_t"])
    mark_non_billable(events, cfg)
    settings_for = _settings_lookup(cfg)
    turns, capped_turns, dropped = _pair_turns(events, settings_for, now)
    a_segs, blocks = agent_segments(turns)
    h_segs = human_segments(blocks, settings_for, now, prototype_rules)
    segs, paused, pieces, away = apply_pauses(a_segs + h_segs, pauses, now)
    segs = sorted(segs, key=lambda s: (s.start, s.end, s.kind, s.key))
    order = lambda s: (s.start, s.end, s.kind, s.key)
    return Timeline(segs, turns, len(capped_turns), capped_turns, dropped, paused,
                    sorted(pieces, key=order), sorted(away, key=order))
