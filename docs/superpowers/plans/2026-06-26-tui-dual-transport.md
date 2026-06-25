# TUI Dual Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `whilly tui` connect either directly to Postgres (full, default) or over the WUI FQDN with a bearer token (read-only), without duplicating snapshot logic.

**Architecture:** A single JSON codec for `OperatorSnapshot` is shared by a new read-only HTTP endpoint (`GET /api/v1/operator/snapshot`) and a new HTTP TUI backend. `whilly tui` selects a backend (`DbOperatorBackend` or `HttpOperatorBackend`) behind one `OperatorBackend` protocol; the asyncpg import is lazy so HTTP mode needs no DB driver. Read-only mode disables mutating hotkeys.

**Tech Stack:** Python 3.12, FastAPI (server), httpx (client), asyncpg (DB only), rich (TUI), pytest. Ruff line length 120, target py312.

**Design of record:** `docs/superpowers/specs/2026-06-26-tui-dual-transport-design.md`

**Process note:** This changes `whilly/` behavior, so Task 1 opens an `opsx` change proposal and Task 8 applies + archives it. The change is not done until the delta is archived and `make spec-check` passes.

---

### Task 1: Open the opsx change proposal

**Files:**
- Create: `openspec/changes/<slug>/` (proposal + spec delta), slug e.g. `tui-dual-transport`

- [ ] **Step 1: Identify the target capability**

Run: `grep -niE 'operator|tui|dashboard|transport|snapshot' openspec/COVERAGE-MATRIX.md`
Pick the capability that owns the operator surface / control-plane transport (likely `operator-surface` or `scheduling`/`transport`). Note the `openspec/specs/<slug>/spec.md` path.

- [ ] **Step 2: Create the proposal with the openspec skill**

Invoke the `openspec-propose` skill (or `opsx:propose`). The delta MUST add a requirement to the chosen capability spec stating: "`whilly tui` SHALL support a read-only HTTP transport selected by `--connect`/`WHILLY_CONTROL_URL`, authenticated by a worker/bootstrap bearer, exposing the operator snapshot via `GET /api/v1/operator/snapshot`; control and human-review actions remain DB-only." Keep DB mode as the default behavior.

- [ ] **Step 3: Commit the proposal**

```bash
git add openspec/changes
git commit -m "spec: propose TUI dual transport (read-only HTTP operator snapshot)"
```

---

### Task 2: Operator snapshot JSON codec

**Files:**
- Create: `whilly/operator_snapshot_codec.py`
- Test: `tests/test_operator_snapshot_codec.py`

- [ ] **Step 1: Write the failing round-trip test**

```python
# tests/test_operator_snapshot_codec.py
from datetime import datetime, timezone

from whilly.operator_snapshot_codec import snapshot_from_dict, snapshot_to_dict
from whilly.operator_views import (
    ComplianceSummary, EventRow, HumanReviewState, OperatorControlState,
    OperatorSnapshot, OperatorTaskRow, ReviewGap, WorkerRow,
)

def _sample() -> OperatorSnapshot:
    ts = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
    return OperatorSnapshot(
        rendered_at=ts,
        summary=ComplianceSummary(
            total_tasks=3, tasks_by_status={"PENDING": 2, "DONE": 1},
            workers_online=1, workers_total=2, failed_tasks=0, open_review_gaps=1,
        ),
        tasks=(OperatorTaskRow(
            task_id="t1", plan_id="p1", status="IN_PROGRESS", priority="P1",
            claimed_by="w1", started_at=ts, updated_at=ts,
            acceptance_criteria=("ac1",), test_steps=("ts1",),
            human_review=HumanReviewState(required=True, decision=None, stage_id="s1"),
            version=2, description="d", key_files=("a.py",), dependencies=("t0",),
        ),),
        workers=(WorkerRow(worker_id="w1", hostname="h", owner_email=None,
                           status="online", last_heartbeat=ts),),
        events=(EventRow(event_id=7, task_id="t1", plan_id="p1",
                         event_type="claimed", created_at=ts, detail={"k": "v"}),),
        review_gaps=(ReviewGap(task_id="t1", plan_id="p1", reason="needs review",
                               stage_id="s1", actionable=True),),
        control_state=OperatorControlState(paused=False),
    )

def test_snapshot_round_trips():
    snap = _sample()
    assert snapshot_from_dict(snapshot_to_dict(snap)) == snap

def test_unknown_keys_are_ignored():
    payload = snapshot_to_dict(_sample())
    payload["future_field"] = 123
    payload["tasks"][0]["future_task_field"] = "x"
    assert snapshot_from_dict(payload) == _sample()

def test_missing_required_key_raises():
    payload = snapshot_to_dict(_sample())
    del payload["summary"]
    import pytest
    with pytest.raises(KeyError):
        snapshot_from_dict(payload)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_operator_snapshot_codec.py -v`
