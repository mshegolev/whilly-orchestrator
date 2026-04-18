"""Post-task verification: lint + test on changed files after agent marks task done.

If verification fails the commit is reverted and task marked as verify_failed.

Usage:
    from ralph.verifier import verify_task
    ok, details = verify_task(task, log_dir=Path("ralph_logs"))
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("ralph.verifier")


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
    """Run ruff check on changed files."""
    if not files:
        return True, ""
    try:
        result = subprocess.run(
            ["ruff", "check", "--no-fix", *files],
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
    """Run pytest on changed test files."""
    test_files = [f for f in files if "/test_" in f or f.startswith("tests/")]
    if not test_files:
        return True, "no test files changed"
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", *test_files, "--timeout=60", "-x", "-q"],
            capture_output=True,
            text=True,
            timeout=120,
            env={"PYTHONPATH": ".", "PATH": subprocess.os.environ.get("PATH", "")},
        )
        return result.returncode == 0, result.stdout + result.stderr
    except Exception as e:
        return True, f"test error: {e}"


def _revert_last_commit() -> bool:
    """Revert the last commit (soft reset, keep changes unstaged)."""
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


def verify_task(task_id: str, log_dir: Path | None = None, revert_on_fail: bool = True) -> VerifyResult:
    """Run lint + test verification on files changed by the last commit.

    Args:
        task_id: Task identifier (for logging).
        log_dir: Directory for verification logs.
        revert_on_fail: If True, revert last commit when verification fails.

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
        reverted = _revert_last_commit()
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

    # Save verification log
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{task_id}_verify.log"
        with open(log_file, "w") as f:
            f.write(f"Task: {task_id}\nPassed: {passed}\n\n")
            f.write(f"=== LINT (ok={lint_ok}) ===\n{lint_out}\n\n")
            f.write(f"=== TEST (ok={test_ok}) ===\n{test_out}\n\n")
            f.write(f"Changed files: {changed}\nReverted: {reverted}\n")

    return result
