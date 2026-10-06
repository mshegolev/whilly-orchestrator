"""Real macOS acceptance for Task6A output and per-launch boundaries."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from whilly.core.swarm_execution import ExecutionPolicy
from whilly.swarm.execution import GuardedExecutor
from whilly.swarm.execution_config import ExecutionProvisioning, ToolchainProvision


pytestmark = pytest.mark.skipif(os.uname().sysname != "Darwin", reason="Task6A acceptance requires macOS sandbox-exec")


def test_child_cannot_read_sibling_launch_secret_or_plant_future_host_log_symlink(tmp_path: Path) -> None:
    launch_one = tmp_path / "launch-one"
    launch_two = tmp_path / "launch-two"
    candidate = tmp_path / "candidate"
    host_logs = tmp_path / "host-logs"
    outside = tmp_path / "outside-sentinel"
    for directory in (launch_one, launch_two, candidate, host_logs):
        directory.mkdir()
    (launch_two / "secret.txt").write_text("synthetic-selected-secret", encoding="utf-8")
    outside.write_text("unchanged", encoding="utf-8")
    future_log = host_logs / "future.stdout.log"
    policy = ExecutionPolicy(
        phase="worker",
        read_roots=(str(candidate), str(launch_one), "/bin"),
        write_roots=(str(candidate), str(launch_one)),
        denied_roots=(str(launch_two), str(host_logs)),
        network=False,
        timeout_seconds=3,
        max_output_bytes=4096,
    )
    executor = GuardedExecutor(
        ExecutionProvisioning(
            toolchains={
                "fixture": ToolchainProvision("fixture", (Path("/bin"), candidate, launch_one), "/bin", launch_one, launch_one)
            },
            phase_toolchains={"worker": "fixture"},
        )
    )
    environment = executor.environment(toolchain_id="fixture", phase="worker", attempt_root=launch_one)
    result = executor.run_sync(
        (
            "/bin/sh",
            "-c",
            'set -eu; test ! -r "$1"; test ! -e "$2"; printf control > "$3"',
            "sh",
            str(launch_two / "secret.txt"),
            str(future_log),
            str(candidate / "control.txt"),
        ),
        phase="worker",
        policy=policy,
        cwd=candidate,
        environment=environment,
        log_dir=host_logs,
    )
    assert result.reason is None
    assert result.exit_code == 0
    assert (candidate / "control.txt").read_text(encoding="utf-8") == "control"
    assert not future_log.exists()
    assert outside.read_text(encoding="utf-8") == "unchanged"