Expected: FAIL with `ModuleNotFoundError: whilly.operator_snapshot_codec`

- [ ] **Step 3: Implement the codec**

```python
# whilly/operator_snapshot_codec.py
"""JSON (de)serialization for OperatorSnapshot — the single wire schema
shared by the HTTP operator-snapshot endpoint and the TUI HTTP backend."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from whilly.operator_views import (
    ComplianceSummary, EventRow, HumanReviewState, OperatorControlState,
    OperatorSnapshot, OperatorTaskRow, ReviewGap, WorkerRow,
)


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _opt_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _human_review_to_dict(h: HumanReviewState) -> dict[str, Any]:
    return {
        "required": h.required, "decision": h.decision, "stage_id": h.stage_id,
        "reason": h.reason, "reviewer": h.reviewer, "approval_channel": h.approval_channel,
    }


def _human_review_from_dict(d: dict[str, Any]) -> HumanReviewState:
    return HumanReviewState(
        required=d.get("required", False), decision=d.get("decision"),
        stage_id=d.get("stage_id", ""), reason=d.get("reason", ""),
        reviewer=d.get("reviewer"), approval_channel=d.get("approval_channel", ""),
    )


def _task_to_dict(t: OperatorTaskRow) -> dict[str, Any]:
    return {
        "task_id": t.task_id, "plan_id": t.plan_id, "status": t.status,
        "priority": t.priority, "claimed_by": t.claimed_by,
        "started_at": _dt(t.started_at), "updated_at": _dt(t.updated_at),
        "acceptance_criteria": list(t.acceptance_criteria),
        "test_steps": list(t.test_steps),
        "human_review": _human_review_to_dict(t.human_review),
        "version": t.version, "description": t.description,
        "key_files": list(t.key_files), "dependencies": list(t.dependencies),
    }


def _task_from_dict(d: dict[str, Any]) -> OperatorTaskRow:
    return OperatorTaskRow(
        task_id=d["task_id"], plan_id=d["plan_id"], status=d["status"],
        priority=d["priority"], claimed_by=d.get("claimed_by"),
        started_at=_opt_dt(d.get("started_at")),
        updated_at=_opt_dt(d["updated_at"]),  # required
        acceptance_criteria=tuple(d.get("acceptance_criteria", ())),
        test_steps=tuple(d.get("test_steps", ())),
        human_review=_human_review_from_dict(d.get("human_review", {})),
        version=d.get("version", 0), description=d.get("description", ""),
        key_files=tuple(d.get("key_files", ())),
        dependencies=tuple(d.get("dependencies", ())),
    )


def _worker_to_dict(w: WorkerRow) -> dict[str, Any]:
    return {"worker_id": w.worker_id, "hostname": w.hostname,
            "owner_email": w.owner_email, "status": w.status,
            "last_heartbeat": _dt(w.last_heartbeat)}


def _worker_from_dict(d: dict[str, Any]) -> WorkerRow:
    return WorkerRow(worker_id=d["worker_id"], hostname=d["hostname"],
                     owner_email=d.get("owner_email"), status=d["status"],
                     last_heartbeat=_opt_dt(d["last_heartbeat"]))


def _event_to_dict(e: EventRow) -> dict[str, Any]:
    return {"event_id": e.event_id, "task_id": e.task_id, "plan_id": e.plan_id,
            "event_type": e.event_type, "created_at": _dt(e.created_at),
            "detail": dict(e.detail)}


def _event_from_dict(d: dict[str, Any]) -> EventRow:
    return EventRow(event_id=d["event_id"], task_id=d.get("task_id"),
                    plan_id=d.get("plan_id"), event_type=d["event_type"],
                    created_at=_opt_dt(d["created_at"]), detail=dict(d.get("detail", {})))


def _gap_to_dict(g: ReviewGap) -> dict[str, Any]:
    return {"task_id": g.task_id, "plan_id": g.plan_id, "reason": g.reason,
            "stage_id": g.stage_id, "reviewer": g.reviewer,
            "approval_channel": g.approval_channel, "actionable": g.actionable}


def _gap_from_dict(d: dict[str, Any]) -> ReviewGap:
    return ReviewGap(task_id=d["task_id"], plan_id=d["plan_id"], reason=d["reason"],
                     stage_id=d.get("stage_id", ""), reviewer=d.get("reviewer"),
                     approval_channel=d.get("approval_channel", ""),
                     actionable=d.get("actionable", False))


def _summary_to_dict(s: ComplianceSummary) -> dict[str, Any]:
    return {"total_tasks": s.total_tasks, "tasks_by_status": dict(s.tasks_by_status),
            "workers_online": s.workers_online, "workers_total": s.workers_total,
            "failed_tasks": s.failed_tasks, "open_review_gaps": s.open_review_gaps}


def _summary_from_dict(d: dict[str, Any]) -> ComplianceSummary:
    return ComplianceSummary(
        total_tasks=d["total_tasks"], tasks_by_status=dict(d["tasks_by_status"]),
        workers_online=d["workers_online"], workers_total=d["workers_total"],
        failed_tasks=d["failed_tasks"], open_review_gaps=d["open_review_gaps"])


def _control_to_dict(c: OperatorControlState) -> dict[str, Any]:
    return {"paused": c.paused, "pause_reason": c.pause_reason,
            "paused_by": c.paused_by, "paused_at": _dt(c.paused_at),
            "updated_at": _dt(c.updated_at)}


def _control_from_dict(d: dict[str, Any]) -> OperatorControlState:
    return OperatorControlState(
        paused=d.get("paused", False), pause_reason=d.get("pause_reason"),
        paused_by=d.get("paused_by"), paused_at=_opt_dt(d.get("paused_at")),
        updated_at=_opt_dt(d.get("updated_at")))


def snapshot_to_dict(snap: OperatorSnapshot) -> dict[str, Any]:
    return {
        "rendered_at": _dt(snap.rendered_at),
        "summary": _summary_to_dict(snap.summary),
        "tasks": [_task_to_dict(t) for t in snap.tasks],
        "workers": [_worker_to_dict(w) for w in snap.workers],
        "events": [_event_to_dict(e) for e in snap.events],
        "review_gaps": [_gap_to_dict(g) for g in snap.review_gaps],
        "control_state": _control_to_dict(snap.control_state),
    }


def snapshot_from_dict(payload: dict[str, Any]) -> OperatorSnapshot:
    return OperatorSnapshot(
        rendered_at=_opt_dt(payload["rendered_at"]),
        summary=_summary_from_dict(payload["summary"]),
        tasks=tuple(_task_from_dict(t) for t in payload.get("tasks", ())),
        workers=tuple(_worker_from_dict(w) for w in payload.get("workers", ())),
        events=tuple(_event_from_dict(e) for e in payload.get("events", ())),
        review_gaps=tuple(_gap_from_dict(g) for g in payload.get("review_gaps", ())),
        control_state=_control_from_dict(payload.get("control_state", {})),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_operator_snapshot_codec.py -v`
