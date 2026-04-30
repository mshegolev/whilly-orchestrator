"""Integration tests for Flow A (intake + strict apply) cross-flow events.

Pins VAL-CROSS-003 / VAL-CROSS-004 / VAL-CROSS-005 (M3 fix-feature):

* The full Flow A scenario produces the contract event distribution
  exactly: ``{plan.created:1, task.created:N, task.skipped:2,
  plan.applied:1}`` for a 5-task forged plan with 2 REJECT-verdict
  tasks (VAL-CROSS-004).
* ``plan apply --strict`` skips REJECT-verdict tasks via
  :meth:`TaskRepository.skip_task` writing the canonical
  ``task.skipped`` literal with ``plan_id`` populated
  (VAL-CROSS-003).
* Re-running intake + strict apply on the same issue ref keeps
  ``plan.created`` count at 1, ``task.created`` count unchanged,
  ``task.skipped`` count unchanged across reruns. ``plan.applied``
  accumulates one row per apply call — by design, not constrained
  by VAL-CROSS-005's idempotency invariant (VAL-CROSS-005).

Implementation pattern
----------------------
Forge intake runs against a stubbed ``tasks_builder`` seam that
returns a deterministic 5-task plan (3 healthy + 2 with empty
``acceptance_criteria``). The same plan dict is also serialised to
disk so ``whilly plan apply --strict <file>`` re-targets the same
plan id. ``ON CONFLICT (id) DO NOTHING`` on both the plan and task
INSERTs in :func:`whilly.cli.plan._insert_plan_and_tasks` makes the
apply path a strict-gate-only operation against the rows that
intake already committed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db import close_pool, create_pool
from whilly.cli.plan import (
    DATABASE_URL_ENV,
    EXIT_OK,
    run_plan_command,
)
from whilly.forge import _gh as forge_gh
from whilly.forge import intake as forge_intake

pytestmark = DOCKER_REQUIRED


# ── Plan payload: 5 tasks, 2 REJECT verdicts (empty acceptance_criteria) ──
def _flow_a_plan(plan_id: str) -> dict[str, Any]:
    """Deterministic 5-task plan with two REJECT tasks (empty AC)."""
    return {
        "plan_id": plan_id,
        "project": "Flow A Cross-Strict Smoke",
        "tasks": [
            {
                "id": "T-FLOW-OK-1",
                "status": "PENDING",
                "priority": "high",
                "description": "Implement the feature flag rollout for the dashboard.",
                "dependencies": [],
                "key_files": [],
                "acceptance_criteria": ["dashboard shows the flag"],
                "test_steps": ["pytest -k dashboard"],
                "prd_requirement": "",
            },
            {
                "id": "T-FLOW-OK-2",
                "status": "PENDING",
                "priority": "medium",
                "description": "Wire telemetry into the new feature flag flow.",
                "dependencies": [],
                "key_files": [],
                "acceptance_criteria": ["telemetry emits one event per click"],
                "test_steps": ["pytest -k telemetry"],
                "prd_requirement": "",
            },
            {
                "id": "T-FLOW-OK-3",
                "status": "PENDING",
                "priority": "medium",
                "description": "Document the rollout plan in the operator runbook.",
                "dependencies": [],
                "key_files": [],
                "acceptance_criteria": ["runbook updated"],
                "test_steps": ["pytest -k docs"],
                "prd_requirement": "",
            },
            {
                "id": "T-FLOW-BAD-1",
                "status": "PENDING",
                "priority": "medium",
                "description": "Refactor logging utilities to use structlog throughout.",
                "dependencies": [],
                "key_files": [],
                "acceptance_criteria": [],  # ← gate REJECT
                "test_steps": ["pytest -k logging"],
                "prd_requirement": "",
            },
            {
                "id": "T-FLOW-BAD-2",
                "status": "PENDING",
                "priority": "low",
                "description": "Audit the worker shutdown signal handling for races.",
                "dependencies": [],
                "key_files": [],
                "acceptance_criteria": [],  # ← gate REJECT
                "test_steps": ["pytest -k shutdown"],
                "prd_requirement": "",
            },
        ],
    }


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["gh"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _canned_issue_payload(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": "[mission-test] flow A cross-strict smoke",
        "body": "Forge intake → strict apply end-to-end fingerprint pin.",
        "labels": [{"name": "whilly-pending"}],
        "comments": [],
        "state": "OPEN",
        "url": f"https://github.com/example/repo/issues/{number}",
    }


async def _run_intake(argv: list[str], **kwargs: Any) -> int:
    return await asyncio.to_thread(
        forge_intake.run_forge_intake_command,
        argv,
        **kwargs,
    )


def _make_prd_runner(prd_text: str = "# PRD: flow A smoke\n\n## Goals\n\nFlow A.\n"):
    """Return a stubbed PRD runner that writes a fixed markdown body."""

    def _stub(*, idea: str, slug: str, output_dir: Path, model: str) -> None:
        del idea, model
        target = Path(output_dir) / f"PRD-{slug}.md"
        target.write_text(prd_text, encoding="utf-8")

    return _stub


def _make_tasks_builder(plan_id: str):
    """Return a stubbed tasks-builder that returns the deterministic Flow A plan."""

    def _stub(*, prd_path: Path, plan_id: str, model: str) -> dict[str, Any]:
        del prd_path, model
        return _flow_a_plan(plan_id)

    return _stub


# ── Fixtures ─────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_db(db_pool: asyncpg.Pool) -> None:
    """Force the autouse db_pool fixture so each test gets a TRUNCATEd schema."""
    return None


@pytest.fixture
def isolated_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    return tmp_path


@pytest.fixture
def database_url(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Set ``WHILLY_DATABASE_URL`` for the test."""
    prior = os.environ.get(DATABASE_URL_ENV)
    monkeypatch.setenv(DATABASE_URL_ENV, postgres_dsn)
    yield postgres_dsn
    if prior is None:
        os.environ.pop(DATABASE_URL_ENV, None)
    else:
        os.environ[DATABASE_URL_ENV] = prior


