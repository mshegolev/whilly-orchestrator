# Whilly v4.0 Architecture

> **TL;DR.** v4 is a Hexagonal (Ports & Adapters) Python service. The pure
> domain layer (`whilly/core/`) knows nothing about Postgres, HTTP, or
> Claude — it's a state-machine + DAG scheduler + prompt builder, no I/O.
> Everything that touches the outside world lives in `whilly/adapters/`,
> driven by composition roots in `whilly/cli/` and `whilly/worker/`. The
> shape is enforced statically by `.importlinter` and verified at runtime
> by Postgres-backed integration tests.

## Layout

```
whilly/
├── core/                       # Pure domain. Zero external deps.
│   ├── models.py              # Task, Plan, TaskStatus, WorkerId, Priority
│   ├── state_machine.py       # apply_transition (Transition × TaskStatus → TaskStatus)
│   ├── scheduler.py           # topological_sort, detect_cycles, next_ready
│   └── prompts.py             # build_task_prompt — pure string templating
│
├── adapters/                   # I/O. One sub-package per outside system.
│   ├── db/                    # asyncpg + Alembic
│   │   ├── pool.py            # create_pool / close_pool
│   │   ├── repository.py      # TaskRepository — claim/start/complete/fail/release
│   │   ├── schema.sql         # canonical schema (mirrors latest migration)
│   │   └── migrations/        # Alembic — env.py + versions/*.py
│   ├── transport/             # FastAPI + httpx
│   │   ├── server.py          # create_app — FastAPI factory
│   │   ├── client.py          # RemoteWorkerClient — httpx wrapper
│   │   ├── auth.py            # bearer_dep / bootstrap_dep
│   │   └── schemas.py         # pydantic wire DTOs
│   ├── runner/                # subprocess agent invocation
│   │   ├── claude_cli.py      # asyncio.create_subprocess_exec wrapper
│   │   └── result_parser.py   # parse_output → AgentResult
│   └── filesystem/
│       └── plan_io.py         # JSON ↔ Plan/Task round-trip (import/export)
│
├── cli/                        # Composition roots — argv → adapter wiring
│   ├── plan.py                # `whilly plan import|export|show`
│   ├── run.py                 # `whilly run` — local worker
│   ├── worker.py              # `whilly-worker` — remote worker (separate script)
│   └── dashboard.py           # `whilly dashboard` — Rich Live TUI
│
├── worker/                     # Async loops (claim → run → complete | fail)
│   ├── local.py               # local worker — talks asyncpg directly
│   ├── main.py                # local-worker heartbeat composition root
│   └── remote.py              # remote worker — talks RemoteWorkerClient
│
└── cli_legacy.py              # v3 CLI — kept for one release cycle, unused on v4 paths
```

## The dependency rule

```
        cli/  ─────────────►  worker/  ─────►  adapters/  ─────►  core/
        (composition)         (loops)          (I/O)              (pure)
```

* **Outer layers depend on inner layers, never the reverse.** `core` cannot
  import anything from `adapters`, `worker`, or `cli`. `adapters` cannot
  import from `worker` / `cli`. `worker` cannot import from `cli`.
* **`core` is dependency-free at runtime.** Standard library only — no
  asyncpg, no httpx, no fastapi, no subprocess, no asyncio (well, see
  caveat below). The `.importlinter` `core-purity` contract enforces:

```ini
[importlinter:contract:core-purity]
name = whilly.core must not import I/O or transport modules
type = forbidden
source_modules = whilly.core
forbidden_modules =
    asyncpg
    httpx
    subprocess
    fastapi
    uvicorn
    alembic
include_external_packages = True
```

CI runs `lint-imports` and a belt-and-suspenders grep for `os.chdir` /
`os.getcwd` (TASK-029) — both pass on every commit to `feat/v4-rewrite`.

* **`asyncio` caveat**: `whilly.core.scheduler.next_ready` is sync (just
  graph traversal); `whilly.core.state_machine.apply_transition` is sync.
  Nothing in core touches an event loop.

## Data flow — local worker shape

```
whilly run --plan <id>
    │
    ▼
whilly/cli/run.py::run_run_command
    ├── opens asyncpg pool (whilly.adapters.db.pool.create_pool)
    ├── INSERT into workers (registers self via repo.register_worker)
    └── invokes whilly/worker/main.py::run_worker
              │
              ▼
        whilly/worker/local.py::run_local_worker
              │
              ▼  (one iteration)
        ┌─────────────────────────────────────────────────────────────────┐
        │ claim_task(worker_id, plan_id)  → tasks.status='CLAIMED'        │
        │   ↓                                                             │
        │ start_task(task.id, version)    → tasks.status='IN_PROGRESS'    │
        │   ↓                                                             │
        │ run_task (whilly.adapters.runner.claude_cli)                    │
        │   ↓ (asyncio.create_subprocess_exec → CLAUDE_BIN)               │
        │   ↓ parse_output → AgentResult(is_complete, exit_code)          │
        │   ↓                                                             │
        │ complete_task(task.id, version) → tasks.status='DONE'           │
        │   OR                                                            │
        │ fail_task(task.id, version)     → tasks.status='FAILED'         │
        └─────────────────────────────────────────────────────────────────┘
```

