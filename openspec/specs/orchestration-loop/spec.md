## Purpose

The orchestration-loop capability defines the v4 worker-claim run path: the
`whilly run` composition root in `whilly/cli/run.py` (`run_run_command` →
`_async_run`) and the per-iteration loop it drives in
`whilly/worker/local.py` (`run_local_worker`). This capability governs how a
local worker is registered, how a single PENDING task is atomically claimed
and routed to a terminal outcome each iteration, how the empty-queue idle poll
behaves, the precedence of the loop's termination paths, and how a lost
optimistic-locking race is tolerated. It references the `task-model-fsm`
capability for the legal status transitions and defers budget thresholds and
verification-gate semantics to their own capabilities rather than redefining
them here.

## Requirements

### Requirement: Run-command composition root
The `_async_run` composition root MUST open an asyncpg pool against
`WHILLY_DATABASE_URL`, register the worker row idempotently, load the plan and
its tasks via `_select_plan_with_tasks`, build a workspace-aware runner, run
the worker loop, and always close the pool in a `finally` block.

#### Scenario: Pool opened, worker registered, loop run, pool closed
- **WHEN** `_async_run` is invoked with a DSN and a plan id that exists in
  Postgres
- **THEN** the system SHALL open the connection pool, register the worker via
  an idempotent `INSERT ... ON CONFLICT` into `workers`, load the plan and its
  tasks via `_select_plan_with_tasks`, and run the worker loop
- **AND** the system SHALL close the pool in the `finally` block even if the
  worker loop raises or is cancelled

#### Scenario: Missing database URL exits with environment error
- **WHEN** `run_run_command` runs and `WHILLY_DATABASE_URL` is unset
- **THEN** the system SHALL print a diagnostic to stderr and return the
  environment-error exit code `2` without opening a pool

#### Scenario: Unknown plan id exits with environment error
- **WHEN** `_async_run` loads a plan id that `_select_plan_with_tasks` reports
  as absent from Postgres
- **THEN** the system SHALL surface the missing-plan signal and
  `run_run_command` SHALL return the environment-error exit code `2`

### Requirement: Worker-claim iteration ordering
The worker loop SHALL, on each iteration, claim one PENDING task into CLAIMED
via `claim_task`, flip it CLAIMED → IN_PROGRESS via `start_task`, invoke the
runner with the prompt from `build_task_prompt`, and route the outcome to a
terminal transition.

#### Scenario: Successful task reaches DONE
- **WHEN** `claim_task` returns a task, `start_task` transitions it to
  IN_PROGRESS, and the runner returns an `AgentResult` with `is_complete` true
  and `exit_code` equal to `0`
- **THEN** the system SHALL call `complete_task` to transition the task
  IN_PROGRESS → DONE
- **AND** the system SHALL increment the completed counter for the iteration

#### Scenario: Non-completing or non-zero exit reaches FAILED
- **WHEN** the runner returns an `AgentResult` whose `is_complete` is false or
  whose `exit_code` is non-zero
- **THEN** the system SHALL call `fail_task` to transition the task
  IN_PROGRESS → FAILED with a reason carrying the exit code and a truncated
  output snippet
- **AND** the system SHALL increment the failed counter for the iteration

### Requirement: Idle-wait poll on empty queue
The worker loop SHALL, when `claim_task` returns `None`, sleep for `idle_wait`
seconds and poll again rather than terminating.

#### Scenario: Empty queue sleeps then re-polls
- **WHEN** `claim_task` returns `None` because no PENDING task is available
- **THEN** the system SHALL increment the idle-poll counter, sleep `idle_wait`
  seconds, and continue to the next iteration without dispatching a runner

### Requirement: Termination path precedence
The worker loop SHALL exit in a defined precedence: a graceful `stop` event
first, then the `max_iterations` cap, then an outer cancellation.

#### Scenario: Stop event releases in-flight task and exits
- **WHEN** the `stop` event fires while a runner call is in flight
- **THEN** the system SHALL cancel the runner, release the in-flight task back
  to PENDING via `release_task`, and exit the loop cleanly
- **AND** the system SHALL increment the released-on-shutdown counter

#### Scenario: Max iterations caps the loop
- **WHEN** `max_iterations` is set and the loop has executed that many
  iterations
- **THEN** the system SHALL stop iterating and return the accumulated
  `WorkerStats`

#### Scenario: Stop checked at iteration boundary before claiming
- **WHEN** the `stop` event is already set at the top of an iteration
- **THEN** the system SHALL break out of the loop before calling `claim_task`

### Requirement: Optimistic-locking race tolerance
The worker loop MUST treat a `VersionConflictError` raised by a repository
transition as a lost race and continue (or exit cleanly on shutdown release)
rather than crashing the loop.

#### Scenario: Lost race on start_task continues
- **WHEN** `start_task` raises `VersionConflictError` because another writer
  released or claimed the row first
- **THEN** the system SHALL log the conflict and continue to the next
  iteration without dispatching a runner

#### Scenario: Lost race on terminal transition continues
- **WHEN** `complete_task` or `fail_task` raises `VersionConflictError`
  because another writer took responsibility for the row
- **THEN** the system SHALL log the conflict and continue to the next
  iteration rather than raising

### Requirement: Workspace-aware runner seam
The composition root MUST wrap the runner so the task workspace is prepared
before each agent dispatch, and a workspace-preparation failure SHALL surface
as a task failure rather than crashing the worker.

#### Scenario: Workspace prepared before dispatch
- **WHEN** the workspace-aware runner is invoked for a claimed task
- **THEN** the system SHALL prepare the task workspace via the workspace
  resolver before invoking the underlying runner

#### Scenario: Workspace preparation failure fails the task
- **WHEN** workspace preparation raises an exception for a task
- **THEN** the system SHALL return an `AgentResult` marking the task not
  complete with a workspace-failure exit code so the loop routes it to
  `fail_task`
