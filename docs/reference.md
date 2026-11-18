# timesheet: reference

The full reference for the timesheet plugin: how time is counted, every setting, the invoice flow in detail, Mercury, privacy and troubleshooting. For a quick start, see the [README](../README.md).

A Claude Code plugin that tracks the time you bill for AI-assisted client work, and turns it into a timesheet PDF and invoice line items per billing period.

Consultants who work through Claude Code bill for two kinds of time: the time the agent spends running, and their own time reading responses, researching and writing the next prompt. This plugin records both automatically (no timers), attributes them to the right client and project from where the work happened, and produces a date-by-date timesheet you can attach to your invoice. It is for people who invoice clients for hours and want every billed minute to trace back to logged events and a stated rule.

It is not an invoicing system. Your invoicing tool issues, numbers and sends the invoice; this plugin produces the supporting timesheet and the line items. Optionally it can create the invoice in Mercury (unsent).

## How time is counted

Hooks log when each turn (one prompt and its reply) starts and ends. Everything is computed from those timestamps; nothing is estimated by a model.

- **Agent time**: any moment at least one turn is running. If turns run in parallel in different projects, that moment is split evenly between them. Time is never double counted.
- **Your time**: the idle gaps between agent activity, credited by this rubric:

| Gap between agent activity | Credited | Config keys (defaults) |
|---|---|---|
| up to `engaged_gap_minutes` | the whole gap | `engaged_gap_minutes` (15) |
| up to `break_gap_minutes` | the first `engaged_gap_minutes` | `break_gap_minutes` (45) |
| longer | `read_allowance_minutes` after the last response plus `rampup_allowance_minutes` before the next prompt | `read_allowance_minutes` (5), `rampup_allowance_minutes` (5) |

Your time is attributed like this: a gap up to `engaged_gap_minutes` goes to the project of the prompt that ends it; the credited part of a medium gap and the read allowance after a break go to the project of the response just read; the ramp-up goes to the project of the next prompt.

With several clients the timeline is shared. Parallel work for two clients splits that moment between them, and a gap leading into client B's prompt is never billed to client A.

- **Billable** = your time + agent time x `agent_billable_fraction` (default 1.0), at the hourly rate.
- **What the client sees**: one hours figure per line at one rate. The split between your time and agent time is for you (the quick report shows it) and stays off the timesheet unless you set `show_agent_time` for that client.
- **Session**: a block of working time (for example a couple of hours) across any threads and projects, split where a gap exceeds `session_break_minutes` (default 45). Sessions are the unit that gets a one-line work summary on the invoice.
- **Interrupted turns**: if you interrupt a turn, Claude Code does not fire `Stop`. That turn is closed at `unclosed_cap_minutes` (default 10) when the session's next prompt or `SessionEnd` arrives, and the invoice draft warns you. A turn that is still open when you run a report or draft, and began more than `unclosed_cap_minutes` ago, is not counted yet; the report and the draft say how many and on which days. It is counted (capped) once the session logs its next event.
- **Non-billable sessions** stay on the timeline (so their time is not credited to anything else) but never appear on an invoice. Coming back to an ignored session is not treated as coming back to the client's work: a gap of up to `break_gap_minutes` that ends at an ignored session's prompt credits only `read_allowance_minutes` to the work before it, not `engaged_gap_minutes`. See `/timesheet:ignore`.

Whether a session belongs to a client is decided by that client's `scope` (see Configuration).

## Requirements

- macOS or Linux (no Windows support in v1).
- `python3` 3.9 or newer on your PATH. No packages to install.
- Optional: Google Chrome, Microsoft Edge or Chromium, for PDF output. Without one, the timesheet is written as HTML and opened so you can use Print, Save as PDF.

## Install

```
claude plugin marketplace add <path-or-git-url-of-this-repo>
claude plugin install timesheet@timesheet
```

Plugins are enabled per Claude config directory, and hooks fire only in accounts where the plugin is enabled. If you use several Claude accounts, repeat the install for each one:

```
CLAUDE_CONFIG_DIR=~/.claude-clientx claude plugin marketplace add <path-or-git-url>
CLAUDE_CONFIG_DIR=~/.claude-clientx claude plugin install timesheet@timesheet
```