## Data flow — remote worker shape (SC-3)

```
whilly-worker --connect URL --token X --plan <id>
    │
    ▼
whilly/cli/worker.py::main
    └── invokes whilly/worker/remote.py::run_remote_worker_with_heartbeat
              │
              ▼  (one iteration over httpx)
        ┌─────────────────────────────────────────────────────────────────┐
        │ POST /tasks/claim          → 200 ClaimResponse | 204            │
        │   ↓ (server-side long-poll loop)                                │
        │ run_task (same runner as local — CLAUDE_BIN subprocess)         │
        │   ↓                                                             │
        │ POST /tasks/{id}/complete  → 200 OK | 409 VersionConflict       │
        │   OR                                                            │
        │ POST /tasks/{id}/fail      → 200 OK | 409 VersionConflict       │
        └─────────────────────────────────────────────────────────────────┘
```

The remote worker **never visits IN_PROGRESS** — the HTTP transport doesn't
expose `/tasks/{id}/start`, and forcing a no-op start RPC just to satisfy a
write-only filter would buy nothing observable. The state machine reflects
this: `(COMPLETE, CLAIMED) → DONE` is a valid edge alongside the
`(COMPLETE, IN_PROGRESS) → DONE` edge used by the local worker.

## Concurrency primitives

* **Optimistic locking** (PRD FR-2.4). Every state-mutating SQL filters by
  `version`; the UPDATE either matches one row (success, version
  incremented) or zero (conflict, raise `VersionConflictError`). No
  `SELECT FOR UPDATE`, no row-level locks held across writes — works
  trivially under HTTP concurrency, doesn't deadlock the audit-event
  insert that fires in the same transaction.
* **`SKIP LOCKED` in `claim_task`** (PRD FR-1.3). Multiple workers can
  hammer `claim_task` simultaneously; Postgres routes each to a different
  PENDING row without contention — proven by
  `tests/integration/test_concurrent_claims.py` (100 concurrent claimers,
  zero double-assignments).
* **Visibility-timeout sweep** (TASK-025a). FastAPI lifespan starts a
  background task that flips claimed-but-stale rows back to PENDING after
  `WHILLY_VISIBILITY_TIMEOUT` seconds. Mirrors SQS / RabbitMQ semantics —
  a SIGKILL'd worker's task is recoverable without operator intervention.
* **Heartbeat-driven offline detection** (TASK-025b). Workers POST
  `/workers/{id}/heartbeat` every 30s; a separate sweep flips
  `workers.status='offline'` after 2× the heartbeat interval and releases
  the worker's in-flight tasks. End-to-end gated by
  `tests/integration/test_phase6_resilience.py`.

## Audit log

Every state transition writes an `events` row in the same transaction as
the `tasks` UPDATE. Schema:

| Column     | Type        | Meaning                                       |
|------------|-------------|-----------------------------------------------|
| id         | BIGSERIAL   | Monotonic — ORDER BY id is the canonical sort |
| task_id    | TEXT        | FK to tasks.id (CASCADE on delete)            |
| event_type | TEXT        | CLAIM / START / COMPLETE / FAIL / RELEASE     |
| payload    | JSONB       | worker_id, version, error message, etc.       |
| created_at | TIMESTAMPTZ | NOW() at INSERT                               |

The dashboard reads this table via a single SELECT — same projection
every consumer uses, no view-side denormalization.

## Why this shape

* **Hexagonal lets us test core without booting Postgres.** The 87-case
  state-machine truth table (`tests/unit/test_state_machine.py`) and the
  31-case scheduler suite run in <100ms total. They'd be impossible to
  write in <100ms if they had to spin up testcontainers.
* **Adapter swap is trivial.** Want SQLite for a developer's laptop?
  Swap `whilly.adapters.db` for an SQLite version implementing the same
  `TaskRepository` shape — `worker/local.py` and `cli/run.py` don't
  notice. Same story for the runner (the `AgentResult` shape is the
  port; CLAUDE_BIN is one adapter, an LLM SDK could be another).
* **The boundary is a contract, not a guideline.** `lint-imports` runs
  in CI; a regression where someone imports asyncpg from
  `whilly/core/scheduler.py` for "just one query" fails the PR. The
  hexagonal split survives churn because violating it is mechanically
  impossible.

## Pointers

* PRD: [`docs/PRD-refactoring-1.md`](PRD-refactoring-1.md)
* Migration from v3: [`docs/Whilly-v4-Migration-from-v3.md`](Whilly-v4-Migration-from-v3.md)
* Worker HTTP protocol: [`docs/Whilly-v4-Worker-Protocol.md`](Whilly-v4-Worker-Protocol.md)
* Release checklist: [`docs/v4.0-release-checklist.md`](v4.0-release-checklist.md)
