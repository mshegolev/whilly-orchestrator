"""Darwin acceptance checks for real guarded Git and hook isolation."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from whilly.adapters.filesystem.swarm_workspace import GuardedGit, materialize_candidate
from whilly.core.swarm_execution import ExecutionPolicy
from whilly.swarm.gitops import GitError, commit_task_changes


pytestmark = pytest.mark.skipif(os.uname().sysname != "Darwin", reason="Task4 acceptance requires macOS sandbox-exec")


def _fixture(
    tmp_path: Path, hook_body: str
) -> tuple[Path, Path, str, dict[str, str], ExecutionPolicy, dict[str, dict[str, object]]]:
    source = tmp_path / "source"
    subprocess.run(["/usr/bin/git", "init", str(source)], capture_output=True, text=True, check=True)
    subprocess.run(["/usr/bin/git", "-C", str(source), "config", "user.name", "Test"], check=True)
    subprocess.run(["/usr/bin/git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
    (source / "tracked.txt").write_text("base\n")
    subprocess.run(["/usr/bin/git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["/usr/bin/git", "-C", str(source), "commit", "-m", "base"], capture_output=True, text=True, check=True
    )
    hook = source / ".git" / "hooks" / "pre-commit"
    hook.write_text(hook_body)
    hook.chmod(0o755)
    base = subprocess.run(
        ["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    destination = tmp_path / "candidate"
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "work-output"),
    }
    (tmp_path / "home").mkdir()
    (tmp_path / "work-output").mkdir()
    policy = ExecutionPolicy(
        phase="git",
        read_roots=(
            str(source),
            str(destination),
            "/bin",
            "/usr/bin",
            "/Applications/Xcode.app/Contents/Developer/usr/bin",
            "/Applications/Xcode.app/Contents/Developer/usr/lib",
            "/Applications/Xcode.app/Contents/Developer/usr/libexec/git-core",
            "/Applications/Xcode.app/Contents/Developer/usr/share/git-core",
        ),
        write_roots=(str(destination), str(tmp_path / "work-output")),
        denied_roots=(str(tmp_path / "outside"),),
        network=False,
        timeout_seconds=10,
        max_output_bytes=64 * 1024,
    )
    import hashlib

    approved = {
        "pre-commit": {
            "sha256": hashlib.sha256(hook.read_bytes()).hexdigest(),
            "dependencies": {},
            "phase": "git",
            "expected_exit": 0,
        }
    }
    return source, destination, base, environment, policy, approved


def test_guarded_git_uses_explicit_git_and_keeps_candidate_metadata_local(tmp_path: Path) -> None:
    source = tmp_path / "source"
    subprocess.run(["/usr/bin/git", "init", str(source)], capture_output=True, text=True, check=True)
    subprocess.run(["/usr/bin/git", "-C", str(source), "config", "user.name", "Test"], check=True)
    subprocess.run(["/usr/bin/git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
    (source / "tracked.txt").write_text("base\n")
    subprocess.run(["/usr/bin/git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["/usr/bin/git", "-C", str(source), "commit", "-m", "base"], capture_output=True, text=True, check=True
    )
    base = subprocess.run(
        ["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    destination = tmp_path / "candidate"
    work_output = tmp_path / "work-output"
    work_output.mkdir()
    policy = ExecutionPolicy(
        phase="git",
        read_roots=(
            str(source),
            str(destination),
            "/bin",
            "/usr/bin",
            "/Applications/Xcode.app/Contents/Developer/usr/bin",
            "/Applications/Xcode.app/Contents/Developer/usr/lib",
            "/Applications/Xcode.app/Contents/Developer/usr/libexec/git-core",
            "/Applications/Xcode.app/Contents/Developer/usr/share/git-core",
        ),
        write_roots=(str(destination), str(work_output)),
        denied_roots=(str(tmp_path / "outside"),),
        network=False,
        timeout_seconds=10,
        max_output_bytes=64 * 1024,
    )

    materialize_candidate(
        source,
        base,
        destination,
        "swarm/acceptance",
        policy,
        environment={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path / "home"),
            "TMPDIR": str(work_output),
        },
    )

    assert GuardedGit.run is not None
    assert not subprocess.run(
        ["/usr/bin/git", "-C", str(destination), "remote"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert not (destination / ".git" / "objects" / "info" / "alternates").exists()


def test_approved_success_hook_runs_in_guarded_git_phase(tmp_path: Path) -> None:
    source, destination, base, environment, policy, approved = _fixture(
        tmp_path, "#!/bin/sh\nset -e\ntouch hook-ran\nexit 0\n"
    )
    materialize_candidate(
        source, base, destination, "swarm/success", policy, environment=environment, hook_policy=approved
    )
    (destination / "change.txt").write_text("candidate\n")
    commit_task_changes(
        destination, expected_branch="swarm/success", task_id="success", policy=policy, environment=environment
    )
    assert (destination / "hook-ran").exists()


def test_failing_approved_hook_blocks_coordinator_commit(tmp_path: Path) -> None:
    source, destination, base, environment, policy, approved = _fixture(tmp_path, "#!/bin/sh\nset -e\nexit 23\n")
    materialize_candidate(
        source, base, destination, "swarm/fail", policy, environment=environment, hook_policy=approved
    )
    (destination / "change.txt").write_text("candidate\n")
    with pytest.raises(GitError, match=r"guarded git .*commit .* failed"):
        commit_task_changes(
            destination, expected_branch="swarm/fail", task_id="fail", policy=policy, environment=environment
        )
    assert (
        subprocess.run(
            [
                str(Path("/Applications/Xcode.app/Contents/Developer/usr/bin/git")),
                "-C",
                str(destination),
                "rev-parse",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        == base
    )


def test_outside_write_hook_is_denied_and_commit_does_not_publish(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("before\n")
    control = subprocess.run(
        ["/bin/sh", "-c", f"set -e; printf 'control\\n' > '{sentinel}'"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert control.returncode == 0
    assert sentinel.read_text() == "control\n"
    sentinel.write_text("before\n")
    source, destination, base, environment, policy, approved = _fixture(
        tmp_path, f"#!/bin/sh\nset -e\nprintf 'denied\\n' > '{sentinel}'\nexit 0\n"
    )
    materialize_candidate(
        source, base, destination, "swarm/outside", policy, environment=environment, hook_policy=approved
    )
    (destination / "change.txt").write_text("candidate\n")
    with pytest.raises(GitError, match=r"guarded git .*commit .* failed"):
        commit_task_changes(
            destination, expected_branch="swarm/outside", task_id="outside", policy=policy, environment=environment
        )
    assert sentinel.read_text() == "before\n"
    assert (
        subprocess.run(
            [
                str(Path("/Applications/Xcode.app/Contents/Developer/usr/bin/git")),
                "-C",
                str(destination),
                "rev-parse",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        == base
    )