All accounts share one data directory, `~/.timesheet/` (override with the `TIMESHEET_HOME` environment variable). Sessions under an account without the plugin enabled are not tracked.

Commands are prefixed with the plugin name: `/timesheet:status`, `/timesheet:invoice`, `/timesheet:report`, `/timesheet:ignore`, `/timesheet:setup`, `/timesheet:doctor`. You can also just ask in plain words ("timesheet status", "invoice Acme for last week", "ignore this session"); the skill picks it up.

## First-time setup

Run `/timesheet:setup`. Claude asks one question at a time (your details, timezone, first client, what identifies their work, rate, billing period), shows you the resulting `~/.timesheet/config.json` before writing it, then runs `doctor` and explains any warnings.

You can also copy `config.example.json` to `~/.timesheet/config.json` and edit it by hand, then check it:

```
python3 scripts/timesheet.py doctor
```

`doctor` verifies Python, the config, each client's scope paths, the PDF browser, logged hook errors, whether the plugin is enabled in each account, and whether the Mercury token variable is set.

## Daily use

You do nothing while you work; the hooks log in the background. Then:

| Command | What it does |
|---|---|
| `/timesheet:report` | Quick terminal report. With no arguments it reports the current billing period when one client is configured, or the current Monday–Sunday week (all clients) when several are. Report options are passed as flags, not words: `/timesheet:report --client acme --period last --by day` |
| `/timesheet:status` | Where you stand with invoicing: hours in the current period and which completed periods still need an invoice (see Status and reminders) |
| `/timesheet:ignore [session]` | Mark the current (or a named) session non-billable |
| `/timesheet:pause [2h \| until 17:00 \| all]` | Stop billing for a while; time is still recorded (see Pause and resume) |
| `/timesheet:away [2h \| until 17:00 \| all]` | Stop counting your own time while agent work keeps running and stays billed |
| `/timesheet:resume [at 15:00]` | Start billing again after a pause or being away, or end a forgotten one at the time you were back |
| `/timesheet:doctor` | Health check |
| `/timesheet:setup` | Guided setup, also used to add a client |
| `/timesheet:invoice [client] [period]` | Draft, review and render an invoice for a billing period |

Report options: `--client KEY|all`, `--period this|last|YYYY-MM-DD|YYYY-Www|START_END`, `--week`, `--since`, `--until`, `--all`, `--by project|day|week`, `--project NAME`, and `--md`, `--csv` or `--json` output. The quick report differs from the invoice in two ways: (1) it leaves out manual entries (`manual.jsonl`), and says under each client's table how many it left out and what they add up to; (2) it does not apply the invoice's per-line rounding, so amounts can differ by cents (or more with coarse rounding).

## Status and reminders

Three optional ways to see whether an invoice is due, all built from the same pieces and the same numbers as the invoice draft. Nothing here is on unless you ask for it, except the `status` command, which only prints.

```json
{"status": {"components": ["invoice_due", "current_hours"], "show_amounts": false, "reminders": "off"}}
```

