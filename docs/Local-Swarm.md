# Local Swarm Runtime

`whilly swarm` is an opt-in CLI for planning and executing bounded work across
registered local Git projects. It stores conversation, plan revisions, task
attempts, messages, and engine-call accounting in PostgreSQL.

Discussion does not execute work. A plan must be proposed, explicitly applied
by `run`, and then executed by the coordinator. A review approval is required
for completion; there is no automatic acceptance or automatic merge.

## Commands

There are 13 top-level commands. `registry validate` is the one nested command,
so the command family has 14 executable leaves.

```text
whilly swarm registry validate --registry FILE [--json]
whilly swarm new --registry FILE [--title TEXT]
whilly swarm chat (--session ID | --registry FILE) [-m TEXT] [--plan]
whilly swarm propose --session ID --plan-file FILE
whilly swarm run --session ID [--revision N | --latest] [--workers N] [--kill-orphans]
whilly swarm resume --session ID [--workers N] [--kill-orphans]
whilly swarm rerun --session ID --task TASK_ID
whilly swarm status --session ID [--json]
whilly swarm report --session ID [--json]
whilly swarm sessions [--json]
whilly swarm stop --session ID [--kill]
whilly swarm message --session ID --from SENDER --to RECIPIENT [--task TASK_ID] TEXT
whilly swarm inbox --session ID --recipient NAME [--all] [--ack ID ...] [--json]
```

Typical flow:

```bash
whilly swarm registry validate --registry registry.json
whilly swarm new --registry registry.json --title "Change set"
whilly swarm chat --session SESSION -m "Prepare a plan" --plan
whilly swarm run --session SESSION --latest --workers 2
whilly swarm report --session SESSION
```

`run --latest` applies the newest proposed revision. `resume` executes the
already applied revision. `rerun` is an explicit retry for a failed task.
`stop` requests a graceful stop; `--kill` requests cleanup when no coordinator
is live, subject to fail-closed orphan checks. `--workers` is bounded by the
registry's parallel-worker cap. Exit codes are 0 for success, 1 for execution
failure, 2 for usage or validation errors, and 4 for a coordinator/revision
conflict.

## Registry

The registry is a local JSON document. It names projects, roles, limits, agent
defaults, and configured engines. Paths and credentials are local operator
configuration and should not be published in shared documentation.

```json
{
  "version": 1,
  "name": "example",
  "projects": {
    "service": {
      "path": "/work/service",
      "purpose": "Application service",
      "base_ref": "main",
      "verification": [["pytest", "-q"]]
    }
  },
  "roles": {
    "implementer": {
      "purpose": "Implement the requested change",
      "projects": ["service"],
      "engine": "codex"
    }
  },
  "engines": {
    "claude": {"executable": ["claude"], "model": "claude-haiku"},
    "codex": {"executable": ["codex"], "model": "gpt-5"}
  },
  "agent": {
    "planner_engine": "claude",
    "review_engine": "claude"
  }
}
```

The supported engines are Claude and Codex. `whilly/swarm/engines.py` owns
engine-specific argv and output parsing. Read-only planner/reviewer modes use
engine-specific restrictions; Codex network access remains disabled. Codex
workers may edit only their task worktree and task mailbox. They do not write
Git metadata, the repository checkout, or review state. This is not a sandbox
relaxation.

For a worker task, engine selection is:

1. the task's optional `engine` field;
2. the selected role's optional `engine`;
3. the registry's planner default.

Review uses the registry review-engine setting. The plan `engine` override must
name a configured engine. An executable override, when configured, changes the
selected executable; it does not bypass validation or accounting.

## Plans and roles

Plans are JSON objects with `summary` and `tasks`. Each task has an `id`,
`project`, `role`, `description`, optional `engine`, dependencies, acceptance
criteria, and optional task-level verification argv. The project and role must
exist, the role must be registered for that project, and dependencies must be
acyclic.

Task-level values are durable in the applied revision. A later registry change
does not silently change an applied task's project, role, base SHA, or
verification context.

Invalid plans remain in revision history and create no queue tasks. Applying a
revision is transactional: task rows, task context, dependency gates,
registry snapshot, and revision state commit together or not at all.

## Execution and evidence

One coordinator lease controls a session. It claims only session-tagged tasks
and removes the waiting tag after every dependency is `DONE`. Each attempt has
a fresh worktree and branch based on the recorded execution base. Claude
workers may commit their own changes. Codex workers do not commit: the trusted
coordinator uses `gitops.commit_task_changes` after checking the expected swarm
branch and literal changed-file names, runs normal Git hooks, then verifies and
reviews the resulting commit before `DONE`.

