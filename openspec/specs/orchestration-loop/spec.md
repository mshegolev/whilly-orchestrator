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
inbox, and write `delivered`, `rejected`, or acknowledgement receipts. The
mailbox transport MUST use one descriptor-anchored boundary for enqueue,
request draining, receipts, inbox state, proposal status, and removals.

#### Scenario: Worker sends while offline
- **WHEN** a worker writes a valid message request to its task-local outbox
- **THEN** the coordinator later validates and persists it, and the worker can
  observe a delivery receipt and refreshed scoped inbox

#### Scenario: Crash after durable delivery
- **WHEN** the coordinator commits a message and crashes before completing
  receipt handling
- **THEN** the request may be retried or redelivered; delivery is at-least-once,
  not exactly-once

#### Scenario: Special files cannot block or dispatch
- **WHEN** an outbox entry is a FIFO, symlink, malformed JSON document, or oversized request
- **THEN** the coordinator SHALL use no-follow nonblocking bounded reads and SHALL NOT invoke the service for that entry
- **AND** it SHALL write a named rejection receipt through the opened receipts descriptor when safe, while retaining the original special file for inspection and avoiding payload retry

#### Scenario: Durable delivery receipt
- **WHEN** a valid request is persisted by the service
- **THEN** the coordinator SHALL write a durable receipt through a descriptor-relative temporary file and atomic replacement
- **AND** the request SHALL be removed only after receipt publication succeeds

#### Scenario: Mailbox directory is replaced while service is awaited
- **WHEN** an outbox or receipts directory is renamed and recreated during an awaited service call
- **THEN** the coordinator SHALL continue using the descriptors opened for that attempt
- **AND** it SHALL NOT modify files in the replacement directories

#### Scenario: Mailbox path ancestors are untrusted
- **WHEN** a mailbox ancestor is a symlink or is swapped to a symlink before the mailbox is opened
- **THEN** the coordinator SHALL open or create path components one at a time with directory descriptors and no-follow checks
- **AND** it SHALL reject the mailbox without modifying the symlink target

#### Scenario: Inbox and status state are bounded
- **WHEN** inbox or proposal status state is read
- **THEN** the adapter SHALL read only regular files through the opened mailbox root descriptor
- **AND** a state document exceeding 4 MiB SHALL raise an explicit error rather than being truncated

#### Scenario: Directory scan and backlog are bounded
- **WHEN** the coordinator scans an outbox containing valid and invalid names
- **THEN** it SHALL inspect at most 100 directory entries per tick, count all inspected entries for the enqueue backlog cap, and avoid materializing or sorting the entire directory

#### Scenario: Atomic write failure cleans temporary state
- **WHEN** an exclusive mailbox temporary file fails during write or fsync
- **THEN** the coordinator SHALL close and remove that temporary file before propagating the error

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

### Requirement: BMAD specification artifacts remain host controlled
The system SHALL keep planners read-only and persist validated BMAD specification artifacts in private revision workspaces through the host executor.

#### Scenario: Script-dependent skill without host executor
- **WHEN** a configured skill requires helper scripts but host execution is unavailable
- **THEN** planning stops with a named setup blocker before invoking a model

#### Scenario: Artifact escapes or failed preservation
- **WHEN** a returned artifact escapes its workspace or reports failed preservation
- **THEN** the specification cannot become approvable

### Requirement: Product features bind execution approval
The system SHALL bind approval to a server-computed digest of the feature specification, plan revision, registry hash, base SHAs, execution profiles and budget.

#### Scenario: Approved metadata changes
- **WHEN** specification, budget, registry or base commit changes
- **THEN** execution is blocked pending renewed planning and approval

### Requirement: Model roles and global capacity are explicit
The system SHALL use configured strong profiles for planning and cheap profiles for implementation and independent review, with at most five admitted model calls globally.

#### Scenario: Missing profile or exhausted capacity
- **WHEN** a required profile is missing or five calls are active
- **THEN** the call is blocked with a named reason and no fallback model is invoked

### Requirement: Publication requires trusted evidence
The system SHALL restrict publication to explicitly allowlisted destinations and SHALL NOT merge or deploy changes.

#### Scenario: Pipeline evidence absent
- **WHEN** exact-SHA successful CI evidence is absent
- **THEN** publication cannot be reported ready

### Requirement: Test database cleanup is explicitly requested
The test harness SHALL NOT automatically delete labelled test database containers at session startup without explicit operator opt-in, and SHALL NOT perform this cleanup from xdist workers.

#### Scenario: Concurrent test sessions start
- **WHEN** a test session starts without explicit cleanup opt-in
- **THEN** existing test database containers remain untouched by startup cleanup

### Requirement: Optional subscription monetary limit
Execution profiles SHALL accept an absent or null monetary limit while retaining
explicit model selection, runtime timeout and feature/global call admission.
An explicit monetary limit SHALL be positive and finite. Unknown costs SHALL
remain unknown, and unsupported provider caps SHALL not be described as enforced.

#### Scenario: Subscription CLI profile
- **WHEN** the operator configures an explicit model without budget_usd
- **THEN** profile validation succeeds without inventing a dollar amount
- **AND** runtime and call admission limits still apply

#### Scenario: Invalid explicit monetary limit
- **WHEN** budget_usd is zero, negative, nonfinite, boolean or a string
- **THEN** validation rejects the profile before execution

#### Scenario: Invalid agent engine selection
- **WHEN** the configured planner or reviewer engine is not a string naming a configured engine
- **THEN** registry validation rejects it before runtime lookup

