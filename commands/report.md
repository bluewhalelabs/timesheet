---
description: "Quick report of tracked hours and amounts"
argument-hint: "[--client K] [--period P] [--by project|day|week]"
---

Use the `timesheet` skill ("Quick report" section).

Arguments as typed: `$ARGUMENTS`

Run the report and show the output unchanged. With no arguments it reports the current billing period when one client is configured, or the current Monday–Sunday week (all clients) when several are. Report options are flags passed through as typed (for example `--client acme --period last --by day`); if I typed bare words such as a client key, turn them into the matching flags (`--client acme`, `--period last`) instead of passing them as words:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/timesheet.py" report $ARGUMENTS
```

Do not recalculate, round or reword the numbers in the report.
