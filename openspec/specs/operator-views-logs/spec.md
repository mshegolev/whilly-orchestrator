## Purpose

The operator-views-logs capability governs the post-run and live operator
inspection surfaces that read execution state without driving the agent loop:
the per-task log viewer (`whilly logs`, backed by `whilly/log_viewer.py`), the
shared operator-views taxonomy (`whilly/operator_views.py` — surfaces, tables,
actions, hotkeys, WUI route prefixes, and UI-artifact inventory), and the
browserless operator TUI (`whilly tui`, backed by `whilly/cli/tui.py`). It
defines how operators discover, show, and live-follow per-task logs; the legal
set of operator surfaces/tables/actions and their hotkeys that the TUI and web
renderers share; and how a single hotkey mutates TUI state. This capability is
the read/inspect boundary: the Rich Live run dashboard is owned by the
`dashboard-tui` capability and the FastAPI browser surfaces by `web-status-ui` —
those are referenced here, not duplicated.
## Requirements
### Requirement: Log listing and per-task show
The `whilly logs` command MUST list discovered tasks via `cmd_list` when invoked
with `--list`, and MUST show a single task's prompt, events timeline, and stdout
via `cmd_show` when invoked with a task id and no tail flag.

#### Scenario: --list prints the discovered task table
- **WHEN** `run_logs_command` is invoked with `--list` in its arguments
- **THEN** `cmd_list` SHALL render a table of every task that `discover_tasks`
  found in the log directory (task id, status, duration, cost, last event)
- **AND** it SHALL return 0
- **AND** an empty log directory SHALL print a "No task logs found" notice and
  still return 0

#### Scenario: task id shows prompt, events, and stdout
- **WHEN** `run_logs_command` is invoked with a positional task id and no tail flag
- **THEN** `cmd_show` SHALL print the task's prompt file, its events timeline
  (from `tasks/<id>.events.jsonl`, falling back to filtering the global
  `whilly_events.jsonl`), and its stdout log when each artifact exists
- **AND** it SHALL return 1 when no artifact exists for the requested task id

#### Scenario: missing task id prints usage and returns 2
- **WHEN** `run_logs_command` is invoked with neither `--list` nor a positional
  task id
- **THEN** it SHALL print the usage block to stdout and return 2

### Requirement: Live tail of a task
The `whilly logs --tail <task_id>` command (also `-f`) MUST live-follow the
task's events file and stdout log via the `cmd_tail` poll loop, emitting only
newly-appended content on each poll until interrupted.

#### Scenario: tail follows new events and stdout
- **WHEN** `run_logs_command` is invoked with `--tail` (or `-f`) and a task id
- **THEN** `cmd_tail` SHALL repeatedly read from the last byte offset of the
  events file and the stdout log, printing only the new lines each cycle and
  sleeping `poll_interval` seconds between cycles

#### Scenario: tail exits cleanly on interrupt
- **WHEN** the operator interrupts an active `cmd_tail` loop (KeyboardInterrupt)
- **THEN** the loop SHALL terminate and return 0

### Requirement: Age-based log cleanup
The `cleanup_old_logs` function MUST remove per-task log artifacts whose
modification time is older than the configured TTL in days, while sparing the
global timeline and the rotating logger files, and MUST be a no-op when the TTL
is non-positive.

#### Scenario: stale artifacts older than the TTL are removed
- **WHEN** `cleanup_old_logs(log_dir, ttl_days)` runs with `ttl_days > 0` and the
  directory contains `.log`, `_prompt.txt`, and `tasks/` files older than the cutoff
- **THEN** those expired files SHALL be unlinked and the count of removed files
  SHALL be returned
- **AND** `whilly.log` and its rotated backups (`whilly.log.*`) SHALL be spared

#### Scenario: non-positive TTL disables cleanup
- **WHEN** `cleanup_old_logs` is called with `ttl_days <= 0`
- **THEN** it SHALL remove nothing and return 0

### Requirement: Operator surfaces, tables, and actions taxonomy
The operator-views module MUST define the stable, shared information
architecture as enumerations — `OperatorSurface` (overview, compliance,
plans_tasks, workers, events), `OperatorTable` (tasks, workers, review_gaps,
events), and `OperatorAction` (quit, refresh, filter.focus, workers.pause,
workers.resume, review.select_next/select_previous, review.approve/reject/
changes_requested) — and SHALL bind each action to its hotkeys via
`OperatorActionSpec` in `OPERATOR_ACTIONS`.

