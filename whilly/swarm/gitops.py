"""Git helpers for swarm worktrees.

Registered checkouts (including bare repositories) are only read from: every
attempt gets a brand-new worktree on a new branch created at the base SHA
recorded when the plan revision was applied. Worktrees, branches and logs
are retained for inspection; nothing is merged or pushed.
"""

from __future__ import annotations

import subprocess
import os
from collections.abc import Mapping
from pathlib import Path

from whilly.core.swarm_execution import ExecutionPolicy
from whilly.swarm.execution import GuardedExecutor

__all__ = [
    "GitError",
    "GuardedGit",
    "changed_files",
    "commit_task_changes",
    "create_worktree",
    "diff_text",
    "dirty_files",
    "git",
    "head_sha",
    "tree_sha",
    "read_instruction_files",
    "resolve_commit",
    "resolve_git_toolchain",
]

INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
_MAX_FILE_CHARS = 20_000
XCODE_GIT = Path("/Applications/Xcode.app/Contents/Developer/usr/bin/git")
XCODE_GIT_EXEC_PATH = Path("/Applications/Xcode.app/Contents/Developer/usr/libexec/git-core")


class GitError(RuntimeError):
    pass


def resolve_git_toolchain(
    git_executable: str | Path | None = None,
    git_exec_path: str | Path | None = None,
) -> tuple[Path, Path]:
    """Resolve a validated coordinator Git binary and its explicit helper root."""
    if git_executable is None:
        executable = XCODE_GIT
    else:
        executable = Path(git_executable)
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise GitError("git_toolchain_unavailable")
    executable = executable.resolve(strict=True)
    if git_exec_path is None:
        if executable == XCODE_GIT.resolve(strict=False):
            helper_root = XCODE_GIT_EXEC_PATH
        else:
            raise GitError("git_toolchain_requires_explicit_helper_root")
    else:
        helper_root = Path(git_exec_path)
    if not helper_root.is_absolute() or not helper_root.is_dir():
        raise GitError("git_toolchain_unavailable")
    helper_root = helper_root.resolve(strict=True)
    helper = helper_root / "git"
    if not helper.is_file() or not os.access(helper, os.X_OK):
        raise GitError("git_toolchain_unavailable")
    return executable, helper_root


def _path_within(child: Path, parent: Path) -> bool:
    try:
        # macOS exposes temporary directories through both /var and the
        # /private/var symlink. Canonicalize both sides before the boundary
        # check so a valid guarded log root is not rejected by spelling.
        child_path = child.resolve(strict=False)
        parent_path = parent.resolve(strict=False)
        return os.path.commonpath((str(child_path), str(parent_path))) == str(parent_path)
    except ValueError:
        return False


class GuardedGit:
    """Synchronous Git seam for candidate-consuming coordinator operations."""

    @staticmethod
    def run(
        root: Path,
        argv: tuple[str, ...],
        *,
        policy: ExecutionPolicy,
        environment: Mapping[str, str],
        git_executable: str | Path | None = None,
        git_exec_path: str | Path | None = None,
        executor: GuardedExecutor | None = None,
        log_dir: Path | None = None,
    ) -> str:
        if not argv:
            raise GitError("guarded Git requires a non-empty argv")
        executable, helper_root = resolve_git_toolchain(git_executable, git_exec_path)
        if argv[0] not in {"git", "/usr/bin/git", str(executable)}:
            raise GitError("guarded Git argv does not match the validated executable")
        argv = (str(executable), *argv[1:])
        root = Path(root).resolve(strict=False)
        if not root.exists() or not any(
            _path_within(root, Path(allowed).resolve(strict=False))
            for allowed in (*policy.read_roots, *policy.write_roots)
        ):
            raise GitError("guarded_git_root_outside_policy")
        env = dict(environment)
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_EXEC_PATH": str(helper_root),
            }
        )
        write_roots = tuple(Path(value) for value in policy.write_roots)
        if not write_roots:
            raise GitError("guarded Git requires an explicit writable log root")
        log_candidates = []
        if environment.get("TMPDIR"):
            log_candidates.append(Path(environment["TMPDIR"]))
        git_metadata = root / ".git"
        if git_metadata.is_dir():
            log_candidates.append(git_metadata)
        log_root = next(
            (
                candidate
                for candidate in log_candidates
                if candidate.exists() and any(_path_within(candidate, allowed) for allowed in write_roots)
            ),
            None,
        )
        if log_root is None:
            raise GitError("git_log_root_unavailable")
        log_root.mkdir(parents=True, exist_ok=True)
        if log_dir is not None:
            logs = Path(log_dir)
        else:
            # TMPDIR is a child-write root; host logs must live beside it so
            # the child cannot rewrite the coordinator's evidence boundary.
            host_log_parent = Path(environment["TMPDIR"]).resolve().parent if environment.get("TMPDIR") else root.parent
            logs = host_log_parent / ".guarded-git-attempt"
        logs.mkdir(parents=True, exist_ok=True)
        runner = executor or GuardedExecutor()
        result = runner.run_sync(
            argv,
            phase="git",
            policy=policy,
            cwd=root,
            environment=env,
            log_dir=logs,
        )
        stdout_path = Path(result.stdout_path)
        stderr_path = Path(result.stderr_path)
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
        if result.reason is not None or result.exit_code != 0:
            detail = stderr.strip() or stdout.strip() or result.reason or "unknown failure"
            raise GitError(f"guarded git {' '.join(argv[1:])} failed: {detail[:2000]}")
        return stdout