| Key | Meaning | Default |
|---|---|---|
| `status.components` | what the one-line status shows, in this order: `client` (the client's name), `invoice_due`, `current_hours`. No repeats; may be empty | `["invoice_due", "current_hours"]` |
| `status.show_amounts` | add money to the hours. When false the line and the reminder show hours only | false |
| `status.reminders` | the session-start notice: `off`, `session_start` (every new session), or `daily` (once per day per client) | `off` |

`status` is set once at the top level of the config. It is how you want things displayed, so it is not a per-client setting.

What the components say:

| Component | Text | With `show_amounts` |
|---|---|---|
| `client` | `Acme Co` | the same |
| `invoice_due` | `Invoice due: Nov 9 – Nov 15 (12.40 h)`; with several periods `Invoice due: 2 periods (20.15 h)`; left out when nothing is due | `Invoice due: Nov 9 – Nov 15 (12.40 h, $1,860.00)` |
| `current_hours` | `Current hours: 5.06 h` (the billing period containing today) | `Current hours: 5.06 h ($759.00)` |

An invoice is due for a completed billing period, among the last six, that has a non-zero total and is not invoiced yet. A period counts as invoiced once its draft is approved and its numbers have not changed since, or once it has a Mercury record. An approved draft whose numbers changed (stale) is due again. The current period is never due.

**The status command.** `/timesheet:status`, or:

```
python3 scripts/timesheet.py status [--client KEY] [--cwd DIR] [--line] [--json]
```

Without `--client` it shows the client whose scope the working directory falls in. Outside every client's scope it shows the only client, or one block per client when several are configured. The default output is the client's name, the current period with its hours, the state of the last completed period (`nothing to bill`, `not drafted`, `stale`, `approved`, `rendered` or `in Mercury`) and one line per period that is due. `--line` prints the one-line form described above. `--json` is for Claude and other tools and always includes amounts, whatever `show_amounts` says.

**The status line (optional).** Claude Code can show one line of text under the prompt. It has a single `statusLine` setting per account, and you may already use it for something else, so the plugin never sets it on its own. To install:

```
python3 scripts/timesheet.py statusline --install
```

This writes a small launcher, `~/.timesheet/statusline.sh`, and points `statusLine` in `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`; `--settings PATH` for another file) at it, refreshed every 60 seconds. Every other setting is kept, and the previous file is copied to `settings.json.bak-timesheet`. If a different status line is already configured, nothing is changed: the command shows the existing one and how to call the launcher from your own script, and `--force` replaces yours. With several Claude accounts, install it in each account where you want it. `statusline --print` shows the launcher path and the settings snippet without changing anything, and `statusline --uninstall` removes the setting only if it is this plugin's.

The status line shows the configured components for the client the session's directory belongs to, and nothing in a directory outside every client's scope. The launcher finds the plugin through `~/.timesheet/plugin-root`, which the plugin refreshes at each session start, so it keeps working after plugin updates. The line is cached for a minute in `~/.timesheet/status-cache.json`.

**The reminder (optional).** With `status.reminders` set to `session_start` or `daily`, starting a new Claude Code session inside a client's scope while an invoice is due for that client shows a one-line notice such as `timesheet: Invoice due: Nov 9 – Nov 15 (12.40 h). Run /timesheet:invoice acme`, and tells Claude that an invoice is due so it can mention it once. It never starts the invoice flow by itself. `daily` shows it at most once per day per client (tracked in `~/.timesheet/reminders.json`).

The reminder appears only in sessions inside a client's scope and only ever carries that client's figures; a session elsewhere shows nothing, and no client's name or numbers appear in another client's session. Resumed and compacted sessions do not repeat it. How Claude Code displays a session-start notice outside the terminal (the desktop app, IDE extensions) is not documented by Claude Code, so it may look different or not appear there.

## Pause and resume

There are two ways to stop the clock without stopping the log:

- **Pause** stops it entirely. Nothing inside the window is billed, neither your time nor agent time.
- **Away** stops counting only your own time. Agent runs you started keep going and their time stays billable.

In both cases the tracking hooks keep recording exactly as before, and every surface that shows numbers says how much was left out, so a window that ran too long can be corrected afterwards and the time comes back.

```
python3 scripts/timesheet.py pause  [--client KEY | --all] [--for DURATION | --until WHEN] [--at WHEN] [--cwd DIR]
python3 scripts/timesheet.py away   [--client KEY | --all] [--for DURATION | --until WHEN] [--at WHEN] [--cwd DIR]
python3 scripts/timesheet.py resume [--client KEY | --all] [--at WHEN] [--cwd DIR]
python3 scripts/timesheet.py pauses [--client KEY] [--json]
python3 scripts/timesheet.py pauses --remove ID
python3 scripts/timesheet.py pauses --edit ID [--start WHEN] [--end WHEN] [--mode all|mine]
```

In Claude Code: `/timesheet:pause [2h | until 17:00 | all]`, `/timesheet:away [2h | until 17:00 | all]` and `/timesheet:resume [at 15:00]`.

