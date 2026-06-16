<!-- refreshed: 2026-06-10 -->
# Architecture

**Analysis Date:** 2026-06-10

## System Overview

Whilly is a distributed task orchestrator built on three main tiers: a Postgres-backed persistent queue, a FastAPI control plane, and remote workers communicating over HTTP. Tasks flow from external sources (GitHub Issues, Jira, JSON plans) through a decision gate, into the queue, where workers claim and execute them, with outcomes recorded in an append-only audit log.

```text
┌──────────────────────────────────────────────────────────────────┐
│                    External Task Sources                          │
│  (GitHub Issues, Jira, JSON Plans, GitHub Projects, PRD Forge)  │
│                      `whilly/sources/`                           │
└────────────────────┬─────────────────────────────────────────────┘
                     │ parse & normalize
                     ▼
┌──────────────────────────────────────────────────────────────────┐
│              Orchestrator CLI (Entry Points)                      │
│  `whilly plan import` → validate cycles → `whilly plan show`    │
│  `whilly run` (local worker) / `whilly worker` (remote worker)  │
│                      `whilly/cli/`                               │
└────────────────────┬─────────────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────────────────┐
│        Postgres-Backed Persistent Queue & State Store            │
│  tables: plans, tasks, workers, events, work_intents, ...       │
│                  `whilly/adapters/db/`                           │
└────────────────────┬─────────────────────────────────────────────┘
                     │
        ┌────────────┼────────────┐
        │            │            │
        ▼            ▼            ▼
   ┌─────────┐  ┌─────────┐  ┌──────────────┐
   │  Local  │  │  Remote │  │   FastAPI    │
   │ Worker  │  │ Workers │  │ Control Plane│
   │`local.py`│ │`remote.py`│ `api/main.py` │
   └─────────┘  └─────────┘  └──────────────┘
        │            │            │
        └────────────┼────────────┘
                     │ HTTP long-poll claim, complete, heartbeat
                     ▼
        ┌──────────────────────┐
        │   Claude CLI Runner  │
        │ (or custom backend)  │
        │ `adapters/runner/`   │
        └──────────────────────┘
        
        │
        ▼
    ┌─────────────────────────────────────┐
    │  Task Completion → Audit Events     │
    │  (append-only, immutable log)       │
    │  `events` table + JSONL mirror      │
    │  `whilly/audit/`                    │
    └─────────────────────────────────────┘
        │
        ▼
    ┌─────────────────────────────────────┐
    │  Post-Completion Sinks              │
    │  (GitHub PRs, GitLab MRs, ...)      │
    │  `whilly/sinks/`                    │
    └─────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| **Plan Import/Export** | Parses task graphs, validates cycles, persists to Postgres, emits canonical JSON | `whilly/cli/plan.py` |
| **Task Queue (Postgres)** | Maintains dependency graph, ready-task query, claim locking, version control | `whilly/adapters/db/repository.py` |
| **Local Worker** | Single process claiming from queue via Postgres, executing tasks sequentially | `whilly/worker/local.py` |
| **Remote Workers** | Long-poll claim endpoint, heartbeat loop, result posting via HTTP to control plane | `whilly/worker/remote.py` |
| **FastAPI Control Plane** | HTTP API for workers (claim, complete, fail, heartbeat); dashboard; event streaming | `whilly/api/` |
| **Agent Runner (Claude)** | Executes tasks via Claude CLI subprocess, parses structured results | `whilly/adapters/runner/claude_cli.py` |
| **Decision Gate** | Validates task viability; outputs SKIP / REJECT / ALLOW verdicts; blocks nonsense work | `whilly/core/gates.py` |
| **TRIZ Analyzer** | Analyzes task contradiction at plan level, surfaces technical conflicts | `whilly/core/triz.py` |
| **Verification Pipeline** | Runs configured verification commands (lint, test, CI polling) before marking DONE | `whilly/pipeline/verification.py` |
| **Audit Event Log** | Immutable append-only event stream; persisted to Postgres + JSONL mirror | `whilly/audit/` |
| **Post-Completion Sinks** | Creates GitHub PRs, GitLab MRs, Slack notifications after task completion | `whilly/sinks/` |

## Pattern Overview

**Overall:** Event-driven distributed orchestration with deterministic state machine, Postgres-backed row locking, long-poll HTTP handoff, and immutable audit trail.

**Key Characteristics:**
- **Pessimistic concurrency:** Tasks use Postgres `FOR UPDATE SKIP LOCKED` to ensure only one worker claims a task
- **Optimistic locking on updates:** Completions use version numbers to detect mid-flight conflicts (409 with full state tuple returned to client)
- **Append-only audit log:** `events` table is write-only; all state changes (status transitions, event logs) are immutable records with timestamps
- **Long-poll worker acquisition:** Control plane holds HTTP request open up to 30s, polling the queue every 1.5s, so workers don't spam queries
- **Deterministic prompts:** Agents receive fixed task context (description, acceptance criteria, test steps); no free-form planning
- **Parallel batch execution (optional):** Multi-task `MAX_PARALLEL` grouping respects dependency chains and key-file overlaps

## Layers

**Core (Domain):**
- Purpose: Pure business logic — no external dependencies (zero asyncpg, zero FastAPI)
- Location: `whilly/core/`
- Contains: Models (`Task`, `Plan`), decision gate, TRIZ analysis, scheduler (cycle detection)
- Depends on: Only Python stdlib
- Used by: Adapters, CLI, workers

**Adapters (I/O Boundary):**
- Purpose: Interface between core logic and external systems
- Location: `whilly/adapters/`
- Contains: Database (pool, repository), filesystem (plan I/O), transport (HTTP), runners (Claude CLI), notifications
- Depends on: Core, external libraries (asyncpg, FastAPI, httpx)
- Used by: CLI, API, workers

**CLI (User Interface):**
- Purpose: Shell command entry points for operators
- Location: `whilly/cli/`
- Contains: Subcommands (`plan`, `run`, `init`, `jira`, `worker`, `dashboard`)
- Depends on: Adapters, core
- Used by: Console script `whilly` and `whilly-worker`

**API (HTTP Control Plane):**
- Purpose: Remote worker registry and task distribution
- Location: `whilly/api/`
- Contains: Authentication (bearer + OIDC + sessions), FastAPI routes, WebSocket/SSE, dashboard templates
- Depends on: Adapters, core
- Used by: Remote workers, web browsers

**Worker (Task Executor):**
- Purpose: Claim tasks from queue, invoke runners, handle failures
- Location: `whilly/worker/`
- Contains: Local worker (single-process queue consumer), remote worker (long-poll HTTP client)
- Depends on: Adapters, core, runners
- Used by: CLI entry points

**Pipeline (Post-Execution):**
- Purpose: Verification, sinks, notifications after task completion
- Location: `whilly/pipeline/`, `whilly/sinks/`
- Contains: Verification command runners, GitHub PR creation, Slack notifications
- Depends on: Adapters, core
- Used by: Workers, event handlers

**Audit (Observability):**
- Purpose: Immutable event recording
- Location: `whilly/audit/`
- Contains: JSONL event writer, event flusher
- Depends on: Core, adapters (Postgres write)
- Used by: All layers (every state change logs an event)

## Data Flow

### Primary Request Path (Local Worker)

1. **Import** — `whilly plan import tasks.json` reads JSON, validates cycles via `detect_cycles()` (`whilly/core/scheduler.py:line 45`), batch inserts via `INSERT ... ON CONFLICT DO NOTHING` (`whilly/cli/plan.py:line 132`)
2. **Fetch Ready Tasks** — `TaskRepository.get_ready_tasks()` queries tasks where `status = 'PENDING'` and all dependencies are `DONE`, locks via `FOR UPDATE SKIP LOCKED` (`whilly/adapters/db/repository.py`)
3. **Run Local Worker** — `whilly run --plan <plan_id>` calls `run_local_worker()` which loops on ready tasks (`whilly/worker/local.py:line 120`)
4. **Claim Task** — Worker locks a task row, transitions `PENDING → CLAIMED` via optimistic version update (`whilly/adapters/db/repository.py:claim_task`)
5. **Invoke Runner** — `run_task()` spawns Claude CLI subprocess with prepared prompt, captures JSON result (`whilly/adapters/runner/claude_cli.py:line 95`)
6. **Verification** — If verification commands configured, run lint/test/CI poll before marking DONE (`whilly/pipeline/verification.py:run_verification_commands`)
7. **Complete** — `repo.complete_task()` sets `status = 'DONE'`, increments version, emits `task.done` event (`whilly/adapters/db/repository.py`)
8. **Post-Completion Sinks** — If enabled, create GitHub PR or send Slack notification (`whilly/sinks/github_pr.py:open_pr_for_task`)

**State Transitions:**
```
PENDING → CLAIMED → IN_PROGRESS → DONE | FAILED | SKIPPED
```

### Remote Worker Path

1. **Register** — Remote worker calls `POST /workers/register` with bootstrap token, receives `worker_id` + bearer token (`whilly/adapters/transport/server.py:line 180`)
2. **Long-Poll Claim** — Worker holds `POST /tasks/claim` open for 30s; control plane polls `get_ready_tasks()` every 1.5s; returns `ClaimResponse` with task payload on success or 204 No Content on timeout (`whilly/adapters/transport/server.py:line 220`)
3. **Execute** — Remote worker invokes agent (Claude, OpenCode, etc.) via local runner
4. **Complete via HTTP** — POST `/tasks/{task_id}/complete` with version + result; control plane applies optimistic lock; returns 200 or 409 Conflict with full state tuple (`whilly/adapters/transport/server.py:line 310`)

### Async Event Flushing (v4.6.1+)

- Task completions enqueue event records to a lifespan-owned queue (`whilly/api/event_flusher.py:EventFlusher`)
- Flusher batch-inserts every `min(500 events, 0.5s timeout)` via single `INSERT ... RETURNING id` statement
- Postgres trigger `whilly_notify_event()` fires on every insert, publishes to `pg_notify('whilly_events', ...)`
- `whilly-event-notify-listener` task owns dedicated `LISTEN` connection; reconnects with exponential backoff
- Per-subscriber SSE broker fans out events to `/events/stream` subscribers; slow subscribers dropped with 1015 code

### Audit Event Recording

Every significant state change (`task.pending`, `task.claimed`, `task.done`, `worker.heartbeat`, etc.) is recorded:
```
INSERT INTO events (id, plan_id, task_id, event_type, created_at, payload, detail)
VALUES (uuid, plan_id, task_id, 'task.done', now(), {...}, {...})
```
Events are immutable — never updated, only appended. Append-only log can be mirrored to JSONL file for offline analysis.

## Key Abstractions

**Task Model:**
- Purpose: Represents a unit of work with dependencies, acceptance criteria, budget constraints
- Examples: `whilly/core/models.py:Task`
- Pattern: Dataclass with validation; status enum; dependency list; priority rank

**Plan Model:**
- Purpose: Represents a directed acyclic graph (DAG) of tasks
- Examples: `whilly/core/models.py:Plan`
- Pattern: ID, name, task list, verification commands; cycle detection before persistence

**Repository Pattern:**
- Purpose: Abstraction over Postgres; decouples domain logic from SQL
- Examples: `whilly/adapters/db/repository.py:TaskRepository`
- Pattern: Async methods for each DB operation (claim, complete, fail); row-level locking; version management

**Runner Interface:**
- Purpose: Abstract agent execution backend (Claude, OpenCode, stub for testing)
- Examples: `whilly/adapters/runner/claude_cli.py:run_task()`
- Pattern: Takes `Task` + `Plan` context, returns `AgentResult` (usage, exit_code, output text)

**Decision Gate:**
- Purpose: Deterministic verdict engine for task viability
- Examples: `whilly/core/gates.py:evaluate_decision_gate()`
- Pattern: Rules-based (vague description → REJECT, missing acceptance criteria → REJECT, pass → ALLOW); results cached in events

**Sink Interface:**
- Purpose: Post-completion actions (PR creation, notifications)
- Examples: `whilly/sinks/github_pr.py:open_pr_for_task()`, `whilly/sinks/gitlab_mr.py`
- Pattern: Async function takes completed task + execution context, returns result or raises

## Entry Points

**`whilly` Console Script:**
- Location: `whilly/cli/__init__.py:main()`
- Triggers: User runs `whilly plan import | run | init | ...`
- Responsibilities: 
  - Legacy v3 flag shim (maps `--tasks`, `--headless`, `--init` to v4 subcommands)
  - Lazy imports to keep startup fast (no asyncpg/FastAPI on help text)
  - Routes to appropriate subcommand handler

**`whilly plan import` Subcommand:**
- Location: `whilly/cli/plan.py:run_plan_command()`
- Triggers: Operator loads a JSON plan file
- Responsibilities:
  - Parse JSON plan file
  - Validate cycles via `detect_cycles()`
  - Atomic batch insert (plan row + N task rows in single transaction)
  - Exit 0 on success, 1 on cycle, 2 on environment failure

**`whilly run` Subcommand:**
- Location: `whilly/cli/run.py:run_run_command()`
- Triggers: Operator starts local worker loop
- Responsibilities:
  - Open asyncpg pool
  - Create `TaskRepository`
  - Call `run_local_worker()` which loops on ready tasks
  - Register placeholder worker row (token_hash='local')
  - Emit audit events
  - Exit 0 on normal completion

**`whilly worker` Subcommand (Remote):**
- Location: `whilly/cli/worker.py:run_worker_command()`
- Triggers: Remote worker process starts
- Responsibilities:
  - Bootstrap token registration → receive bearer token
  - Long-poll `POST /tasks/claim` loop
  - Invoke agent runner
  - POST completion/failure to control plane
  - Heartbeat loop every `DEFAULT_HEARTBEAT_INTERVAL`

**FastAPI Control Plane (`whilly server`):**
- Location: `whilly/api/main.py:create_app()` + `whilly/adapters/transport/server.py`
- Triggers: Operator runs `whilly server` or container starts
- Responsibilities:
  - Serve HTTP API for remote workers
  - Host dashboard at `/` (HTMX Jinja2)
  - Stream events via `/events/stream` (SSE with `pg_notify`)
  - Expose Prometheus metrics at `/metrics`
  - Health probes at `/health`, `/health/live`, `/health/ready`

**`whilly init` (PRD Wizard):**
- Location: `whilly/cli/init.py:run_init_command()`
- Triggers: User runs `whilly init "problem statement"` or `whilly init --interactive`
- Responsibilities:
  - Interactive Claude conversation to generate PRD
  - Auto-decompose PRD → task list
  - Save `PRD-*.md` + `tasks.json`
  - Optionally start worker

## Architectural Constraints

- **Threading:** Single-threaded async event loop (asyncio). Postgres pool uses greenlets for concurrent I/O in migrations (Alembic env.py only). Workers do not spawn threads — just subprocesses (Claude CLI, verification commands).
- **Global state:** Minimal. FastAPI `app.state` holds pool + auth tokens + event flusher. CLI stores state in `.whilly_state.json` (resumable run metadata). Each worker loop is stateless (reads from queue on every iteration).
- **Circular imports:** Worker imports guard against FastAPI leak (fix-m1-whilly-worker-fastapi-leak). Remote worker only imports `whilly.core`, `whilly.adapters.transport.client`, `whilly.adapters.transport.schemas`, httpx, pydantic. Transport `__init__.py` uses `__getattr__` to defer `fastapi`/`asyncpg` imports until control-plane code accesses them (`whilly/adapters/transport/__init__.py:line 48`).
- **Database consistency:** Optimistic locking on task updates (version number in WHERE clause). Pessimistic locking on claims (SELECT ... FOR UPDATE SKIP LOCKED). Plan import is atomic (single transaction for plan row + all tasks). No cascading deletes — tasks are soft-deleted via status changes.
- **Network:** HTTP 1.1 long-poll; no WebSocket. Timeouts: claim_long_poll_timeout=30s (configurable), claim_poll_interval=1.5s. Workers auto-reconnect on network failure (exponential backoff).

## Anti-Patterns

### Unvalidated Plans Entering Queue

**What happens:** A plan with cycles or vague tasks (no acceptance criteria) is inserted into Postgres, blocking the worker loop.

**Why it's wrong:** Postgres becomes a sink for garbage data; workers waste time on unmeetable tasks; audit log fills with FAILED events.

**Do this instead:** Validate at import time via `detect_cycles()` and `evaluate_decision_gate()`. Reject before INSERT. (`whilly/cli/plan.py:line 78` calls `detect_cycles` before the pool is even opened.)

### Agent Choosing Its Own Tasks

**What happens:** Agent receives full task list and picks what to work on next.

**Why it's wrong:** Breaks dependency order; allows priority bypass; makes execution non-deterministic.

**Do this instead:** Prepare a single task prompt with only the current task's context. Worker claims via `claim_task()`, agent never sees the queue. (`whilly/adapters/runner/claude_cli.py:run_task()` receives one `Task` object, builds a focused prompt.)

### Blocking on Network I/O in Database Handler

**What happens:** A route handler (e.g., `/tasks/complete`) calls GitHub API to open a PR synchronously.

**Why it's wrong:** Blocks the asyncpg connection; pools eventually starve; slow API causes cascading timeouts.

**Do this instead:** Queue post-completion actions as audit events. Background task or webhook handler picks them up asynchronously. (`whilly/sinks/post_complete_pr_hook.py` is awaitable and called from worker; FastAPI does not block on it.)

### Persisting Plaintext Tokens

**What happens:** Bearer token stored in Postgres `workers.token_hash` column.

**Why it's wrong:** Breach exposes all worker credentials; no way to rotate without re-registering all workers.

**Do this instead:** Hash tokens via SHA-256 before storage (PRD NFR-3). Return plaintext exactly once in the registration response. Worker stores it locally (env var, config file). (`whilly/adapters/transport/server.py:line 118` hashes the token; plaintext is never persisted.)

### Mutable Audit Events

**What happens:** An operator corrects a log entry, updating an old `events` row.

**Why it's wrong:** Destroys chain of custody; dashboards show inconsistent state; post-mortems become unreliable.

**Do this instead:** `events` table is append-only (no UPDATE). Corrections append a new event. JSONL mirror is immutable. (`whilly/audit/` never updates, only inserts.)

## Error Handling

**Strategy:** Fail-fast on validation; graceful degradation on transient network errors; explicit manual recovery on data conflicts.

**Patterns:**
- **Validation errors (exit 1):** Malformed JSON, cycles, vague tasks. Operator must fix input. (`whilly/cli/plan.py:EXIT_VALIDATION_ERROR`)
- **Environment errors (exit 2):** Missing `WHILLY_DATABASE_URL`, plan_id not in DB, file not found. Operator must fix setup. (`whilly/cli/plan.py:EXIT_ENVIRONMENT_ERROR`)
- **Transient errors (retry with backoff):** Network timeouts, Postgres connection drop. Exponential backoff (5/10/20/40/60s). (`whilly/worker/remote.py` implements backoff on claim timeout.)
- **Version conflict (409 HTTP):** Remote worker posts completion, another worker already did. Control plane returns full state tuple; client must INSPECT (not auto-retry). Manual review required. (`whilly/adapters/transport/server.py:line 310`)
- **Task failure (async recovery):** Agent produces bad code. Worker marks task FAILED, emits reason event. Operator triggers rollback or manual fix. (`whilly/worker/local.py` catches exception from runner, calls `repo.fail_task()`)

## Cross-Cutting Concerns

**Logging:** Python stdlib `logging` module. Modules define `logger = logging.getLogger(__name__)`. CLI / API configure handlers at startup (file + stderr). No centralized sink; JSONL events are the source of truth for audit trail.

**Validation:** 
- Input: Pydantic models for HTTP DTOs, custom validators for task IDs, plan IDs (regex + uniqueness checks). (`whilly/core/task_id.py:validate_task_id`)
- Gate verdicts: Decision gate rules in `whilly/core/gates.py`
- Cycle detection: Tarjan's algorithm in `whilly/core/scheduler.py:detect_cycles`

**Authentication:** 
- Bootstrap token (cluster-join): Shared secret `WHILLY_BOOTSTRAP_TOKEN` (e.g., in Kubernetes Secret)
- Per-worker bearer token: SHA-256 hash of 256-bit random token (PRD NFR-3)
- OIDC (dashboard): User login via `WHILLY_OIDC_PROVIDER` + `WHILLY_OIDC_CLIENT_ID`
- Session management: HTTPOnly cookies; CSRF tokens for POST forms

**Concurrency:**
- Database: Optimistic locking (version number) on task updates; pessimistic locking (FOR UPDATE SKIP LOCKED) on claims
- Workers: No shared state. Each worker reads its own claimed task from DB; completes independently
- Event flushing: Batched async queue; single writer thread in lifespan task

**Metrics & Observability:**
- Prometheus: `whilly_claims_total`, `whilly_completes_total`, `whilly_fails_total{reason=...}`, `whilly_workers_online` (`whilly/api/metrics.py`)
- Events: Append-only `events` table + JSONL mirror. SSE stream at `/events/stream` for real-time dashboard
- Dashboard: HTMX HTML + TUI (`rich` library) for terminal operators

---

*Architecture analysis: 2026-06-10*
