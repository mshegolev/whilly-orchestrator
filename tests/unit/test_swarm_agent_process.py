"""Unit tests for bounded swarm subprocesses and agent argv construction."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from whilly.cli.swarm import run_swarm_command
from whilly.swarm.agent import build_agent_argv, process_group_alive, run_bounded
from whilly.swarm.registry import AgentConfig

# Parent spawns a grandchild in the same process group, prints its pid, then
# sleeps; killing only the parent would leave the grandchild editing files.
_SPAWNER = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(60)\n"
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); check state via ps.
    import subprocess

    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


async def _wait_for_pid(path: Path) -> int:
    for _ in range(100):
        text = path.read_text() if path.exists() else ""
        if text.strip():
            return int(text.split()[0])
        await asyncio.sleep(0.05)
    raise AssertionError("grandchild pid never printed")


async def test_timeout_kills_whole_process_group(tmp_path: Path) -> None:
    started = time.monotonic()
    outcome = await run_bounded(
        [sys.executable, "-c", _SPAWNER],
        cwd=tmp_path,
        timeout_seconds=1.0,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
    )
    assert outcome.timed_out and not outcome.ok
    assert time.monotonic() - started < 15
    grandchild = int((tmp_path / "out.log").read_text().split()[0])
    await asyncio.sleep(0.2)
    assert not _pid_alive(grandchild)


async def test_cancellation_kills_process_group(tmp_path: Path) -> None:
    pgids: list[int] = []

    async def on_start(pgid: int) -> None:
        pgids.append(pgid)

    handle = asyncio.create_task(
        run_bounded(
            [sys.executable, "-c", _SPAWNER],
            cwd=tmp_path,
            timeout_seconds=60,
            stdout_path=tmp_path / "out.log",
            stderr_path=tmp_path / "err.log",
            on_start=on_start,
        )
    )
    grandchild = await _wait_for_pid(tmp_path / "out.log")
    handle.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle
    await asyncio.sleep(0.2)
    assert pgids and not process_group_alive(pgids[0])
    assert not _pid_alive(grandchild)


async def test_nonzero_exit_and_spawn_error(tmp_path: Path) -> None:
    failed = await run_bounded(
        [sys.executable, "-c", "import sys; sys.exit(7)"],
        cwd=tmp_path,
        timeout_seconds=10,
        stdout_path=tmp_path / "a.log",
        stderr_path=tmp_path / "b.log",
    )
    assert failed.exit_code == 7 and not failed.ok
    missing = await run_bounded(
        ["/nonexistent/agent-binary"],
        cwd=tmp_path,
        timeout_seconds=10,
        stdout_path=tmp_path / "c.log",
        stderr_path=tmp_path / "d.log",
    )
    assert missing.spawn_error and "spawn failed" in missing.describe()


async def test_failed_pgid_persistence_reaps_spawned_process_group(tmp_path: Path) -> None:
    from whilly.swarm.agent import kill_process_group

    pgids = []

    async def failing_start(pgid):
        pgids.append(pgid)
        await _wait_for_pid(tmp_path / "child.log")
        raise RuntimeError("database unavailable during PGID persistence")

    try:
        with pytest.raises(RuntimeError, match="PGID persistence"):
            await run_bounded(
                [sys.executable, "-c", _SPAWNER],
                cwd=tmp_path,
                timeout_seconds=60,
                stdout_path=tmp_path / "child.log",
                stderr_path=tmp_path / "child.err",
                on_start=failing_start,
            )
        assert pgids and not process_group_alive(pgids[0])
    finally:
        for pgid in pgids:
            kill_process_group(pgid)


def test_read_only_argv_is_explicit() -> None:
    config = AgentConfig(executable=("ch",), worker_args=("--dangerously-skip-permissions",))
    argv = build_agent_argv(config, mode="read_only", max_turns=5, budget_usd=0.5, empty_mcp_config="/tmp/e.json")
    assert argv[:2] == ["ch", "-p"]
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == "/tmp/e.json"
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--max-turns") + 1] == "5"
    assert argv[argv.index("--max-budget-usd") + 1] == "0.50"
    assert "--dangerously-skip-permissions" not in argv


def test_worker_argv_uses_configured_permissions_and_no_inherited_mcp() -> None:
    config = AgentConfig(executable=("ch",), worker_args=("--permission-mode", "acceptEdits"))
    argv = build_agent_argv(config, mode="worker", max_turns=10, budget_usd=2, empty_mcp_config="/tmp/e.json")
    assert "--tools" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--strict-mcp-config" in argv


def test_cli_registry_validate_and_missing_dsn(tmp_path: Path, monkeypatch, capsys) -> None:
    bad = tmp_path / "r.json"
    bad.write_text("{not json")
    assert run_swarm_command(["registry", "validate", "--registry", str(bad)]) == 2
    monkeypatch.delenv("WHILLY_DATABASE_URL", raising=False)
    assert run_swarm_command(["status", "--session", "s0"]) == 2
    assert "WHILLY_DATABASE_URL" in capsys.readouterr().err
