# Operator UI Parity And Global Pause Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make WUI and TUI expose the same operator commands, labels, hotkeys, and backend behavior, with `Pause`/`Resume` controlling workers globally rather than freezing UI refresh.

**Architecture:** Add a DB-backed singleton control state and expose it through repository methods plus admin HTTP endpoints. Worker claim/execution paths read that state at safe boundaries. WUI and TUI both consume the same command contract: `p` pauses workers, `R` resumes workers, `r` refreshes the view, and review decisions use the same payload semantics.

**Tech Stack:** Python 3.12, asyncpg, Alembic, FastAPI, Pydantic v2, Jinja2/HTMX/SSE, Rich TUI, pytest/pytest-asyncio.

---

## Files

- Create: `whilly/adapters/db/migrations/versions/014_control_state.py`
- Modify: `whilly/adapters/db/schema.sql`
- Modify: `whilly/adapters/db/repository.py`
- Modify: `whilly/adapters/transport/schemas.py`
- Modify: `whilly/adapters/transport/server.py`
- Modify: `whilly/adapters/transport/client.py`
- Modify: `whilly/operator_views.py`
- Modify: `whilly/api/dashboard.py`
- Modify: `whilly/api/templates/index.html.j2`
- Modify: `whilly/cli/tui.py`
- Modify: `whilly/worker/local.py`
- Modify: `whilly/worker/remote.py`
- Test: `tests/unit/test_control_state_repository.py`
- Test: `tests/unit/test_tui.py`
- Test: `tests/unit/test_remote_worker.py`
- Test: `tests/unit/test_local_worker.py`
- Test: `tests/unit/test_transport_schemas.py`
- Test: `tests/integration/test_control_state_admin_api.py`
- Test: `tests/integration/test_htmx_dashboard.py`
- Test: `tests/integration/test_alembic_full_chain.py`

## Contract

| Command | WUI | TUI | Backend meaning |
| --- | --- | --- | --- |
| `p` | Pause button and hotkey | Pause command | Set global worker pause. UI keeps refreshing. |
| `R` | Resume button and hotkey | Resume command | Clear global worker pause. |
| `r` | Refresh button/hotkey | Refresh command | Refresh the operator view only. |
| `q` | Stop live page updates | Exit TUI | Leave the operator session. Does not pause workers. |
| `1-5` | Switch tabs | Switch surfaces | Same surface order. |
| `/` | Focus filter input | Enter filter mode | Filter rows on current view data. |
| `j/k` | Select review gap on Compliance only | Select review gap on Compliance only | Move selected actionable review row. |
| `a/x/c` | Approve/reject/request changes on Compliance only | Same | Record same decision payload shape. |

## Task 1: Control-State Data Model

**Files:**
- Create: `whilly/adapters/db/migrations/versions/014_control_state.py`
- Modify: `whilly/adapters/db/schema.sql`
- Test: `tests/integration/test_alembic_full_chain.py`

- [ ] **Step 1: Add failing migration/full-chain expectation**

Add assertions that the upgraded schema contains `control_state` with singleton-compatible columns:

```python
rows = await conn.fetch(
    """
    SELECT column_name
    FROM information_schema.columns
    WHERE table_name = 'control_state'
    ORDER BY ordinal_position
    """
)
assert [row["column_name"] for row in rows] == [
    "id",
    "paused",
    "pause_reason",
    "paused_by",
    "paused_at",
    "updated_at",
]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -q tests/integration/test_alembic_full_chain.py --maxfail=1`

Expected: FAIL because `control_state` does not exist.

- [ ] **Step 3: Add migration**

Create revision `014_control_state` after `013_work_intents_repo_targets`:

```python
op.create_table(
    "control_state",
    sa.Column("id", sa.Text(), primary_key=True),
    sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    sa.Column("pause_reason", sa.Text(), nullable=True),
    sa.Column("paused_by", sa.Text(), nullable=True),
    sa.Column("paused_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    sa.CheckConstraint("id = 'global'", name="ck_control_state_singleton"),
)
```

- [ ] **Step 4: Update reference schema**

Add the same table to `whilly/adapters/db/schema.sql` near other singleton/operator tables.

- [ ] **Step 5: Run migration test**