The worker returns structured output, the coordinator checks the worktree and
runs the concatenation of project-level verification commands followed by
task-level verification commands, then a separate read-only reviewer
examines the diff, result, acceptance criteria, and verification evidence.
Only an approved review can be accepted. Acceptance is durable and atomic:
attempt result, verification, review, head SHA, call evidence, spend, and the
task's `DONE` transition are committed through the repository completion
transaction. A partial accepted result must not be visible as `DONE`.

Engine calls are reserved before launch in `swarm_agent_calls`. Reservations
record session, engine, mode, task, and prompt size. Usage and cost are filled
when the call ends. Costs are nullable because a failed, interrupted, or
engine-invalid call may have no trustworthy usage value; unknown cost must not
be rewritten as zero.

## Dependencies and same-repository work

An accepted dependency contributes a durable result packet and its commit head.
For a dependent task in the same project, the coordinator uses the sole
accepted predecessor head as the worktree base. It does not silently merge
multiple branches. If more than one same-project predecessor has a different
accepted head, the task fails closed with `integration_required`; an explicit
integration step is required before execution can proceed.

Dependencies in different projects provide result packets and metadata; they
do not alter the dependent project's Git base.

Peer messages are addressed to participants in the applied revision. Inbox
reads and acknowledgements are session- and recipient-scoped. Messages are
durable and delivered at task start; a message does not interrupt a running
agent.

### Worker mailbox

When `WHILLY_SWARM_MAILBOX` is set, the worker CLI's `message` and `inbox`
commands use task-local JSON IPC and do not require PostgreSQL credentials or
network access. A worker writes bounded requests to its local `outbox` and
reads its scoped `inbox.json`.

The coordinator drains the outbox, pins the sender to the task identity, checks
the session and recipient through the durable store, and refreshes the scoped
inbox. Each request receives a `delivered`, `rejected`, or `acknowledged`
receipt. A crash between database commit and receipt handling can cause
redelivery; this is at-least-once delivery, not exactly-once.

## Recovery and safety

Lease expiry does not prove that an agent is safe to kill. Recovery refuses
live process groups, remote attempts, and running attempts with no recorded
PGID: NULL can mean a crash between OS spawn and database persistence, not
proof that nothing is running. `--kill-orphans` never overrides these checks.
An operator must verify uncertain children have stopped before reconciling
the attempt. The launcher terminates and reaps children if its PGID callback
fails. Normal cancellation records a cancelled attempt and supports resume.

Claims and attempts are reconciled with optimistic versions. A stale worker
cannot overwrite a newer task state. Failed tasks remain failed until an
explicit `rerun` or a new plan revision. Worktrees, branches, and logs are
retained for inspection; nothing is pushed or merged automatically.

## Persistence

Migration 029 adds session, revision, message, task-context, and attempt
metadata. Migration 030 adds durable engine-call reservations and makes attempt
cost nullable. Existing queue tables remain the scheduling source of truth.

The public report exposes task state, accepted/failed summaries, attempts,
verification, review, worktree, branch, and cost when known. It does not claim
that code was merged, pushed, deployed, or automatically accepted.

## Verification checklist

- Validate the registry before creating a session.
- Confirm the intended revision is `proposed` before `run`.
- Inspect `status` and `report` after execution.
- Treat `FAILED`, `ABANDONED`, and `integration_required` as requiring operator
  action.
- Inspect retained worktrees and review evidence before any manual integration.
- Main PostgreSQL atomic/budget checks currently pass (4/4); the first four
  runtime checks pass while the remaining runtime checks are still validating.
- The current canary contract has a Haiku-produced task accepted by Codex; the
  first Codex consumer attempt was blocked by `.git` metadata/network access,
  is being fixed and rerun, and is not yet a live-passed result.
- Run the repository's OpenSpec and remaining integration checks before calling
  the runtime production-ready.

## Admin permission boundary

The integrated admin UI/server path resolves the authenticated user by the
canonical session email, including the seeded `admin@whilly.local` identity,
then applies the existing admin-role guard. Missing or non-unique identity
fails closed; this fix does not bypass or broaden permissions. BrowserQA found
the regression and the three added red unit tests drove the correction;
17 router tests and 22 transport tests passed.

## Non-goals

- automatic push, merge, deployment, or production mutation;
- unattended acceptance without verification and independent review;
- OS isolation beyond the selected engine's process/sandbox controls;
- cross-host execution of one coordinator session;
- implicit integration of divergent same-project predecessor branches.
