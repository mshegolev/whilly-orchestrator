"""Independent candidate Git repositories and pinned guarded hooks."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path

from whilly.core.swarm_execution import ExecutionPolicy
from whilly.swarm.gitops import GitError, GuardedGit, resolve_git_toolchain

MAX_HOOKS = 32
MAX_DEPENDENCIES = 64
MAX_RELATIVE_PATH_BYTES = 4096
MAX_FILE_BYTES = 1024 * 1024
MAX_HOOK_CONTENT_BYTES = 4 * 1024 * 1024

__all__ = [
    "GuardedGit",
    "HookPolicyError",
    "candidate_identity",
    "materialize_candidate",
    "resolve_hook_policy",
    "resolve_git_toolchain",
]


class HookPolicyError(GitError):
    """Named fail-closed hook approval error."""


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path.resolve(strict=False)), str(root.resolve(strict=False)))) == str(
            root.resolve(strict=False)
        )
    except ValueError:
        return False


def _git_dir(source: Path) -> Path:
    output = _trusted_git(source, "rev-parse", "--git-dir").strip()
    candidate = Path(output)
    return candidate if candidate.is_absolute() else (source / candidate).resolve()


def _trusted_git(source: Path, *args: str) -> str:
    """Read-only source inspection; it never mutates a registered checkout."""
    executable, helper_root = resolve_git_toolchain()
    result = subprocess.run(
        [str(executable), "-C", str(source), *args],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_EXEC_PATH": str(helper_root),
        },
    )
    if result.returncode:
        raise GitError(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()[:2000]}")
    return result.stdout


def _effective_hooks(source: Path) -> tuple[Path, str]:
    git_dir = _git_dir(source)
    try:
        configured = _trusted_git(source, "config", "--get", "core.hooksPath").strip()
    except GitError as exc:
        if "failed (1)" not in str(exc):
            raise
        configured = ""
    if configured:
        hook_dir = (source / configured).resolve() if not Path(configured).is_absolute() else Path(configured).resolve()
        hook_config = configured
    else:
        hook_dir = (git_dir / "hooks").resolve()
        hook_config = ""
    return hook_dir, hook_config


def _effective_config(source: Path) -> dict[str, str]:
    raw = _trusted_git(source, "config", "--local", "--null", "--list")
    values: dict[str, str] = {}
    for record in raw.split("\0"):
        if not record:
            continue
        key, separator, value = record.partition("\n")
        if separator:
            values[key] = value
    executable_keys = (
        "core.fsmonitor",
        "core.gitproxy",
        "credential.helper",
        "commit.gpgsign",
        "gpg.",
        "filter.",
        "diff.",
        "merge.",
    )
    if any(key.startswith(prefix) for key in values for prefix in executable_keys):
        raise HookPolicyError("hook_policy_required")
    return values


def _hook_files(source: Path) -> tuple[Path, str]:
    hook_dir, hook_config = _effective_hooks(source)
    if not hook_dir.is_dir():
        return (), hook_config
    return tuple(
        sorted(
            (
                path
                for path in hook_dir.iterdir()
                if path.is_file() and not path.name.endswith(".sample") and os.access(path, os.X_OK)
            ),
            key=lambda p: p.name,
        )
    ), hook_config


def _read_regular_nofollow(root: Path, relative: Path) -> bytes:
    """Read one bounded regular file through descriptor-safe nofollow traversal."""
    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or any("\x00" in part for part in relative.parts)
    ):
        raise HookPolicyError("hook_policy_required")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory_fd = os.open(root, flags)
    except (OSError, ValueError) as exc:
        raise HookPolicyError("hook_policy_required") from exc
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE_BYTES:
                raise HookPolicyError("hook_policy_required")
            content = bytearray()
            while True:
                chunk = os.read(file_fd, min(1024 * 1024, MAX_FILE_BYTES + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > MAX_FILE_BYTES:
                    raise HookPolicyError("hook_policy_required")
            return bytes(content)
        finally:
            os.close(file_fd)
    except (OSError, ValueError) as exc:
        raise HookPolicyError("hook_policy_required") from exc
    finally:
        os.close(directory_fd)


def _write_relative_nofollow(root: Path, relative: Path, content: bytes, mode: int) -> None:
    """Write pinned bytes without following an existing destination symlink."""
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise HookPolicyError("hook_policy_required")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory_fd = os.open(root, flags)
    except (OSError, ValueError) as exc:
        raise HookPolicyError("hook_policy_required") from exc
    try:
        for part in relative.parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=directory_fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            relative.parts[-1],
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            mode & 0o777,
            dir_fd=directory_fd,
        )
        try:
            view = memoryview(content)
            while view:
                written = os.write(file_fd, view)
                view = view[written:]
            os.fchmod(file_fd, mode & 0o777)
        finally:
            os.close(file_fd)
    except (OSError, ValueError) as exc:
        raise HookPolicyError("hook_policy_required") from exc
    finally:
        os.close(directory_fd)


def _resolve_hook_policy(
    source: Path, approved: Mapping[str, Mapping[str, object]]
) -> tuple[dict, dict[str, bytes], dict[str, bytes]]:
    config = _effective_config(source)
    hook_files, hooks_path = _hook_files(source)
    hook_dir, _ = _effective_hooks(source)
    if not _within(hook_dir, source) or len(hook_files) > MAX_HOOKS:
        raise HookPolicyError("hook_policy_required")
    names = {path.name for path in hook_files}
    if names and set(approved) != names:
        raise HookPolicyError("hook_policy_required")
    if not names:
        if approved:
            raise HookPolicyError("hook_policy_required")
        payload = {"config": config, "hooks": {}, "hooks_path": hooks_path}
        payload["config_fingerprint"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        payload["digest"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return payload, {}, {}

    canonical: dict[str, dict[str, object]] = {}
    hook_contents: dict[str, bytes] = {}
    dependency_contents: dict[str, bytes] = {}
    total_bytes = 0
    total_dependency_entries = 0
    for hook in hook_files:
        entry = approved.get(hook.name)
        if not isinstance(entry, Mapping):
            raise HookPolicyError("hook_policy_required")
        expected = {"sha256", "dependencies", "phase", "expected_exit"}
        if set(entry) != expected or entry["phase"] != "git" or entry["expected_exit"] != 0:
            raise HookPolicyError("hook_policy_required")
        try:
            hook_relative = hook.relative_to(source)
            hook_content = _read_regular_nofollow(source, hook_relative)
        except (OSError, ValueError) as exc:
            raise HookPolicyError("hook_policy_required") from exc
        if entry["sha256"] != _sha256_bytes(hook_content) or not isinstance(entry["sha256"], str):
            raise HookPolicyError("hook_policy_required")
        total_bytes += len(hook_content)
        if total_bytes > MAX_HOOK_CONTENT_BYTES:
            raise HookPolicyError("hook_policy_required")
        hook_contents[hook.name] = hook_content
        dependencies = entry["dependencies"]
        if not isinstance(dependencies, Mapping) or len(dependencies) > MAX_DEPENDENCIES:
            raise HookPolicyError("hook_policy_required")
        total_dependency_entries += len(dependencies)
        if total_dependency_entries > MAX_DEPENDENCIES:
            raise HookPolicyError("hook_policy_required")
        checked_dependencies: dict[str, str] = {}
        for relative, digest in sorted(dependencies.items()):
            if (
                not isinstance(relative, str)
                or not isinstance(digest, str)
                or len(relative.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
            ):
                raise HookPolicyError("hook_policy_required")
            try:
                dependency_content = _read_regular_nofollow(source, Path(relative))
            except (OSError, ValueError) as exc:
                raise HookPolicyError("hook_policy_required") from exc
            if _sha256_bytes(dependency_content) != digest:
                raise HookPolicyError("hook_policy_required")
            if relative not in dependency_contents:
                total_bytes += len(dependency_content)
                if total_bytes > MAX_HOOK_CONTENT_BYTES:
                    raise HookPolicyError("hook_policy_required")
                dependency_contents[relative] = dependency_content
            checked_dependencies[relative] = digest
        canonical[hook.name] = {
            "sha256": entry["sha256"],
            "dependencies": checked_dependencies,
            "phase": "git",
            "expected_exit": 0,
        }
    config_payload = {"config": config, "hooks_path": hooks_path, "hooks": canonical}
    config_fingerprint = hashlib.sha256(
        json.dumps(config_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    result = {**config_payload, "config_fingerprint": config_fingerprint}
    result["digest"] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result, hook_contents, dependency_contents


def resolve_hook_policy(source: Path, approved: Mapping[str, Mapping[str, object]]) -> dict:
    """Resolve effective executable hooks and require byte/config/dependency pins."""
    return _resolve_hook_policy(Path(source).resolve(), approved)[0]


def _guarded_git_dir(
    root: Path,
    policy: ExecutionPolicy,
    environment: Mapping[str, str],
    *,
    executor=None,
    log_dir: Path | None = None,
) -> Path:
    output = _guarded(
        root,
        ("rev-parse", "--git-dir"),
        policy,
        environment,
        executor=executor,
        log_dir=log_dir,
    ).strip()
    candidate = Path(output)
    git_dir = candidate if candidate.is_absolute() else root / candidate
    if not _within(git_dir, root):
        raise HookPolicyError("hook_policy_required")
    return git_dir


def _guarded(
    root: Path,
    args: tuple[str, ...],
    policy: ExecutionPolicy,
    environment: Mapping[str, str],
    *,
    executor=None,
    log_dir: Path | None = None,
) -> str:
    options = {"policy": policy, "environment": environment}
    if executor is not None:
        options["executor"] = executor
    if log_dir is not None:
        options["log_dir"] = log_dir
    return GuardedGit.run(root, ("/usr/bin/git", *args), **options)


def materialize_candidate(
    source: Path,
    base_sha: str,
    destination: Path,
    branch: str,
    policy: ExecutionPolicy,
    *,
    environment: Mapping[str, str],
    hook_policy: Mapping[str, Mapping[str, object]] | None = None,
    executor=None,
    log_dir: Path | None = None,
) -> Path:
    """Copy only the approved base into an independent local candidate repository."""
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    resolved_hooks, hook_contents, dependency_contents = _resolve_hook_policy(source, hook_policy or {})
    if destination.exists() and any(destination.iterdir()):
        raise GitError(f"candidate destination already exists: {destination}")
    if not any(_within(destination, Path(root)) for root in policy.write_roots):
        raise HookPolicyError("candidate_destination_outside_policy")
    destination.mkdir(parents=True, exist_ok=True)
    _trusted_git(source, "rev-parse", f"{base_sha}^{{commit}}")
    staging = destination / ".candidate-staging"
    _guarded(
        source,
        ("clone", "--no-local", "--no-hardlinks", "--no-tags", str(source), str(staging)),
        policy,
        environment,
        executor=executor,
        log_dir=log_dir,
    )
    for child in tuple(staging.iterdir()):
        shutil.move(str(child), str(destination / child.name))
    staging.rmdir()
    _guarded(
        destination,
        ("fetch", "--no-tags", "origin", base_sha),
        policy,
        environment,
        executor=executor,
        log_dir=log_dir,
    )
    _guarded(destination, ("remote", "remove", "origin"), policy, environment, executor=executor, log_dir=log_dir)
    _guarded(destination, ("checkout", "-B", branch, base_sha), policy, environment, executor=executor, log_dir=log_dir)
    candidate_git_dir = _guarded_git_dir(
        destination,
        policy,
        environment,
        executor=executor,
        log_dir=log_dir,
    )
    for name, entry in resolved_hooks["hooks"].items():
        _write_relative_nofollow(candidate_git_dir, Path("hooks") / name, hook_contents[name], 0o755)
        for relative in entry["dependencies"]:
            _write_relative_nofollow(destination, Path(relative), dependency_contents[relative], 0o755)
    if resolved_hooks["hooks"]:
        _guarded(
            destination,
            ("config", "core.hooksPath", ".git/hooks"),
            policy,
            environment,
            executor=executor,
            log_dir=log_dir,
        )
    return destination


def candidate_identity(root: Path, *, policy: ExecutionPolicy, environment: Mapping[str, str]) -> tuple[str, str]:
    """Return the exact candidate head and tracked tree identities through GuardedGit."""
    head = _guarded(Path(root), ("rev-parse", "HEAD"), policy, environment).strip()
    tree = _guarded(Path(root), ("rev-parse", "HEAD^{tree}"), policy, environment).strip()
    return head, tree