def git(cwd: str | Path, *args: str, timeout: float = 120) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {' '.join(args)} failed: {exc}") from exc
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()[:2000]}")
    return proc.stdout


def resolve_commit(repo: str | Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def create_worktree(
    repo: str | Path,
    path: Path,
    branch: str,
    base_sha: str,
    *,
    policy: ExecutionPolicy | None = None,
    environment: Mapping[str, str] | None = None,
    executor: GuardedExecutor | None = None,
    log_dir: Path | None = None,
) -> None:
    """Create ``path`` as a new worktree on new ``branch`` at ``base_sha``."""
    if path.exists():
        raise GitError(f"worktree path already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if policy is None:
        git(repo, "worktree", "add", "-b", branch, str(path), base_sha)
        return
    GuardedGit.run(
        Path(repo),
        ("/usr/bin/git", "worktree", "add", "-b", branch, str(path), base_sha),
        policy=policy,
        environment=environment or {},
        executor=executor,
        log_dir=log_dir,
    )


def head_sha(worktree: str | Path, **guard) -> str:
    out = (
        GuardedGit.run(Path(worktree), ("/usr/bin/git", "rev-parse", "HEAD"), **guard)
        if guard.get("policy") is not None
        else git(worktree, "rev-parse", "HEAD")
    )
    return out.strip()


def tree_sha(worktree: str | Path, **guard) -> str:
    out = (
        GuardedGit.run(Path(worktree), ("/usr/bin/git", "rev-parse", "HEAD^{tree}"), **guard)
        if guard.get("policy") is not None
        else git(worktree, "rev-parse", "HEAD^{tree}")
    )
    return out.strip()


def commit_task_changes(
    worktree: str | Path,
    *,
    expected_branch: str,
    task_id: str,
    policy: ExecutionPolicy | None = None,
    environment: Mapping[str, str] | None = None,
    executor: GuardedExecutor | None = None,
    log_dir: Path | None = None,
) -> str:
    """Record a sandbox worker's files on the coordinator-created task branch.

    Workers need no Git metadata write permission. This trusted operation is
    scoped to the fresh task worktree, with literal pathspecs and normal hooks.
    Verification and independent review must still succeed before acceptance.
    """
    if policy is not None:

        def run(*args: str) -> str:
            return GuardedGit.run(
                Path(worktree),
                ("/usr/bin/git", *args),
                policy=policy,
                environment=environment or {},
                executor=executor,
                log_dir=log_dir,
            )
    else:
        raise GitError("guarded_git_policy_required")

    branch = run("symbolic-ref", "--short", "HEAD").strip()
    if not expected_branch.startswith("swarm/") or branch != expected_branch:
        raise GitError("refusing coordinator commit outside the expected swarm branch")
    paths = sorted(
        set(
            filter(
                None,
                run("ls-files", "--modified", "--deleted", "--others", "--exclude-standard", "-z").split("\0"),
            )
        )
    )
    if len(paths) > 500:
        raise GitError("task change set exceeds 500-file commit limit")
    if paths:
        run("--literal-pathspecs", "add", "--", *paths)
    if run("diff", "--cached", "--name-only", "-z"):
        run(
            "-c",
            "user.name=Whilly Swarm",
            "-c",
            "user.email=swarm@localhost",
            "commit",
            "-m",
            f"swarm: {task_id}",
        )
    return run("rev-parse", "HEAD").strip()


def dirty_files(
    worktree: str | Path,
    *,
    policy: ExecutionPolicy | None = None,
    environment: Mapping[str, str] | None = None,
    executor: GuardedExecutor | None = None,
    log_dir: Path | None = None,
) -> list[str]:
    out = (
        GuardedGit.run(
            Path(worktree),
            ("/usr/bin/git", "status", "--porcelain", "--untracked-files=all"),
            policy=policy,
            environment=environment or {},
            executor=executor,
            log_dir=log_dir,
        )
        if policy is not None
        else git(worktree, "status", "--porcelain", "--untracked-files=all")
    )
    return [line[3:] for line in out.splitlines() if line.strip()]


def changed_files(worktree: str | Path, base: str, head: str, **guard) -> list[str]:
    out = (
        GuardedGit.run(Path(worktree), ("/usr/bin/git", "diff", "--name-only", base, head), **guard)
        if guard.get("policy") is not None
        else git(worktree, "diff", "--name-only", base, head)
    )
    return [line for line in out.splitlines() if line.strip()]


def diff_text(worktree: str | Path, base: str, head: str, *, limit: int = 60_000, **guard) -> str:
    out = (
        GuardedGit.run(Path(worktree), ("/usr/bin/git", "diff", "--stat", "--patch", base, head), **guard)
        if guard.get("policy") is not None
        else git(worktree, "diff", "--stat", "--patch", base, head)
    )
    if len(out) > limit:
        return out[:limit] + f"\n[diff truncated at {limit} characters]\n"
    return out


def read_instruction_files(root: Path, extra: tuple[str, ...] = ()) -> dict[str, str]:
    """Return bounded contents of repository instructions and context files."""
    found: dict[str, str] = {}
    for rel in (*INSTRUCTION_FILES, *extra):
        if rel in found:
            continue
        candidate = (root / rel).resolve()
        if root.resolve() not in candidate.parents and candidate != root.resolve():
            continue
        if not candidate.is_file():
            continue
        text = candidate.read_text(encoding="utf-8", errors="replace")
        if len(text) > _MAX_FILE_CHARS:
            text = text[:_MAX_FILE_CHARS] + f"\n[truncated at {_MAX_FILE_CHARS} characters]\n"
        found[rel] = text
    return found
