"""Real macOS sandbox-exec acceptance probes using disposable fixtures only."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from whilly.adapters.runner.swarm_sandbox import probe_sandbox
from whilly.core.swarm_execution import ExecutionPolicy


def test_real_macos_sandbox_has_positive_controls_and_denies_escape(tmp_path: Path) -> None:
    if sys.platform != "darwin":
        pytest.fail("unavailable: execution_isolation_unavailable (local acceptance requires macOS)")

    allowed = tmp_path / "allowed"
    output = tmp_path / "output"
    outside = tmp_path / "synthetic-secret"
    allowed.mkdir()
    output.mkdir()
    outside.write_text("synthetic-secret", encoding="utf-8")
    git_metadata = output / ".git"
    git_metadata.mkdir()
    (git_metadata / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    policy = ExecutionPolicy(
        phase="verify",
        read_roots=(str(allowed), str(output), "/bin", "/usr"),
        write_roots=(str(output),),
        denied_roots=(str(outside),),
        network=False,
        timeout_seconds=3,
        max_output_bytes=1024 * 1024,
        protected_write_roots=(str(git_metadata),),
    )

    evidence = probe_sandbox(policy, tmp_path)

    assert evidence["backend"] == "macos-sandbox-exec"
    assert evidence["available"] is True
    assert evidence["controls"]["outside_read"] is True
    assert evidence["controls"]["outside_write"] is True
    assert evidence["controls"]["network"] is True
    assert evidence["checks"]["allowed_read"] is True
    assert evidence["checks"]["outside_read_denied"] is True
    assert evidence["checks"]["outside_write_denied"] is True
    assert evidence["checks"]["grandchild_write_denied"] is True
    assert evidence["checks"]["network_denied"] is True
    assert evidence["checks"]["sibling_write_allowed"] is True
    assert evidence["checks"]["protected_metadata_write_denied"] is True
    assert evidence["checks"]["protected_metadata_unlink_denied"] is True
    assert evidence["checks"]["protected_metadata_rename_denied"] is True

    online = probe_sandbox(replace(policy, phase="worker", network=True), tmp_path)
    assert online["available"] is True
    assert online["controls"]["network"] is True
    assert online["checks"]["network_allowed"] is True