Expected: PASS (3 tests). Then `ruff check whilly/operator_snapshot_codec.py`.

- [ ] **Step 5: Commit**

```bash
git add whilly/operator_snapshot_codec.py tests/test_operator_snapshot_codec.py
git commit -m "feat: operator snapshot JSON codec (shared wire schema)"
```

---

### Task 3: HTTP operator-snapshot endpoint

**Files:**
- Modify: `whilly/adapters/transport/server.py` (add a route next to the `GET /events/stream` route, ~line 3121)
- Test: `tests/test_operator_snapshot_endpoint.py`

- [ ] **Step 1: Write the failing endpoint test**

```python
# tests/test_operator_snapshot_endpoint.py
import pytest
from httpx import ASGITransport, AsyncClient

from whilly.adapters.transport.server import create_app
from whilly.operator_snapshot_codec import snapshot_from_dict

pytestmark = pytest.mark.asyncio


async def _client(pool):
    app = create_app(pool, worker_token="legacy-worker", bootstrap_token="legacy-boot")
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_snapshot_requires_bearer(fake_pool):
    async with await _client(fake_pool) as c:
        resp = await c.get("/api/v1/operator/snapshot")
    assert resp.status_code == 401


async def test_snapshot_rejects_bad_bearer(fake_pool):
    async with await _client(fake_pool) as c:
        resp = await c.get("/api/v1/operator/snapshot",
                           headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 403


async def test_snapshot_returns_payload_with_legacy_token(fake_pool):
    async with await _client(fake_pool) as c:
        resp = await c.get("/api/v1/operator/snapshot",
                           headers={"Authorization": "Bearer legacy-worker"})
    assert resp.status_code == 200
    snap = snapshot_from_dict(resp.json())  # must decode cleanly
    assert snap.summary.total_tasks >= 0
```