#### Scenario: action specs carry hotkeys and review surface scoping
- **WHEN** `operator_action_specs()` is read
- **THEN** each `OperatorActionSpec` SHALL expose its `OperatorAction`, label, and
  hotkey tuple (e.g. quit=`q`/`Q`, refresh=`r`, pause=`p`/`P`, resume=`R`,
  filter=`/`)
- **AND** the review actions (select_next/previous, approve, reject,
  changes_requested) SHALL be scoped to the `COMPLIANCE` surface

#### Scenario: surface switch hotkeys are sequential digits
- **WHEN** `operator_surface_hotkeys()` is read
- **THEN** it SHALL map the digit keys `1`..`N` (one per `OperatorSurface` in
  display order) to their surfaces

### Requirement: Operator action web-UI route prefixes
The operator-views module MUST expose the canonical web-UI route prefixes that
back operator actions — worker control under `/api/v1/admin/workers/` and
human-review decisions under `/api/v1/tasks/` — via `OPERATOR_WUI_ROUTE_PREFIXES`
and the per-action `wui_route_prefix` fields.

#### Scenario: worker-control and review actions carry route prefixes
- **WHEN** `operator_wui_route_prefixes()` is read, or the pause/resume and
  approve/reject/changes_requested action specs are inspected
- **THEN** worker-control actions SHALL carry the `/api/v1/admin/workers/` prefix
- **AND** the human-review decision actions SHALL carry the `/api/v1/tasks/` prefix

### Requirement: Operator UI artifact inventory with status
The operator-views module MUST enumerate the operator UI artifacts in
`OPERATOR_WUI_ARTIFACTS`, each carrying an `OperatorUiArtifactStatus` of
`active`, `routeable_noncanonical`, or `inactive_quarantined`, and quarantined
or non-canonical artifacts MUST carry a reason and follow-up phase.

#### Scenario: artifacts expose status and follow-up metadata
- **WHEN** `operator_wui_artifacts()` is read
- **THEN** each `OperatorUiArtifact` SHALL expose its template/static path and
  status
- **AND** non-active artifacts (`routeable_noncanonical`, `inactive_quarantined`)
  SHALL carry a `reason` and a `followup_phase`

#### Scenario: filtering by status returns only matching artifacts
- **WHEN** `operator_wui_artifacts(status)` is called with a specific status
- **THEN** only artifacts whose status matches SHALL be returned

### Requirement: Operator TUI hotkey state transitions
The `handle_tui_key` function MUST apply exactly one hotkey to the mutable
`TuiState`, switching the active surface when a surface digit key in
`_SURFACE_BY_KEY` is pressed, toggling search mode on `/`, requesting quit on
`q`/`Q`, and queueing pause/resume control actions on `p`/`P` and `R`.

#### Scenario: digit key switches the active surface
- **WHEN** `handle_tui_key(state, key)` is called with a digit key present in
  `_SURFACE_BY_KEY` while not in search mode
- **THEN** `state.surface` SHALL be set to the mapped `OperatorSurface`

#### Scenario: control hotkeys queue a pending action and refresh
- **WHEN** `handle_tui_key` receives `p`/`P` or `R` while not in search mode
- **THEN** `state.pending_control_action` SHALL be set to `pause` or `resume`
  respectively
- **AND** `state.immediate_refresh` SHALL be set so the next poll applies it

#### Scenario: filter mode captures printable characters
- **WHEN** `state.searching` is true and a printable key is pressed
- **THEN** the key SHALL be appended to `state.filter_text`, and Enter or Escape
  SHALL exit search mode

### Requirement: TUI review decisions require a reviewer identity
The operator TUI MUST require a non-empty reviewer identity (from `--reviewer` or
the `WHILLY_OPERATOR_EMAIL` environment variable) before recording any
human-review decision (approve, reject, changes_requested), and MUST surface an
error rather than recording a decision when the identity is absent.

