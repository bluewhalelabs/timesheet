#!/usr/bin/env python3
"""Mercury add-on: create an UNSENT invoice from an approved draft, and look up the
customer and account ids the config needs. Nothing here can email a customer."""
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ats import cli, config, drafts, lines, mercury_api  # noqa: E402
from ats.cli import UsageError, flat as _flat  # noqa: E402
from ats.mercury_api import ConfigProblem, MercuryError, NotReady  # noqa: E402

REMINDER = ("The invoice was created but NOT sent. Open it in Mercury, attach the "
            "timesheet PDF, and send it from there.")


def _client(cfg):
    try:
        return mercury_api.client_from_config(cfg)
    except ConfigProblem as e:
        raise UsageError(e.body)


def _fail(e):
    print("error: %s" % e, file=sys.stderr)
    return 5


def cmd_accounts(args):
    cfg = config.load()
    client = _client(cfg)
    try:
        accounts = client.paged("/accounts", "accounts")
    except MercuryError as e:
        return _fail(e)
    print("id\tname\tkind\tstatus")
    for a in accounts:
        print("\t".join([_flat(a.get("id")), _flat(a.get("nickname") or a.get("name")),
                         _flat(a.get("kind")), _flat(a.get("status"))]))
    return 0


def cmd_customers(args):
    cfg = config.load()
    client = _client(cfg)
    try:
        customers = client.paged("/ar/customers", "customers")
    except MercuryError as e:
        return _fail(e)
    needle = (args.find or "").lower()
    print("id\tname\temail")
    for c in customers:
        if c.get("deletedAt"):
            continue
        name, email = c.get("name") or "", c.get("email") or ""
        if needle and needle not in name.lower() and needle not in email.lower():
            continue
        print("\t".join([_flat(c.get("id")), _flat(name), _flat(email)]))
    return 0


def cmd_create_customer(args):
    cfg = config.load()
    base = mercury_api.describe_host(_base(cfg))
    if not args.yes:
        print("Would create a Mercury customer on %s:\n  name:  %s\n  email: %s"
              % (base, _flat(args.name), _flat(args.email)))
        print("re-run with --yes to create the customer", file=sys.stderr)
        return 4
    client = _client(cfg)
    try:
        made = client.post("/ar/customers", {"name": args.name, "email": args.email})
    except MercuryError as e:
        return _fail(e)
    print("created customer %s" % _flat(made.get("id")))
    return 0


def _base(cfg):
    try:
        return mercury_api.resolve_base_url(cfg)
    except ConfigProblem as e:
        raise UsageError(e.body)


