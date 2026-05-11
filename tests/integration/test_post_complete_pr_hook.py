"""Integration: post-COMPLETE PR opener hook end-to-end (VAL-PR-005..008).

Drives a single task to COMPLETE through the local-worker CLI
(``whilly run``), with subprocess.run mocked so the ``git push`` /
``gh pr create`` calls inside ``open_pr_for_task`` produce
deterministic outputs without touching real GitHub. Asserts:

* ``WHILLY_AUTO_OPEN_PR=1`` AND ``plans.github_issue_ref`` set →
  exactly one ``gh pr create`` invocation, one ``pull_requests`` row
  with the returned ``pr_url`` / ``branch`` / ``task_id``, and one
  ``pr.opened`` event in both Postgres ``events`` and the JSONL
  mirror with the documented ``detail`` keys.

* ``WHILLY_AUTO_OPEN_PR`` unset → zero PR-opener invocations, zero
  ``pull_requests`` rows, zero ``pr.opened`` events; existing
  ``done`` behaviour is preserved bit-for-bit.

* ``WHILLY_AUTO_OPEN_PR=1`` but ``plans.github_issue_ref`` NULL →
  zero PR-opener invocations, zero ``pull_requests`` rows, zero
  ``pr.opened`` events, no warning event.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.runner.result_parser import AgentResult, AgentUsage
from whilly.audit import DEFAULT_JSONL_FILENAME, LOG_DIR_ENV
from whilly.cli.run import EXIT_OK, run_run_command
from whilly.core.models import Task
from whilly.sinks import github_pr as gp

pytestmark = DOCKER_REQUIRED


PLAN_ID = "PLAN-PR-HOOK-INTG-1"
TASK_ID = "T-PR-HOOK-INTG-1"


@pytest.fixture
def db_url(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("WHILLY_DATABASE_URL", postgres_dsn)
    yield postgres_dsn


@pytest.fixture
def whilly_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    log_dir = tmp_path / "whilly_logs"
    monkeypatch.setenv(LOG_DIR_ENV, str(log_dir))
    return log_dir


@pytest.fixture
def fake_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a clean tiny Git repository the hook will treat as the worktree.

    The hook in :mod:`whilly.cli.run` resolves ``Path.cwd()`` at
    construction time, so we change the process CWD to the temporary
    worktree before invoking the CLI. PR push / create subprocess calls
    are mocked, but rollback preflight inspects real local Git state.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init"], cwd=worktree, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "whilly-test@example.invalid"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Whilly Test"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    (worktree / "README.md").write_text("test repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=worktree, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=worktree, check=True, capture_output=True, text=True)
    monkeypatch.chdir(worktree)
    return worktree


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


async def _seed_plan_with_task(
    pool: asyncpg.Pool,
    plan_id: str,
    task_id: str,
    *,
    github_issue_ref: str | None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO plans (id, name, github_issue_ref) VALUES ($1, $2, $3)",
            plan_id,
            f"plan {plan_id}",
            github_issue_ref,
        )
        await conn.execute(
            """
            INSERT INTO tasks (
                id, plan_id, status, dependencies, key_files,
                priority, description, acceptance_criteria,
                test_steps, prd_requirement, version
            )
            VALUES ($1, $2, 'PENDING', '[]'::jsonb, '[]'::jsonb,
                    'high', $3, '[]'::jsonb, '[]'::jsonb, $4, 0)
            """,
            task_id,
            plan_id,
            "Add /health endpoint returning ok",
            "https://github.com/foo/bar/issues/42",
        )


async def _fake_runner_complete(task: Task, prompt: str) -> AgentResult:  # noqa: ARG001
    return AgentResult(
        usage=AgentUsage(),
        exit_code=0,
        is_complete=True,
        output=f"<promise>COMPLETE</promise> for {task.id}",
    )


def _read_jsonl_lines(jsonl_path: Path) -> list[dict[str, object]]:
    if not jsonl_path.is_file():
        return []
    raw = jsonl_path.read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.split("\n") if line.strip()]


def _make_subprocess_recorder(pr_url: str = "https://github.com/foo/bar/pull/77"):
    """Build a fake ``_run`` recorder that returns canned successes."""
    push = _Proc(0, "")
    pr = _Proc(0, f"{pr_url}\n")
    captured: list[list[str]] = []

    def fake_run(cmd, cwd, timeout=60):  # noqa: ARG001
        captured.append(list(cmd))
        return push if cmd[0] == "git" else pr

    return fake_run, captured


# ---------------------------------------------------------------------------
# VAL-PR-005 + VAL-PR-006: hook fires, row + pr.opened present in both sinks
# ---------------------------------------------------------------------------


async def test_hook_fires_when_env_on_and_issue_ref_set(
    db_pool: asyncpg.Pool,
    db_url: str,
    whilly_log_dir: Path,
    fake_worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHILLY_AUTO_OPEN_PR", "1")
    await _seed_plan_with_task(
        db_pool,
        PLAN_ID,
        TASK_ID,
        github_issue_ref="foo/bar/42",
    )
    fake_run, captured = _make_subprocess_recorder()
    monkeypatch.setattr(gp, "_run", fake_run)

    exit_code = await asyncio.to_thread(
        run_run_command,
        ["--plan", PLAN_ID, "--max-iterations", "5", "--idle-wait", "0.01", "--heartbeat-interval", "60.0"],
        runner=_fake_runner_complete,
        install_signal_handlers=False,
    )
    assert exit_code == EXIT_OK

    gh_create_calls = [cmd for cmd in captured if cmd[:3] == ["gh", "pr", "create"]]
    assert len(gh_create_calls) == 1, f"expected 1 gh pr create call, got {len(gh_create_calls)}: {captured!r}"
    gh_argv = gh_create_calls[0]
    head_idx = gh_argv.index("--head") + 1
    branch = gh_argv[head_idx]
    assert branch.startswith("whilly/"), f"unexpected branch on argv: {branch!r}"

    async with db_pool.acquire() as conn:
        pr_rows = await conn.fetch("SELECT * FROM pull_requests WHERE plan_id = $1", PLAN_ID)
        events = await conn.fetch(
            "SELECT event_type, payload FROM events WHERE task_id = $1 ORDER BY id",
            TASK_ID,
        )

    assert len(pr_rows) == 1, f"expected 1 pull_requests row, got {len(pr_rows)}"
    pr_row = pr_rows[0]
    assert pr_row["plan_id"] == PLAN_ID
    assert pr_row["task_id"] == TASK_ID
    assert pr_row["pr_url"] == "https://github.com/foo/bar/pull/77"
    assert pr_row["pr_number"] == 77
    assert pr_row["branch"] == branch
    assert pr_row["state"] == "open"

    pr_opened = [e for e in events if e["event_type"] == "pr.opened"]
    pr_failed = [e for e in events if e["event_type"] == "pr.open_failed"]
    assert len(pr_opened) == 1, f"expected one pr.opened, got {[e['event_type'] for e in events]!r}"
    assert pr_failed == []
    pg_payload = pr_opened[0]["payload"]
    if isinstance(pg_payload, str):
        pg_payload = json.loads(pg_payload)
    for key in ("pr_url", "pr_number", "branch", "head_sha", "task_id"):
        assert key in pg_payload, f"detail missing {key!r}: {pg_payload!r}"
    assert pg_payload["pr_url"] == "https://github.com/foo/bar/pull/77"
    assert pg_payload["pr_number"] == 77
    assert pg_payload["task_id"] == TASK_ID

    jsonl_lines = _read_jsonl_lines(whilly_log_dir / DEFAULT_JSONL_FILENAME)
    pr_opened_jsonl = [line for line in jsonl_lines if line["event_type"] == "pr.opened"]
    assert len(pr_opened_jsonl) == 1, f"expected one pr.opened JSONL line, got {len(pr_opened_jsonl)}"
    jsonl_payload = pr_opened_jsonl[0]["payload"]
    assert jsonl_payload == pg_payload, "Postgres and JSONL payload diverged"


# ---------------------------------------------------------------------------
# VAL-PR-007: WHILLY_AUTO_OPEN_PR unset → zero invocations / rows / events
# ---------------------------------------------------------------------------


async def test_hook_does_not_fire_when_env_var_unset(
    db_pool: asyncpg.Pool,
    db_url: str,
    whilly_log_dir: Path,
    fake_worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WHILLY_AUTO_OPEN_PR", raising=False)
    await _seed_plan_with_task(
        db_pool,
        PLAN_ID,
        TASK_ID,
        github_issue_ref="foo/bar/42",
    )
    fake_run, captured = _make_subprocess_recorder()
    monkeypatch.setattr(gp, "_run", fake_run)

    exit_code = await asyncio.to_thread(
        run_run_command,
        ["--plan", PLAN_ID, "--max-iterations", "5", "--idle-wait", "0.01", "--heartbeat-interval", "60.0"],
        runner=_fake_runner_complete,
        install_signal_handlers=False,
    )
    assert exit_code == EXIT_OK
    assert captured == [], f"unexpected subprocess invocations when env var unset: {captured!r}"

    async with db_pool.acquire() as conn:
        pr_rows = await conn.fetch("SELECT 1 FROM pull_requests WHERE plan_id = $1", PLAN_ID)
        events = await conn.fetch(
            "SELECT event_type FROM events WHERE task_id = $1",
            TASK_ID,
        )
    assert pr_rows == []
    pr_event_types = [e["event_type"] for e in events if e["event_type"].startswith("pr.")]
    assert pr_event_types == [], f"unexpected pr.* events: {pr_event_types!r}"


# ---------------------------------------------------------------------------
# VAL-PR-008: env on but github_issue_ref NULL → no fire, no warning
# ---------------------------------------------------------------------------


async def test_hook_skipped_when_github_issue_ref_is_null(
    db_pool: asyncpg.Pool,
    db_url: str,
    whilly_log_dir: Path,
    fake_worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHILLY_AUTO_OPEN_PR", "1")
    await _seed_plan_with_task(
        db_pool,
        PLAN_ID,
        TASK_ID,
        github_issue_ref=None,
    )
    fake_run, captured = _make_subprocess_recorder()
    monkeypatch.setattr(gp, "_run", fake_run)

    exit_code = await asyncio.to_thread(
        run_run_command,
        ["--plan", PLAN_ID, "--max-iterations", "5", "--idle-wait", "0.01", "--heartbeat-interval", "60.0"],
        runner=_fake_runner_complete,
        install_signal_handlers=False,
    )
    assert exit_code == EXIT_OK
    assert captured == [], f"PR opener invoked even though github_issue_ref is NULL: {captured!r}"

    async with db_pool.acquire() as conn:
        pr_rows = await conn.fetch("SELECT 1 FROM pull_requests WHERE plan_id = $1", PLAN_ID)
        events = await conn.fetch(
            "SELECT event_type FROM events WHERE task_id = $1",
            TASK_ID,
        )
    assert pr_rows == []
    pr_event_types = [e["event_type"] for e in events if e["event_type"].startswith("pr.")]
    assert pr_event_types == [], f"unexpected pr.* events when ref is NULL: {pr_event_types!r}"