#### Scenario: review action without reviewer surfaces an error
- **WHEN** a pending review action (approve/reject/changes_requested) is applied
  and the reviewer identity is empty
- **THEN** the action SHALL NOT be recorded
- **AND** `state.last_error` SHALL report that a reviewer is required via
  `--reviewer` or `WHILLY_OPERATOR_EMAIL`

#### Scenario: review action with reviewer records the decision
- **WHEN** a pending review action is applied with a non-empty reviewer and an
  actionable review gap is selected
- **THEN** the decision SHALL be recorded for the selected gap's task via the
  human-review decision path

### Requirement: TUI requires database configuration
The `whilly tui` command MUST exit with `EXIT_ENVIRONMENT_ERROR` (2) when
`WHILLY_DATABASE_URL` is not set, and MUST otherwise poll the operator snapshot
from Postgres on its configured interval.

#### Scenario: missing DSN exits 2
- **WHEN** `run_tui_command` runs with `WHILLY_DATABASE_URL` unset
- **THEN** a diagnostic SHALL be written to stderr and the command SHALL return 2

#### Scenario: configured DSN drives the poll loop
- **WHEN** `run_tui_command` runs with `WHILLY_DATABASE_URL` set
- **THEN** it SHALL poll `fetch_operator_snapshot` on the configured interval and
  return 0 when the loop terminates

### Requirement: TUI read-only HTTP transport
The `whilly tui` command SHALL support a read-only HTTP transport when
`--connect URL` (or `WHILLY_CONTROL_URL`) is supplied, polling the
control-plane's `GET /api/v1/operator/snapshot` endpoint with a bearer token
from `--token` or `WHILLY_WORKER_TOKEN` in place of the direct Postgres
transport; when no connect URL is configured the command SHALL default to the
direct Postgres transport (`WHILLY_DATABASE_URL`, unchanged full-capability
behavior). In HTTP transport mode the TUI SHALL be read-only: control actions
(pause/resume) and human-review decisions SHALL be disabled and the TUI footer
SHALL surface a read-only indicator showing they are unavailable. A plain
`http://` URL targeting a non-loopback host SHALL be rejected with an error
unless `--insecure` (`WHILLY_INSECURE=1`) is set. The operator snapshot wire
schema SHALL be a single shared codec used by both the HTTP client and the server
endpoint.

#### Scenario: HTTP transport selected by --connect
- **WHEN** `whilly tui` is invoked with `--connect <URL>` (or
  `WHILLY_CONTROL_URL` set) and a bearer token from `--token` or
  `WHILLY_WORKER_TOKEN`
- **THEN** the TUI SHALL poll `GET /api/v1/operator/snapshot` on the configured
  URL using `Authorization: Bearer <token>`
- **AND** the TUI SHALL NOT require `WHILLY_DATABASE_URL` to be set

#### Scenario: Postgres transport remains the default
- **WHEN** `whilly tui` is invoked with no `--connect` argument and
  `WHILLY_CONTROL_URL` is unset
- **THEN** the TUI SHALL behave exactly as before — requiring `WHILLY_DATABASE_URL`
  and polling the operator snapshot directly from Postgres

#### Scenario: HTTP mode disables control and review actions
- **WHEN** the TUI is running in HTTP transport mode and the operator presses a
  control hotkey (pause `p`/`P`, resume `R`) or a review-decision hotkey
  (approve, reject, changes_requested)
- **THEN** the TUI SHALL NOT dispatch the action to any backend
- **AND** the TUI footer SHALL display a read-only indicator communicating that
  control and review actions are unavailable in HTTP mode

#### Scenario: Non-loopback http:// rejected without --insecure
- **WHEN** `whilly tui` is invoked with `--connect http://<non-loopback-host>`
  and `--insecure` is not set and `WHILLY_INSECURE` is not `1`
- **THEN** the command SHALL exit with an error stating that plain HTTP to a
  non-loopback host requires `--insecure` or `WHILLY_INSECURE=1`

#### Scenario: --insecure permits non-loopback http
- **WHEN** `whilly tui` is invoked with `--connect http://<non-loopback-host>`
  and `--insecure` is set (or `WHILLY_INSECURE=1`)
- **THEN** the TUI SHALL proceed with the HTTP transport without raising an error

