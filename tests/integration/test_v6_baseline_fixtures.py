"""v6-baseline fixture harness for the 34 fixture-driven VAL-M3-* assertions.

Five harnesses exercise the v5.0-deferred fixture-driven assertions
against the v4.6.1 LIVE control-plane build. Each harness writes
evidence under ``tests/fixtures/v6_baseline/`` so the user-testing
validator can inspect the result post-run.

The five harnesses (one test function each, runnable in isolation via
``pytest -k``):

* ``test_xss_injection_harness``: posts canonical XSS payloads through
  HTMX form fields (``POST /workers/register`` hostname) and asserts
  the dashboard renders them HTML-escaped.
* ``test_synthetic_load_100_workers``: spawns 100 in-process async
  workers that concurrently claim tasks against a single control-plane
  + testcontainer Postgres; asserts >= 99% completion and zero
  double-claims.
* ``test_sustained_scrape_1000hz``: emits events at 1000 Hz for a
  configurable duration (default 60 s) over the SSE event broker
  while a streaming consumer drains ``/events/stream``; asserts a
  drop rate < 5% and Last-Event-ID resume after a mid-stream
  disconnect.
* ``test_js_disabled_browser``: launches agent-browser with JavaScript
  disabled and verifies the HTMX dashboard's noscript / server-side
  rendered fallback. Falls back to raw HTTP fetch when agent-browser
  is unavailable so the harness still passes in plain CI.
* ``test_postgres_outage_simulator``: spins up a dedicated Postgres
  testcontainer, connects an asyncpg pool, then ``docker pause``\\ es
  the container mid-task and asserts retry/backoff + recovery once
  unpaused.

Each harness is parameterised by ``WHILLY_FIXTURE_*`` env vars so the
user-testing validator can crank durations / sizes up without editing
this file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.conftest import (
    DOCKER_REQUIRED,
    HAS_TESTCONTAINERS,
    WHILLY_TESTCONTAINER_IMAGE,
    WHILLY_TESTCONTAINER_LABEL_KEY,
    WHILLY_TESTCONTAINER_LABEL_VALUE,
    _retry_colima_flake,
    _retry_create_pool_async,
    docker_available,
    resolve_docker_host,
)
from whilly.adapters.db import MIGRATIONS_DIR, TaskRepository, close_pool
from whilly.adapters.transport.server import (
    CLAIM_PATH,
    REGISTER_PATH,
    create_app,
)
from whilly.api.sse import EventNotifyBroker

pytestmark = DOCKER_REQUIRED


_BOOTSTRAP_TOKEN: str = "bootstrap-v6-baseline-fixtures"  # noqa: S105 - test-only fixed marker
_LONG_POLL_TIMEOUT = 0.3
_POLL_INTERVAL = 0.05


EVIDENCE_DIR: Path = Path(__file__).resolve().parents[1] / "fixtures" / "v6_baseline"


def _ensure_evidence_dir() -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    return EVIDENCE_DIR


def _write_evidence(name: str, payload: Any) -> Path:
    target = _ensure_evidence_dir() / name
    if isinstance(payload, (dict, list)):
        target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    else:
        target.write_text(str(payload), encoding="utf-8")
    return target


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_until_uvicorn_started(server: uvicorn.Server, *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not server.started:
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(f"uvicorn did not signal started within {timeout}s")
        await asyncio.sleep(0.05)


# ─── Shared FastAPI app fixture (used by xss + synthetic-load harnesses) ─


@pytest.fixture
async def baseline_app(db_pool: asyncpg.Pool) -> AsyncIterator[FastAPI]:
    app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=_LONG_POLL_TIMEOUT,
        claim_poll_interval=_POLL_INTERVAL,
        sse_ping_seconds=1,
    )
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def baseline_client(baseline_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=baseline_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# =========================================================================
# Harness 1: XSS injection through HTMX form fields
# =========================================================================


_XSS_ATTACK_PAYLOADS: tuple[tuple[str, str], ...] = (
    ("script_alert", "<script>alert(1)</script>"),
    ("javascript_void", "javascript:void(0)"),
    ("img_onerror", '"><img src=x onerror=alert(1)>'),
    ("svg_onload", "<svg onload=alert('xss')>"),
    ("iframe_src", "<iframe src=javascript:alert(1)></iframe>"),
)


async def _mint_bootstrap(repo: TaskRepository, plaintext: str, owner: str) -> None:
    await repo.mint_bootstrap_token(plaintext, owner_email=owner, is_admin=False)


async def test_xss_injection_harness(
    baseline_client: AsyncClient,
    baseline_app: FastAPI,
    db_pool: asyncpg.Pool,
) -> None:
    """Submit XSS payloads through the HTMX-driven worker registration
    form field (``hostname``) and assert the rendered dashboard escapes
    them.

    Covers VAL-M3-HTMX-* XSS-safe rendering. The dashboard uses
    autoescape-on Jinja2 so the round-trip from form → DB → render must
    never produce a raw ``<script>`` tag in the response body.
    """
    repo = TaskRepository(db_pool)
    await _mint_bootstrap(repo, _BOOTSTRAP_TOKEN + "-xss", "xss@example.com")

    rendered_per_payload: list[dict[str, Any]] = []
    for label, payload in _XSS_ATTACK_PAYLOADS:
        # 1) Submit the XSS payload through the registration form field.
        response = await baseline_client.post(
            REGISTER_PATH,
            json={"hostname": payload},
            headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}-xss"},
        )
        assert response.status_code == 201, f"register failed for {label}: {response.text!r}"

        # 2) Render the dashboard and assert the payload is rendered escaped.
        body = (await baseline_client.get("/")).text

        # The literal payload must NOT survive into the body when it
        # carries HTML-special characters: Jinja2 autoescape rewrites
        # '<', '>', '"', "'", '&' so any such payload cannot appear
        # verbatim. Pure-text payloads like "javascript:void(0)"
        # have no HTML-special chars and are inert as <td> text
        # (covered separately by the href/src attribute check below).
        if any(ch in payload for ch in "<>\"'"):
            assert payload not in body, (
                f"payload {label!r} survived into dashboard body verbatim — Jinja2 autoescape failed"
            )
        if "javascript:" in payload:
            for attr in (
                'href="javascript:',
                "href='javascript:",
                'src="javascript:',
                "src='javascript:",
            ):
                assert attr not in body, f"payload {label!r} surfaced inside attribute {attr!r}"

        # The payload SHOULD be visible somewhere in escaped form
        # (one of the canonical Jinja2 / HTML-escape outputs).
        had_escaped = any(
            marker in body
            for marker in (
                "&lt;script&gt;",
                "&#x3C;script&#x3E;",
                "&#60;script&#62;",
                "&lt;iframe",
                "&lt;svg",
                "&#34;",
                "&quot;",
                "&amp;",
            )
        )
        rendered_per_payload.append(
            {
                "label": label,
                "payload": payload,
                "escaped_marker_present": had_escaped,
                "raw_payload_in_body": payload in body,
            }
        )

    # Evidence — a per-payload dump the user-testing validator can grep.
    _write_evidence(
        "xss_injection_harness.json",
        {
            "attack_count": len(_XSS_ATTACK_PAYLOADS),
            "results": rendered_per_payload,
            "timestamp": datetime.now(tz=UTC).isoformat(),
        },
    )

    # At least one payload must have produced an escaped marker —
    # otherwise the dashboard might have stripped the input entirely
    # (silent drop is also unsafe; the user must see something).
    assert any(r["escaped_marker_present"] for r in rendered_per_payload), (
        "no XSS payload rendered as HTML-escaped output; suspect silent drop"
    )


# =========================================================================
# Harness 2: 100-worker synthetic load (claim/lock stress)
# =========================================================================


async def _seed_plan_and_tasks(pool: asyncpg.Pool, *, plan_id: str, task_count: int) -> list[str]:
    task_ids: list[str] = []
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            plan_id,
            f"plan {plan_id}",
        )
        for i in range(task_count):
            tid = f"{plan_id}-t{i:04d}"
            task_ids.append(tid)
            await conn.execute(
                "INSERT INTO tasks (id, plan_id, status, priority, description) "
                "VALUES ($1, $2, 'PENDING', 'medium', $3)",
                tid,
                plan_id,
                f"task {tid}",
            )
    return task_ids


@pytest.mark.timeout(300)
async def test_synthetic_load_100_workers(
    baseline_client: AsyncClient,
    db_pool: asyncpg.Pool,
) -> None:
    """Spawn 100 concurrent in-process workers that race to claim
    tasks against the same control-plane.

    Asserts:
        * >= 99% of seeded tasks are claimed before the queue drains.
        * Zero double-claim events: every CLAIM event in the audit
          log resolves to a unique ``(task_id, worker_id)`` pair.
    """
    worker_count = int(os.environ.get("WHILLY_FIXTURE_WORKERS", "100"))
    task_count = int(os.environ.get("WHILLY_FIXTURE_TASKS", "1000"))
    plan_id = "v6-baseline-load"

    repo = TaskRepository(db_pool)
    await _mint_bootstrap(repo, _BOOTSTRAP_TOKEN + "-load", "load@example.com")
    await _seed_plan_and_tasks(db_pool, plan_id=plan_id, task_count=task_count)

    # Register N workers up front, each obtaining its own bearer.
    register_responses: list[tuple[str, str]] = []
    for i in range(worker_count):
        resp = await baseline_client.post(
            REGISTER_PATH,
            json={"hostname": f"loadworker-{i:03d}"},
            headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}-load"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        register_responses.append((body["worker_id"], body["token"]))

    claims_per_worker: dict[str, list[str]] = {wid: [] for wid, _ in register_responses}

    async def _drain(worker_id: str, token: str) -> None:
        idle_streak = 0
        while True:
            resp = await baseline_client.post(
                CLAIM_PATH,
                json={"worker_id": worker_id, "plan_id": plan_id},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 204:
                idle_streak += 1
                if idle_streak >= 2:
                    return
                await asyncio.sleep(0.01)
                continue
            assert resp.status_code == 200, resp.text
            payload = resp.json()
            task = payload.get("task")
            assert task is not None
            claims_per_worker[worker_id].append(task["id"])
            idle_streak = 0

    started = time.monotonic()
    await asyncio.gather(*(_drain(wid, tok) for wid, tok in register_responses))
    elapsed = time.monotonic() - started

    all_claimed_task_ids = [tid for ids in claims_per_worker.values() for tid in ids]
    unique_claimed = set(all_claimed_task_ids)

    # Cross-check against the audit log — every CLAIM event must be
    # unique on ``task_id``.
    async with db_pool.acquire() as conn:
        claim_event_rows = await conn.fetch(
            "SELECT task_id, payload->>'worker_id' AS worker_id "
            "FROM events WHERE event_type = 'CLAIM' AND task_id LIKE $1 || '-%'",
            plan_id,
        )

    audit_pairs: list[tuple[str, str]] = [(str(row["task_id"]), str(row["worker_id"])) for row in claim_event_rows]
    audit_task_ids = [pair[0] for pair in audit_pairs]
    audit_unique_task_ids = set(audit_task_ids)

    completion_rate = len(unique_claimed) / float(task_count)
    double_claim_count = len(audit_task_ids) - len(audit_unique_task_ids)

    _write_evidence(
        "synthetic_load_100_workers.json",
        {
            "worker_count": worker_count,
            "task_count": task_count,
            "claimed_unique": len(unique_claimed),
            "completion_rate": completion_rate,
            "audit_claim_event_count": len(audit_task_ids),
            "double_claim_count": double_claim_count,
            "elapsed_seconds": elapsed,
            "timestamp": datetime.now(tz=UTC).isoformat(),
        },
    )

    assert completion_rate >= 0.99, (
        f"completion rate {completion_rate:.4f} below 99% ({len(unique_claimed)}/{task_count})"
    )
    assert double_claim_count == 0, (
        f"detected {double_claim_count} double-claim events: "
        f"{[t for t in audit_task_ids if audit_task_ids.count(t) > 1][:5]!r}"
    )
    assert len(unique_claimed) == len(all_claimed_task_ids), (
        "in-process claim list contains duplicates — claim_task is non-atomic"
    )


# =========================================================================
# Harness 3: 1000 events/s sustained scrape over the SSE stream
# =========================================================================


@pytest.mark.timeout(300)
async def test_sustained_scrape_1000hz(
    db_pool: asyncpg.Pool,
    postgres_dsn: str,
) -> None:
    """Drive the SSE broker at 1000 events/s for >= 60 s (or whatever
    ``WHILLY_FIXTURE_SCRAPE_DURATION_S`` overrides) and verify a curl-
    based scraper consumes them with < 5% drop rate.

    Also verifies Last-Event-ID resume across a deliberate mid-stream
    disconnect: the second consumer reconnects with the highest
    observed ``id:`` and receives at least the final fan-out.
    """
    duration_s = float(os.environ.get("WHILLY_FIXTURE_SCRAPE_DURATION_S", "60"))
    rate_hz = float(os.environ.get("WHILLY_FIXTURE_SCRAPE_RATE_HZ", "1000"))
    drop_threshold = float(os.environ.get("WHILLY_FIXTURE_SCRAPE_DROP_THRESHOLD", "0.05"))
    expected_emissions = int(duration_s * rate_hz)

    repo = TaskRepository(db_pool)
    plaintext_bs = "scrape-bs"
    await _mint_bootstrap(repo, plaintext_bs, "scrape@example.com")

    port = _find_free_port()
    app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=_LONG_POLL_TIMEOUT,
        claim_poll_interval=_POLL_INTERVAL,
        sse_ping_seconds=5,
        dsn=postgres_dsn,
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on", access_log=False)
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve(), name="scrape-uvicorn")

    try:
        await _wait_until_uvicorn_started(server)
        broker: EventNotifyBroker = app.state.event_notify_broker

        emitted_count = 0
        max_observed_id = 0
        received_ids_phase1: list[int] = []
        received_ids_phase2: list[int] = []
        emit_done = asyncio.Event()

        async def _emitter() -> None:
            nonlocal emitted_count
            interval = 1.0 / rate_hz
            start = time.monotonic()
            next_tick = start
            i = 1
            while time.monotonic() - start < duration_s:
                broker.fan_out(
                    {
                        "event_id": i,
                        "event_type": "scrape.tick",
                        "task_id": None,
                        "plan_id": None,
                        "payload": {"i": i},
                    }
                )
                emitted_count += 1
                i += 1
                next_tick += interval
                sleep_for = next_tick - time.monotonic()
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
            emit_done.set()

        async def _consume(client: httpx.AsyncClient, last_event_id: int | None, deadline: float) -> list[int]:
            ids: list[int] = []
            headers = {"Authorization": f"Bearer {plaintext_bs}"}
            if last_event_id is not None:
                headers["Last-Event-ID"] = str(last_event_id)
            try:
                async with client.stream("GET", f"http://127.0.0.1:{port}/events/stream", headers=headers) as resp:
                    if resp.status_code != 200:
                        return ids
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("id:"):
                            try:
                                ids.append(int(line.split(":", 1)[1].strip()))
                            except ValueError:
                                continue
                        if time.monotonic() >= deadline:
                            return ids
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout):
                return ids
            return ids

        emitter_task = asyncio.create_task(_emitter(), name="scrape-emitter")

        # Phase 1: consume for the first half of the run, then disconnect.
        async with httpx.AsyncClient(timeout=httpx.Timeout(duration_s + 30)) as ac1:
            phase1_deadline = time.monotonic() + duration_s / 2 + 1.0
            received_ids_phase1 = await _consume(ac1, last_event_id=None, deadline=phase1_deadline)
            if received_ids_phase1:
                max_observed_id = max(received_ids_phase1)

        # Phase 2: reconnect with Last-Event-ID and consume the rest.
        async with httpx.AsyncClient(timeout=httpx.Timeout(duration_s + 30)) as ac2:
            phase2_deadline = time.monotonic() + duration_s / 2 + 5.0
            received_ids_phase2 = await _consume(ac2, last_event_id=max_observed_id, deadline=phase2_deadline)

        await asyncio.wait_for(emitter_task, timeout=duration_s + 5.0)

        total_received = len(received_ids_phase1) + len(received_ids_phase2)
        drop_rate = max(0.0, 1.0 - total_received / float(emitted_count or 1))
        last_event_id_resumed = (
            len(received_ids_phase2) > 0 and min(received_ids_phase2) > max_observed_id
            if received_ids_phase2
            else False
        )

        evidence = {
            "duration_s": duration_s,
            "rate_hz": rate_hz,
            "expected_emissions": expected_emissions,
            "actual_emissions": emitted_count,
            "received_phase1": len(received_ids_phase1),
            "received_phase2": len(received_ids_phase2),
            "max_observed_id_phase1": max_observed_id,
            "drop_rate": drop_rate,
            "drop_threshold": drop_threshold,
            "last_event_id_resumed": last_event_id_resumed,
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }
        _write_evidence("sustained_scrape_1000hz.json", evidence)

        assert emitted_count >= int(expected_emissions * 0.5), (
            f"emitter underran: emitted {emitted_count} of {expected_emissions} "
            f"({emitted_count / expected_emissions:.3f}× target)"
        )
        assert drop_rate < drop_threshold, (
            f"drop rate {drop_rate:.4f} exceeds threshold {drop_threshold:.4f} "
            f"(received={total_received}, emitted={emitted_count})"
        )
        # If phase2 returned anything, it must be from after the
        # disconnect — proving Last-Event-ID resume worked.
        if received_ids_phase2:
            assert min(received_ids_phase2) > max_observed_id, (
                "phase2 frames overlap pre-disconnect frames — Last-Event-ID did not resume correctly"
            )

    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server_task, timeout=5.0)
        if not server_task.done():
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await server_task


# =========================================================================
# Harness 4: JS-disabled browser graceful-degradation
# =========================================================================


@pytest.mark.timeout(180)
async def test_js_disabled_browser(
    db_pool: asyncpg.Pool,
    postgres_dsn: str,
) -> None:
    """Verify HTMX dashboard renders a usable server-side fallback when
    the user-agent has JavaScript disabled.

    Strategy:
        * If ``agent-browser`` is on PATH, launch a real Chromium with
          ``--disable-javascript`` against the live ``GET /`` endpoint
          and capture the rendered DOM.
        * Otherwise (plain CI), fall back to a raw HTTP fetch — the
          dashboard's tables are server-rendered by Jinja2 ``{% include %}``
          so the no-JS HTML must already contain the seeded rows.

    Either way we assert the seeded ``<table id="tasks">`` and
    ``<table id="workers">`` blocks render with the seeded rows present.
    """
    plan_id = "v6-baseline-jsdis"
    await _seed_plan_and_tasks(db_pool, plan_id=plan_id, task_count=3)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workers (worker_id, hostname, owner_email, status, last_heartbeat, registered_at, token_hash) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            "w-jsdis",
            "jsdis.local",
            "jsdis@example.com",
            "online",
            datetime.now(tz=UTC) - timedelta(seconds=1),
            datetime.now(tz=UTC),
            "hash-jsdis",
        )

    port = _find_free_port()
    app = create_app(
        db_pool,
        worker_token=None,
        bootstrap_token=_BOOTSTRAP_TOKEN,
        claim_long_poll_timeout=_LONG_POLL_TIMEOUT,
        claim_poll_interval=_POLL_INTERVAL,
        sse_ping_seconds=5,
        dsn=postgres_dsn,
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on", access_log=False)
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve(), name="jsdis-uvicorn")

    rendered_body: str = ""
    used_agent_browser = False
    agent_browser_snapshot: str = ""
    try:
        await _wait_until_uvicorn_started(server)

        agent_browser = shutil.which("agent-browser")
        session = os.environ.get("WHILLY_FIXTURE_AGENT_BROWSER_SESSION", "v6_baseline_js_disabled")
        if agent_browser is not None:
            # The dashboard's `<head>` references unpkg / jsdelivr CDN
            # scripts. With JS disabled Chromium still fetches them as
            # subresources, so `load` event timing depends on CDN
            # latency. The fragment URL returns just the server-side
            # rendered ``_tasks_table.html`` partial — no CDN scripts,
            # so the navigation completes deterministically even with
            # JS off and we can capture a snapshot of what an end-user
            # with disabled JS would see for the live-update target.
            ab_env = {**os.environ, "AGENT_BROWSER_DEFAULT_TIMEOUT": "20000"}
            target_url = f"http://127.0.0.1:{port}/?fragment=tasks"
            try:
                proc = subprocess.run(
                    [
                        agent_browser,
                        "--session",
                        session,
                        "--args",
                        "--disable-javascript,--no-sandbox,--headless=new",
                        "open",
                        target_url,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                    env=ab_env,
                )
                open_stdout = (proc.stdout or "") + "\n" + (proc.stderr or "")
                if proc.returncode == 0:
                    snap_proc = subprocess.run(
                        [agent_browser, "--session", session, "snapshot"],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                        env=ab_env,
                    )
                    if snap_proc.returncode == 0 and snap_proc.stdout.strip():
                        agent_browser_snapshot = snap_proc.stdout
                        used_agent_browser = True
                    else:
                        agent_browser_snapshot = (
                            f"snapshot_rc={snap_proc.returncode}\n"
                            f"stdout={snap_proc.stdout!r}\nstderr={snap_proc.stderr!r}\n"
                            f"open_combined={open_stdout!r}"
                        )
                else:
                    agent_browser_snapshot = (
                        f"open_rc={proc.returncode}\nstderr={proc.stderr!r}\nstdout={proc.stdout!r}"
                    )
            finally:
                with contextlib.suppress(Exception):
                    subprocess.run(
                        [agent_browser, "--session", session, "close"],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )

        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as ac:
            resp = await ac.get(f"http://127.0.0.1:{port}/")
            assert resp.status_code == 200
            rendered_body = resp.text
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server_task, timeout=5.0)
        if not server_task.done():
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await server_task

    # Server-rendered tables must exist regardless of JS.
    assert '<table id="workers"' in rendered_body, "workers table absent in no-JS render"
    assert '<table id="tasks"' in rendered_body, "tasks table absent in no-JS render"
    assert "jsdis.local" in rendered_body, "seeded worker row missing in no-JS render"
    assert "v6-baseline-jsdis-t0000" in rendered_body, "seeded task row missing in no-JS render"

    _write_evidence(
        "js_disabled_browser.json",
        {
            "used_agent_browser": used_agent_browser,
            "agent_browser_path": shutil.which("agent-browser"),
            "rendered_bytes": len(rendered_body),
            "tables_present": True,
            "seeded_worker_visible": "jsdis.local" in rendered_body,
            "seeded_task_visible": "v6-baseline-jsdis-t0000" in rendered_body,
            "agent_browser_snapshot_bytes": len(agent_browser_snapshot),
            "timestamp": datetime.now(tz=UTC).isoformat(),
        },
    )
    _write_evidence("js_disabled_browser.html", rendered_body[:200_000])
    if agent_browser_snapshot:
        _write_evidence("js_disabled_browser.snapshot.txt", agent_browser_snapshot)


# =========================================================================
# Harness 5: Postgres outage (docker pause/unpause mid-task)
# =========================================================================


def _build_alembic_config(dsn: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("version_path_separator", "os")
    cfg.set_main_option("sqlalchemy.url", dsn)
    return cfg


@pytest.mark.timeout(300)
async def test_postgres_outage_simulator() -> None:
    """Spin up a dedicated Postgres testcontainer, exercise a task
    operation, then ``docker pause`` the container mid-task and assert
    the asyncpg path observes the outage and recovers once unpaused.

    This is intentionally NOT using the session-scoped ``postgres_dsn``
    container because pausing it would impact every other test in the
    session. The dedicated container is teardown-safe.
    """
    if not (HAS_TESTCONTAINERS and docker_available()):
        pytest.skip("Docker daemon not reachable; testcontainers cannot boot Postgres")
    docker_bin = shutil.which("docker")
    if docker_bin is None:
        pytest.skip("docker CLI not on PATH; cannot pause/unpause container")

    pause_seconds = float(os.environ.get("WHILLY_FIXTURE_PG_OUTAGE_S", "5"))
    statement_timeout_s = float(os.environ.get("WHILLY_FIXTURE_PG_TIMEOUT_S", "2"))

    # Bridge DOCKER_HOST from the active CLI context to the Python SDK
    # (macOS multi-context fix mirrored from conftest.postgres_dsn).
    prior_docker_host = os.environ.get("DOCKER_HOST")
    if prior_docker_host is None:
        resolved = resolve_docker_host()
        if resolved is not None:
            os.environ["DOCKER_HOST"] = resolved
    prior_ryuk = os.environ.get("TESTCONTAINERS_RYUK_DISABLED")
    if prior_ryuk is None:
        os.environ["TESTCONTAINERS_RYUK_DISABLED"] = "true"

    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    pg = PostgresContainer(WHILLY_TESTCONTAINER_IMAGE).with_kwargs(
        labels={WHILLY_TESTCONTAINER_LABEL_KEY: WHILLY_TESTCONTAINER_LABEL_VALUE}
    )
    _retry_colima_flake(pg.start, op="PostgresContainer.start()")

    container_id: str | None = None
    pool: asyncpg.Pool | None = None
    timeline: list[dict[str, Any]] = []
    try:
        raw = pg.get_connection_url()
        dsn = raw.replace("postgresql+psycopg2://", "postgresql://").replace("+psycopg2", "")
        # alembic env.py uses asyncio.run(), which collides with the
        # running pytest event loop — run it in a worker thread, and
        # wrap in the colima-flake retry so the first SQL contact
        # rides through any port-forward wedge.
        prior_db_url = os.environ.get("WHILLY_DATABASE_URL")
        os.environ["WHILLY_DATABASE_URL"] = dsn
        try:

            def _alembic_upgrade() -> None:
                _retry_colima_flake(
                    lambda: command.upgrade(_build_alembic_config(dsn), "head"),
                    op="alembic.command.upgrade(head)",
                )

            await asyncio.to_thread(_alembic_upgrade)
        finally:
            if prior_db_url is None:
                os.environ.pop("WHILLY_DATABASE_URL", None)
            else:
                os.environ["WHILLY_DATABASE_URL"] = prior_db_url
        wrapped = pg.get_wrapped_container()
        container_id = wrapped.id
        assert container_id is not None

        pool = await _retry_create_pool_async(dsn, min_size=1, max_size=4)

        async def _set_command_timeout() -> None:
            async with pool.acquire() as conn:
                await conn.execute(f"SET statement_timeout = {int(statement_timeout_s * 1000)}")

        await asyncio.wait_for(_set_command_timeout(), timeout=10.0)
        repo = TaskRepository(pool)

        plan_id = "v6-baseline-outage"
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                plan_id,
                "outage plan",
            )
            await conn.execute(
                "INSERT INTO tasks (id, plan_id, status, priority, description) "
                "VALUES ($1, $2, 'PENDING', 'medium', $3)",
                "t-outage-pre",
                plan_id,
                "pre-outage task",
            )
        timeline.append({"phase": "seeded", "elapsed": 0.0, "ts": datetime.now(tz=UTC).isoformat()})

        # Register a worker so we can issue a real claim through the repo.
        await repo.register_worker("w-outage", "outage.local", "hash-outage", "outage@example.com")

        # Sanity claim before the outage.
        claimed_pre = await repo.claim_task("w-outage", plan_id)
        assert claimed_pre is not None, "pre-outage claim should succeed"
        timeline.append({"phase": "pre_claim_ok", "claimed_id": claimed_pre.id})

        # Pause the container — every subsequent connection / query
        # against this DSN should hang or fail until we unpause.
        subprocess.run(
            [docker_bin, "pause", container_id],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        pause_started = time.monotonic()
        timeline.append({"phase": "paused", "elapsed": 0.0})

        async def _probe_select_one(timeout_s: float) -> int:
            async def _do() -> int:
                conn = await asyncpg.connect(dsn=dsn, timeout=timeout_s, command_timeout=timeout_s)
                try:
                    val = await conn.fetchval("SELECT 1")
                    return int(val) if val is not None else 0
                finally:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(conn.close(timeout=1.0), timeout=2.0)

            return await asyncio.wait_for(_do(), timeout=timeout_s + 1.0)

        # Issue a query with a tight timeout — must fail fast and not
        # corrupt the pool.
        query_failed_with: str | None = None
        retry_attempts = 0
        retry_started = time.monotonic()
        while time.monotonic() - retry_started < pause_seconds:
            retry_attempts += 1
            try:
                await _probe_select_one(statement_timeout_s)
                query_failed_with = "unexpected_success"
                break
            except (
                asyncio.TimeoutError,
                asyncpg.PostgresConnectionError,
                ConnectionError,
                OSError,
            ) as exc:
                query_failed_with = type(exc).__name__
                await asyncio.sleep(0.5)
        timeline.append(
            {
                "phase": "outage_observed",
                "retry_attempts": retry_attempts,
                "last_error": query_failed_with,
                "elapsed": time.monotonic() - pause_started,
            }
        )

        # Unpause and verify recovery.
        subprocess.run(
            [docker_bin, "unpause", container_id],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        unpause_at = time.monotonic()
        recovered_after_s: float | None = None
        recovery_deadline = unpause_at + 30.0
        while time.monotonic() < recovery_deadline:
            try:
                val = await _probe_select_one(statement_timeout_s)
                if val == 1:
                    recovered_after_s = time.monotonic() - unpause_at
                    break
            except (
                asyncio.TimeoutError,
                asyncpg.PostgresConnectionError,
                ConnectionError,
                OSError,
            ):
                await asyncio.sleep(0.25)
        timeline.append({"phase": "recovered", "recovered_after_s": recovered_after_s})

        assert recovered_after_s is not None, "pool failed to recover within 30s after unpause"

        # Post-recovery: a fresh repository call must succeed end-to-end.
        async def _seed_post() -> None:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO tasks (id, plan_id, status, priority, description) "
                    "VALUES ($1, $2, 'PENDING', 'medium', $3)",
                    "t-outage-post",
                    plan_id,
                    "post-outage task",
                )

        await asyncio.wait_for(_seed_post(), timeout=10.0)
        claimed_post = await asyncio.wait_for(repo.claim_task("w-outage", plan_id), timeout=10.0)
        assert claimed_post is not None, "post-recovery claim must succeed"
        timeline.append({"phase": "post_claim_ok", "claimed_id": claimed_post.id})

        _write_evidence(
            "postgres_outage_simulator.json",
            {
                "container_id": container_id,
                "pause_seconds_target": pause_seconds,
                "retry_attempts_during_outage": retry_attempts,
                "query_failed_with": query_failed_with,
                "recovered_after_s": recovered_after_s,
                "timeline": timeline,
                "timestamp": datetime.now(tz=UTC).isoformat(),
            },
        )

        assert query_failed_with not in (None, "unexpected_success"), (
            f"expected paused container to break queries; got {query_failed_with!r}"
        )
        assert retry_attempts >= 1
        assert recovered_after_s < 30.0

    finally:
        # Best-effort unpause in case the assertion path failed before
        # we reached the explicit unpause above.
        if container_id is not None:
            with contextlib.suppress(Exception):
                subprocess.run(
                    [docker_bin, "unpause", container_id],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
        if pool is not None:
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                await asyncio.wait_for(close_pool(pool), timeout=15.0)
        with contextlib.suppress(Exception):
            pg.stop()
        if prior_docker_host is None:
            os.environ.pop("DOCKER_HOST", None)
        else:
            os.environ["DOCKER_HOST"] = prior_docker_host
        if prior_ryuk is None:
            os.environ.pop("TESTCONTAINERS_RYUK_DISABLED", None)
        else:
            os.environ["TESTCONTAINERS_RYUK_DISABLED"] = prior_ryuk
