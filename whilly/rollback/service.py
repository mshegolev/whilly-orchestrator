"""Rollback safety-net service logic."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from whilly.rollback.git_ops import GitClient, RollbackError
from whilly.rollback.models import PreflightReport, ProtectionSignal, RestoreResult, RollbackPoint, WorktreeState

UTC = timezone.utc

ROLLBACK_TAG_PREFIX = "whilly/rollback/"
PROTECTED_OPERATIONS = {"push", "merge", "restore"}


def create_rollback_point(
    repo: Path | str = ".",
    *,
    operation: str = "manual",
    message: str | None = None,
    now: datetime | None = None,
) -> RollbackPoint:
    """Create an annotated rollback tag at HEAD."""
    client = GitClient(repo)
    repo_root = Path(client.require("rev-parse", "--show-toplevel").strip()).resolve()
    root_client = GitClient(repo_root)
    branch = root_client.require("branch", "--show-current").strip() or "detached"
    head_sha = root_client.require("rev-parse", "HEAD").strip()
    created_at = _utc(now)
    tag_name = f"{ROLLBACK_TAG_PREFIX}{_sanitize_branch(branch)}/{_tag_timestamp(created_at)}-{head_sha[:12]}"
    tag_message = message if message is not None else f"Whilly rollback point before {operation}"

    root_client.require("check-ref-format", f"refs/tags/{tag_name}")
    root_client.require("tag", "-a", tag_name, "-m", tag_message, head_sha, timeout=60.0)
    return RollbackPoint(
        name=tag_name,
        target_sha=head_sha,
        branch=branch,
        created_at=created_at,
        message=tag_message,
    )


def list_rollback_points(repo: Path | str = ".", *, branch: str | None = None) -> tuple[RollbackPoint, ...]:
    """Return deterministic Whilly rollback tags."""
    client = GitClient(repo)
    repo_root = Path(client.require("rev-parse", "--show-toplevel").strip()).resolve()
    root_client = GitClient(repo_root)
    output = root_client.require(
        "for-each-ref",
        "refs/tags/whilly/rollback",
        "--format=%(refname:short)%09%(*objectname)%09%(taggerdate:iso-strict)%09%(contents:subject)",
    )
    wanted_branch = _sanitize_branch(branch) if branch else None
    points: list[RollbackPoint] = []
    for line in output.splitlines():
        if not line:
            continue
        name, target_sha, taggerdate, message = _split_ref_line(line)
        if not name.startswith(ROLLBACK_TAG_PREFIX) or not target_sha:
            continue
        parsed_branch = _branch_from_tag_name(name)
        if wanted_branch is not None and parsed_branch != wanted_branch:
            continue
        points.append(
            RollbackPoint(
                name=name,
                target_sha=target_sha,
                branch=parsed_branch,
                created_at=_created_at_from_tag(name, taggerdate),
                message=message or None,
            )
        )
    return tuple(sorted(points, key=lambda point: point.name))


def build_preflight_report(
    repo: Path | str = ".",
    *,
    operation: str,
    target_ref: str | None = None,
    protection_probe: Callable[[Path, str], ProtectionSignal] | None = None,
) -> PreflightReport:
    """Build a structured report before push, merge, or restore operations."""
    operation = operation.strip().lower()
    client = GitClient(repo)
    root_result = client.run("rev-parse", "--show-toplevel")
    if root_result.returncode != 0:
        repo_root = Path(repo).resolve()
        worktree = WorktreeState(
            repo_root=repo_root,
            branch="",
            head_sha="",
            upstream=None,
            dirty=False,
            dirty_entries=(),
        )
        return PreflightReport(
            operation=operation,
            worktree=worktree,
            backup_points=(),
            protection=ProtectionSignal(provider="", status="unknown", reason="not requested"),
            blockers=("not a git repository",),
            warnings=(),
        )

    repo_root = Path(root_result.stdout.strip()).resolve()
    root_client = GitClient(repo_root)
    branch = root_client.require("branch", "--show-current").strip()
    head_sha = root_client.require("rev-parse", "HEAD").strip()
    upstream = _optional_stdout(root_client, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    dirty_entries = _dirty_entries(root_client)
    dirty = bool(dirty_entries)
    backup_points = _rollback_points_at_head(root_client, head_sha)
    worktree = WorktreeState(
        repo_root=repo_root,
        branch=branch,
        head_sha=head_sha,
        upstream=upstream,
        dirty=dirty,
        dirty_entries=dirty_entries,
    )
    protection_key = target_ref or branch
    protection = _protection_signal(repo_root, protection_key, protection_probe)
    blockers: list[str] = []
    warnings: list[str] = []

    if dirty and operation in PROTECTED_OPERATIONS:
        blockers.append("dirty worktree")
    if not branch:
        if operation in {"merge", "restore"}:
            blockers.append("detached HEAD")
        elif operation == "push":
            warnings.append("detached HEAD")
    if protection.status == "protected" and operation in PROTECTED_OPERATIONS:
        blockers.append(f"target {protection_key} is protected: {protection.reason}")
    elif protection.status == "unknown":
        warnings.append(f"branch protection unknown for {protection_key}: {protection.reason}")
    if not backup_points:
        warnings.append("no rollback point at current HEAD")

    return PreflightReport(
        operation=operation,
        worktree=worktree,
        backup_points=backup_points,
        protection=protection,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )


def confirmation_phrase(report: PreflightReport, target_sha: str) -> str:
    """Return the exact confirmation phrase required for restore."""
    return f"restore {target_sha[:12]} to {report.worktree.branch}"


def restore_to_ref(repo: Path | str, target_ref: str, *, confirm: str = "", dry_run: bool = False) -> RestoreResult:
    """Restore a clean worktree to ``target_ref`` after exact confirmation."""
    report = build_preflight_report(repo, operation="restore", target_ref=target_ref)
    if not report.ok:
        raise RollbackError("; ".join(report.blockers))

    repo_root = Path(report.worktree.repo_root)
    client = GitClient(repo_root)
    target_sha = client.require("rev-parse", target_ref).strip()
    expected = confirmation_phrase(report, target_sha)
    if confirm != expected:
        raise RollbackError(f"confirmation required: {expected}")

    if dry_run:
        return RestoreResult(
            repo_root=repo_root,
            branch=report.worktree.branch,
            target_ref=target_ref,
            target_sha=target_sha,
            dry_run=True,
            reset_performed=False,
            preflight=report,
            message="dry run: no reset performed",
        )

    client.require("reset", "--hard", target_sha, timeout=60.0)
    return RestoreResult(
        repo_root=repo_root,
        branch=report.worktree.branch,
        target_ref=target_ref,
        target_sha=target_sha,
        dry_run=False,
        reset_performed=True,
        preflight=report,
        message=f"restored {target_sha[:12]}",
    )


def _optional_stdout(client: GitClient, *args: str) -> str | None:
    result = client.run(*args)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _dirty_entries(client: GitClient) -> tuple[str, ...]:
    result = client.run("status", "--porcelain=v1")
    if result.returncode != 0:
        raise RollbackError((result.stderr or result.stdout or "git status failed").strip())
    return tuple(line for line in result.stdout.splitlines() if line)


def _rollback_points_at_head(client: GitClient, head_sha: str) -> tuple[RollbackPoint, ...]:
    result = client.run("tag", "--points-at", "HEAD", "--list", f"{ROLLBACK_TAG_PREFIX}*")
    if result.returncode != 0:
        raise RollbackError((result.stderr or result.stdout or "git tag lookup failed").strip())
    names = sorted(line.strip() for line in result.stdout.splitlines() if line.strip())
    points = [
        RollbackPoint(
            name=name,
            target_sha=head_sha,
            branch=_branch_from_tag_name(name),
            created_at=_created_at_from_tag(name, ""),
            message=_tag_subject(client, name),
        )
        for name in names
    ]
    return tuple(points)


def _tag_subject(client: GitClient, name: str) -> str | None:
    result = client.run("for-each-ref", f"refs/tags/{name}", "--format=%(contents:subject)")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _protection_signal(
    repo_root: Path,
    key: str,
    protection_probe: Callable[[Path, str], ProtectionSignal] | None,
) -> ProtectionSignal:
    if protection_probe is None:
        return ProtectionSignal(provider="", status="unknown", reason="not requested")
    try:
        signal = protection_probe(repo_root, key)
    except Exception as exc:  # noqa: BLE001 - protection evidence must fail closed to unknown
        return ProtectionSignal(provider="", status="unknown", reason=f"unavailable: {exc}")
    if not isinstance(signal, ProtectionSignal):
        return ProtectionSignal(provider="", status="unknown", reason="unavailable: invalid probe result")
    return signal


def _split_ref_line(line: str) -> tuple[str, str, str, str]:
    parts = line.split("\t", 3)
    while len(parts) < 4:
        parts.append("")
    return parts[0], parts[1], parts[2], parts[3]


def _branch_from_tag_name(name: str) -> str:
    rest = name.removeprefix(ROLLBACK_TAG_PREFIX)
    branch, _separator, _stamp = rest.rpartition("/")
    return branch or "detached"


def _created_at_from_tag(name: str, taggerdate: str) -> datetime:
    timestamp = name.rsplit("/", 1)[-1].split("-", 1)[0]
    try:
        return datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        if taggerdate:
            return datetime.fromisoformat(taggerdate.replace("Z", "+00:00")).astimezone(UTC)
    return datetime.fromtimestamp(0, tz=UTC)


def _sanitize_branch(branch: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", branch or "detached").strip("-._")
    return cleaned[:96] or "detached"


def _tag_timestamp(value: datetime) -> str:
    return _utc(value).strftime("%Y%m%dT%H%M%SZ")


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).replace(microsecond=0)
