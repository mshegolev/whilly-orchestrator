## ADDED Requirements

### Requirement: Dual-engine task dispatch

The runtime MUST support Claude and Codex engine adapters with engine-specific
argv construction and structured output parsing. A task-level engine override
MUST take precedence over its role engine, which MUST take precedence over the
registry default. Unknown engines MUST fail validation.

#### Scenario: Task override

- **WHEN** a task selects a configured engine
- **THEN** the worker uses that engine while the task's role and registry remain
  unchanged

### Requirement: Durable call accounting

The runtime MUST reserve each planner, worker, and reviewer call before launch.
The reservation MUST remain durable if the process fails, and usage/cost MAY be
unknown. Unknown cost MUST remain null rather than being reported as zero.

#### Scenario: Engine process fails before usage

- **WHEN** an engine exits without a trustworthy usage envelope
- **THEN** its call reservation records the error and nullable cost

### Requirement: Task-local mailbox IPC

The task-local mailbox contract MUST be available to offline workers.
When `WHILLY_SWARM_MAILBOX` is set, worker `message` and `inbox` operations
MUST use task-local JSON IPC without PostgreSQL credentials or network access.
The coordinator MUST drain requests, pin the sender to the task identity,
validate session and recipient through the durable store, refresh the scoped
inbox, and write `delivered`, `rejected`, or acknowledgement receipts.

#### Scenario: Worker sends while offline

- **WHEN** a worker writes a valid message request to its task-local outbox
- **THEN** the coordinator later validates and persists it, and the worker can
  observe a delivery receipt and refreshed scoped inbox

#### Scenario: Crash after durable delivery

- **WHEN** the coordinator commits a message and crashes before completing
  receipt handling
- **THEN** the request may be retried or redelivered; delivery is at-least-once,
  not exactly-once

### Requirement: Codex sandbox boundary

Codex network access MUST remain disabled. A Codex worker MUST be able to edit
only the task worktree and task-local mailbox. It MUST NOT write Git metadata,
the repository checkout outside that worktree, or review state; review MUST
remain read-only. No sandbox relaxation is part of this contract.

#### Scenario: Codex consumer cannot use network or broad checkout writes

- **WHEN** a Codex worker attempts network access or writes outside the allowed
  worktree/mailbox set
- **THEN** the attempt is blocked and the canary is not reported as passed

### Requirement: Coordinator-owned Codex commit

For Codex tasks, the trusted coordinator MUST commit worker changes after the
worker exits. It MUST guard the expected swarm branch, stage literal named
files, use normal Git hooks, and verify the resulting head before independent
verification and review. Claude workers MAY commit their own changes.

#### Scenario: Codex worker leaves edits without Git metadata access

- **WHEN** a Codex worker edits its worktree and mailbox but cannot commit
- **THEN** the coordinator creates the guarded task commit and only then runs
  verification and review before allowing `DONE`

### Requirement: Atomic acceptance

The runtime MUST commit accepted attempt evidence and the task `DONE` transition
in one repository transaction through the completion callback. It MUST NOT
expose `DONE` while result, verification, review, and accepted-attempt evidence
are missing.

#### Scenario: Completion callback fails

- **WHEN** persistence of accepted evidence fails
- **THEN** the task transition and completion event roll back together

### Requirement: Fail-closed recovery and repository integration

Recovery MUST refuse ambiguous orphan process identities rather than promising
to kill a possibly reused process group. A same-project dependent task MUST use
its sole accepted predecessor head as its base. Multiple divergent accepted
predecessor heads MUST fail with `integration_required` and MUST NOT be
implicitly merged.

#### Scenario: Divergent same-project predecessors

- **WHEN** two accepted predecessors have different heads in one project
- **THEN** the dependent task remains unexecuted and reports `integration_required`

#### Scenario: Crash before launch identity is persisted

- **WHEN** a running attempt has no recorded process identity
- **THEN** recovery refuses to release it automatically, and a launcher callback
  failure terminates and reaps any process group already created

### Requirement: Combined verification

The coordinator MUST execute project-level verification commands followed by
task-level verification commands. Task-level verification MUST add to the
project contract; it MUST NOT replace it or be treated as an inheritance-only
alternative.

#### Scenario: Project and task checks are configured

- **WHEN** both levels provide verification commands
- **THEN** the runtime executes the project command list followed by the task
  command list