| Flag | Meaning |
|---|---|
| `--client KEY` / `--all` | which client; `--all` covers every client. Default: the client the working directory belongs to (as for `status`), else the only configured client; otherwise the command stops and asks for one of the two |
| `--for DURATION` | plan an automatic resume after `90m`, `2h`, `1h30m` or `1d`, counted from the start of the window |
| `--until WHEN` | plan an automatic resume at that time |
| `--at WHEN` | `pause` / `away`: it began then (not in the future). `resume`: billing resumed then, for a window you forgot to end (not before it began, not in the future) |
| `--start WHEN` / `--end WHEN` | with `pauses --edit`: the new start or end (start before end, neither in the future) |
| `--mode all` / `--mode mine` | with `pauses --edit`: make the window a full pause (`all`) or an away window (`mine`) |

`WHEN` is `HH:MM` (today, in the config timezone; for `--until` a time already past means tomorrow), `YYYY-MM-DD` (the start of that day) or a full ISO timestamp (without an offset it is read in the config timezone).

`resume` ends whichever window is active and prints which kind it closed, its length and the logged hours it left unbilled for that client. Resuming one client while a window covers all clients ends that window and opens an equal one for the other clients, so only the named client resumes. `pauses` lists the windows, newest first, with their kind (`paused` or `away`) and the unbilled hours in each. `pauses --remove` makes a window's time billable again and `pauses --edit` moves its start or end or changes its kind; both print the window before and after. If a draft was already approved for a period the change touches, its numbers no longer match and it becomes stale (see the invoice flow).

**One window at a time.** A client is never under two windows at once, counting the ones for all clients. A full pause is stronger than away. Opening a window when one is already active:

| Active | You run | What happens |
|---|---|---|
| a pause | `pause` or `away` for that client | nothing; it says since when ("a full pause covers away") |
| away | `away` for that client | nothing; it says since when |
| away | `pause` for that client | the away window ends at the pause's start and the pause begins. If the away covered all clients, it is re-opened for the others |
| a client's own pause or away | `pause --all` | the client's own window ends there and the pause for all clients takes over |
| a client's own away | `away --all` | the same: its own window ends, the one for all clients takes over |
| a client's own pause | `away --all` | the paused client is left as it is; every other client that is not covered gets its own away window, and no window for all clients is made |

One line is printed for each window that was ended or left alone. Ended windows stay in the file as history. A backdated `--at`, an `--until` or a `pauses --edit` that would make two windows of one client overlap in time is refused with exit 2, naming the other window, and nothing is changed.

**The rule.** The timeline is built as if there were no windows. Then every credited segment of a client that overlaps one of that client's pause windows, agent time and your time alike, is cut at the window's edges, and the part inside the window is treated like an ignored session: kept on the timeline, never billed. An away window does the same to your time only; agent segments inside it are untouched. Where a pause and an away window cover the same moment (possible only in a hand-edited file) the pause counts, and overlapping windows are merged. Nothing else changes. So a reading allowance that runs into a pause is cut where the pause starts, and a ramp-up that begins before a resume is cut at the resume. Time of sessions that are already ignored is not counted as paused. One client's window never changes another client's numbers.

**The file.** `~/.timesheet/pauses.json` is a JSON list, written with mode 0600:

```json
[{"id": "p1", "client": "acme", "mode": "all", "start": "2026-11-12T10:00:00Z", "end": null, "until": "2026-11-12T12:00:00Z", "created": "2026-11-12T10:00:03Z"}]
```

`client` is a client key or `"*"` for every client. `"mode"` is `"all"` for a pause or `"mine"` for away; an entry without it is a pause. A window ends at `end` when set, else at `until` (the planned resume) when set; with neither it is still open and runs to the present. A window whose `until` has passed is simply over; the file is not rewritten. Ids are `p1`, `p2`, and so on. If the file cannot be read or an entry is invalid, every command that computes billed time stops with an error instead of billing paused time, and `doctor` reports FAIL.

**Where it shows.**