> NOTE: reuse the existing API test harness for `fake_pool`. Run
> `grep -rn "def fake_pool" tests/` and import/fixture exactly as the
> `/events/stream` tests do (search `grep -rln "events/stream" tests/`).
> If no shared `fake_pool` fixture exists, copy the pool stub those SSE
> tests use into a `conftest.py` fixture.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_operator_snapshot_endpoint.py -v`
Expected: FAIL — 404 on the route (not yet registered).

- [ ] **Step 3: Register the route inside `create_app`**

Locate the `@app.get("/events/stream", ...)` route in `whilly/adapters/transport/server.py` (~line 3121). Immediately after it (still inside `create_app`, where `repo`, `legacy_worker_token`, `legacy_bootstrap_token` are in scope), add:

```python
    @app.get("/api/v1/operator/snapshot", include_in_schema=True)
    async def _operator_snapshot(request: Request, plan: str | None = None):
        # Read-only operator surface for the TUI HTTP backend. Same bearer
        # gate as GET /events/stream (worker bearer / bootstrap / legacy).
        await _authenticate_stream_request(
            repo=repo,
            authorization=request.headers.get("authorization"),
            legacy_worker_token=legacy_worker_token,
            legacy_bootstrap_token=legacy_bootstrap_token,
        )
        snapshot = await fetch_operator_snapshot(pool, plan_id=plan)
        return JSONResponse(snapshot_to_dict(snapshot))
```

Add imports near the other `whilly.api` imports at the top of the file:

```python
from whilly.operator_views import fetch_operator_snapshot
from whilly.operator_snapshot_codec import snapshot_to_dict
```

Ensure `JSONResponse` and `Request` are imported (search the file; `Request` is already used by `_authenticate_events_stream_request`; add `from fastapi.responses import JSONResponse` if not already present).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_operator_snapshot_endpoint.py -v`
Expected: PASS (3 tests). Then `ruff check whilly/adapters/transport/server.py`.

- [ ] **Step 5: Commit**

```bash
git add whilly/adapters/transport/server.py tests/test_operator_snapshot_endpoint.py tests/conftest.py
git commit -m "feat: read-only GET /api/v1/operator/snapshot endpoint"
```

---

### Task 4: TUI backends (DB + HTTP)