### Requirement: Explicit complete product publication policies
The system SHALL require a policy for every registered project when the explicit product registry loader is requested, while legacy registry loading remains valid without product policies.

#### Scenario: Missing policy or remote in one project
- **WHEN** any declared project lacks required publication policy fields
- **THEN** the whole product snapshot is blocked rather than silently omitting that project

#### Scenario: Thirteen project inventory
- **WHEN** thirteen valid projects form a dependency graph
- **THEN** all thirteen policies and their dependencies remain in the immutable snapshot

### Requirement: Strict inert product policy declarations
The system SHALL validate canonical remote and unique GitLab identities, protected-target and branch-prefix declarations, bounded local argv and CI checks, ownership and relative allowed paths, artifact digest rules, stage/prod delivery observations, manual-decision boundaries and revert-MR compensation without executing commands or contacting remotes.

#### Scenario: Invalid graph or untrusted literal
- **WHEN** dependencies contain cycles or unknown projects, identities conflict, JSON keys repeat or a secret-looking literal is present
- **THEN** parsing fails with a bounded error without echoing registry paths or credentials

#### Scenario: Declarative protection
- **WHEN** a target is declared protected in a valid registry
- **THEN** the snapshot records that policy requirement without claiming it was observed remotely
- **AND** prod delivery remains conditional on release approval and compensation never declares history rewriting

### Requirement: Canonical product policy approval binding
The system SHALL expose separate canonical policy and complete registry bytes and digests, bind both to approval input, and reject a changed snapshot after verification before it can be used for a later external effect.

#### Scenario: Policy or profile changes after verification
- **WHEN** remote, target, checks, dependencies, artifact, delivery, compensation or full-registry profile content changes
- **THEN** the registry binding and approval digest change and snapshot revalidation blocks continuation

#### Scenario: Formatting-only change
- **WHEN** JSON key order or whitespace changes without changing resolved content
- **THEN** canonical digests remain equal
- **AND** returned dictionaries cannot mutate the frozen snapshot

### Requirement: Product change-set definitions retain complete registry scope

The system SHALL create an immutable product change-set definition with a stable
identity, goal, acceptance criteria, canonical registry digest, every registered
repository baseline and an acyclic dependency graph; execution SHALL require a
recorded approval digest and repository progress SHALL remain within the current
product phase.

#### Scenario: Registered repository omitted or dependency unknown

- **WHEN** baselines omit a registered repository or the graph contains an unknown
  dependency or cycle
- **THEN** creation is rejected before any state or external effect is persisted

#### Scenario: Non-impacted repository identified

- **WHEN** impact evidence excludes a planned repository
- **THEN** its explicit NOT_IMPACTED record is retained instead of silently dropping
  it from the immutable definition

### Requirement: Product change-set outcomes preserve acceptance and compensation boundaries

The system SHALL allow only named product/repository state transitions, preserve
the recorded resume boundary of blocked decisions, prevent generic failure after
an observed merge, and permit DONE only from stage acceptance with passed stage
evidence and artifact-ready mandatory parts; rollback completion SHALL require
observed reverts without unresolved merged parts.

#### Scenario: Local success offered as product completion

- **WHEN** a caller offers local verification, unavailable acceptance or unfinished
  required artifacts as completion evidence
- **THEN** DONE is rejected and prior state/evidence remains unchanged

#### Scenario: Mandatory stage probe fails after merge

- **WHEN** a failed stage observation initiates compensation
- **THEN** ROLLING_BACK can retain that failed observation without claiming success
- **AND** incomplete compensation remains ROLLBACK_FAILED rather than FAILED or DONE

### Requirement: Product merge opens only behind one exact-SHA barrier

The system SHALL revalidate the immutable approval and registry binding, current
merge-request identity, protected target SHA, latest exact-source-SHA pipeline,
and every required CI job for every mandatory repository before permitting the
first merge. The resulting immutable barrier receipt SHALL preserve repositories
in dependency order and SHALL NOT treat an older pipeline or partial repository
set as product readiness.

#### Scenario: One mandatory repository is stale or incomplete

- **WHEN** any mandatory repository is missing, its target SHA changed, its merge
  request no longer names the approved source, or a required job is not green for
  that exact source SHA
- **THEN** the product barrier remains closed and no merge effect is authorized

#### Scenario: All repositories retain the approved identity

- **WHEN** every mandatory repository passes all barrier checks in one verification
- **THEN** the system produces an immutable dependency-ordered barrier receipt
- **AND** the receipt alone does not merge or deploy any repository

### Requirement: Product merge is sequential and compensates observed partial effects

The system SHALL merge mandatory repositories in dependency order, re-read each
protected target immediately before its merge effect, and persist an idempotent
effect receipt for every merge. A failure after any observed merge SHALL enter
PARTIAL_MERGE and compensate merged repositories in reverse order through
receipt-backed revert merge requests; it SHALL NOT force-push or directly update
a protected target.

#### Scenario: Target changes after an earlier repository merged

- **WHEN** a later repository target no longer equals the barrier target SHA
- **THEN** that repository is not merged
- **AND** already merged repositories enter reverse-order revert compensation

#### Scenario: Compensation cannot be observed to completion

- **WHEN** a revert effect is unavailable or its response identity is invalid
- **THEN** the repository and product retain ROLLBACK_FAILED with named evidence
- **AND** a restart does not repeat any merge or completed revert effect

#### Scenario: Mutation route is outside the allowlist

- **WHEN** a caller attempts force-push, branch update, delete, or an unrelated
  GitLab mutation through the guarded transport
- **THEN** the request is rejected before credentials or network are used