- `status`: `PAUSED since <time>` or `AWAY since <time>` (and `until <time>`) under the client's name; `--line` and the status line start with `PAUSED` or `AWAY` (and `until HH:MM`) before the configured components; `--json` has `paused: {id, mode, start, until}` or `null`.
- `status`, the report footer and the invoice draft's warnings, when time was left out of the period shown: `1.25 h were logged while paused and are not billed (2026-11-12). If a pause ran too long: timesheet pauses`. When only away windows removed it: `0.33 h of your own time were logged while away and are not billed (2026-11-12). If you were back sooner: timesheet pauses`. The hours are what would have been billed (your time plus agent time at `agent_billable_fraction`), before rounding. The draft JSON also has `paused_hours`, `paused_dates` and `paused_kind` (`"paused"`, `"away"` or `null`); the report JSON has `paused_hours` and `paused_kind`.
- Session start: in a session of a paused client you see `timesheet: PAUSED for Acme Co since <time>. Run /timesheet:resume to start billing again.`, and while away `timesheet: AWAY for Acme Co since <time>; your own time is not being counted. Run /timesheet:resume when you are back.` These notices do not depend on `status.reminders`; when an invoice reminder is due as well, both appear on one line.
- `doctor`: the `pauses` line is WARN while a window is active (`paused since <time> for acme`, `away since <time> for acme`) or when the file contains overlapping windows for a client (it names the ids), and FAIL when the file is invalid.

## The invoice flow

`/timesheet:invoice acme last` does the following. Steps marked (you) need your decision.

1. Computes the period's lines, hours and totals with `invoice.py compute`. Claude never calculates these.
2. Reads a digest of each session's transcript (only for sessions without a saved summary) and drafts one plain client-facing line per session.
3. Shows the full draft: label, description, hours, rate, amount, the total, and every warning. (you) Review it. Edit any wording. You cannot change hours through Claude: to change numbers, change the config (rate, rounding, thresholds), add a `manual.jsonl` entry, or mark a session non-billable, then it recomputes.
4. (you) Approve. Only then are the summaries saved (`invoice.py save`), to `<output_dir>/<client>/<period-id>/lines.json`. Saving removes any timesheet rendered for an earlier approval. If the period overlaps another period of the same client that was already approved or invoiced (for example after switching a client from weekly to monthly), the draft warns and `save` refuses unless you pass `--allow-overlap`.
5. Renders the timesheet (`invoice.py render`): HTML and PDF in the same folder, plus a tab-separated block of line items to paste into your invoicing tool, and the PDF path. It also writes `render.json`, recording which approval and total were rendered.
6. If Mercury is enabled for the client: shows a dry run (target SANDBOX, PRODUCTION or CUSTOM, total, line count). (you) Say yes to create the invoice, unsent. Mercury refuses (also for the dry run) unless the timesheet was rendered for the current approval, refuses a period that overlaps another approved or invoiced period unless `--allow-overlap`, and refuses a period that has not ended yet unless `--allow-incomplete`.
7. (you) Attach the PDF and send the invoice yourself. Nothing is ever sent for you.

If you re-run for the same period, approved summaries are reused and numbers are recomputed. If the numbers changed since approval, the draft is marked stale and you must approve again before rendering.

## Configuration

One file: `~/.timesheet/config.json`. Settings cascade: `defaults`, then the client's `overrides`, then the project's entry under `projects`. Everything that affects a number, label or piece of invoice text is a setting; there is no hardcoded client, rate or currency.

### Worked example: two clients, different rates and periods

```json
{
  "you": {"name": "Your Name", "business": "Your Business LLC", "email": "you@example.com",
          "address": "123 Example St, Anytown", "logo": ""},
  "timezone": "America/Los_Angeles",
  "defaults": {
    "hourly_rate": 150,
    "billing_period": {"type": "weekly", "week_starts": "monday"}
  },
  "clients": {
    "acme": {
      "name": "Acme Co",
      "scope": {"accounts": ["~/.claude-clientx"], "directories": ["~/Code/ClientX"], "repos": []},
      "projects": {"project-a": {"hourly_rate": 175}}
    },
    "beta": {
      "name": "Beta Inc",
      "scope": {"directories": ["~/Code/ClientY"]},
      "overrides": {
        "hourly_rate": 200,
        "billing_period": {"type": "monthly"},
        "rounding": {"increment_hours": 0.25, "mode": "up"}
      }
    }
  }
}
```

`acme` uses the defaults (150/h, weekly, rounding to 0.01 h) except `project-a`, billed at 175/h. `beta` is 200/h, invoiced monthly, with every line rounded up to the next quarter hour. Missing keys are filled from defaults, so a client entry can be this short. `config.example.json` is a longer starting point.

### Scope

A client's `scope` says which sessions are theirs. A session matching any entry counts.