@pytest.fixture
def gh_recorder(monkeypatch: pytest.MonkeyPatch):
    """Minimal gh recorder mirroring :mod:`tests.integration.test_forge_intake`."""

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.responses: list[subprocess.CompletedProcess[str]] = []

        def queue(self, response: subprocess.CompletedProcess[str]) -> None:
            self.responses.append(response)

        def __call__(
            self,
            args: list[str],
            *,
            timeout: float = forge_gh.DEFAULT_GH_TIMEOUT_SECONDS,
        ) -> subprocess.CompletedProcess[str]:
            del timeout
            if shutil.which("gh") is None:
                raise forge_gh.GHCLIMissingError(
                    "gh CLI is not on PATH; install via `brew install gh` or see https://cli.github.com."
                )
            self.calls.append(list(args))
            if not self.responses:
                raise AssertionError(f"gh_recorder: no canned response queued for invocation {args!r}")
            return self.responses.pop(0)

    rec = _Recorder()
    monkeypatch.setattr(forge_gh, "_run_gh", rec)
    return rec


def _query_db_sync(postgres_dsn: str, sql: str, *args: object) -> list[asyncpg.Record]:
    async def _go() -> list[asyncpg.Record]:
        pool = await create_pool(postgres_dsn)
        try:
            async with pool.acquire() as conn:
                return await conn.fetch(sql, *args)
        finally:
            await close_pool(pool)

    return asyncio.run(_go())


def _write_plan_to_disk(tmp_path: Path, plan_id: str) -> Path:
    target = tmp_path / "flow-a-plan.json"
    target.write_text(json.dumps(_flow_a_plan(plan_id)), encoding="utf-8")
    return target