Run: `.venv/bin/python -m pytest -q tests/integration/test_alembic_full_chain.py --maxfail=1`

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add whilly/adapters/db/migrations/versions/014_control_state.py whilly/adapters/db/schema.sql tests/integration/test_alembic_full_chain.py
git commit -m "feat(control): add global control state schema"
```

## Task 2: Repository Control-State API

**Files:**
- Modify: `whilly/adapters/db/repository.py`
- Test: `tests/unit/test_control_state_repository.py`

- [ ] **Step 1: Write failing repository tests**

Cover:

```python
state = await repo.get_control_state()
assert state.paused is False
paused = await repo.pause_workers(reason="deploy", operator="lead@example.com")
assert paused.paused is True
assert paused.pause_reason == "deploy"
assert paused.paused_by == "lead@example.com"
resumed = await repo.resume_workers(operator="lead@example.com")
assert resumed.paused is False
assert await repo.is_workers_paused() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -q tests/unit/test_control_state_repository.py --maxfail=1`

Expected: FAIL because methods/types do not exist.

- [ ] **Step 3: Implement repository methods**

Add a frozen `ControlState` dataclass and methods:

```python
async def get_control_state(self) -> ControlState: ...
async def pause_workers(self, *, reason: str | None, operator: str | None) -> ControlState: ...
async def resume_workers(self, *, operator: str | None) -> ControlState: ...
async def is_workers_paused(self) -> bool: ...
```

`get_control_state()` must create the singleton row on first read using `INSERT ... ON CONFLICT DO NOTHING`.

- [ ] **Step 4: Run repository tests**

Run: `.venv/bin/python -m pytest -q tests/unit/test_control_state_repository.py --maxfail=1`

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add whilly/adapters/db/repository.py tests/unit/test_control_state_repository.py
git commit -m "feat(control): add repository pause state"
```

## Task 3: Admin API And Schemas

**Files:**
- Modify: `whilly/adapters/transport/schemas.py`
- Modify: `whilly/adapters/transport/server.py`
- Test: `tests/unit/test_transport_schemas.py`
- Test: `tests/integration/test_control_state_admin_api.py`

- [ ] **Step 1: Write failing schema/API tests**

Cover:

```python
pause = ControlPauseRequest(reason="deploy")
assert pause.reason == "deploy"
state = ControlStateResponse(paused=True, pause_reason="deploy", paused_by="lead@example.com")
assert state.paused is True
```

And integration:

```python
await repo.mint_bootstrap_token("admin-token", owner_email="lead@example.com", is_admin=True)
response = await client.post("/api/v1/admin/workers/pause", json={"reason": "deploy"}, headers=admin_headers)
assert response.status_code == 200
assert response.json()["paused"] is True
response = await client.post("/api/v1/admin/workers/resume", headers=admin_headers)
assert response.status_code == 200
assert response.json()["paused"] is False
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/python -m pytest -q tests/unit/test_transport_schemas.py tests/integration/test_control_state_admin_api.py --maxfail=1`

Expected: FAIL because schemas/routes do not exist.

- [ ] **Step 3: Implement schemas and routes**

Add:

```python
class ControlPauseRequest(_FrozenModel):
    reason: str = ""

class ControlStateResponse(_FrozenModel):
    paused: bool
    pause_reason: str | None = None
    paused_by: str | None = None
    paused_at: datetime | None = None
    updated_at: datetime
```

Add admin routes:

```python
GET /api/v1/admin/workers/control-state
POST /api/v1/admin/workers/pause
POST /api/v1/admin/workers/resume
```

Use `request.state.bootstrap_owner_email` as operator when available.

- [ ] **Step 4: Run API tests**

Run: `.venv/bin/python -m pytest -q tests/unit/test_transport_schemas.py tests/integration/test_control_state_admin_api.py --maxfail=1`

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add whilly/adapters/transport/schemas.py whilly/adapters/transport/server.py tests/unit/test_transport_schemas.py tests/integration/test_control_state_admin_api.py
git commit -m "feat(control): expose admin pause API"
```

## Task 4: Worker Enforcement

**Files:**
- Modify: `whilly/adapters/db/repository.py`
- Modify: `whilly/adapters/transport/client.py`
- Modify: `whilly/worker/local.py`
- Modify: `whilly/worker/remote.py`
- Test: `tests/unit/test_local_worker.py`
- Test: `tests/unit/test_remote_worker.py`

- [ ] **Step 1: Write failing worker tests**

Local worker:

```python
repo.pause_workers.return_value = paused_state
stats = await run_local_worker(repo, runner, plan, worker_id, idle_wait=0, max_iterations=1)
assert repo.claim_task.await_count == 0
assert stats.idle_polls == 1
```

Remote worker:

```python
client.control_state_results.append(paused_state)
stats = await run_remote_worker(client, runner, plan, worker_id, max_iterations=1)
assert client.claim_calls == []
assert stats.idle_polls == 1
```

Active-task release:

```python
repo.is_workers_paused.side_effect = [False, True]
assert release.reason == "operator_pause"
```

- [ ] **Step 2: Run worker tests to verify failure**

Run: `.venv/bin/python -m pytest -q tests/unit/test_local_worker.py tests/unit/test_remote_worker.py --maxfail=1`

Expected: FAIL because workers do not check control state.

- [ ] **Step 3: Implement enforcement**

Local worker checks `repo.get_control_state()` before claim and after runner/verification before complete/fail. If paused after claiming, release the task with `operator_pause`.

Remote worker adds `RemoteWorkerClient.control_state()` and checks before claim and after runner/verification before complete/fail. If paused after claiming, release with `operator_pause`.

- [ ] **Step 4: Run worker tests**

Run: `.venv/bin/python -m pytest -q tests/unit/test_local_worker.py tests/unit/test_remote_worker.py --maxfail=1`

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add whilly/adapters/transport/client.py whilly/worker/local.py whilly/worker/remote.py tests/unit/test_local_worker.py tests/unit/test_remote_worker.py
git commit -m "feat(control): pause workers at safe checkpoints"
```

