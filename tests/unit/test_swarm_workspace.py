"""TDD contracts for independent candidate repositories and guarded hooks."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from whilly.adapters.filesystem.swarm_workspace import (
    HookPolicyError,
    GuardedGit,
    candidate_identity,
    materialize_candidate,
    resolve_hook_policy,
    resolve_git_toolchain,
)
from whilly.core.swarm_execution import ExecutionPolicy


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="requires the validated Xcode Git toolchain")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["/usr/bin/git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return result.stdout


def _policy(tmp_path: Path, *, phase: str = "git", write: Path | None = None) -> ExecutionPolicy:
    write_root = write or tmp_path / "candidate"
    write_root.mkdir(parents=True, exist_ok=True)
    return ExecutionPolicy(
        phase=phase,
        read_roots=(str(tmp_path), "/usr/bin", "/Applications/Xcode.app/Contents/Developer/usr/libexec/git-core"),
        write_roots=(str(write_root),),
        denied_roots=(str(tmp_path / "outside"),),
        network=False,
        timeout_seconds=5,
        max_output_bytes=64 * 1024,
        protected_write_roots=(),
    )


def _repo(tmp_path: Path, *, bare: bool = False) -> tuple[Path, str]:
    source = tmp_path / ("source.git" if bare else "source")
    command = ["/usr/bin/git", "init", "--bare", str(source)] if bare else ["/usr/bin/git", "init", str(source)]
    subprocess.run(command, capture_output=True, text=True, check=True)
    if bare:
        work = tmp_path / "seed"
        subprocess.run(["/usr/bin/git", "clone", str(source), str(work)], capture_output=True, text=True, check=True)
    else:
        work = source
    subprocess.run(["/usr/bin/git", "-C", str(work), "config", "user.name", "Test"], check=True)
    subprocess.run(["/usr/bin/git", "-C", str(work), "config", "user.email", "test@example.invalid"], check=True)
    (work / "tracked.txt").write_text("base\n")
    subprocess.run(["/usr/bin/git", "-C", str(work), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["/usr/bin/git", "-C", str(work), "commit", "-m", "base"], capture_output=True, text=True, check=True
    )
    if bare:
        subprocess.run(
            ["/usr/bin/git", "-C", str(work), "push", "origin", "HEAD:refs/heads/main"],
            capture_output=True,
            text=True,
            check=True,
        )
    base = _git(work, "rev-parse", "HEAD").strip()
    (work / "tracked.txt").write_text("dirty user edit\n")
    (work / "user-only.txt").write_text("do not copy\n")
    return source, base


def test_default_git_toolchain_selects_validated_xcode_binary_without_ambient_exec_path() -> None:
    executable, exec_path = resolve_git_toolchain()

    assert executable == Path("/Applications/Xcode.app/Contents/Developer/usr/bin/git")
    assert exec_path == Path("/Applications/Xcode.app/Contents/Developer/usr/libexec/git-core")


def test_materialize_preserves_base_and_dirty_source_for_ordinary_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, base = _repo(tmp_path)
    destination = tmp_path / "candidate"
    calls: list[tuple[str, ...]] = []

    def run(_root: Path, argv: tuple[str, ...], *, policy: ExecutionPolicy, environment: dict[str, str]) -> str:
        calls.append(argv)
        return _git(_root, *argv[1:])

    monkeypatch.setattr(GuardedGit, "run", staticmethod(run))
    result = materialize_candidate(
        source, base, destination, "swarm/candidate", _policy(tmp_path), environment={"PATH": "/usr/bin"}
    )

    assert result == destination
    assert _git(destination, "rev-parse", "HEAD").strip() == base
    assert (source / "tracked.txt").read_text() == "dirty user edit\n"
    assert (source / "user-only.txt").exists()
    assert not (destination / "user-only.txt").exists()
    assert not (destination / ".git" / "objects" / "info" / "alternates").exists()
    source_inodes = {path.stat().st_ino for path in (source / ".git" / "objects").rglob("*") if path.is_file()}
    candidate_inodes = {path.stat().st_ino for path in (destination / ".git" / "objects").rglob("*") if path.is_file()}
    assert source_inodes.isdisjoint(candidate_inodes)
    assert not _git(destination, "remote").strip()
    assert calls


def test_materialize_supports_bare_source_without_imported_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, base = _repo(tmp_path, bare=True)
    destination = tmp_path / "candidate"
    monkeypatch.setattr(GuardedGit, "run", staticmethod(lambda root, argv, **_: _git(root, *argv[1:])))

    materialize_candidate(source, base, destination, "swarm/bare", _policy(tmp_path), environment={"PATH": "/usr/bin"})

    assert _git(destination, "rev-parse", "HEAD").strip() == base
    assert not _git(destination, "remote").strip()


def test_materialize_supports_project_root_with_nested_bare_store_and_sibling_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare_source, base = _repo(tmp_path, bare=True)
    project_root = tmp_path / "project-root"
    project_root.mkdir()
    bare_source.rename(project_root / ".git")
    _git(project_root, "update-ref", "refs/remotes/origin/master", base)
    _git(project_root, "update-ref", "-d", "refs/heads/main")
    hooks = project_root / ".githooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text("#!/bin/sh\nexec ./hook-helper\n")
    hook.chmod(0o755)
    dependency = project_root / "hook-helper"
    dependency.write_text("#!/bin/sh\nexit 0\n")
    dependency.chmod(0o755)
    _git(project_root, "config", "core.hooksPath", str(hooks))
    destination = tmp_path / "candidate"
    monkeypatch.setattr(GuardedGit, "run", staticmethod(lambda root, argv, **_: _git(root, *argv[1:])))

    materialize_candidate(
        project_root,
        base,
        destination,
        "swarm/nested-bare",
        _policy(tmp_path),
        environment={"PATH": "/usr/bin"},
        hook_policy={
            "pre-push": {
                "sha256": hashlib.sha256(hook.read_bytes()).hexdigest(),
                "dependencies": {"hook-helper": hashlib.sha256(dependency.read_bytes()).hexdigest()},
                "phase": "git",
                "expected_exit": 0,
            }
        },
    )

    assert _git(destination, "rev-parse", "HEAD").strip() == base
    assert not _git(destination, "remote").strip()
    assert (destination / ".git" / "hooks" / "pre-push").read_bytes() == hook.read_bytes()


def test_unapproved_required_hook_blocks_before_materialization(tmp_path: Path) -> None:
    source, base = _repo(tmp_path)
    hooks = source / ".git" / "hooks"
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)

    with pytest.raises(HookPolicyError, match="hook_policy_required"):
        materialize_candidate(source, base, tmp_path / "candidate", "swarm/hook", _policy(tmp_path), environment={})


def test_approved_hook_policy_is_pinned_and_digest_is_deterministic(tmp_path: Path) -> None:
    source, _base = _repo(tmp_path)
    hook = source / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    dependency = source / "hook-helper"
    dependency.write_text("helper\n")
    approved = {
        "pre-commit": {
            "sha256": hashlib.sha256(hook.read_bytes()).hexdigest(),
            "dependencies": {"hook-helper": hashlib.sha256(dependency.read_bytes()).hexdigest()},
            "phase": "git",
            "expected_exit": 0,
        }
    }

    first = resolve_hook_policy(source, approved)
    second = resolve_hook_policy(source, approved)
    assert first == second
    assert first["digest"]
    assert first["hooks"]["pre-commit"]["sha256"] == approved["pre-commit"]["sha256"]


def _hook_approval(hook: Path, dependencies: dict[str, Path]) -> dict[str, dict[str, object]]:
    return {
        "pre-commit": {
            "sha256": hashlib.sha256(hook.read_bytes()).hexdigest(),
            "dependencies": {
                name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in dependencies.items()
            },
            "phase": "git",
            "expected_exit": 0,
        }
    }


def test_dependency_destination_symlink_is_rejected_without_following_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _base = _repo(tmp_path)
    hook = source / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    dependency = source / "nested" / "helper"
    dependency.parent.mkdir()
    dependency.write_text("approved\n")
    destination = tmp_path / "candidate"
    outside = tmp_path / "outside-target"
    outside.mkdir()

    def run(root: Path, argv: tuple[str, ...], **_: object) -> str:
        if argv[1:] and argv[1] == "checkout":
            (destination / "nested").symlink_to(outside, target_is_directory=True)
        return _git(root, *argv[1:])

    monkeypatch.setattr(GuardedGit, "run", staticmethod(run))
    with pytest.raises(HookPolicyError, match="hook_policy_required"):
        materialize_candidate(
            source,
            _git(source, "rev-parse", "HEAD").strip(),
            destination,
            "swarm/symlink",
            _policy(tmp_path),
            environment={"PATH": "/usr/bin"},
            hook_policy=_hook_approval(hook, {"nested/helper": dependency}),
        )
    assert not (outside / "helper").exists()


def test_materialization_copies_the_pinned_snapshot_after_source_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, base = _repo(tmp_path)
    hook = source / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    dependency = source / "hook-helper"
    dependency.write_text("approved\n")
    destination = tmp_path / "candidate"

    def run(root: Path, argv: tuple[str, ...], **_: object) -> str:
        result = _git(root, *argv[1:])
        if argv[1:] and argv[1] == "checkout":
            dependency.write_text("tampered after approval\n")
        return result

    monkeypatch.setattr(GuardedGit, "run", staticmethod(run))
    materialize_candidate(
        source,
        base,
        destination,
        "swarm/toctou",
        _policy(tmp_path),
        environment={"PATH": "/usr/bin"},
        hook_policy=_hook_approval(hook, {"hook-helper": dependency}),
    )
    assert (destination / "hook-helper").read_text() == "approved\n"


@pytest.mark.parametrize("kind", ["symlink", "special"])
def test_dependency_source_symlinks_and_special_files_fail_closed(tmp_path: Path, kind: str) -> None:
    source, _base = _repo(tmp_path)
    hook = source / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    dependency = source / "hook-helper"
    if kind == "symlink":
        target = source / "target"
        target.write_text("not an approved regular file entry\n")
        dependency.symlink_to(target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
    else:
        os.mkfifo(dependency)
        digest = "0" * 64
    approved = {
        "pre-commit": {
            "sha256": hashlib.sha256(hook.read_bytes()).hexdigest(),
            "dependencies": {dependency.name: digest},
            "phase": "git",
            "expected_exit": 0,
        }
    }
    with pytest.raises(HookPolicyError, match="hook_policy_required"):
        resolve_hook_policy(source, approved)


@pytest.mark.parametrize(
    "case",
    ["too_many_dependencies", "too_long_path", "oversized_file", "oversized_aggregate", "too_many_hooks"],
)
def test_hook_policy_bounds_fail_closed(tmp_path: Path, case: str) -> None:
    source, _base = _repo(tmp_path)
    hooks = source / ".git" / "hooks"
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    dependencies: dict[str, Path] = {}
    if case == "too_many_dependencies":
        for index in range(65):
            path = source / f"dep-{index}"
            path.write_text("x")
            dependencies[path.name] = path
    elif case == "too_long_path":
        dependencies["x" * 4097] = source / "missing"
    elif case == "oversized_file":
        path = source / "large"
        path.write_bytes(b"x" * (1024 * 1024 + 1))
        dependencies[path.name] = path
    elif case == "oversized_aggregate":
        for index in range(4):
            path = source / f"large-{index}"
            path.write_bytes(b"x" * (1024 * 1024))
            dependencies[path.name] = path
        fifth = source / "large-4"
        fifth.write_bytes(b"x")
        dependencies[fifth.name] = fifth
    else:
        for index in range(33):
            extra = hooks / f"hook-{index}"
            extra.write_text("#!/bin/sh\nexit 0\n")
            extra.chmod(0o755)
    if case == "too_long_path":
        approved = {
            "pre-commit": {
                "sha256": hashlib.sha256(hook.read_bytes()).hexdigest(),
                "dependencies": {next(iter(dependencies)): "0" * 64},
                "phase": "git",
                "expected_exit": 0,
            }
        }
    else:
        approved = _hook_approval(hook, dependencies)
    if case == "too_many_hooks":
        approved = {name: approved["pre-commit"] for name in ["pre-commit", *[f"hook-{i}" for i in range(33)]]}
    with pytest.raises(HookPolicyError, match="hook_policy_required"):
        resolve_hook_policy(source, approved)


def test_hook_policy_global_dependency_bound_counts_entries_across_hooks(tmp_path: Path) -> None:
    source, _base = _repo(tmp_path)
    hooks = source / ".git" / "hooks"
    approved: dict[str, dict[str, object]] = {}
    for hook_name in ("pre-commit", "post-commit"):
        hook = hooks / hook_name
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
        dependencies: dict[str, str] = {}
        for index in range(33 if hook_name == "pre-commit" else 32):
            dependency = source / f"{hook_name}-dep-{index}"
            dependency.write_text("dependency\n")
            dependencies[dependency.name] = hashlib.sha256(dependency.read_bytes()).hexdigest()
        approved[hook_name] = {
            "sha256": hashlib.sha256(hook.read_bytes()).hexdigest(),
            "dependencies": dependencies,
            "phase": "git",
            "expected_exit": 0,
        }

    with pytest.raises(HookPolicyError, match="hook_policy_required"):
        resolve_hook_policy(source, approved)


def test_candidate_identity_returns_head_and_tracked_tree_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, base = _repo(tmp_path)
    destination = tmp_path / "candidate"
    monkeypatch.setattr(GuardedGit, "run", staticmethod(lambda root, argv, **_: _git(root, *argv[1:])))
    materialize_candidate(source, base, destination, "swarm/id", _policy(tmp_path), environment={"PATH": "/usr/bin"})

    head, tree = candidate_identity(
        destination, policy=_policy(tmp_path, write=destination), environment={"PATH": "/usr/bin"}
    )
    assert head == base
    assert tree == _git(destination, "rev-parse", "HEAD^{tree}").strip()
