"""Integration test for ``whilly init`` end-to-end (TASK-104a-5).

Drives the entire v4 PRD-wizard surface against a real Postgres
instance booted via testcontainers + a real Claude CLI substitute
(``tests/fixtures/fake_claude_prd.sh``). Closes SC-1 / SC-2 / SC-6 of
``docs/PRD-v41-prd-wizard-port.md``.

What's exercised end-to-end
---------------------------

1. ``whilly init "<idea>" --headless --slug X`` argv parsing
   (whilly.cli.init._build_parser).
2. Headless dispatch → ``prd_generator.generate_prd`` →
   ``_call_claude`` → spawn ``CLAUDE_BIN`` subprocess (the stub) →
   parse stdout → write ``docs/PRD-X.md``.
3. ``prd_generator.generate_tasks_dict`` → another subprocess to the
   stub (different prompt → tasks JSON) → return validated payload.
4. ``parse_plan_dict`` → shape-check the payload.
5. ``whilly.cli.plan._async_import`` → open asyncpg pool → INSERT
   ``plans`` row + ``tasks`` rows in one transaction → close pool.
6. Assertions: PRD file on disk, plan row in Postgres, task rows
   match payload, no events rows yet (worker not run).

What's NOT exercised here (and why)
-----------------------------------

* Worker run loop / task completion — TASK-104a-5 only covers the
  plan-import surface; running the seeded plan is the job of the
  worker tests (test_phase4_e2e for local, test_phase5_remote for
  remote). Mixing them in here would multiply the test surface
  without adding signal.
* Interactive (TTY) path — ``prd_launcher.run_prd_wizard`` opens
  Claude in foreground and reads stdin from a real terminal; you
  can't drive that from pytest without faking the TTY itself.
  Headless covers the same wire shape (PRD file → tasks payload →
  DB) so headless e2e + the unit tests for argparse / mode-switching
  give us full coverage of init's surface.

Why a single test rather than several
-------------------------------------

The whole point of the e2e gate is that the *composition* works
end-to-end. Splitting "PRD generated", "plan inserted", "tasks
match payload" into three separate tests would need three pytest
orchestrations of the same testcontainers Postgres + ``whilly init``
subprocess invocation — same setup, same cleanup, same wallclock
cost. One linear test asserting the full chain reads as the integration
contract: when this fails, you fix the broken link, not three.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED

pytestmark = DOCKER_REQUIRED


REPO_ROOT: Path = Path(__file__).resolve().parents[2]
FAKE_CLAUDE_PRD: Path = REPO_ROOT / "tests" / "fixtures" / "fake_claude_prd.sh"
PLAN_SLUG = "init-e2e-test"
PLAN_PROJECT = "fake-init-project"  # matches what fake_claude_prd.sh emits


@pytest.fixture(autouse=True)
def _reset_db(db_pool: asyncpg.Pool) -> None:
    """Sanity: db_pool fixture truncates everything; this is just a guard.

    The shared db_pool fixture in tests/conftest.py already TRUNCATEs
    on setup. Declaring it as an autouse dep here makes the dependency
    explicit so the test reads top-down.
    """
    return None


@pytest.fixture
def isolated_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the whole test in a tmp cwd so PRD files don't pollute the repo.

    ``whilly init --output-dir docs`` resolves the dir relative to the
    process cwd. Without this fixture the test would write to the
    real ``docs/`` and leave a stray PRD-init-e2e-test.md after each
    run. Chdir into ``tmp_path`` keeps everything contained.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    return tmp_path


@pytest.fixture
def database_url(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """Set WHILLY_DATABASE_URL for the duration of one test.

    cli.init reads it from env in the production code path. Tests that
    inject fake plan_inserter don't need this fixture; the e2e test
    that drives the real inserter does.
    """
    monkeypatch.setenv("WHILLY_DATABASE_URL", postgres_dsn)
    return postgres_dsn


@pytest.fixture
def fake_claude_env(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CLAUDE_BIN at the PRD-aware stub.

    Sanity-check the fixture stays executable — a previous git pull
    can drop the +x bit on shell scripts on filesystems that don't
    preserve mode (Windows-shared volumes, some FUSE mounts), and the
    failure mode (Permission denied from subprocess) is a confusing
    way to learn the file lost its bit.
    """
    assert FAKE_CLAUDE_PRD.exists(), f"fixture missing: {FAKE_CLAUDE_PRD}"
    assert os.access(FAKE_CLAUDE_PRD, os.X_OK), (
        f"fixture lost its executable bit: {FAKE_CLAUDE_PRD}; run `chmod +x` to restore"
    )
    monkeypatch.setenv("CLAUDE_BIN", str(FAKE_CLAUDE_PRD))
    return FAKE_CLAUDE_PRD


