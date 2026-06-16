## Purpose

The self-update-doctor capability governs Whilly's self-maintenance surface:
checking for and applying package updates (`whilly/update.py`,
`whilly/cli/update.py`), read-only environment diagnostics
(`whilly/doctor.py`), bounded automated repair of failed tasks
(`whilly/repair/*`), and Git-based rollback safety nets
(`whilly/rollback/*`, `whilly/cli/rollback.py`). This capability covers how
Whilly inspects its installed version against PyPI, how the doctor surfaces
stale plans and runtime leftovers without deleting anything, how the repair
policy deterministically decides to retry or escalate within a budget, and how
rollback points are created and restored behind a refusal-first preflight and
an exact confirmation phrase.

## Requirements

### Requirement: Non-mutating update check
The system SHALL check for a newer published release by comparing the installed
version against the PyPI JSON API without mutating the installation, and SHALL
fail closed by reporting an error rather than raising when the network boundary
fails.

#### Scenario: Newer release available
- **WHEN** `check_for_update` loads a latest version that compares greater than
  the installed version via `compare_versions`
- **THEN** the system SHALL return an `UpdateCheckResult` with
  `update_available` true and no error
- **AND** the CLI `whilly update check` SHALL print the available upgrade and
  return exit code 0

#### Scenario: Already up to date
- **WHEN** the latest published version is not greater than the installed
  version
- **THEN** the system SHALL return `update_available` false and the CLI SHALL
  report that Whilly is up to date with exit code 0

#### Scenario: Latest-version lookup fails
- **WHEN** the latest-version loader raises any exception (network, parse, or
  missing `info.version`)
- **THEN** `check_for_update` SHALL capture the failure into the result `error`
  field with `latest_version` and `update_available` set to None
- **AND** the CLI SHALL print the manual upgrade guidance to stderr and return
  exit code 1

### Requirement: Explicit package update execution
The system SHALL apply package updates only through an explicit `install`
invocation, selecting the package manager from the requested installer (auto,
pip, or pipx) and supporting a dry run that prints the command without running
it.

#### Scenario: Auto installer prefers pipx in a pipx context
- **WHEN** `build_install_command` runs with installer `auto`, a pipx
  executable is present, and `PIPX_HOME` or `PIPX_BIN_DIR` is set in the
  environment
- **THEN** the system SHALL build a `pipx upgrade` command for the package
- **AND** SHALL otherwise build a `pip install --upgrade` command using the
  current Python executable

#### Scenario: Dry run prints without installing
- **WHEN** `run_package_update` is called with `dry_run` true
- **THEN** the system SHALL return a result whose command is the resolved argv,
  return code 0, and `dry_run` true without invoking the runner
- **AND** the CLI SHALL print the `Would run:` line and return exit code 0

#### Scenario: Unsupported installer requested
- **WHEN** an installer is requested that cannot be satisfied (for example pipx
  with no pipx executable found)
- **THEN** the system SHALL return an unsupported-kind result with guidance and
  a non-zero return code rather than executing any command

### Requirement: Automatic update policy fails closed to off
The system SHALL resolve the automatic update policy from
`WHILLY_UPDATE_MODE` (or an explicit override) to one of off, check, or
install, and SHALL treat any unrecognized value as off so updates never run
implicitly.

#### Scenario: Unknown mode value
- **WHEN** `resolve_update_mode` receives a value that is not off, check, or
  install
- **THEN** the system SHALL return `UpdateMode.OFF`

#### Scenario: Auto policy off
- **WHEN** the resolved mode is off
- **THEN** `whilly update auto` SHALL print that automatic updates are off,
  perform no check or install, and return exit code 0

#### Scenario: Auto policy install applies an available update
- **WHEN** the resolved mode is install and the update check reports an
  available newer release
- **THEN** the system SHALL run the package update and, on success, report the
  installed version and return exit code 0

### Requirement: Read-only doctor diagnostics
The system SHALL run the doctor as a strictly read-only diagnostic that
inspects the working directory for orphan plan files, a stale state file,
leftover workspaces and worktrees, and leftover Whilly tmux sessions, and SHALL
NOT delete or modify any file or session.

#### Scenario: Findings are reported without deletion
- **WHEN** `run_doctor` discovers orphan plan files, leftover
  `.whilly_workspaces`/`.whilly_worktrees` directories, or `whilly-` tmux
  sessions
- **THEN** the system SHALL include each finding in the `DoctorReport` and the
  formatted output SHALL advise manual cleanup
- **AND** the system SHALL NOT remove any discovered file or kill any session

#### Scenario: Clean environment
- **WHEN** `run_doctor` finds no orphan plans, no stale state file, and no
  leftover workspaces, worktrees, or tmux sessions
- **THEN** the report `findings` SHALL be empty and the formatted output SHALL
  report that all is clean

### Requirement: Ghost and stale plan classification
The system SHALL classify each discovered orphan plan file as ghost, stale,
invalid_name, not_a_plan, or healthy, marking all-resolved or
all-pending-with-closed-issues plans as ghost and partially-closed-issue plans
as stale.

#### Scenario: All tasks resolved is a ghost
- **WHEN** `diagnose_plan` evaluates a plan whose tasks are all done or skipped
- **THEN** the system SHALL classify the plan as ghost safe to archive

