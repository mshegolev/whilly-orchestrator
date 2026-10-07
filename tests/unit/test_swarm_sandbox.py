"""Unit tests for the macOS sandbox adapter and bounded transport."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

import whilly.adapters.runner.swarm_sandbox as sandbox_module
from whilly.adapters.runner.swarm_sandbox import (
    probe_sandbox,
    run_sandboxed,
    run_sandboxed_sync,
    sandbox_argv,
)
from whilly.core.swarm_execution import ExecutionBlocked, ExecutionPolicy, SandboxResult


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS sandbox-exec")


def _policy(tmp_path: Path, **overrides: object) -> ExecutionPolicy:
    (tmp_path / "candidate").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    values: dict[str, object] = {
        "phase": "verify",
        "read_roots": (str(tmp_path / "candidate"), "/bin", "/usr"),
        "write_roots": (str(tmp_path / "out"),),
        "denied_roots": (str(tmp_path / "secret"),),
        "network": False,
        "timeout_seconds": 2,
        "max_output_bytes": 1024,
        "protected_write_roots": (),
    }
    values.update(overrides)
    return ExecutionPolicy(**values)


def test_sandbox_argv_has_no_shell_and_quotes_paths_as_profile_literals(tmp_path: Path) -> None:
    policy = _policy(tmp_path / "path with 'quotes'")
    argv = sandbox_argv(("/bin/echo", "ok"), policy)

    assert argv[0].endswith("sandbox-exec")
    assert argv[1] == "-p"
    assert "(deny default)" in argv[2]
    assert "file-write*" in argv[2]
    assert "network" not in argv[2]
    assert '(allow file-read* (subpath "/Library"))' not in argv[2]
    assert '(allow file-read* (subpath "/System"))' not in argv[2]
    assert argv[3:] == ("/bin/echo", "ok")


def test_sandbox_profile_grants_network_only_when_policy_allows_it(tmp_path: Path) -> None:
    profile = sandbox_argv(("/bin/echo",), _policy(tmp_path, phase="worker", network=True))[2]

    assert "(allow network-outbound)" in profile
    assert "(allow network-inbound)" in profile
    # Native TLS (codex) needs exactly these trust services, never a blanket mach-lookup.
    assert '(global-name "com.apple.trustd")' in profile
    assert '(global-name "com.apple.SecurityServer")' in profile
    assert "(allow mach-lookup)" not in profile


def test_sandbox_profile_has_no_mach_lookup_without_network(tmp_path: Path) -> None:
    profile = sandbox_argv(("/bin/echo",), _policy(tmp_path))[2]

    assert "mach-lookup" not in profile


def test_sandbox_argv_rejects_empty_or_nul_argv(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    with pytest.raises(ValueError, match="argv"):
        sandbox_argv((), policy)
    with pytest.raises(ValueError, match="NUL"):
        sandbox_argv(("/bin/echo", "bad\x00arg"), policy)
    with pytest.raises(ValueError, match="absolute"):
        sandbox_argv(("echo", "ok"), policy)


def test_sandbox_profile_allows_sibling_write_but_protects_nested_metadata(tmp_path: Path) -> None:
    policy = _policy(tmp_path, protected_write_roots=(str(tmp_path / "out" / ".git"),))
    profile = sandbox_argv(("/bin/echo",), policy)[2]

    assert "(allow file-write* (subpath" in profile
    assert f'(deny file-write* (subpath "{tmp_path / "out" / ".git"}"))' in profile


def test_allowed_and_denied_roots_overlap_after_canonicalization(tmp_path: Path) -> None:
    policy = _policy(tmp_path, read_roots=(str(tmp_path / "candidate"), "/usr"), denied_roots=("/usr/local",))
    with pytest.raises(ExecutionBlocked, match="policy_root_overlap"):
        sandbox_argv(("/bin/echo",), policy)


def test_adapter_reexports_the_pure_core_result() -> None:
    from whilly.core.swarm_execution import SandboxResult as CoreSandboxResult

    assert SandboxResult is CoreSandboxResult


def test_sync_transport_runs_allowed_command_and_writes_bounded_logs(tmp_path: Path) -> None:
    output = tmp_path / "out"
    output.mkdir()
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    result = run_sandboxed_sync(
        ("/bin/echo", "allowed"),
        policy=_policy(tmp_path),
        cwd=tmp_path / "candidate",
        environment={"PATH": os.environ["PATH"], "HOME": str(output), "TMPDIR": str(output)},
        stdout_path=stdout,
        stderr_path=stderr,
    )

    assert result.exit_code == 0
    assert result.reason is None
    assert result.backend == "macos-sandbox-exec"
    assert stdout.read_text() == "allowed\n"


def test_relative_executable_uses_only_explicit_child_path(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    result = run_sandboxed_sync(
        ("echo", "relative"),
        policy=_policy(tmp_path),
        cwd=tmp_path / "candidate",
        environment={"PATH": "/bin"},
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )

    assert result.exit_code == 0
    assert (tmp_path / "stdout.log").read_text() == "relative\n"


def test_relative_executable_without_child_path_is_named_and_not_host_resolved(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    result = run_sandboxed_sync(
        ("echo", "relative"),
        policy=_policy(tmp_path),
        cwd=tmp_path / "candidate",
        environment={},
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )

    assert result.exit_code is None
    assert result.reason == "executable_path_unavailable"


@pytest.mark.asyncio
async def test_async_transport_cancellation_returns_bounded_result(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    task = asyncio.create_task(
        run_sandboxed(
            ("/bin/sleep", "30"),
            policy=_policy(tmp_path, timeout_seconds=10),
            cwd=tmp_path / "candidate",
            environment={"PATH": os.environ["PATH"]},
            stdout_path=tmp_path / "stdout.log",
            stderr_path=tmp_path / "stderr.log",
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    result = await asyncio.wait_for(task, timeout=2)

    assert result.cancelled is True
    assert result.reason == "cancelled"


@pytest.mark.asyncio
async def test_timeout_covers_hanging_on_start_callback(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()

    async def hanging_callback(_pgid: int) -> None:
        await asyncio.Event().wait()

    result = await asyncio.wait_for(
        run_sandboxed(
            ("/bin/sleep", "30"),
            policy=_policy(tmp_path, timeout_seconds=1),
            cwd=tmp_path / "candidate",
            environment={"PATH": "/bin"},
            stdout_path=tmp_path / "stdout.log",
            stderr_path=tmp_path / "stderr.log",
            on_start=hanging_callback,
        ),
        timeout=2,
    )

    assert result.timed_out is True
    assert result.reason == "timeout"


@pytest.mark.asyncio
async def test_sync_on_start_is_rejected_before_child_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "out").mkdir()
    spawned = False

    def sync_callback(_pgid: int) -> None:
        raise AssertionError("unsupported callback must not run")

    async def fail_if_spawned(*_args: object, **_kwargs: object) -> None:
        nonlocal spawned
        spawned = True
        raise AssertionError("child must not spawn")

    monkeypatch.setattr(sandbox_module.asyncio, "create_subprocess_exec", fail_if_spawned)

    result = await asyncio.wait_for(
        run_sandboxed(
            ("/bin/sleep", "30"),
            policy=_policy(tmp_path, timeout_seconds=1),
            cwd=tmp_path / "candidate",
            environment={"PATH": "/bin"},
            stdout_path=tmp_path / "stdout.log",
            stderr_path=tmp_path / "stderr.log",
            on_start=sync_callback,
        ),
        timeout=2,
    )

    assert result.timed_out is False
    assert result.reason == "on_start_unsupported"
    assert spawned is False


@pytest.mark.asyncio
async def test_on_start_failure_returns_named_result_and_reaps_group(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    seen: list[int] = []

    async def failing_callback(pgid: int) -> None:
        seen.append(pgid)
        raise RuntimeError("callback boom")

    result = await run_sandboxed(
        ("/bin/sleep", "30"),
        policy=_policy(tmp_path),
        cwd=tmp_path / "candidate",
        environment={"PATH": "/bin"},
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
        on_start=failing_callback,
    )

    assert seen
    assert result.reason == "on_start_failed:RuntimeError"
    with pytest.raises(ProcessLookupError):
        os.killpg(seen[0], 0)


@pytest.mark.asyncio
async def test_normal_leader_exit_reaps_grandchild_group(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    seen: list[int] = []

    async def remember_group(pgid: int) -> None:
        seen.append(pgid)

    result = await run_sandboxed(
        ("/bin/sh", "-c", "/bin/sleep 30 & exit 0"),
        policy=_policy(tmp_path),
        cwd=tmp_path / "candidate",
        environment={"PATH": "/bin"},
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
        on_start=remember_group,
    )

    assert result.exit_code == 0
    assert result.reason is None
    assert result.timed_out is False
    with pytest.raises(ProcessLookupError):
        os.killpg(seen[0], 0)


def test_missing_executable_is_named_and_never_direct_host_fallback(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    result = run_sandboxed_sync(
        (str(tmp_path / "does-not-exist"),),
        policy=_policy(tmp_path),
        cwd=tmp_path,
        environment={},
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )

    assert result.exit_code is None
    assert result.reason == "executable_not_found"


def test_output_limit_terminates_a_noisy_process(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    result = run_sandboxed_sync(
        ("/usr/bin/yes", "noise"),
        policy=_policy(tmp_path, max_output_bytes=1024),
        cwd=tmp_path / "candidate",
        environment={"PATH": os.environ["PATH"]},
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )

    assert result.reason == "output_limit_exceeded"
    assert result.timed_out is False
    assert (tmp_path / "stdout.log").stat().st_size <= 1024


def test_execution_blocked_can_be_raised_by_argv_builder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("whilly.adapters.runner.swarm_sandbox.sys.platform", "linux")
    with pytest.raises(ExecutionBlocked, match="execution_isolation_unavailable"):
        sandbox_argv((sys.executable,), _policy(tmp_path))


def test_probe_rejects_targets_outside_fixture_without_writing_them(tmp_path: Path) -> None:
    real_fixture = tmp_path / "real-fixture"
    real_fixture.mkdir()
    symlink_fixture = tmp_path / "symlink-fixture"
    symlink_fixture.symlink_to(real_fixture, target_is_directory=True)
    evidence = probe_sandbox(_policy(real_fixture), symlink_fixture)

    assert evidence["available"] is False
    assert evidence["reason"] == "probe_fixture_unsafe"


def test_probe_does_not_treat_backend_failure_as_denial(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    failed = SandboxResult(None, "executable_not_found", False, False, 0.0, "out", "err", "macos-sandbox-exec")
    monkeypatch.setattr(sandbox_module, "_probe_result", lambda *_args: failed)
    evidence = probe_sandbox(_policy(tmp_path), tmp_path)

    assert evidence["available"] is False
    assert evidence["reason"] == "execution_isolation_unavailable"
    assert evidence["failed_checks"]


def test_probe_requires_successful_allowed_read_positive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    failed_positive = SandboxResult(1, None, False, False, 0.0, "out", "err", "macos-sandbox-exec")
    monkeypatch.setattr(sandbox_module, "_probe_result", lambda *_args: failed_positive)

    evidence = probe_sandbox(_policy(tmp_path), tmp_path)

    assert evidence["available"] is False
    assert evidence["reason"] == "execution_isolation_unavailable"