## Task 5: Operator Snapshot And WUI Parity

**Files:**
- Modify: `whilly/operator_views.py`
- Modify: `whilly/api/dashboard.py`
- Modify: `whilly/api/templates/index.html.j2`
- Test: `tests/integration/test_htmx_dashboard.py`

- [ ] **Step 1: Write failing dashboard parity test**

Assert:

```python
assert "p=pause workers" in body
assert "R=resume workers" in body
assert "Pause workers" in body
assert "Resume workers" in body
assert "togglePolling" not in body
assert "pollingPaused" not in body
```

- [ ] **Step 2: Run dashboard test to verify failure**

Run: `.venv/bin/python -m pytest -q tests/integration/test_htmx_dashboard.py --maxfail=1`

Expected: FAIL because WUI still exposes refresh pause.

- [ ] **Step 3: Implement WUI parity**

Add control state to `OperatorSnapshot`, render cluster state, add Pause/Resume buttons, map hotkeys `p` and `R` to admin endpoints, keep `r` as refresh, and remove local refresh-freeze logic.

- [ ] **Step 4: Run dashboard test**

Run: `.venv/bin/python -m pytest -q tests/integration/test_htmx_dashboard.py --maxfail=1`

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add whilly/operator_views.py whilly/api/dashboard.py whilly/api/templates/index.html.j2 tests/integration/test_htmx_dashboard.py
git commit -m "feat(operator): align web pause controls"
```

## Task 6: TUI Parity

**Files:**
- Modify: `whilly/cli/tui.py`
- Test: `tests/unit/test_tui.py`

- [ ] **Step 1: Write failing TUI parity tests**

Assert:

```python
assert "p=pause workers" in rendered
assert "R=resume workers" in rendered
assert "p=pause  " not in rendered
handle_tui_key(state, "p")
assert state.pending_control_action == "pause"
handle_tui_key(state, "R")
assert state.pending_control_action == "resume"
```

- [ ] **Step 2: Run TUI test to verify failure**

Run: `.venv/bin/python -m pytest -q tests/unit/test_tui.py --maxfail=1`

Expected: FAIL because TUI still toggles local `paused`.

- [ ] **Step 3: Implement TUI parity**

Replace local `paused` state with `pending_control_action`. Apply actions through repository methods during the poll loop. Show `WORKERS PAUSED` when snapshot control state is paused. Keep `r` as refresh and never stop polling while workers are paused.

- [ ] **Step 4: Run TUI test**

Run: `.venv/bin/python -m pytest -q tests/unit/test_tui.py --maxfail=1`

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add whilly/cli/tui.py tests/unit/test_tui.py
git commit -m "feat(operator): align tui pause controls"
```

## Task 7: Review Decision Parity

**Files:**
- Modify: `whilly/cli/tui.py`
- Modify: `whilly/api/templates/index.html.j2`
- Test: `tests/unit/test_tui.py`
- Test: `tests/integration/test_htmx_dashboard.py`

- [ ] **Step 1: Write failing review parity tests**

Assert WUI/TUI both:

```python
assert "j/k=select" in rendered
assert "a=approve" in rendered
assert "x=reject" in rendered
assert "c=changes" in rendered
```

And TUI records `stage_id`, `reviewer`, and decision source consistently.

- [ ] **Step 2: Implement only Compliance-surface activation**

WUI `j/k/a/x/c` should no-op unless Compliance is selected, matching TUI.

- [ ] **Step 3: Run review parity tests**

Run: `.venv/bin/python -m pytest -q tests/unit/test_tui.py tests/integration/test_htmx_dashboard.py --maxfail=1`

Expected: PASS.

- [ ] **Step 4: Commit**

Run:

```bash
git add whilly/cli/tui.py whilly/api/templates/index.html.j2 tests/unit/test_tui.py tests/integration/test_htmx_dashboard.py
git commit -m "feat(operator): align review hotkeys"
```

## Task 8: Verification And Push

**Files:**
- All changed files

- [ ] **Step 1: Run focused verification**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_control_state_repository.py \
  tests/unit/test_transport_schemas.py \
  tests/unit/test_tui.py \
  tests/unit/test_local_worker.py \
  tests/unit/test_remote_worker.py \
  tests/integration/test_control_state_admin_api.py \
  tests/integration/test_htmx_dashboard.py \
  --maxfail=1
```

Expected: PASS.

- [ ] **Step 2: Run repository checks**

Run: `make test`

Expected: PASS, or record exact failing test if the full suite is blocked by Docker/network.

- [ ] **Step 3: Push**

Run: `git push origin main`

Expected: remote main receives all phase commits. No merge is required when already working on `main`.

## Self-Review

- Spec coverage: covers global pause/resume DB state, admin API, worker enforcement, WUI/TUI hotkey parity, and review hotkey parity.
- Placeholder scan: no TBD/TODO placeholders.
- Type consistency: repository returns `ControlState`; transport exposes `ControlStateResponse`; operator snapshot carries control state for WUI/TUI rendering.
