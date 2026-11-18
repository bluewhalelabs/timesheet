---
description: "Draft, review and render the timesheet and invoice line items for a client's billing period"
argument-hint: "[client] [period]"
---

Run the invoice flow from the `timesheet` skill (section "Invoice flow"), following its Rules exactly.

Arguments as typed: `$ARGUMENTS`

- First word, if given: the client key. Second word, if given: the period (`last`, `this`, `YYYY-MM-DD`, `YYYY-Www` or `START_END`).
- With no client and several clients configured, ask which one. With no period, use the last complete one and tell me which period that is. When I gave no client or no period, omit `--client` / `--period` from the command instead of inventing a value.

Start with:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/invoice.py" compute --client <client> --period <period>
```

Never compute or change hours or amounts yourself, and never send anything to the client.

- Save the draft only after I approve it as shown (re-show it after every edit).
- Run `mercury.py create-invoice --yes` only after I give an explicit yes to the dry run.
- Never pass `--force`, `--allow-overlap` or `--allow-incomplete` unless I explicitly ask for that flag.
