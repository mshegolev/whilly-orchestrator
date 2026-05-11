"""Integration coverage for project-config GitHub PR sink policy."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.filesystem.plan_io import parse_plan_dict
from whilly.adapters.runner.result_parser import AgentResult, AgentUsage
from whilly.audit import DEFAULT_JSONL_FILENAME, LOG_DIR_ENV
from whilly.cli.plan import _insert_plan_and_tasks
from whilly.cli.run import EXIT_OK, run_run_command
from whilly.core.models import Task
from whilly.project_config import build_plan_payload, project_config_from_dict
from whilly.sinks import github_pr as gp
from whilly.workspaces import WORKSPACE_BASE_ENV

pytestmark = DOCKER_REQUIRED


PLAN_ID = "PLAN-PR-SINK-PROFILE-1"
SINK_TASK_ID = "CFG-SINK-001-PUBLISH-PR"


@pytest.fixture
def db_url(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("WHILLY_DATABASE_URL", postgres_dsn)
    yield postgres_dsn


@pytest.fixture
def whilly_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    log_dir = tmp_path / "whilly_logs"
    monkeypatch.setenv(LOG_DIR_ENV, str(log_dir))
    return log_dir


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_subprocess_recorder(pr_url: str = "https://github.com/foo/bar/pull/77"):
    push = _Proc(0, "")
    pr = _Proc(0, f"{pr_url}\n")
    captured: list[list[str]] = []

    def fake_run(cmd, cwd, timeout=60):  # noqa: ARG001
        captured.append(list(cmd))
        return push if cmd[0] == "git" else pr

    return fake_run, captured


def _read_jsonl_lines(jsonl_path: Path) -> list[dict[str, object]]:
    if not jsonl_path.is_file():
        return []
    raw = jsonl_path.read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.split("\n") if line.strip()]


def _make_local_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.email", "whilly@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Whilly Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True, text=True, check=True)
    return repo


async def _fake_runner_complete(task: Task, prompt: str) -> AgentResult:  # noqa: ARG001
    return AgentResult(
        usage=AgentUsage(),
        exit_code=0,
        is_complete=True,
        output=f"<promise>COMPLETE</promise> for {task.id}",
    )


def _profile_config(clone_url: str) -> dict:
    return {
        "name": "Profile PR sink integration",
        "project_type": "python_backend",
        "repositories": [
            {
                "id": "app",
                "role": "code",
                "provider": "github",
                "repo_full_name": "foo/bar",
                "clone_url": clone_url,
            }
        ],
        "pipeline": [
            {"id": "implement", "type": "development", "title": "Implement feature", "repo_role": "code"},
            {
                "id": "verify",
                "type": "quality_gate",
                "title": "Verify feature",
                "depends_on": ["implement"],
            },
        ],
        "sinks": [
            {"type": "github_pr", "config": {"repo_role": "code", "stage_id": "publish-pr", "approval": "profile"}}
        ],
        "human_loop": {"enabled": False},
    }


async def test_profile_github_pr_sink_opens_once_for_explicit_sink_stage(
    db_pool: asyncpg.Pool,
    db_url: str,
    whilly_log_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHILLY_AUTO_OPEN_PR", "1")
    monkeypatch.setenv(WORKSPACE_BASE_ENV, str(tmp_path / "workspaces"))
    source_repo = _make_local_git_repo(tmp_path)
    payload = build_plan_payload(project_config_from_dict(_profile_config(str(source_repo))), plan_id=PLAN_ID)
    plan, tasks = parse_plan_dict(payload)
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await _insert_plan_and_tasks(conn, plan, tasks)
    fake_run, captured = _make_subprocess_recorder()
    monkeypatch.setattr(gp, "_run", fake_run)

    exit_code = await asyncio.to_thread(
        run_run_command,
        ["--plan", PLAN_ID, "--max-iterations", "8", "--idle-wait", "0.01", "--heartbeat-interval", "60.0"],
        runner=_fake_runner_complete,
        install_signal_handlers=False,
    )
    assert exit_code == EXIT_OK

    gh_create_calls = [cmd for cmd in captured if cmd[:3] == ["gh", "pr", "create"]]
    assert len(gh_create_calls) == 1, f"expected one PR opener call, got {captured!r}"

    async with db_pool.acquire() as conn:
        pr_rows = await conn.fetch("SELECT * FROM pull_requests WHERE plan_id = $1", PLAN_ID)
        events = await conn.fetch(
            "SELECT event_type, task_id, payload FROM events WHERE plan_id = $1 ORDER BY id", PLAN_ID
        )

    assert len(pr_rows) == 1
    assert pr_rows[0]["task_id"] == SINK_TASK_ID
    assert pr_rows[0]["repo_target_id"] == "github:foo/bar"
    assert pr_rows[0]["pr_url"] == "https://github.com/foo/bar/pull/77"

    pr_opened = [e for e in events if e["event_type"] == "pr.opened"]
    pr_failed = [e for e in events if e["event_type"] == "pr.open_failed"]
    assert len(pr_opened) == 1
    assert pr_opened[0]["task_id"] == SINK_TASK_ID
    assert pr_failed == []
    pg_payload = pr_opened[0]["payload"]
    if isinstance(pg_payload, str):
        pg_payload = json.loads(pg_payload)
    assert pg_payload["repo_target_id"] == "github:foo/bar"

    jsonl_lines = _read_jsonl_lines(whilly_log_dir / DEFAULT_JSONL_FILENAME)
    pr_opened_jsonl = [line for line in jsonl_lines if line["event_type"] == "pr.opened"]
    assert len(pr_opened_jsonl) == 1
    assert pr_opened_jsonl[0]["payload"] == pg_payload