# ── VAL-CROSS-003 + VAL-CROSS-004: full Flow A fingerprint ───────────────
async def test_flow_a_event_fingerprint_intake_plus_strict_apply(
    monkeypatch: pytest.MonkeyPatch,
    isolated_workdir: Path,
    database_url: str,
    db_pool: asyncpg.Pool,
    gh_recorder,
) -> None:
    """Flow A produces ``{plan.created:1, task.created:5, task.skipped:2, plan.applied:1}``.

    Pins VAL-CROSS-003 (skip_task writes ``task.skipped`` with
    ``plan_id`` populated) and VAL-CROSS-004 (per-flow event
    fingerprint within 200 ms of the API call).
    """
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: "/usr/local/bin/gh")
    issue_number = 401
    plan_slug = forge_intake._slug_for_issue("owner", "repo", issue_number)

    # Queue gh issue view + gh issue edit for the intake call.
    gh_recorder.queue(_completed(stdout=json.dumps(_canned_issue_payload(issue_number))))
    gh_recorder.queue(_completed(stdout=f"https://github.com/example/repo/issues/{issue_number}"))

    # Step 1: forge intake — produces 1 plan.created + 5 task.created events.
    rc_intake = await _run_intake(
        [f"owner/repo/{issue_number}"],
        prd_runner=_make_prd_runner(),
        tasks_builder=_make_tasks_builder(plan_slug),
    )
    assert rc_intake == forge_intake.EXIT_OK

    # Step 2: write the equivalent plan JSON to disk for `whilly plan apply --strict`.
    plan_file = _write_plan_to_disk(isolated_workdir, plan_slug)

    # Run apply --strict in a worker thread (CLI uses asyncio.run internally).
    rc_apply = await asyncio.to_thread(run_plan_command, ["apply", "--strict", str(plan_file)])
    assert rc_apply == EXIT_OK

    # ── VAL-CROSS-004: event fingerprint exactly matches contract ─────
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_type, count(*)::int AS c FROM events WHERE plan_id=$1 GROUP BY event_type",
            plan_slug,
        )
    distribution = {r["event_type"]: r["c"] for r in rows}
    assert distribution == {
        "plan.created": 1,
        "task.created": 5,
        "task.skipped": 2,
        "plan.applied": 1,
    }, f"Flow A event distribution drifted: {distribution}"

    # ── VAL-CROSS-003: task.skipped rows carry plan_id + reason ──────
    async with db_pool.acquire() as conn:
        skipped_rows = await conn.fetch(
            "SELECT task_id, plan_id, payload FROM events "
            "WHERE plan_id=$1 AND event_type='task.skipped' ORDER BY task_id",
            plan_slug,
        )
    assert len(skipped_rows) == 2
    assert {r["task_id"] for r in skipped_rows} == {"T-FLOW-BAD-1", "T-FLOW-BAD-2"}
    for row in skipped_rows:
        assert row["plan_id"] == plan_slug
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        assert payload["reason"] == "decision_gate_failed"
        assert isinstance(payload["missing"], list) and len(payload["missing"]) >= 1

    # Anti-regression: no rows of the legacy uppercase 'SKIP' literal.
    async with db_pool.acquire() as conn:
        legacy_count = await conn.fetchval(
            "SELECT count(*) FROM events WHERE event_type='SKIP'",
        )
    assert legacy_count == 0

    # ── VAL-CROSS-004 latency: every per-plan event has been
    # observable within 200 ms of the apply call returning. The
    # apply path commits its own transactions before returning, so
    # row visibility is post-commit; we measure observed latency on
    # the most recent row (== the plan.applied row).
    async with db_pool.acquire() as conn:
        max_age_seconds = await conn.fetchval(
            "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(created_at))) FROM events WHERE plan_id=$1",
            plan_slug,
        )
    assert max_age_seconds is not None
    assert float(max_age_seconds) < 0.2, (
        f"VAL-CROSS-004 latency budget breached: {float(max_age_seconds) * 1000:.1f} ms"
    )


