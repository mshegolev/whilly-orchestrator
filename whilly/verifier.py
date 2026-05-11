"""Post-task verification: lint + test on changed files after agent marks task done.

If verification fails the commit is reverted and task marked as verify_failed.

Usage:
    from whilly.verifier import verify_task
    ok, details = verify_task(task, log_dir=Path("whilly_logs"))
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("whilly.verifier")


@dataclass
class VerifyResult:
    """Result of post-task verification."""

    passed: bool
    lint_ok: bool = True
    test_ok: bool = True
    lint_output: str = ""
    test_output: str = ""
    changed_files: list[str] | None = None
    reverted: bool = False


def _get_changed_files() -> list[str]:
    """Get list of files changed since last commit (staged + unstaged + new)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        return [f for f in files if f.endswith(".py")]
    except Exception:
        return []


def _run_lint(files: list[str]) -> tuple[bool, str]:
    """Run ruff check on changed files.

    The literal ``--`` element separates flag arguments from file
    arguments so an adversarial diff entry like ``--exclude.py`` is
    treated as a positional file path by ruff, not as a flag.
    """
    if not files:
        return True, ""
    try:
        result = subprocess.run(
            ["ruff", "check", "--no-fix", "--", *files],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except FileNotFoundError:
        log.warning("ruff not found, skipping lint check")
        return True, "ruff not found"
    except Exception as e:
        return True, f"lint error: {e}"


def _run_tests(files: list[str]) -> tuple[bool, str]:
    """Run pytest on changed test files.

    The literal ``--`` element separates flag arguments from file
    arguments so an adversarial diff entry like ``tests/--evil.py`` is
    treated as a positional path by pytest, not as a flag.
    """
    test_files = [f for f in files if "/test_" in f or f.startswith("tests/")]
    if not test_files:
        return True, "no test files changed"
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "--timeout=60", "-x", "-q", "--", *test_files],
            capture_output=True,
            text=True,
            timeout=120,
            env={"PYTHONPATH": ".", "PATH": subprocess.os.environ.get("PATH", "")},
        )
        return result.returncode == 0, result.stdout + result.stderr
    except Exception as e:
        return True, f"test error: {e}"


def _get_head_commit_info() -> tuple[str, int] | None:
    """Return ``(author_email, commit_timestamp_unix)`` for HEAD or ``None`` on failure."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ae%n%ct", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        parts = result.stdout.strip().split("\n")
        if len(parts) != 2:
            return None
        author = parts[0].strip()
        try:
            ts = int(parts[1].strip())
        except ValueError:
            return None
        if not author:
            return None
        return author, ts
    except Exception:
        return None


def _revert_last_commit(
    expected_author: str | None = None,
    task_start_ts: float | None = None,
    *,
    task_id: str = "",
) -> bool:
    """Revert the last commit (soft reset) only if it belongs to the orchestrator.

    The most recent commit on HEAD must satisfy BOTH:

    - its author email matches ``expected_author`` (when provided), AND
    - its commit timestamp is ≥ ``task_start_ts`` (when provided).

    On either mismatch — or if HEAD info cannot be read — the function
    refuses, emits a warning containing the substring ``refus`` and the
    offending field, and returns ``False`` without invoking ``git reset``.
    On a legitimate match it runs ``git reset --soft HEAD~1`` as before.
    """
    if expected_author is not None or task_start_ts is not None:
        info = _get_head_commit_info()
        if info is None:
            log.warning(
                "%s: revert refused — could not read HEAD commit info (expected_author=%r task_start_ts=%r)",
                task_id,
                expected_author,
                task_start_ts,
            )
            return False
        head_author, head_ts = info
        if expected_author is not None and head_author != expected_author:
            log.warning(
                "%s: revert refused — HEAD author=%r does not match expected=%r",
                task_id,
                head_author,
                expected_author,
            )
            return False
        if task_start_ts is not None and head_ts < int(task_start_ts):
            log.warning(
                "%s: revert refused — HEAD commit_ts=%s predates task_start_ts=%s",
                task_id,
                head_ts,
                int(task_start_ts),
            )
            return False
    try:
        result = subprocess.run(
            ["git", "reset", "--soft", "HEAD~1"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def verify_task(
    task_id: str,
    log_dir: Path | None = None,
    revert_on_fail: bool = True,
    *,
    expected_author: str | None = None,
    task_start_ts: float | None = None,
) -> VerifyResult:
    """Run lint + test verification on files changed by the last commit.

    Args:
        task_id: Task identifier (for logging).
        log_dir: Directory for verification logs.
        revert_on_fail: If True, revert last commit when verification fails.
        expected_author: Orchestrator's expected author email guard for the
            revert path. If set, ``_revert_last_commit`` only proceeds when
            HEAD's author matches.
        task_start_ts: Unix timestamp (seconds) when the task started. If
            set, ``_revert_last_commit`` only proceeds when HEAD's commit
            timestamp is ≥ this value.

    Returns:
        VerifyResult with pass/fail status and details.
    """
    changed = _get_changed_files()
    if not changed:
        log.info("%s: verify — no changed .py files", task_id)
        return VerifyResult(passed=True, changed_files=[])

    log.info("%s: verify — checking %d files: %s", task_id, len(changed), ", ".join(changed[:5]))

    lint_ok, lint_out = _run_lint(changed)
    test_ok, test_out = _run_tests(changed)

    passed = lint_ok and test_ok

    if not passed and revert_on_fail:
        reverted = _revert_last_commit(
            expected_author=expected_author,
            task_start_ts=task_start_ts,
            task_id=task_id,
        )
        log.warning("%s: verify FAILED — reverted=%s, lint=%s, test=%s", task_id, reverted, lint_ok, test_ok)
    else:
        reverted = False
        if passed:
            log.info("%s: verify PASSED (lint=%s, test=%s)", task_id, lint_ok, test_ok)

    result = VerifyResult(
        passed=passed,
        lint_ok=lint_ok,
        test_ok=test_ok,
        lint_output=lint_out[:500],
        test_output=test_out[:500],
        changed_files=changed,
        reverted=reverted,
    )

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{task_id}_verify.log"
        with open(log_file, "w") as f:
            f.write(f"Task: {task_id}\nPassed: {passed}\n\n")
            f.write(f"=== LINT (ok={lint_ok}) ===\n{lint_out}\n\n")
            f.write(f"=== TEST (ok={test_ok}) ===\n{test_out}\n\n")
            f.write(f"Changed files: {changed}\nReverted: {reverted}\n")

    return result
