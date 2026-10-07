"""Trusted coordinator commits only its task worktree, never the user checkout."""

import os
import sys
from pathlib import Path

import pytest
from whilly.core.swarm_execution import ExecutionPolicy

from tests.swarm_helpers import git, make_repo
from whilly.swarm.gitops import GitError, _path_within, commit_task_changes, create_worktree


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS /private/var alias regression")
def test_path_within_canonicalizes_macos_private_var_alias() -> None:
    assert _path_within(Path("/private/var"), Path("/var"))


def test_path_within_rejects_sibling_prefix_and_parent_traversal(tmp_path: Path) -> None:
    parent = tmp_path / "allowed"
    sibling = tmp_path / "allowed-sibling"
    parent.mkdir()
    sibling.mkdir()
    assert not _path_within(sibling / "file", parent)
    assert not _path_within(parent / ".." / "allowed-sibling" / "file", parent)


def test_path_within_resolves_symlink_escape(tmp_path: Path) -> None:
    parent = tmp_path / "allowed"
    outside = tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    link = parent / "link"
    link.symlink_to(outside, target_is_directory=True)
    assert not _path_within(link / "secret", parent)


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS sandbox-exec and the Xcode Git toolchain")
def test_coordinator_commit_is_branch_scoped_and_preserves_original(tmp_path):
    repo = make_repo(tmp_path, "source")
    original = git(repo, "rev-parse", "HEAD").strip()
    worktree = tmp_path / "task"
    host_logs = tmp_path.parent / f"{tmp_path.name}-host-logs"
    host_logs.mkdir()
    create_worktree(repo, worktree, "swarm/test-task", original)
    (worktree / "new file.py").write_text("VALUE = 1\n")
    policy = ExecutionPolicy(
        phase="git",
        read_roots=(
            str(worktree),
            "/Applications/Xcode.app/Contents/Developer/usr/bin",
            "/Applications/Xcode.app/Contents/Developer/usr/libexec/git-core",
        ),
        write_roots=(str(worktree), str(tmp_path)),
        denied_roots=(),
        network=False,
        timeout_seconds=30,
        max_output_bytes=1024 * 1024,
    )
    env = {"PATH": "/Applications/Xcode.app/Contents/Developer/usr/bin", "TMPDIR": str(tmp_path)}
    with pytest.raises(GitError, match="branch"):
        commit_task_changes(
            worktree, expected_branch="main", task_id="one", policy=policy, environment=env, log_dir=host_logs
        )
    head = commit_task_changes(
        worktree, expected_branch="swarm/test-task", task_id="one", policy=policy, environment=env, log_dir=host_logs
    )
    assert head != original
    assert git(repo, "rev-parse", "HEAD").strip() == original
    assert not (repo / "new file.py").exists()
    assert git(worktree, "status", "--porcelain") == ""
    assert (
        commit_task_changes(
            worktree,
            expected_branch="swarm/test-task",
            task_id="one",
            policy=policy,
            environment=env,
            log_dir=host_logs,
        )
        == head
    )