**Files:**
- Create: `whilly/cli/tui_backends.py`
- Test: `tests/test_tui_backends.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tui_backends.py
import httpx
import pytest

from whilly.cli.tui_backends import HttpOperatorBackend, build_scheme_guard_error
from whilly.operator_snapshot_codec import snapshot_to_dict
from tests.test_operator_snapshot_codec import _sample  # reuse sample snapshot

pytestmark = pytest.mark.asyncio


async def test_http_backend_parses_snapshot():
    payload = snapshot_to_dict(_sample())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/operator/snapshot"
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    backend = HttpOperatorBackend("https://whilly.corp", "tok", insecure=False,
                                  transport=transport)
    snap = await backend.fetch_snapshot(plan_id=None)
    assert snap == _sample()
    assert backend.read_only is True
    await backend.close()


async def test_http_backend_rejects_plain_http_non_loopback():
    with pytest.raises(ValueError):
        HttpOperatorBackend("http://whilly.corp", "tok", insecure=False)


async def test_http_backend_allows_plain_http_with_insecure():
    backend = HttpOperatorBackend("http://whilly.corp", "tok", insecure=True)
    assert backend.read_only is True
    await backend.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tui_backends.py -v`
Expected: FAIL — `ModuleNotFoundError: whilly.cli.tui_backends`

- [ ] **Step 3: Implement the backends**

```python
# whilly/cli/tui_backends.py
"""Transport backends for `whilly tui`: direct Postgres (full) and
read-only HTTP against the WUI control-plane."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx

from whilly.operator_snapshot_codec import snapshot_from_dict
from whilly.operator_views import OperatorSnapshot

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def build_scheme_guard_error(url: str) -> ValueError:
    return ValueError(
        f"whilly tui: refusing plain http:// to non-loopback host in {url!r}; "
        "use https:// or pass --insecure (WHILLY_INSECURE=1)."
    )


@runtime_checkable
class OperatorBackend(Protocol):
    read_only: bool
    async def fetch_snapshot(self, plan_id: str | None) -> OperatorSnapshot: ...
    async def close(self) -> None: ...


class DbOperatorBackend:
    """Direct Postgres pool — full capability (view + control + review)."""
    read_only = False

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def fetch_snapshot(self, plan_id: str | None) -> OperatorSnapshot:
        # Lazy import so the HTTP path never needs the DB view module's deps.
        from whilly.operator_views import fetch_operator_snapshot
        return await fetch_operator_snapshot(self._pool, plan_id=plan_id)

    @property
    def pool(self) -> Any:
        return self._pool

    async def close(self) -> None:
        from whilly.adapters.db import close_pool
        await close_pool(self._pool)


class HttpOperatorBackend:
    """Read-only HTTP backend against GET /api/v1/operator/snapshot."""
    read_only = True

    def __init__(self, base_url: str, token: str, *, insecure: bool = False,
                 transport: httpx.BaseTransport | None = None) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme == "http" and (parsed.hostname or "") not in _LOOPBACK_HOSTS \
                and not insecure:
            raise build_scheme_guard_error(base_url)
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            transport=transport, timeout=10.0,
        )

    async def fetch_snapshot(self, plan_id: str | None) -> OperatorSnapshot:
        params = {"plan": plan_id} if plan_id else None
        resp = await self._client.get(f"{self._base}/api/v1/operator/snapshot",
                                      params=params)
        resp.raise_for_status()
        return snapshot_from_dict(resp.json())

    async def close(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tui_backends.py -v`
Expected: PASS (3 tests). Then `ruff check whilly/cli/tui_backends.py`.

- [ ] **Step 5: Commit**

```bash
git add whilly/cli/tui_backends.py tests/test_tui_backends.py
git commit -m "feat: TUI operator backends (DB full + HTTP read-only)"
```

---

### Task 5: Wire backends into `whilly tui` (args + mode resolution + lazy asyncpg)