| Kind | Matches | Project is |
|---|---|---|
| `accounts` | every session under a Claude config dir (`CLAUDE_CONFIG_DIR`, default `~/.claude`) | the folder under one of the client's `directories` if the cwd is there, else the git repo name, else the cwd's folder name |
| `directories` | sessions whose cwd (or a worktree's main repo) is inside the folder | the first folder under it; `(root)` at the folder itself |
| `repos` | one git repo, by remote URL or path, worktrees included | the repo name |

When several clients match, the most specific wins (repo, then directory, then account); ties go to the first client in config order. No match means nothing is logged.

### Reference

Top level:

| Key | Meaning | Default |
|---|---|---|
| `you` | `name`, `business`, `email`, `address`, `logo` (path to an image) shown in the PDF header | empty |
| `timezone` | IANA zone used for days and billing periods; empty means the system zone | empty |
| `defaults` | shared settings (below) | see below |
| `clients` | object keyed by client key (letters, digits, `.`, `_`, `-`) | none |
| `pdf` | `browser_path` (empty = auto-detect), `output_dir` | auto, `~/.timesheet/invoices` |
| `mercury` | `api_key_env` (name of the env var holding the token), `base_url` (empty = production), `allow_custom_host` (true lets `base_url` point at a host other than `api.mercury.com`, `api-sandbox.mercury.com` or loopback) | `MERCURY_API_KEY`, empty, false |
| `status` | `components` (list of `client`, `invoice_due`, `current_hours`), `show_amounts`, `reminders` (`off`, `session_start`, `daily`); see Status and reminders. Top level only | `["invoice_due", "current_hours"]`, false, `off` |

Settings. "Where" says where a key may be set: `all` means `defaults`, a client's `overrides` and a project entry under `projects`; `client` means `defaults` or a client's `overrides` only (a project entry with such a key is a config error).

| Key | Meaning | Default | Allowed | Where |
|---|---|---|---|---|
| `hourly_rate` | rate per hour | 150 | number >= 0 | all |
| `currency` | currency label shown on the timesheet | USD | text | client |
| `agent_billable_fraction` | share of agent time that is billed | 1.0 | 0 to 1 | all |
| `engaged_gap_minutes` | gaps up to this count in full | 15 | >= 0, at most `break_gap_minutes` | all |
| `break_gap_minutes` | gaps up to this credit `engaged_gap_minutes` | 45 | >= 0 | all |
| `session_break_minutes` | gap that starts a new session | 45 | >= 0 | client |
| `read_allowance_minutes` | credit after the last response before a long break | 5 | >= 0; with `rampup_allowance_minutes` at most `break_gap_minutes` | all |
| `rampup_allowance_minutes` | credit before the next prompt after a long break | 5 | >= 0; see `read_allowance_minutes` | all |
| `unclosed_cap_minutes` | cap for a turn that never closed (interrupted) | 10 | >= 0 | all |
| `rounding` | `increment_hours` and `mode` | 0.01, nearest | increment > 0; mode `nearest`, `up`, `down` | client |
| `billing_period` | `type`, plus `week_starts` or `anchor` | weekly, monday | see Billing periods | client |
| `line_items` | how time becomes invoice lines | `session` | `day`, `session`, `project`, `period` | client |
| `show_session_detail` | add a detail page with clock times | false | true or false | client |
| `show_project_subtotals` | add a "By project (before per-line rounding)" table when the period has more than one project; its figures are rounded once per project, so they can differ slightly from the line total | false | true or false | client |
| `show_agent_time` | on the session detail page, split each session into "Your time" and "Agent time" ; when false the page has one Hours column (the billed time) | false | true or false | client |
| `project_labels` | project name to display label | `{"(root)": "General / cross-project"}` | object of text | client |
| `non_billable_sessions` | session ids or unique prefixes to exclude (a session is excluded whatever project it worked in) | `[]` | list of text | client |
| `text` | PDF wording: `title`, `period_label`, `counting_note`, `footer` | "Timesheet", "Professional services", empty (no note), empty | text; `counting_note` may use `{engaged_gap_minutes}`, `{break_gap_minutes}`, `{read_allowance_minutes}`, `{rampup_allowance_minutes}` | client |

The gap thresholds are checked on the effective settings at each level (defaults, each client, each project): `engaged_gap_minutes` must not exceed `break_gap_minutes`, and `read_allowance_minutes + rampup_allowance_minutes` must not exceed `break_gap_minutes`, so a break never credits more than the gap itself.

Per client: `name`, `contact`, `address`, `scope`, `overrides`, `projects`, `non_billable_sessions`, `mercury` (below). `timezone` is only valid at the top level.

### Billing periods

| `type` | Period | Extra keys |
|---|---|---|
| `weekly` | seven days from `week_starts` (default `monday`) | `week_starts`: lowercase weekday |
| `biweekly` | fourteen days counted from `anchor` | `anchor`: `YYYY-MM-DD` (required) |
| `semimonthly` | 1st to 15th, 16th to month end | none |
| `monthly` | calendar month | none |

Selectors for `--period`: `last` (default for invoices), `this`, a date inside the period (`YYYY-MM-DD`), an ISO week (`YYYY-Www`), or an explicit `START_END`.

### Rounding

Each line's hours are rounded by `rounding`, the line amount is the rounded hours times the rate (to the cent), and the total is the sum of the lines. This keeps the PDF and the invoice identical to the cent, but it means an invoice total can differ from the unrounded `report` total. With 0.25-hour rounding up the difference can be large by design.

### Line item modes

| `line_items` | One line per |
|---|---|
| `session` | work session (default; labelled with its date, and numbered when a day has several) |
| `day` | day (description is that day's session summaries) |
| `project` | project in the period |
| `period` | the whole period |

A line that would mix projects with different rates is split into one line per rate.

### Timesheet PDF and branding

The PDF is date-centric with no clock times: header (your business and logo, client, period, reference such as `acme-2026-03-02_2026-03-08`), a table of Date, Description, Hours, Rate and Amount, a total, optional per-project subtotals (`show_project_subtotals`), and optionally (`show_session_detail`) a page of sessions with start and end times, and their hours. Nothing on the timesheet explains how time is counted unless you write a `text.counting_note`, which is printed under that page. That page shows one Hours column; set `show_agent_time` to split it into your time and agent time.

To rebrand, copy or edit `templates/timesheet.html` (HTML and CSS). It uses these slots, which are replaced at render time: `{{title}}`, `{{reference}}`, `{{business}}`, `{{logo}}`, `{{you_block}}`, `{{client_block}}`, `{{period_label}}`, `{{item_header}}`, `{{rows}}`, `{{subtotals}}`, `{{total_hours}}`, `{{total}}`, `{{detail_page}}`, `{{footer}}`. Keep the slots; change everything around them. Fixed wording (title, period label, footer, counting note) is configurable under `text`.

### Manual entries

For time outside Claude Code (calls, email, backfills), append lines to `~/.timesheet/manual.jsonl`:

```
{"date": "2026-03-04", "client": "acme", "project": "project-a", "hours": 1.5, "description": "Planning call"}
```

`project` is optional (default `(root)`), `hours` must be greater than 0. These count as your time and are included in the next draft.

## Mercury add-on (optional, off by default)

What it does: creates an invoice in Mercury (Accounts Receivable API, beta) from the approved line items, using the client's `customer_id` and `destination_account_id`, invoice date = period end, due date = `due_in_days` later. Records the Mercury invoice id in `mercury.json` next to the draft and refuses to create a second invoice for the same period unless `--force` is passed. An existing record blocks a second invoice, including a pending one left behind by a network failure, so check Mercury before forcing.

What it does not do: send anything. Every invoice is created with `sendEmailOption: DontSend`. Mercury's API cannot attach files or send the invoice, so you open it in Mercury, attach the timesheet PDF and send it yourself.

Setup:

1. Export a token in your shell profile; the config holds only the variable name (`mercury.api_key_env`). Never put the token in config, in chat or on a command line. Read-write tokens need an allowlisted IP address in Mercury's settings (per Mercury's docs).
2. For Mercury's sandbox, set `mercury.base_url` to the sandbox URL from Mercury's documentation and use a sandbox token. The dry run prints the target as SANDBOX, PRODUCTION or CUSTOM. Only `api.mercury.com`, `api-sandbox.mercury.com` and loopback (`127.0.0.1`, `localhost`, for testing) are accepted; any other host is refused unless `mercury.allow_custom_host` is true, so a typo cannot send your token and invoice data elsewhere.
3. `python3 scripts/mercury.py accounts` lists accounts (`destination_account_id`); `customers --find "text"` finds a customer (`customer_id`); `create-customer --name ... --email ... --yes` creates one.
4. Per client set `mercury.enabled` to true. Options: `due_in_days`, `cc_emails`, `ach_debit_enabled`, `credit_card_enabled`, `use_real_account_number`.
5. Render the timesheet first (`invoice.py render`). `create-invoice --dry-run` shows the request body and sends nothing; `--yes` creates it. Both refuse a draft whose timesheet was not rendered for the current approval, a period that overlaps another approved or invoiced period (`--allow-overlap` overrides), and a period that has not ended (`--allow-incomplete` overrides).

Status: verified against Mercury's sandbox (list accounts, create a customer, dry run, create an unsent invoice, read it back as Unpaid with matching line items and total, second run refused). Not yet run against a production account.

## Privacy

Stored, in `~/.timesheet/events.jsonl`: timestamp, event type, session id, client, project, working directory and transcript path. No prompt or response text is ever written by the plugin.

Transcripts are read locally, on demand, only while drafting summaries (`invoice.py digest` prints to the terminal for Claude to read). The only text saved is the one-line summaries you approve, in `lines.json`. Nothing is sent anywhere by the plugin except, if you enable it, the invoice request to Mercury. The tracking hook prints nothing, always exits 0 and never blocks a session; its errors go to `hook-errors.log`. The session-start hook also always exits 0; it prints only when the client of that session is paused, or when you turned reminders on and an invoice is due for it (see Status and reminders, Pause and resume). Pause windows are kept in `pauses.json` (times and client keys only). The status features keep three small files next to the log: `status-cache.json` (the last status line per client), `reminders.json` (the day each client was last reminded) and `plugin-root` (where the plugin is installed).

## Troubleshooting

- Run `/timesheet:doctor`. WARN and FAIL lines say what to fix.
- No time logged for a client: check the scope paths exist and match, and that the plugin is enabled in the Claude account you used (`CLAUDE_CONFIG_DIR`). Sessions under an account without the plugin are not tracked.
- `~/.timesheet/hook-errors.log` records hook failures; `doctor` reports its count and the last one. Once you have dealt with them, `python3 scripts/timesheet.py doctor --clear-hook-errors` prints the count and empties the log, so later warnings mean new errors.
- If the config cannot be read (invalid JSON or an invalid value), the hook keeps each event in `~/.timesheet/unattributed.jsonl` (time, event, session, working directory, transcript path and Claude config dir; no client, no prompt text) instead of losing it. Reports, drafts and `doctor` warn while that file has events. After fixing the config, run `python3 scripts/timesheet.py reattribute` (`--dry-run` first to see the counts): events that now match a client's scope are added to the log, duplicates are skipped, events that match no client are dropped, and the file is removed. A missing config keeps nothing.
- Interrupted turns do not fire `Stop`; they are capped at `unclosed_cap_minutes` and flagged in the draft. A final turn with no later event in its session is not counted until one arrives; the report and the draft list those days.
- "event(s) logged for unknown client" or "manual entr(y/ies) for unknown client": the log or `manual.jsonl` names a client key that is no longer in the config (renamed or removed). That time appears on no report or invoice until the key is back in `clients`.
- No PDF: install Chrome, Edge or Chromium, or set `pdf.browser_path`. The HTML is still written.
- Importing an older event log: `timesheet.py import --from <file> --client KEY [--dry-run]` (safe to re-run; duplicates are skipped).

## Limits and non-goals

- macOS and Linux only; Python stdlib only.
- Time outside Claude Code is not tracked; add it with `manual.jsonl`.
- Not an invoice of record: no invoice numbering, payments, taxes, currency conversion or accounting exports.
- The rubric is a convention you choose; adjust the thresholds to what you and your client agree on.

## Development

```
python3 -m unittest discover -s tests -v
claude plugin validate .
claude --plugin-dir .
```

Tests use temporary data directories and never touch `~/.timesheet`. Licensed under the MIT License (see `LICENSE`).
