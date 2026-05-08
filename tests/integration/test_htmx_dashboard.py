"""Integration tests for the M3 HTMX dashboard endpoint (``GET /``).

Covers the m3-htmx-dashboard feature:

* ``GET /`` returns 200 ``text/html`` with the full dashboard
* Initial render lists workers + tasks (rows keyed by ``id``)
* htmx-ext-sse@2.2.4 + htmx 1.x are referenced (CDN URLs in the
  page source) and ``hx-ext="sse"`` / ``sse-connect="/events/stream"``
  are wired
* ``hx-trigger="every 5s"`` polling fallback is present on both tables
* Empty state renders a ``"No workers connected"`` / ``"No tasks in queue"``
  message instead of a blank ``<tbody>``
* DB-down error state renders a friendly banner with a Retry button
  (still ``200`` so the polling fallback doesn't blank the page)
* Mobile/tablet-responsive ``@media (max-width: 900px)`` block ships in the page
* The inline TUI-aligned shell uses ``prefers-color-scheme`` for dark-mode
  palette selection
* Jinja2 autoescape on — a worker registered with
  ``hostname=<script>alert(1)</script>`` renders escaped, no real
  ``<script>`` tag
* ``?fragment=workers|tasks`` returns just the table partial
* ``jinja2>=3.1`` is listed in ``[project.optional-dependencies].server``
"""

from __future__ import annotations

import re
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.transport.server import create_app

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN = "bootstrap-htmx-dashboard-test"


@pytest.fixture
async def app(db_pool: asyncpg.Pool) -> AsyncIterator[FastAPI]:
    fastapi_app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=0.3,
        claim_poll_interval=0.05,
        sse_ping_seconds=1,
    )
    async with fastapi_app.router.lifespan_context(fastapi_app):
        yield fastapi_app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _seed_plan(pool: asyncpg.Pool, plan_id: str = "plan-htmx") -> str:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            plan_id,
            f"plan {plan_id}",
        )
    return plan_id


async def _seed_task(
    pool: asyncpg.Pool,
    *,
    task_id: str,
    plan_id: str,
    status: str = "PENDING",
    priority: str = "medium",
    claimed_by: str | None = None,
    acceptance_criteria: list[str] | None = None,
    test_steps: list[str] | None = None,
) -> None:
    claimed_at = datetime.now(tz=UTC) if claimed_by is not None else None
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, priority, claimed_by, claimed_at, description,
                acceptance_criteria, test_steps
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb)
            """,
            task_id,
            plan_id,
            status,
            priority,
            claimed_by,
            claimed_at,
            f"task {task_id}",
            json.dumps(acceptance_criteria or []),
            json.dumps(test_steps or []),
        )


async def _seed_worker(
    pool: asyncpg.Pool,
    *,
    worker_id: str,
    hostname: str,
    owner_email: str | None = None,
    status: str = "online",
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO workers (worker_id, hostname, owner_email, status, last_heartbeat, registered_at, token_hash)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            worker_id,
            hostname,
            owner_email,
            status,
            datetime.now(tz=UTC) - timedelta(seconds=5),
            datetime.now(tz=UTC),
            f"hash-{worker_id}",
        )


async def _seed_event(
    pool: asyncpg.Pool,
    *,
    task_id: str | None,
    plan_id: str | None,
    event_type: str,
    payload: dict[str, object] | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO events (task_id, plan_id, event_type, payload, detail, created_at)
            VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6)
            """,
            task_id,
            plan_id,
            event_type,
            json.dumps(payload or {}),
            json.dumps({"source": "test-dashboard"}),
            datetime.now(tz=UTC),
        )


