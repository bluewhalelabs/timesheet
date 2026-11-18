---
description: "Resume billing after a pause or after being away, also for one you forgot to end"
argument-hint: "[at 15:00]"
---

Use the `timesheet` skill ("Pause and resume" section).

Arguments as typed: `$ARGUMENTS`

Translate what I typed into flags; never pass my words through as they are:

- `at <time>` or `since <time>` (I was back at work then and forgot to resume): `--at <time>` (`HH:MM`, `YYYY-MM-DD` or an ISO timestamp)
- `all` or `everything`: `--all`; a client key: `--client <key>`
- nothing: no flags (the client of this folder, resumed now)

Then run it and show the output unchanged, including the hours that were left unbilled:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/timesheet.py" resume [--client <key> | --all] [--at <time>]
```

It ends whichever is active, a pause or an away window. If it has already ended on its own, or I want to change an older pause, follow the skill's "Pause and resume" section (`pauses`, then `pauses --edit` after showing me the change).
