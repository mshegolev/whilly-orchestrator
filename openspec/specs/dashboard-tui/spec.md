## Purpose

The dashboard-tui capability governs Whilly's two operator progress surfaces: the
in-terminal Rich Live full-screen dashboard (`whilly/dashboard.py` and the read-only
`whilly dashboard` subcommand in `whilly/cli/dashboard.py`) and the browser-side
HTMX/SSE dashboard page served at `GET /` (`whilly/api/dashboard.py`). This capability
covers the Rich Live render cadence, its overlay-mode state transitions, the registered
interactive hotkey contract, the headless `NullDashboard` substitution, and how the web
dashboard pushes live updates over the `/events/stream` SSE channel with an `hx-get`
fragment-polling fallback. The FastAPI control plane itself (endpoints, transport RPC,
auth) belongs to the `web-status-ui` capability and is referenced here, not duplicated.

## Requirements

### Requirement: Rich Live full-screen render cadence
The system SHALL start the in-terminal dashboard as a full-screen Rich `Live` display
configured with `screen=True` and `refresh_per_second=1` when `Dashboard.start` is called.

#### Scenario: Dashboard starts at the documented cadence
- **WHEN** `Dashboard.start` is invoked on a TTY
- **THEN** a Rich `Live` instance SHALL be created with `screen=True` and
  `refresh_per_second=1`
- **AND** the Live display SHALL be started before the keyboard listener is registered

#### Scenario: Each update pushes a fresh render frame
- **WHEN** `Dashboard.update` is called while the Live display is active
- **THEN** the system SHALL push a newly built render frame to the terminal via
  `live.update`

### Requirement: Read-only subcommand polling cadence
The `whilly dashboard` subcommand SHALL poll Postgres for the plan's tasks at a default
cadence of one second per tick (`DEFAULT_POLL_INTERVAL = 1.0`) and re-render the Rich
table in place each tick from the database, not from in-memory state.

#### Scenario: Default polling interval
- **WHEN** `whilly dashboard --plan <id>` runs without an explicit `--interval`
- **THEN** the polling loop SHALL re-fetch and re-render once per second

#### Scenario: Missing database pointer fails with environment exit code
- **WHEN** `WHILLY_DATABASE_URL` is unset at `run_dashboard_command`
- **THEN** the system SHALL print a diagnostic to stderr and return
  `EXIT_ENVIRONMENT_ERROR` (2) without grabbing the terminal

#### Scenario: Missing plan fails with environment exit code
- **WHEN** the requested `--plan` id is absent from the `plans` table
- **THEN** the system SHALL raise the internal plan-not-found signal, print a diagnostic,
  and return `EXIT_ENVIRONMENT_ERROR` (2)

### Requirement: Registered interactive hotkey contract
The system SHALL bind exactly the interactive hotkeys registered in `Dashboard.start` —
`d` (task detail), `l` (log overlay), `t` (all tasks), `s` (stats), `$` (cost panel),
`p` (PRD info), `g` (TRIZ generate plan), `c` (challenge plan), `n` (new idea / PRD
wizard), `r` (reset task), and `h` / `?` (help) — and treat any unbound key as a no-op.

#### Scenario: A registered hotkey invokes its callback
- **WHEN** the operator presses any of `d`, `l`, `t`, `s`, `$`, `p`, `g`, `c`, `n`, `r`,
  `h`, or `?` while the dashboard is in normal (non-input) mode
- **THEN** the system SHALL dispatch the registered callback for that key

#### Scenario: An unbound key is ignored
- **WHEN** the operator presses a key that has no registered callback
- **THEN** the system SHALL take no action and SHALL NOT alter the current overlay state

#### Scenario: Hotkey dispatch is case-insensitive
- **WHEN** a hotkey is registered via `KeyboardHandler.register`
- **THEN** the system SHALL store and match the key by its lowercased form

### Requirement: New-idea wizard mode selection keys
The system SHALL, while the operator is choosing a PRD-wizard mode after entering a new
idea, accept the transient keys `1` (interactive), `2` (background), and `3` (tmux) and
SHALL unregister them once a mode has been selected.

#### Scenario: Mode keys are registered after an idea is entered
- **WHEN** the operator submits a non-empty idea in the `n` new-idea input
- **THEN** the system SHALL register `1`, `2`, and `3` to the interactive, background, and
  tmux wizard-mode callbacks respectively

#### Scenario: Mode keys are cleared after selection
- **WHEN** any wizard mode (`1`, `2`, or `3`) is selected
- **THEN** the system SHALL reset the `1`, `2`, and `3` handlers to no-ops

### Requirement: Overlay-mode state transitions
The system SHALL track the active overlay through `_overlay_mode` values of `log`,
`task_log`, or `detail`, and SHALL re-read the underlying log file on each render tick
only while the overlay mode is `log` or `task_log`.

#### Scenario: Log overlays refresh live each tick
- **WHEN** `_overlay_mode` is `log` or `task_log` and overlay text is present during a
  render
- **THEN** the system SHALL re-read the current log file and rebuild the overlay text
  before rendering

#### Scenario: Detail-to-log transition switches to the task log
- **WHEN** the operator presses `l` while a task `detail` overlay is open for a known task
- **THEN** the system SHALL switch `_overlay_mode` to `task_log`, bind the log to that
  task's id, and refresh the overlay

#### Scenario: Closing an overlay clears overlay state
- **WHEN** overlay text is cleared to `None` during a render
- **THEN** the system SHALL reset `_overlay_mode`, `_log_task_id`, and `_detail_task_id`
  to `None`

### Requirement: Headless NullDashboard substitution
The system SHALL substitute `NullDashboard` for the live `Dashboard` in headless / non-TTY
mode so that `start`, `stop`, and `update` are no-ops and no Rich Live display is created.

#### Scenario: NullDashboard suppresses the live display
- **WHEN** `NullDashboard.start`, `NullDashboard.stop`, or `NullDashboard.update` is called
- **THEN** the system SHALL perform no terminal rendering and SHALL NOT instantiate a Rich
  `Live` display

#### Scenario: Keyboard listener never grabs a non-TTY stdin
- **WHEN** `KeyboardHandler.start` is called and `sys.stdin.isatty()` is false
- **THEN** the system SHALL NOT start the key-listener thread

### Requirement: Web dashboard live updates over SSE with polling fallback
The web dashboard page SHALL push live updates over the `GET /events/stream` SSE channel
and SHALL degrade to `hx-get` fragment refreshes against `/?fragment=workers|tasks|logs`
when the SSE EventSource is unavailable.

#### Scenario: Full page renders the SSE-connected tables
- **WHEN** `render_dashboard` is invoked without a `fragment`
- **THEN** the system SHALL return a 200 HTML response from the `index.html.j2` template
  wired to the `/events/stream` SSE channel

#### Scenario: Fragment request returns the matching partial
- **WHEN** `render_dashboard` is invoked with `fragment` normalising to `workers`,
  `tasks`, or `logs`
- **THEN** the system SHALL return the corresponding partial template (the workers table,
  the tasks table, or the logs fragment) with a `Cache-Control: no-store` header

#### Scenario: Database failure renders a banner, not a 500
- **WHEN** the operator-snapshot fetch raises during `render_dashboard`
- **THEN** the system SHALL return a 200 HTML response carrying an error banner instead of
  raising a server error
