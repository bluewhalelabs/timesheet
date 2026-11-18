---
description: "Ignore a session: mark the current (or a named) session as not billable"
argument-hint: "[session]"
---

Use the `timesheet` skill and follow its "Non-billable sessions" section.

Arguments as typed: `$ARGUMENTS` (the optional session id or unique prefix; with none, the current session).

The command is `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/timesheet.py" ignore [session]`. Tell me which session was marked and for which client. To bill it again, follow the same section (it is a config change; show it and confirm first).
