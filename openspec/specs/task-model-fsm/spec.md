## Purpose

The task-model-fsm capability defines the legal state machine for Whilly task
status. A task moves through a strict set of states governed by `TaskManager`
in `whilly/task_manager.py`: pending, in_progress, done, failed, skipped,
blocked, and human_loop. This capability governs which status values are legal,
how stale in-progress tasks are reset on startup, which terminal states exclude
tasks from re-dispatch, and how the ready-set is derived each iteration.

## Requirements

### Requirement: Legal status values
The system SHALL restrict task status to exactly the seven legal values defined
in `VALID_STATUSES`: pending, in_progress, done, failed, skipped, blocked, and
human_loop.

#### Scenario: Invalid status rejected at mark_status
- **WHEN** `TaskManager.mark_status` is called with a status value not in
  `VALID_STATUSES`
- **THEN** the system SHALL raise a `ValueError` before writing any change to
  the plan file

#### Scenario: Valid statuses accepted without error
- **WHEN** `TaskManager.mark_status` is called with any of the seven legal
  status strings
- **THEN** the system SHALL update the task status and persist the change to
  the plan file atomically

### Requirement: Startup stale reset
The system SHALL reset any task found in `in_progress` status at startup to
`pending` before any agent is dispatched.

#### Scenario: Stale in-progress task is reset
- **WHEN** the orchestrator calls `TaskManager.reset_stale_tasks` at startup
  and at least one task has status `in_progress`
- **THEN** each such task SHALL have its status set to `pending`
- **AND** the updated plan SHALL be persisted before any agent dispatch begins

#### Scenario: No stale tasks leaves plan unchanged
- **WHEN** `TaskManager.reset_stale_tasks` is called and no tasks have status
  `in_progress`
- **THEN** the plan file SHALL NOT be rewritten and the method SHALL return
  zero as the reset count

### Requirement: Terminal state exclusion from ready set
The system SHALL NOT include tasks in the candidate dispatch set once they have
reached a terminal status of done, failed, or skipped.

#### Scenario: Done task excluded from ready set
- **WHEN** `TaskManager.get_ready_tasks` is evaluated on any iteration
- **THEN** tasks with status `done` SHALL be excluded from the returned list
  regardless of their dependency state

#### Scenario: Failed task excluded from ready set
- **WHEN** `TaskManager.get_ready_tasks` is evaluated on any iteration
- **THEN** tasks with status `failed` SHALL be excluded from the returned list

#### Scenario: Skipped task excluded from ready set
- **WHEN** `TaskManager.get_ready_tasks` is evaluated on any iteration
- **THEN** tasks with status `skipped` SHALL be excluded from the returned list

### Requirement: Ready-set selection from pending only
The system SHALL include in the ready dispatch set only tasks whose status is
`pending` and whose declared dependencies all have status `done`.

#### Scenario: Pending task with satisfied dependencies is ready
- **WHEN** a task has status `pending` and every task ID listed in its
  `dependencies` field has status `done`
- **THEN** `TaskManager.get_ready_tasks` SHALL include that task in the
  returned list

#### Scenario: Pending task with unsatisfied dependencies is not ready
- **WHEN** a task has status `pending` and at least one dependency task does
  not have status `done`
- **THEN** `TaskManager.get_ready_tasks` SHALL NOT include that task in the
  returned list
