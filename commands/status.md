---
description: "Where you stand with invoicing: hours this period and which periods still need an invoice"
argument-hint: "[client]"
---

Use the `timesheet` skill ("Status and reminders" section).

Arguments as typed: `$ARGUMENTS`

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/timesheet.py" status` (add `--client <key>` if I named a client) and show the output unchanged. If an invoice is due, mention `/timesheet:invoice <key>`; do not start it.

If I ask to turn on reminders, change what the status shows, or install the status line, follow the skill's "Status and reminders" section (ask before installing anything).

Do not recalculate, round or reword the numbers.