#### Scenario: Pending plan with all linked issues closed is a ghost
- **WHEN** a plan is entirely pending and every referenced GitHub issue
  resolved via `gh` is CLOSED
- **THEN** the system SHALL classify the plan as ghost
- **AND** when only some referenced issues are CLOSED the system SHALL classify
  the plan as stale

#### Scenario: Filename leaked from a URL
- **WHEN** an orphan plan filename contains a colon
- **THEN** the system SHALL classify it as invalid_name without attempting to
  parse it as plan JSON

### Requirement: Bounded repair decision
The system SHALL produce a deterministic request-or-escalate repair decision
for a failure trigger against an explicit attempt budget, requesting a new
bounded repair attempt while under budget and escalating when repair is
disabled or the budget is exhausted.

#### Scenario: Under budget requests a repair attempt
- **WHEN** `decide_repair` runs with a budget whose `max_attempts` is positive
  and the trigger's current attempt is below it
- **THEN** the system SHALL return a `request_repair` decision with the next
  attempt number and a `repair_task_id` of the form `<orig>-repair-<attempt>`

#### Scenario: Disabled budget escalates
- **WHEN** the budget `max_attempts` is zero or negative
- **THEN** the system SHALL return an escalate decision with reason
  `repair_disabled`

#### Scenario: Exhausted budget escalates
- **WHEN** the trigger's current attempt is at or above `max_attempts`
- **THEN** the system SHALL return an escalate decision with reason
  `repair_budget_exhausted`

### Requirement: Repair task construction and audit events
The system SHALL build a repair task from a `request_repair` decision that
carries no dependency on the failed original task and references the bounded
repair budget, and SHALL emit typed audit events for requested attempts,
completed attempts, and escalations.

#### Scenario: Repair task is independent of the failed original
- **WHEN** `build_repair_task` runs with a `request_repair` decision
- **THEN** the new pending task SHALL use the decision `repair_task_id`, inherit
  the original task's key files, priority, and repo target, and declare no
  dependencies on the failed original

#### Scenario: Requested-attempt event requires a request decision
- **WHEN** `make_repair_attempt_requested_event` is given a decision whose
  action is not `request_repair` or that lacks a `repair_task_id`
- **THEN** the system SHALL raise a `ValueError` rather than emit an event

#### Scenario: Completed-attempt event requires a terminal status
- **WHEN** `make_repair_attempt_completed_event` is given a terminal status
  other than DONE or FAILED
- **THEN** the system SHALL raise a `ValueError`

### Requirement: Rollback point creation
The system SHALL create an annotated Git tag at HEAD under the
`whilly/rollback/` namespace as a named rollback target, recording the branch,
target SHA, and creation time, and SHALL surface Git failures as a
`RollbackError`.

#### Scenario: Annotated tag created at HEAD
- **WHEN** `create_rollback_point` runs in a valid repository
- **THEN** the system SHALL create an annotated tag named
  `whilly/rollback/<branch>/<timestamp>-<sha12>` at the current HEAD
- **AND** SHALL return a `RollbackPoint` capturing the tag name, target SHA,
  branch, and creation timestamp

#### Scenario: Git command failure raises RollbackError
- **WHEN** a required Git command invoked through `GitClient.require` exits
  non-zero
- **THEN** the system SHALL raise a `RollbackError` carrying the failing argv
  and captured output

### Requirement: Refusal-first rollback preflight
The system SHALL build a structured preflight report before push, merge, or
restore operations that records blockers and warnings, blocking on a dirty
worktree for protected operations, on detached HEAD for merge and restore, and
on a protected target branch.

#### Scenario: Dirty worktree blocks a protected operation
- **WHEN** `build_preflight_report` runs for push, merge, or restore and the
  worktree has uncommitted changes
- **THEN** the report SHALL include a `dirty worktree` blocker and `ok` SHALL be
  false

#### Scenario: Missing rollback point warns
- **WHEN** no `whilly/rollback/` tag points at the current HEAD
- **THEN** the report SHALL include a `no rollback point at current HEAD`
  warning without blocking

#### Scenario: Not a git repository blocks
- **WHEN** the preflight target path is not inside a Git repository
- **THEN** the report SHALL include a `not a git repository` blocker and `ok`
  SHALL be false

### Requirement: Confirmed rollback restore
The system SHALL restore a clean worktree to a target ref via
`git reset --hard` only after the preflight passes and the caller supplies the
exact confirmation phrase, and SHALL perform no reset on a dry run.

#### Scenario: Exact confirmation phrase required
- **WHEN** `restore_to_ref` is called with a confirmation string that does not
  equal `restore <sha12> to <branch>`
- **THEN** the system SHALL raise a `RollbackError` and SHALL NOT reset the
  worktree

#### Scenario: Confirmed restore resets HEAD
- **WHEN** the preflight passes and the supplied confirmation matches the
  required phrase exactly
- **THEN** the system SHALL run `git reset --hard` to the resolved target SHA
  and return a `RestoreResult` with `reset_performed` true

#### Scenario: Dry run performs no reset
- **WHEN** `restore_to_ref` is called with `dry_run` true and a matching
  confirmation
- **THEN** the system SHALL return a `RestoreResult` with `dry_run` true and
  `reset_performed` false without modifying the worktree
