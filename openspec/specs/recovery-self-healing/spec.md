## Purpose

The recovery-self-healing capability documents Whilly's legacy crash-recovery and
exception self-healing helpers: `whilly/recovery.py` (file-based task-status
reconstruction and consistency validation against the in-memory `TaskManager`,
`progress.txt`, and per-task `whilly_logs/*.log` files) and `whilly/self_healing.py`
(traceback analysis, suggested fixes, and a global `sys.excepthook` installer). Both
modules are reverse-spec'd from observed behavior and BOTH are legacy/unwired: they
have zero callers in the v4 Postgres worker-claim path. The live v4 crash-recovery
mechanism is the visibility-timeout sweep `release_stale_tasks` (with optimistic
locking) documented in the `state-persistence` capability; this spec marks the two
modules legacy in the same manner that `state-persistence` marks the v3 StateStore
legacy, and it does NOT assert progress-file recovery as the live v4 recovery path.
## Requirements
### Requirement: File-based done-status recovery
The system SHALL reconstruct completed task IDs in `recover_task_statuses` by unioning IDs parsed from `progress.txt` lines of the form `[id] DONE` with IDs from per-task `whilly_logs/*.log` files whose content contains `COMPLETION_MARKER` (preferring a valid `{"type":"result"}` JSON line with `is_error` False), SHALL flip matching non-done legacy `TaskManager` tasks to `done`, SHALL persist only when at least one status changed, and SHALL return a change dict mapping each affected task ID to `"<old> → done"`.

#### Scenario: progress and log evidence flips tasks to done
- **WHEN** `recover_task_statuses` runs and a non-done task ID appears in a `[id] DONE` progress line or in a log file containing a non-error `{"type":"result"}` completion marker
- **THEN** the system SHALL set that task's status to `done`
- **AND** SHALL return a change dict entry of the form `"<old> → done"` for it

#### Scenario: no changes leaves the plan unsaved
- **WHEN** `recover_task_statuses` finds no non-done task with completion evidence
- **THEN** the system SHALL NOT call `TaskManager.save`
- **AND** SHALL return an empty change dict

### Requirement: Task consistency validation
The system SHALL return, from `validate_task_consistency`, a warning string listing tasks that are completed per `progress.txt`/log evidence but not marked `done`, and a separate warning listing tasks marked `done` with no completion evidence in either source.

#### Scenario: completed-but-not-marked mismatch warned
- **WHEN** evidence in `progress.txt` or logs shows a task completed while the legacy task is not marked `done`
- **THEN** the system SHALL include a warning naming the sorted set of such task IDs

#### Scenario: marked-done-without-evidence mismatch warned
- **WHEN** a task is marked `done` but neither `progress.txt` nor the log files show completion evidence
- **THEN** the system SHALL include a warning naming the sorted set of such task IDs

### Requirement: Self-healing error classification
The system SHALL classify, in `SelfHealingHandler.analyze_error`, an error and traceback into a `CodeError` with a `suggested_fix` for NameError (undefined name), missing-positional-argument TypeError, ImportError or ModuleNotFoundError, and AttributeError, and SHALL return `None` when no file/line frame is parseable or no pattern matches.

#### Scenario: ImportError classified with install suggestion
- **WHEN** `analyze_error` receives a `ModuleNotFoundError`/`ImportError` naming a missing module with a parseable traceback frame
- **THEN** the system SHALL return a `CodeError` of type `ImportError` whose `suggested_fix` proposes `pip install <module>`

#### Scenario: unrecognised error yields no CodeError
- **WHEN** `analyze_error` receives an error whose message matches none of the supported patterns or whose traceback has no `File "...", line N` frame
- **THEN** the system SHALL return `None`

### Requirement: Self-healing fix application scope
The system SHALL, in `SelfHealingHandler.apply_fix`, act only on NameError (logging the suggested fix and returning `True`) and ImportError (attempting `pip install` of the module and returning the install outcome), and SHALL return `False` for every other error type.

#### Scenario: NameError fix logs and succeeds
- **WHEN** `apply_fix` receives a `CodeError` of type `NameError`
- **THEN** the system SHALL log the suggestion and return `True` without editing source

#### Scenario: unsupported type is a no-op
- **WHEN** `apply_fix` receives a `CodeError` whose type is neither `NameError` nor `ImportError`
- **THEN** the system SHALL return `False` and SHALL NOT modify any files

### Requirement: Global exception hook installation
The system SHALL, via `global_exception_handler`, run analyze-then-apply on an
uncaught exception, and SHALL print the full formatted traceback EXCEPT when
`apply_fix` succeeds — in which case it SHALL print a restart notice and return
early without printing the traceback. `enable_self_healing` SHALL install
`global_exception_handler` as `sys.excepthook`.

#### Scenario: handler prints full traceback when no fix is applied
- **WHEN** `global_exception_handler` is invoked for an uncaught exception and
  `apply_fix` returns `False` (or no `CodeError` is classified)
- **THEN** the system SHALL attempt `analyze_error` and `apply_fix`
- **AND** SHALL print the full formatted traceback afterward

#### Scenario: handler returns early after a successful auto-fix
- **WHEN** `global_exception_handler` is invoked and `apply_fix` returns `True`
- **THEN** the system SHALL print a restart notice and return early
- **AND** SHALL NOT print the full formatted traceback

### Requirement: Legacy status and live recovery reference
The system SHALL treat `whilly/recovery.py` and `whilly/self_healing.py` as legacy modules that are NOT wired into the v4 Postgres worker-claim path (zero callers), and the live stale-task recovery contract SHALL be `release_stale_tasks` (the visibility-timeout sweep with optimistic locking) defined in the `state-persistence` capability.

#### Scenario: legacy modules are not in the v4 dispatch path
- **WHEN** the v4 worker-claim dispatch path recovers from a crashed worker
- **THEN** the system SHALL rely on `release_stale_tasks` and SHALL NOT invoke `recover_task_statuses` or the self-healing excepthook

#### Scenario: live recovery emits visibility-timeout release
- **WHEN** an aged-out CLAIMED or IN_PROGRESS row is recovered in v4
- **THEN** the authoritative behavior SHALL be the `state-persistence` `release_stale_tasks` sweep that returns the row to PENDING under optimistic locking

