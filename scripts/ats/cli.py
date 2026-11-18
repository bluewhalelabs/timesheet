"""Plumbing shared by the invoice and Mercury CLIs: usage errors, the --now parser,
client and period resolution, and turning parsed arguments into a run context."""
from datetime import datetime, timezone

from ats import config, events as ev, periods

__all__ = ["UsageError", "flat", "parse_now", "resolve_client", "resolve_period", "setup"]


class UsageError(Exception):
    pass


def flat(text) -> str:
    """text on one line with single spaces (None becomes empty)."""
    return " ".join(str(text if text is not None else "").replace("\t", " ").split())


def parse_now(text):
    if text is None:
        return datetime.now(timezone.utc)
    try:
        return ev.parse_ts(text)
    except ValueError:
        raise UsageError("invalid --now timestamp: %r" % text)


def resolve_client(cfg, wanted):
    keys = config.client_keys(cfg)
    if not keys:
        raise UsageError("no clients configured")
    if wanted is None:
        if len(keys) == 1:
            return keys[0]
        raise UsageError("several clients configured; pass --client (%s)" % ", ".join(keys))
    if wanted not in keys:
        raise UsageError("unknown client: %s (configured: %s)" % (wanted, ", ".join(keys)))
    return wanted


def resolve_period(cfg, client, spec, now):
    st = config.settings(cfg, client)
    try:
        return periods.resolve(spec, st["billing_period"], now.astimezone(config.tz(cfg)).date())
    except ValueError as e:
        raise UsageError(str(e))


def setup(args):
    """Parsed args (with .now, .client, .period) -> (cfg, client, period, now)."""
    cfg = config.load()
    now = parse_now(args.now)
    client = resolve_client(cfg, args.client)
    period = resolve_period(cfg, client, args.period, now)
    return cfg, client, period, now