def _read_raw(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _parse_record(raw):
    try:
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            return data
    except (AttributeError, ValueError):       # not UTF-8 (UnicodeDecodeError) or not JSON
        pass
    return {"unreadable": True}


def _write_bytes(directory, data):
    """Atomically replace <directory>/mercury.json with exactly data (mode 0600)."""
    fd, tmp = tempfile.mkstemp(prefix=".mercury-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, os.path.join(directory, "mercury.json"))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _dump(record):
    return json.dumps(record, indent=2, ensure_ascii=False) + "\n"


def _write_record(directory, record):
    _write_bytes(directory, _dump(record).encode("utf-8"))


def _create_exclusive(path, record):
    """Create path atomically and only if absent (FileExistsError otherwise)."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_dump(record))
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _record_target(rec, fallback=""):
    return rec.get("target") or (mercury_api.target_label(rec["base_url"])
                                 if rec.get("base_url") else fallback or "unknown")


def _describe_record(rec):
    """Short label for a record: 'pending' or its invoice id."""
    return "pending" if rec.get("status") == "pending" else _flat(rec.get("invoice_id"))


def _refusal(rec, path, base, current_target):
    if rec.get("unreadable"):
        text = "a record exists but cannot be read (%s)" % path
    else:
        theirs = _record_target(rec)
        if rec.get("status") == "pending":
            text = ("an earlier attempt on %s against %s Mercury (%s) did not finish and may "
                    "have created an invoice" % (_flat(rec.get("attempted_at")), theirs,
                                                 _flat(rec.get("base_url"))))
        else:
            text = ("an invoice for this period was already created in %s Mercury (%s) on "
                    "%s: id %s" % (theirs, _flat(rec.get("base_url")),
                                   _flat(rec.get("created_at")), _flat(rec.get("invoice_id"))))
        if rec.get("base_url") != base or theirs != current_target:
            text += "; note: that record is for %s; this run targets %s" % (theirs, current_target)
    return ("%s. Check Mercury, and use --force only to create another invoice (%s)."
            % (text, path))


def _finalise(directory, record):
    """Replace the pending record with the full one. If that write fails the pending
    record stays on disk and everything needed is printed. Returns the exit code."""
    try:
        _write_record(directory, record)
    except OSError as e:
        print("error: the invoice WAS created but the record could not be written: %s" % e,
              file=sys.stderr)
        print("Invoice id: %s" % record.get("invoice_id"))
        print("Slug:       %s" % record.get("slug"))
        print("Record to keep (mercury.json in the draft folder still says pending):")
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return 5
    return 0


def _discard_pending(directory, path, old_raw):
    """Undo the pending record after an unambiguous failure."""
    try:
        if old_raw is None:
            os.unlink(path)
        else:
            _write_bytes(directory, old_raw)
    except OSError as e:
        print("error: could not clean up %s: %s" % (path, e), file=sys.stderr)


def _iso(now):
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _note_existing(path, target):
    raw = _read_raw(path)
    if raw is not None:
        rec = _parse_record(raw)
        print("NOTE: a Mercury record already exists for this period (%s, %s)"
              % (_describe_record(rec), _record_target(rec, target)))


RENDER_FIRST = ("render the timesheet first (invoice.py render): the invoice must match the "
                "PDF you attach")


def _check_guards(args, cfg, payload, period, now):
    """Refusals that hold for --dry-run too: overlapping approved periods, a period that
    is not over yet, and a draft whose rendered timesheet is missing or out of date."""
    if payload["overlaps"] and not args.allow_overlap:
        raise UsageError(
            "this period overlaps already approved period(s): %s. Invoicing it would bill "
            "those days again; pass --allow-overlap only if that is intended"
            % drafts.overlap_text(payload["overlaps"], payload["currency"]))
    if period.end >= now.astimezone(config.tz(cfg)).date() and not args.allow_incomplete:
        raise UsageError("period %s is not complete yet (it ends %s); invoice it after it "
                         "ends, or pass --allow-incomplete only if that is intended"
                         % (period.id, period.end.isoformat()))
    if not payload.get("rendered"):
        raise UsageError(RENDER_FIRST)


def cmd_create_invoice(args):
    cfg, client_key, period, now = cli.setup(args)
    payload = drafts.compute(cfg, client_key, period, now)      # fresh numbers, this run
    try:
        mercury_api.check_ready(payload, cfg, client_key, drafts.is_approved)
    except NotReady as e:
        raise UsageError(str(e))
    _check_guards(args, cfg, payload, period, now)
    base = _base(cfg)
    host = mercury_api.describe_host(base)
    target = mercury_api.target_label(base)
    try:
        body = mercury_api.invoice_body(payload, cfg, client_key)
    except NotReady as e:
        raise UsageError(str(e))
    directory = drafts.draft_dir(cfg, client_key, period)
    record_path = os.path.join(directory, "mercury.json")
    if args.dry_run:
        print("Target: %s" % host)
        _note_existing(record_path, target)
        print("POST %s/ar/invoices (dry run, nothing sent)" % base)
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return 0

    cur = payload["currency"]
    total = Decimal(payload["total"])
    if not args.yes:
        print("Target:    %s" % host)
        print("Client:    %s" % payload["client"]["name"])
        print("Customer:  %s" % body["customerId"])
        print("Period:    %s" % payload["period"]["label"])
        print("Lines:     %d" % len(payload["lines"]))
        print("Total:     %s" % lines.money(total, cur))
        _note_existing(record_path, target)
        print("re-run with --yes to create the invoice (it will NOT be emailed)",
              file=sys.stderr)
        return 4

    old_raw = _read_raw(record_path)
    if old_raw is not None and not args.force:
        raise UsageError(_refusal(_parse_record(old_raw), record_path, base, target))
    client = _client(cfg)                       # a missing token must not leave a record
    memo = mercury_api.memo(payload)
    sent_total = mercury_api.money_text(total)
    pending = {"status": "pending", "reference": payload["reference"], "base_url": base,
               "target": target, "total_sent": sent_total, "attempted_at": _iso(now)}
    if old_raw is not None:                     # --force: keep the earlier record
        earlier = _parse_record(old_raw)
        history = list(earlier.pop("previous", []) or [])
        pending["previous"] = history + [earlier]
        try:
            _write_record(directory, pending)
        except OSError as e:
            raise UsageError("cannot write %s: %s" % (record_path, e))
    else:
        try:
            _create_exclusive(record_path, pending)
        except FileExistsError:                 # a concurrent run got there first
            raise UsageError(_refusal(_parse_record(_read_raw(record_path) or b""),
                                      record_path, base, target))
        except OSError as e:
            raise UsageError("cannot write %s: %s" % (record_path, e))

    def ambiguous(why):
        print("error: %s" % why, file=sys.stderr)
        print('The request may have reached Mercury. Check %s Mercury for an invoice with '
              'memo "%s" If none exists, re-run with --force.' % (target, memo),
              file=sys.stderr)
        return 5

    try:
        made = client.post("/ar/invoices", body)
    except MercuryError as e:
        if mercury_api.is_ambiguous(e):
            return ambiguous(e)
        _discard_pending(directory, record_path, old_raw)
        return _fail(e)
    if not isinstance(made, dict) or not made.get("id"):
        return ambiguous("Mercury answered but the response had no invoice id")

    theirs = mercury_api.money_text(made.get("amount"))
    record = {"invoice_id": made.get("id"), "slug": made.get("slug"),
              "status": made.get("status"), "amount": made.get("amount"),
              "invoice_number": made.get("invoiceNumber"),
              "created_at": made.get("createdAt") or _iso(now),
              "base_url": base, "target": target, "total_sent": sent_total,
              "reference": payload["reference"], "attempted_at": pending["attempted_at"]}
    mismatch = theirs != sent_total
    if mismatch:
        record["amount_mismatch"] = True
    if "previous" in pending:
        record["previous"] = pending["previous"]
    code = _finalise(directory, record)
    if code:
        return code
    print("Invoice id: %s" % made.get("id"))
    if target == "PRODUCTION":
        print("Pay page:   %s" % mercury_api.pay_url(made.get("slug")))
    else:
        print("Slug:       %s" % made.get("slug"))
        print("The pay page lives in the %s environment's web app." % target)
    print("Status:     %s" % made.get("status"))
    print(REMINDER)
    if mismatch:
        print("WARNING: Mercury computed %s but the timesheet total is %s. Do not send the "
              "invoice until this is resolved." % (theirs or "an unknown amount", sent_total),
              file=sys.stderr)
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="mercury.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("accounts", help="list Mercury accounts (for destination_account_id)"
                   ).set_defaults(func=cmd_accounts)
    c = sub.add_parser("customers", help="list Mercury customers (for customer_id)")
    c.add_argument("--find", help="case-insensitive text to match in name or email")
    c.set_defaults(func=cmd_customers)
    n = sub.add_parser("create-customer", help="create a Mercury customer")
    n.add_argument("--name", required=True)
    n.add_argument("--email", required=True)
    n.add_argument("--yes", action="store_true", help="actually create it")
    n.set_defaults(func=cmd_create_customer)
    i = sub.add_parser("create-invoice", help="create an UNSENT invoice from an approved draft")
    i.add_argument("--now", help="ISO timestamp to treat as now (tests)")
    i.add_argument("--client", help="client key")
    i.add_argument("--period", help="last, this, YYYY-MM-DD, YYYY-Www or START_END")
    i.add_argument("--dry-run", action="store_true", help="print the request body; send nothing")
    i.add_argument("--yes", action="store_true", help="actually create the invoice")
    i.add_argument("--force", action="store_true",
                   help="create another invoice although mercury.json exists "
                        "(only when the user explicitly asks)")
    i.add_argument("--allow-overlap", action="store_true",
                   help="invoice although the period overlaps another approved period "
                        "(only when the user explicitly asks)")
    i.add_argument("--allow-incomplete", action="store_true",
                   help="invoice a period that has not ended yet "
                        "(only when the user explicitly asks)")
    i.set_defaults(func=cmd_create_invoice)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (config.ConfigError, UsageError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