async def _seed_noise_events(pool: asyncpg.Pool, *, plan_id: str, count: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO events (task_id, plan_id, event_type, payload, detail, created_at)
            SELECT NULL, $1, 'noise.event', '{}'::jsonb, '{}'::jsonb, NOW()
            FROM generate_series(1, $2)
            """,
            plan_id,
            count,
        )


# ─── Endpoint registration ───────────────────────────────────────────────


def test_dashboard_route_registered(app: FastAPI) -> None:
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/" in paths


# ─── Happy path: full page render ────────────────────────────────────────


async def test_get_root_returns_html_200(client: AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


async def test_dashboard_includes_tui_shell_styles_and_htmx_sse(client: AsyncClient) -> None:
    response = await client.get("/")
    body = response.text
    assert "@picocss/pico" not in body, "dashboard should use the TUI-aligned shell instead of Pico cards"
    assert 'class="operator-console"' in body
    assert 'data-dashboard-state="live"' in body
    assert "Queue health" in body
    assert "metric-strip" in body
    assert "htmx.org@1.9.12" in body, "htmx CDN reference missing"
    assert "htmx-ext-sse@2.2.4" in body, "htmx-ext-sse@2.2.4 CDN reference missing"


async def test_dashboard_wires_sse_connect_to_events_stream(client: AsyncClient) -> None:
    response = await client.get("/")
    body = response.text
    assert 'hx-ext="sse"' in body
    assert re.search(r'sse-connect="/events/stream(?:\?token=[^"]+)?"', body)
    assert 'name="whilly-events-token"' in body
    for target in ('hx-select="#summary"', 'hx-select="#review-gaps"'):
        assert target in body
    assert "sse:human_review.approved" in body


async def test_dashboard_polling_fallback_every_5s(client: AsyncClient) -> None:
    response = await client.get("/")
    body = response.text
    matches = re.findall(r'hx-trigger="[^"]*every 5s[^"]*"', body)
    assert len(matches) >= 2, f"expected at least 2 hx-trigger every 5s elements, got {matches!r}"


async def test_dashboard_dark_mode_via_pico_prefers_color_scheme(client: AsyncClient) -> None:
    response = await client.get("/")
    body = response.text
    assert "prefers-color-scheme" in body, "TUI shell should still respect OS color preference"


async def test_dashboard_mobile_responsive_block(client: AsyncClient) -> None:
    response = await client.get("/")
    body = response.text
    assert "@media (max-width: 900px)" in body, "mobile-responsive media query missing"


async def test_dashboard_mirrors_operator_surfaces_and_hotkeys(client: AsyncClient) -> None:
    response = await client.get("/")
    body = response.text

    for label in ("Overview", "Compliance", "Plans/Tasks", "Workers", "Events"):
        assert label in body
    assert 'id="dashboard-filter"' in body
    assert 'data-surface="overview"' in body
    assert 'data-surface="compliance"' in body
    assert 'data-surface="plans_tasks"' in body
    assert 'data-surface="workers"' in body
    assert 'data-surface="events"' in body
    assert "q=quit" in body
    assert "r=refresh" in body
    assert "R=resume workers" in body
    assert "1-5=switch" in body
    assert "/=filter" in body
    assert "p=pause workers" in body
    assert "Pause workers" in body
    assert "Resume workers" in body
    assert "Workers running" in body
    assert "quitDashboard" in body
    assert "closeLiveStreams" in body
    assert "manualRefreshRequested" in body
    assert "markManualLiveRequest" in body
    assert "isManualLiveIntent" in body
    assert 'type === "click"' in body
    assert "submitControlAction" in body
    assert "/api/v1/admin/workers/" in body
    assert "togglePolling" not in body
    assert "pollingPaused" not in body
    assert "htmx:sseOpen" in body
    assert "htmx:afterRequest" in body
    assert "htmx:sendError" in body
    assert "htmx:afterSwap" in body
    assert "isEditableTarget" in body, "hotkeys must not hijack typing in inputs"


async def test_dashboard_queue_health_is_persistent_outside_active_surface(client: AsyncClient) -> None:
    response = await client.get("/")
    body = response.text

    assert body.index('class="metric-strip"') < body.index('id="surface-overview"')
    summary_block = re.search(r'<section class="metric-strip".*?</section>', body, re.DOTALL)
    assert summary_block is not None
    summary_html = summary_block.group(0)
    for trigger in (
        "every 5s",
        "sse:task.created",
        "sse:CLAIM",
        "sse:START",
        "sse:COMPLETE",
        "sse:FAIL",
        "sse:RELEASE",
        "sse:SKIP",
        "sse:task.skipped",
        "sse:worker.registered",
        "sse:worker.heartbeat",
        "sse:worker.revoked",
        "sse:worker.offline",
        "sse:human_review.required",
    ):
        assert trigger in summary_html


async def test_dashboard_renders_compliance_gaps_and_events(
    client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    plan_id = await _seed_plan(db_pool, "plan-review")
    await _seed_worker(db_pool, worker_id="worker-review", hostname="review.local")
    await _seed_task(
        db_pool,
        task_id="t-human-review",
        plan_id=plan_id,
        status="IN_PROGRESS",
        priority="critical",
        claimed_by="worker-review",
        acceptance_criteria=["manual approval is captured"],
        test_steps=["human review required"],
    )
    await _seed_task(db_pool, task_id="t-missing-ac", plan_id=plan_id, status="PENDING")
    await _seed_event(db_pool, task_id="t-human-review", plan_id=plan_id, event_type="START")
    await _seed_event(
        db_pool,
        task_id="t-human-review",
        plan_id=plan_id,
        event_type="human_review.required",
        payload={
            "task_id": "t-human-review",
            "plan_id": plan_id,
            "stage_id": "release_review",
            "reason": "stage_human_gate",
        },
    )
    await _seed_noise_events(db_pool, plan_id=plan_id, count=205)
    await _seed_event(db_pool, task_id="t-human-review", plan_id=plan_id, event_type="START")

    response = await client.get("/")
    body = response.text

    assert "Compliance - Human review / verification gaps" in body
    assert "awaiting human review" in body
    assert "release_review" in body
    assert "missing acceptance criteria" in body
    assert "t-human-review" in body
    assert "human_review.required" in body
    assert "Events" in body
    assert "START" in body
    assert 'id="dashboard-admin-token"' in body
    assert 'id="dashboard-reviewer"' in body
    assert "j/k=select" in body
    assert "a=approve" in body
    assert "x=reject" in body
    assert "c=changes" in body
    assert 'data-review-task="t-human-review"' in body
    assert 'data-review-stage="release_review"' in body
    assert 'data-review-actionable="true"' in body
    assert 'data-review-decision="approved"' in body
    assert 'data-review-decision="rejected"' in body
    assert 'data-review-decision="changes_requested"' in body
    assert "/api/v1/tasks/" in body
    assert _BOOTSTRAP_TOKEN not in body


# ─── Initial render: rows for ready / in_progress / done tasks ─────────


async def test_initial_render_shows_one_row_per_task(client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    plan_id = await _seed_plan(db_pool)
    await _seed_task(db_pool, task_id="t-pending", plan_id=plan_id, status="PENDING")
    await _seed_task(
        db_pool,
        task_id="t-in-progress",
        plan_id=plan_id,
        status="IN_PROGRESS",
        claimed_by=None,
    )
    await _seed_task(db_pool, task_id="t-done", plan_id=plan_id, status="DONE")

    response = await client.get("/")
    body = response.text
    assert 'id="task-t-pending"' in body
    assert 'id="task-t-in-progress"' in body
    assert 'id="task-t-done"' in body


async def test_initial_render_shows_workers(client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    await _seed_worker(db_pool, worker_id="w-alpha", hostname="alpha.local", owner_email="a@x.com")
    await _seed_worker(db_pool, worker_id="w-beta", hostname="beta.local", owner_email="b@x.com")

    response = await client.get("/")
    body = response.text
    assert 'id="worker-w-alpha"' in body
    assert 'id="worker-w-beta"' in body
    assert "alpha.local" in body
    assert "a@x.com" in body


# ─── Empty state ─────────────────────────────────────────────────────────


async def test_empty_state_no_workers_no_tasks(client: AsyncClient) -> None:
    response = await client.get("/")
    body = response.text
    assert response.status_code == 200
    assert "No workers connected" in body
    assert "No tasks in queue" in body


# ─── Error state (DB unreachable) ───────────────────────────────────────


async def test_error_banner_when_pool_fails(db_pool: asyncpg.Pool) -> None:
    """A pool that errors on acquire should render the dashboard with a banner.

    We close the pool *after* the app's lifespan has booted so the
    handler hits a real failure when it tries to ``acquire()``. The
    response stays 200 and an HTML banner with a Retry button is
    rendered (no 500) so the polling fallback can re-try the page
    without flashing the user a blank page.
    """
    fastapi_app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=0.3,
        claim_poll_interval=0.05,
        sse_ping_seconds=1,
    )
    async with fastapi_app.router.lifespan_context(fastapi_app):
        await db_pool.close()
        try:
            transport = ASGITransport(app=fastapi_app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                response = await ac.get("/")
            assert response.status_code == 200
            body = response.text
            assert "Control plane unavailable" in body
            assert "Retry" in body
        finally:
            pass


# ─── XSS-safety via Jinja2 autoescape ───────────────────────────────────


async def test_xss_payload_in_hostname_is_escaped(client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    payload = "<script>alert('xss')</script>"
    await _seed_worker(db_pool, worker_id="w-xss", hostname=payload, owner_email="x@x.com")
    response = await client.get("/")
    body = response.text
    assert payload not in body, "raw <script> payload must be escaped"
    assert "&lt;script&gt;" in body or "&#x3C;script&#x3E;" in body or "&#60;script&#62;" in body, (
        "expected HTML-escaped <script> in rendered page"
    )


async def test_xss_payload_in_owner_email_is_escaped(client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    payload = '"><img src=x onerror=alert(1)>'
    await _seed_worker(db_pool, worker_id="w-xss2", hostname="h", owner_email=payload)
    response = await client.get("/")
    body = response.text
    assert payload not in body
    assert "&#34;" in body or "&quot;" in body, "expected escaped quote"


# ─── Fragment partials (polling fallback target) ────────────────────────


async def test_workers_fragment_returns_just_workers_table(client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    await _seed_worker(db_pool, worker_id="w-frag", hostname="frag.local")
    response = await client.get("/?fragment=workers")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert '<table id="workers"' in body
    assert 'id="worker-w-frag"' in body
    assert "<!DOCTYPE html>" not in body, "fragment must not contain full document"


async def test_tasks_fragment_returns_just_tasks_table(client: AsyncClient, db_pool: asyncpg.Pool) -> None:
    plan_id = await _seed_plan(db_pool)
    await _seed_task(db_pool, task_id="t-frag", plan_id=plan_id, status="PENDING")
    response = await client.get("/?fragment=tasks")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert '<table id="tasks"' in body
    assert 'id="task-t-frag"' in body
    assert "<!DOCTYPE html>" not in body


async def test_unknown_fragment_falls_back_to_full_page(
    client: AsyncClient,
) -> None:
    response = await client.get("/?fragment=nope")
    assert response.status_code == 200
    assert "<!DOCTYPE html>" in response.text


# ─── Static contract assertions ─────────────────────────────────────────


def test_pyproject_lists_jinja2_in_server_extras() -> None:
    import tomllib
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text())
    server_extras = pyproject["project"]["optional-dependencies"]["server"]
    assert any(dep.startswith("jinja2") for dep in server_extras), (
        f"jinja2 missing from [project.optional-dependencies].server: {server_extras!r}"
    )


def test_dashboard_template_file_exists() -> None:
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[2]
    template_path = project_root / "whilly" / "api" / "templates" / "index.html.j2"
    assert template_path.is_file(), f"index.html.j2 missing at {template_path}"
    workers_partial = project_root / "whilly" / "api" / "templates" / "_workers_table.html"
    tasks_partial = project_root / "whilly" / "api" / "templates" / "_tasks_table.html"
    assert workers_partial.is_file()
    assert tasks_partial.is_file()


def test_setuptools_includes_template_package_data() -> None:
    import tomllib
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text())
    package_data = pyproject["tool"]["setuptools"]["package-data"]["whilly"]
    assert any("templates" in p for p in package_data), f"templates not declared as package-data: {package_data!r}"


# ─── Auth model: dashboard is publicly readable (documented) ───────────


async def test_dashboard_is_public_no_auth_required(client: AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200, "dashboard must be reachable without auth"
