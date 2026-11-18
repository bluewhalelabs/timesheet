---
description: "Stepping away: stop counting your own time while agent work you started keeps running and stays billed"
argument-hint: "[2h | until 17:00 | all]"
---

Use the `timesheet` skill ("Pause and resume" section).

Arguments as typed: `$ARGUMENTS`

Translate what I typed into flags; never pass my words through as they are:

- a duration (`2h`, `90m`, `1h30m`, `1d`): `--for <duration>`
- `until <time>`: `--until <time>` (`HH:MM`, `YYYY-MM-DD` or an ISO timestamp)
- `since <time>` or `at <time>` (I already left then): `--at <time>`
- `all` or `everything`: `--all`; a client key: `--client <key>`
- nothing: no flags (the client of this folder, away until I resume)

Then run it and show the output unchanged:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/timesheet.py" away [--client <key> | --all] [--for <duration> | --until <time>] [--at <time>]
```

If it asks for `--client` or `--all`, ask me which client. When I say I am back, that is `/timesheet:resume`. Do not resume, remove or edit a window unless I ask.