**Files:**
- Modify: `whilly/cli/tui.py` (parser, `run_tui_command`, `_async_run`, mutating-key gating; remove top-level `create_pool`/`close_pool` import — make it lazy)
- Test: `tests/test_tui_mode_resolution.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tui_mode_resolution.py
import sys
import pytest

from whilly.cli.tui import resolve_backend_spec, EXIT_ENVIRONMENT_ERROR


def test_connect_url_selects_http():
    spec = resolve_backend_spec(connect="https://whilly.corp", token="t",
                                insecure=False, dsn=None)
    assert spec.kind == "http"
    assert spec.base_url == "https://whilly.corp"


def test_dsn_selects_db_when_no_connect():
    spec = resolve_backend_spec(connect=None, token=None, insecure=False,
                                dsn="postgresql://x")
    assert spec.kind == "db"


def test_neither_is_error():
    with pytest.raises(ValueError):
        resolve_backend_spec(connect=None, token=None, insecure=False, dsn=None)


def test_http_path_does_not_import_asyncpg(monkeypatch):
    # Simulate a host with no asyncpg: importing it must not happen on HTTP mode.
    monkeypatch.setitem(sys.modules, "asyncpg", None)  # None => import raises
    spec = resolve_backend_spec(connect="https://whilly.corp", token="t",
                                insecure=False, dsn=None)
    assert spec.kind == "http"  # resolution itself triggers no asyncpg import
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tui_mode_resolution.py -v`
Expected: FAIL — `cannot import name 'resolve_backend_spec'`

- [ ] **Step 3: Implement mode resolution + lazy DB import + parser/run wiring**

In `whilly/cli/tui.py`:

(a) Remove the top-level DB import line `from whilly.adapters.db import close_pool, create_pool` (keep `TaskRepository` import only if still used in the DB branch; otherwise make it lazy too). Add near the constants:

```python
from dataclasses import dataclass

CONTROL_URL_ENV: Final[str] = "WHILLY_CONTROL_URL"
WORKER_TOKEN_ENV: Final[str] = "WHILLY_WORKER_TOKEN"
INSECURE_ENV: Final[str] = "WHILLY_INSECURE"


@dataclass(frozen=True)
class BackendSpec:
    kind: str               # "db" | "http"
    dsn: str | None = None
    base_url: str | None = None
    token: str = ""
    insecure: bool = False


def resolve_backend_spec(*, connect: str | None, token: str | None,
                         insecure: bool, dsn: str | None) -> BackendSpec:
    if connect:
        return BackendSpec(kind="http", base_url=connect.rstrip("/"),
                           token=(token or ""), insecure=insecure)
    if dsn:
        return BackendSpec(kind="db", dsn=dsn)
    raise ValueError(
        f"whilly tui: set --connect ({CONTROL_URL_ENV}) for HTTP mode or "
        f"{DATABASE_URL_ENV} for direct DB mode."
    )
```

(b) In `build_tui_parser`, add:

```python
    parser.add_argument("--connect", dest="connect", default=None,
                        help="Control-plane URL for read-only HTTP mode "
                             "(env WHILLY_CONTROL_URL).")
    parser.add_argument("--token", dest="token", default=None,
                        help="Bearer token for HTTP mode (env WHILLY_WORKER_TOKEN).")
    parser.add_argument("--insecure", action="store_true",
                        help="Allow plain http:// to a non-loopback host.")
```

(c) Rewrite `run_tui_command` to resolve the spec instead of hard-requiring the DSN:

```python
def run_tui_command(argv, *, key_source=None) -> int:
    parser = build_tui_parser()
    args = parser.parse_args(list(argv))
    connect = args.connect or os.environ.get(CONTROL_URL_ENV) or None
    token = args.token or os.environ.get(WORKER_TOKEN_ENV)
    insecure = args.insecure or os.environ.get(INSECURE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
    dsn = os.environ.get(DATABASE_URL_ENV)
    try:
        spec = resolve_backend_spec(connect=connect, token=token,
                                    insecure=insecure, dsn=dsn)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ENVIRONMENT_ERROR

    use_color = not args.no_color and _stream_supports_color()
    try:
        asyncio.run(_async_run(
            spec=spec, plan_id=args.plan_id, interval=args.interval,
            max_iterations=args.max_iterations, use_color=use_color,
            key_source=key_source or _default_key_source(),
            reviewer=(args.reviewer or os.environ.get(REVIEWER_ENV) or "").strip(),
        ))
    except (OSError, ValueError) as exc:
        print(f"whilly tui: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ENVIRONMENT_ERROR
    return EXIT_OK
```