# ── VAL-CROSS-005: idempotent re-run keeps the contract counts stable ──
async def test_flow_a_idempotent_rerun_keeps_event_counts_stable(
    monkeypatch: pytest.MonkeyPatch,
    isolated_workdir: Path,
    database_url: str,
    db_pool: asyncpg.Pool,
    gh_recorder,
) -> None:
    """Re-running intake + strict apply keeps the contract counts stable.

    Pins VAL-CROSS-005:
      * ``plan.created`` count remains 1 across both runs.
      * ``task.created`` count remains 5 across both runs (ON
        CONFLICT DO NOTHING gates duplicate event emission).
      * ``task.skipped`` count remains 2 across both runs (the
        idempotency probe in :meth:`TaskRepository.skip_task`
        short-circuits once a task is SKIPPED).

    ``plan.applied`` accumulates one row per apply call by design;
    the contract does NOT constrain its rerun count, so we leave
    that channel free.
    """
    monkeypatch.setattr(shutil, "which", lambda *_args, **_kwargs: "/usr/local/bin/gh")
    issue_number = 402
    plan_slug = forge_intake._slug_for_issue("owner", "repo", issue_number)

    # Run 1: intake + apply --strict.
    gh_recorder.queue(_completed(stdout=json.dumps(_canned_issue_payload(issue_number))))
    gh_recorder.queue(_completed(stdout=f"https://github.com/example/repo/issues/{issue_number}"))
    rc1_intake = await _run_intake(
        [f"owner/repo/{issue_number}"],
        prd_runner=_make_prd_runner(),
        tasks_builder=_make_tasks_builder(plan_slug),
    )
    assert rc1_intake == forge_intake.EXIT_OK

    plan_file = _write_plan_to_disk(isolated_workdir, plan_slug)
    rc1_apply = await asyncio.to_thread(run_plan_command, ["apply", "--strict", str(plan_file)])
    assert rc1_apply == EXIT_OK

    # Snapshot counts after run 1.
    async with db_pool.acquire() as conn:
        rows1 = await conn.fetch(
            "SELECT event_type, count(*)::int AS c FROM events WHERE plan_id=$1 GROUP BY event_type",
            plan_slug,
        )
    counts_run1 = {r["event_type"]: r["c"] for r in rows1}
    assert counts_run1["plan.created"] == 1
    assert counts_run1["task.created"] == 5
    assert counts_run1["task.skipped"] == 2
    assert counts_run1["plan.applied"] == 1

    # Run 2: same issue ref + same plan file. Intake idempotent
    # short-circuit returns existing plan; no gh invocation should
    # occur this time. apply --strict iterates the plan again.
    rc2_intake = await _run_intake(
        [f"owner/repo/{issue_number}"],
        prd_runner=_make_prd_runner(),
        tasks_builder=_make_tasks_builder(plan_slug),
    )
    assert rc2_intake == forge_intake.EXIT_OK

    rc2_apply = await asyncio.to_thread(run_plan_command, ["apply", "--strict", str(plan_file)])
    assert rc2_apply == EXIT_OK

    # Snapshot counts after run 2.
    async with db_pool.acquire() as conn:
        rows2 = await conn.fetch(
            "SELECT event_type, count(*)::int AS c FROM events WHERE plan_id=$1 GROUP BY event_type",
            plan_slug,
        )
    counts_run2 = {r["event_type"]: r["c"] for r in rows2}

    # Idempotency invariants per VAL-CROSS-005:
    assert counts_run2["plan.created"] == 1, "plan.created must remain 1 across reruns"
    assert counts_run2["task.created"] == 5, "task.created must remain 5 across reruns (ON CONFLICT DO NOTHING)"
    assert counts_run2["task.skipped"] == 2, "task.skipped must remain 2 across reruns (idempotency probe)"
    # plan.applied accumulates one row per apply call — contract
    # explicitly does NOT constrain this.
    assert counts_run2["plan.applied"] == 2

    # Defence in depth: zero legacy 'SKIP' rows even after the rerun.
    async with db_pool.acquire() as conn:
        legacy_count = await conn.fetchval(
            "SELECT count(*) FROM events WHERE event_type='SKIP'",
        )
    assert legacy_count == 0