async def test_init_headless_creates_prd_and_imports_plan(
    isolated_workdir: Path,
    database_url: str,
    fake_claude_env: Path,
    db_pool: asyncpg.Pool,
) -> None:
    """SC-1 of PRD: headless init produces PRD on disk + plan in Postgres.

    Drives the CLI entry point as a subprocess so the test exercises
    the same path an operator sees: argv parsing, dispatcher routing,
    composition root, real Claude subprocess, real DB writes.
    """
    cmd = [
        sys.executable,
        "-m",
        "whilly.cli",
        "init",
        "build a synthetic CLI tool",
        "--headless",
        "--slug",
        PLAN_SLUG,
    ]
    # Pass the parent env down so CLAUDE_BIN + WHILLY_DATABASE_URL +
    # PYTHONPATH all reach the subprocess. capture_output for diag.
    result = subprocess.run(
        cmd,
        cwd=isolated_workdir,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    diag = (
        f"\n--- whilly init exit={result.returncode} ---\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}\n--- end ---"
    )

    assert result.returncode == 0, f"whilly init failed{diag}"
    assert "Plan 'init-e2e-test' imported" in result.stdout, diag
    assert "whilly run --plan init-e2e-test" in result.stdout, diag

    # PRD on disk.
    prd_path = isolated_workdir / "docs" / f"PRD-{PLAN_SLUG}.md"
    assert prd_path.exists(), f"PRD not written at {prd_path}{diag}"
    prd_text = prd_path.read_text(encoding="utf-8")
    assert "PRD: fake init project" in prd_text, "PRD content missing expected H1"
    assert "## Goals" in prd_text, "PRD content missing Goals section"

    # Plan row in Postgres.
    async with db_pool.acquire() as conn:
        plan_row = await conn.fetchrow("SELECT id, name FROM plans WHERE id = $1", PLAN_SLUG)
    assert plan_row is not None, f"plan {PLAN_SLUG!r} not in plans table{diag}"
    assert plan_row["id"] == PLAN_SLUG
    assert plan_row["name"] == PLAN_PROJECT  # matches what fake_claude emits

    # Task rows in Postgres — exactly the one task the stub emits.
    async with db_pool.acquire() as conn:
        task_rows = await conn.fetch(
            "SELECT id, status, priority, description FROM tasks WHERE plan_id = $1 ORDER BY id",
            PLAN_SLUG,
        )
    assert len(task_rows) == 1, f"expected 1 task, got {len(task_rows)}{diag}"
    task = task_rows[0]
    assert task["id"] == "TASK-FAKE-001"
    assert task["status"] == "PENDING"  # parse_plan_dict normalises lower → upper
    assert task["priority"] == "critical"
    assert "Synthetic task" in task["description"]

    # No events yet — worker hasn't run; init only imports.
    async with db_pool.acquire() as conn:
        event_count = await conn.fetchval(
            "SELECT COUNT(*) FROM events e JOIN tasks t ON t.id = e.task_id WHERE t.plan_id = $1",
            PLAN_SLUG,
        )
    assert event_count == 0, f"expected no events, got {event_count}{diag}"


async def test_init_headless_no_import_skips_db(
    isolated_workdir: Path,
    fake_claude_env: Path,
    db_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC-2 of PRD: --no-import writes PRD but leaves Postgres untouched.

    No WHILLY_DATABASE_URL fixture needed — --no-import skips the DB
    code path entirely, so the env-error guard in cli.init shouldn't
    fire either.
    """
    monkeypatch.delenv("WHILLY_DATABASE_URL", raising=False)

    cmd = [
        sys.executable,
        "-m",
        "whilly.cli",
        "init",
        "another idea",
        "--headless",
        "--no-import",
        "--slug",
        "no-import-test",
    ]
    result = subprocess.run(
        cmd,
        cwd=isolated_workdir,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    diag = (
        f"\n--- whilly init exit={result.returncode} ---\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}\n--- end ---"
    )
    assert result.returncode == 0, f"whilly init --no-import failed{diag}"
    assert "--no-import was set" in result.stdout, diag

    # PRD on disk.
    prd_path = isolated_workdir / "docs" / "PRD-no-import-test.md"
    assert prd_path.exists()

    # No plan in DB — the import step was skipped.
    async with db_pool.acquire() as conn:
        plan_count = await conn.fetchval("SELECT COUNT(*) FROM plans WHERE id = $1", "no-import-test")
    assert plan_count == 0, f"--no-import should not write to plans table{diag}"


async def test_init_existing_prd_without_force_aborts(
    isolated_workdir: Path,
    database_url: str,
    fake_claude_env: Path,
    db_pool: asyncpg.Pool,
) -> None:
    """SC-6 / FR-7: re-running with same slug + existing PRD aborts."""
    # Pre-create a PRD for the slug we'll try to use.
    existing_prd = isolated_workdir / "docs" / "PRD-existing-slug.md"
    existing_prd.write_text("pre-existing PRD\n", encoding="utf-8")

    cmd = [
        sys.executable,
        "-m",
        "whilly.cli",
        "init",
        "fresh idea",
        "--headless",
        "--slug",
        "existing-slug",
    ]
    result = subprocess.run(
        cmd,
        cwd=isolated_workdir,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1, f"expected exit 1, got {result.returncode}: {result.stderr}"
    assert "already exists" in result.stderr
    assert "--force" in result.stderr

    # PRD content unchanged (wizard never ran).
    assert existing_prd.read_text(encoding="utf-8") == "pre-existing PRD\n"

    # No plan written.
    async with db_pool.acquire() as conn:
        plan_count = await conn.fetchval("SELECT COUNT(*) FROM plans WHERE id = $1", "existing-slug")
    assert plan_count == 0