(d) Rewrite `_async_run` to build the backend from the spec (lazy DB import lives here):

```python
async def _async_run(*, spec, plan_id, interval, max_iterations, use_color,
                     key_source, reviewer) -> None:
    from whilly.cli.tui_backends import DbOperatorBackend, HttpOperatorBackend
    if spec.kind == "db":
        from whilly.adapters.db import create_pool  # lazy: no asyncpg in HTTP mode
        backend = DbOperatorBackend(await create_pool(spec.dsn))
    else:
        backend = HttpOperatorBackend(spec.base_url, spec.token, insecure=spec.insecure)

    state = TuiState()
    state.read_only = backend.read_only
    snapshot = await _empty_snapshot()
    console = Console(file=sys.stdout, force_terminal=use_color,
                      no_color=not use_color, highlight=False)
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_listen_for_keys(state, key_source))
            tg.create_task(_poll_loop(backend, plan_id, state, snapshot=snapshot,
                                      console=console, interval=interval,
                                      max_iterations=max_iterations, reviewer=reviewer))
    finally:
        await backend.close()
```

(e) Update `_poll_loop` to take `backend` instead of `pool` and call `backend.fetch_snapshot(plan_id)` (replace the `fetch_operator_snapshot(pool, ...)` call). Where `_poll_loop` currently passes `pool` into `_apply_pending_control_action` / `_apply_pending_review_action`, guard those on `state.read_only`: if read-only, skip the mutation and surface a one-line footer hint instead. Add a `read_only: bool = False` field to `TuiState`, and in `_apply_pending_control_action` / `_apply_pending_review_action` early-return `False` when `state.read_only` is true. For the DB branch only, those helpers obtain the pool via `backend.pool` (DbOperatorBackend exposes `.pool`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tui_mode_resolution.py -v`
Expected: PASS (4 tests).
Run the existing TUI suite to confirm DB mode is unbroken:
Run: `pytest tests/ -k tui -v`
Expected: PASS (existing DB-mode tests unchanged). Then `ruff check whilly/cli/tui.py`.

- [ ] **Step 5: Commit**

```bash
git add whilly/cli/tui.py tests/test_tui_mode_resolution.py
git commit -m "feat: whilly tui --connect read-only HTTP mode (DSN fallback, lazy asyncpg)"
```

---

### Task 6: Read-only footer hint in the TUI render

**Files:**
- Modify: `whilly/cli/tui.py` (`_header` or footer render path)
- Test: `tests/test_tui_readonly_hint.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tui_readonly_hint.py
from whilly.cli.tui import TuiState, _read_only_hint


def test_hint_present_in_read_only():
    state = TuiState()
    state.read_only = True
    assert "read-only" in _read_only_hint(state).lower()


def test_no_hint_in_db_mode():
    state = TuiState()
    state.read_only = False
    assert _read_only_hint(state) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tui_readonly_hint.py -v`
Expected: FAIL — `cannot import name '_read_only_hint'`

- [ ] **Step 3: Implement the hint and render it**

```python
def _read_only_hint(state: TuiState) -> str:
    if getattr(state, "read_only", False):
        return "read-only (HTTP) — connect to the DB for control/review"
    return ""
```

In the header/footer builder (e.g. `_header`), append the hint as a dim line when non-empty so it shows in HTTP mode.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tui_readonly_hint.py -v`
Expected: PASS (2 tests). Then `ruff check whilly/cli/tui.py`.

- [ ] **Step 5: Commit**

```bash
git add whilly/cli/tui.py tests/test_tui_readonly_hint.py
git commit -m "feat: read-only footer hint in TUI HTTP mode"
```

---

### Task 7: Docs

**Files:**
- Modify: `docs/Whilly-Usage.md` (TUI section + env var reference)
- Modify: `deploy/helm/whilly/README.md` and `deploy/helm/whilly/templates/NOTES.txt`

- [ ] **Step 1: Document the TUI dual transport**

In `docs/Whilly-Usage.md`, under the `whilly tui` entry, add:

```markdown
`whilly tui` connects two ways:
- **Direct DB (default, full):** set `WHILLY_DATABASE_URL`. View + control + review.
- **HTTP (read-only):** `whilly tui --connect https://whilly.<domain> --token <bearer>`
  (or env `WHILLY_CONTROL_URL` / `WHILLY_WORKER_TOKEN`). View-only; control and
  human-review are disabled. Plain `http://` to a non-loopback host needs
  `--insecure` (`WHILLY_INSECURE=1`). Bearer = a worker or bootstrap token.
```

Add `WHILLY_CONTROL_URL`, `WHILLY_WORKER_TOKEN`, `WHILLY_INSECURE` to the env var reference table with the note "consumed by `whilly tui` HTTP mode (and the remote worker)".

- [ ] **Step 2: Update the Helm operator note**

In `deploy/helm/whilly/README.md` (the "Using it → TUI" bullet) and `templates/NOTES.txt` (item 2), add that operators without DB reachability can run the TUI read-only against the WUI FQDN:

```bash
whilly tui --connect https://whilly.<corp-domain> --token <worker-or-bootstrap-bearer>
```

- [ ] **Step 3: Commit**

```bash
git add docs/Whilly-Usage.md deploy/helm/whilly/README.md deploy/helm/whilly/templates/NOTES.txt
git commit -m "docs: TUI dual transport (DB full + read-only HTTP via FQDN)"
```

---

### Task 8: Apply & archive the opsx change; full gate

**Files:**
- Modify: `openspec/specs/<slug>/spec.md` (delta applied), `openspec/changes/archive/...`

- [ ] **Step 1: Apply the change to the capability spec**

Invoke `openspec-apply-change` (or `opsx:apply`) to merge the delta from Task 1 into `openspec/specs/<slug>/spec.md`. Confirm the new read-only-HTTP requirement reads as the source of truth.

- [ ] **Step 2: Run the spec gate**

Run: `make spec-check`
Expected: PASS (no drift).

- [ ] **Step 3: Run the full test + lint suite**

Run: `pytest -q`
Expected: PASS.
Run: `ruff check whilly/ tests/`
Expected: clean.

- [ ] **Step 4: Archive the change**

Invoke `openspec-archive-change` (or `opsx:archive`) to move the change under `openspec/changes/archive/`.

- [ ] **Step 5: Commit**

```bash
git add openspec
git commit -m "spec: apply+archive TUI dual transport delta"
```

---

## Self-Review

- **Spec coverage:** §2 codec → Task 2; §3.2 endpoint → Task 3; §3.3 backends → Task 4; §2/§3.4 mode resolution + lazy asyncpg → Task 5; read-only UI (§3.4) → Task 6; §8 docs → Task 7; §5 auth reuse → Task 3 (gate reuse) + Task 4 (scheme guard); §6 error handling → Tasks 4–5 (exit 2, scheme guard, raise_for_status); §9 opsx → Tasks 1 & 8. All spec sections covered.
- **Error-handling note:** the design's "disconnected, retrying" transient banner for mid-loop network errors is a refinement of `_poll_loop`; if the existing loop already swallows fetch exceptions per tick, keep that behavior and ensure an `httpx` error does not crash the loop (wrap the `backend.fetch_snapshot` call in the same try/except the DB path uses). Verify during Task 5 Step 3(e).
- **Type consistency:** `OperatorBackend.read_only` (bool), `fetch_snapshot(plan_id)`, `close()` used identically across Tasks 4–6; `BackendSpec.kind` values `"db"`/`"http"` consistent across Task 5; codec function names `snapshot_to_dict`/`snapshot_from_dict` consistent across Tasks 2–4.
- **Placeholder scan:** no TBD/TODO; every code step shows full code. The only deferred lookup is the shared `fake_pool` fixture (Task 3 NOTE) — grounded with exact grep commands rather than left vague.
