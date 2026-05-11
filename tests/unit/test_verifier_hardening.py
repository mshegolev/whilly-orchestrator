"""Verifier hardening tests (M1 VAL-SEC-029 / VAL-SEC-030 / VAL-SEC-031).

Two surfaces are exercised:

* ``_run_lint`` and ``_run_tests`` must place a literal ``--`` element
  between flag arguments and file arguments derived from ``git diff`` —
  so a path beginning with ``-`` (e.g. ``--exclude.py``) cannot be
  re-interpreted as a flag by ruff/pytest.

* ``_revert_last_commit`` must only invoke ``git reset --soft HEAD~1``
  when HEAD's most recent commit (a) was authored by the orchestrator's
  expected author email AND (b) has a commit timestamp ≥ the task's
  start timestamp. On either mismatch no subprocess argv is produced,
  HEAD is unchanged, and a structured warning containing ``refus`` is
  emitted.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from whilly import verifier


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace ``verifier.subprocess.run`` with a stub that records argvs."""
    captured: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)
    return captured


def _assert_dash_separator(argv: list[str], files: list[str]) -> None:
    """Every file token must come AFTER the literal ``--`` element."""
    assert "--" in argv, f"missing -- separator in argv: {argv!r}"
    sep_idx = argv.index("--")
    for f in files:
        assert f in argv, f"file {f!r} missing from argv: {argv!r}"
        assert argv.index(f) > sep_idx, f"file {f!r} positioned before -- separator at idx {sep_idx} in argv: {argv!r}"
    flags_before = argv[:sep_idx]
    for token in flags_before:
        assert token not in files, f"file token {token!r} found before -- in argv: {argv!r}"


# ─── VAL-SEC-029 (lint) ───────────────────────────────────────────────


def test_lint_argv_includes_double_dash_separator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adversarial filename starting with ``-`` must be treated as a positional."""
    captured = _capture_run(monkeypatch)
    files = ["whilly/cli.py", "--exclude.py"]

    ok, _ = verifier._run_lint(files)

    assert ok is True
    assert len(captured) == 1
    argv = captured[0]
    assert argv[0] == "ruff"
    assert argv[1] == "check"
    _assert_dash_separator(argv, files)


def test_lint_argv_separator_after_all_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--no-fix`` and any other flag must precede the ``--`` separator."""
    captured = _capture_run(monkeypatch)
    files = ["a.py", "-b.py"]

    verifier._run_lint(files)

    argv = captured[0]
    sep_idx = argv.index("--")
    assert "--no-fix" in argv[:sep_idx], f"--no-fix flag must come before -- separator: argv={argv!r}"


# ─── VAL-SEC-029 (pytest) ─────────────────────────────────────────────


def test_pytest_argv_includes_double_dash_separator(monkeypatch: pytest.MonkeyPatch) -> None:
    """File arguments to pytest must be after a literal ``--`` token."""
    captured = _capture_run(monkeypatch)
    files = ["tests/test_a.py", "tests/--evil.py"]

    ok, _ = verifier._run_tests(files)

    assert ok is True
    assert len(captured) == 1
    argv = captured[0]
    assert argv[0] == "python"
    assert argv[1] == "-m"
    assert argv[2] == "pytest"
    _assert_dash_separator(argv, files)


def test_pytest_argv_separator_after_pytest_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--timeout=60``, ``-x``, ``-q`` must all sit before the ``--`` separator."""
    captured = _capture_run(monkeypatch)
    files = ["tests/test_x.py"]

    verifier._run_tests(files)

    argv = captured[0]
    sep_idx = argv.index("--")
    flags_seen = set(argv[:sep_idx])
    assert "--timeout=60" in flags_seen
    assert "-x" in flags_seen
    assert "-q" in flags_seen


# ─── VAL-SEC-030: refuse on author mismatch ──────────────────────────


def test_revert_refuses_on_author_mismatch(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """HEAD authored by someone else → no git reset, warning emitted."""
    captured = _capture_run(monkeypatch)

    monkeypatch.setattr(
        verifier,
        "_get_head_commit_info",
        lambda: ("attacker@evil.example", int(time.time())),
    )

    with caplog.at_level(logging.WARNING, logger="whilly.verifier"):
        ok = verifier._revert_last_commit(
            expected_author="orchestrator@whilly.local",
            task_start_ts=time.time() - 60,
            task_id="T-author-mismatch",
        )

    assert ok is False
    git_resets = [c for c in captured if c[:2] == ["git", "reset"]]
    assert git_resets == [], f"git reset must not be invoked on author mismatch: {captured!r}"

    matching = [r for r in caplog.records if "refus" in r.getMessage().lower()]
    assert matching, f"no refusal warning emitted; records={[r.getMessage() for r in caplog.records]!r}"
    msg = " ".join(r.getMessage() for r in matching)
    assert "attacker@evil.example" in msg, f"offending author email missing from refusal warning: {msg!r}"


# ─── VAL-SEC-030: refuse on timestamp predates task_start ────────────


def test_revert_refuses_on_timestamp_predates_task_start(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """HEAD commit timestamp predates task start → no git reset, warning emitted."""
    captured = _capture_run(monkeypatch)

    head_ts = int(time.time()) - 3600  # one hour ago
    task_start = time.time()  # now (after the head commit)

    monkeypatch.setattr(
        verifier,
        "_get_head_commit_info",
        lambda: ("orchestrator@whilly.local", head_ts),
    )

    with caplog.at_level(logging.WARNING, logger="whilly.verifier"):
        ok = verifier._revert_last_commit(
            expected_author="orchestrator@whilly.local",
            task_start_ts=task_start,
            task_id="T-ts-mismatch",
        )

    assert ok is False
    git_resets = [c for c in captured if c[:2] == ["git", "reset"]]
    assert git_resets == [], f"git reset must not be invoked on timestamp mismatch: {captured!r}"

    matching = [r for r in caplog.records if "refus" in r.getMessage().lower()]
    assert matching, f"no refusal warning emitted; records={[r.getMessage() for r in caplog.records]!r}"
    msg = " ".join(r.getMessage() for r in matching)
    assert str(head_ts) in msg or str(int(task_start)) in msg, (
        f"offending timestamp value missing from refusal warning: {msg!r}"
    )


# ─── VAL-SEC-031: legitimate match → reset proceeds (real git repo) ──


def _git(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    cmd_env = os.environ.copy()
    if env:
        cmd_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=cmd_env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_revert_proceeds_on_legitimate_match(tmp_path: Path) -> None:
    """Real tempdir git repo: orchestrator-authored post-task-start commit is rolled back."""
    repo = tmp_path / "repo"
    repo.mkdir()

    expected_author = "orchestrator@whilly.local"
    base_env = {
        "GIT_AUTHOR_NAME": "Whilly Orchestrator",
        "GIT_AUTHOR_EMAIL": expected_author,
        "GIT_COMMITTER_NAME": "Whilly Orchestrator",
        "GIT_COMMITTER_EMAIL": expected_author,
    }

    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", expected_author)
    _git(repo, "config", "user.name", "Whilly Orchestrator")
    (repo / "a.txt").write_text("first\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "first", env=base_env)

    first_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    task_start = time.time() - 1
    time.sleep(1.1)

    (repo / "b.txt").write_text("second\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "second", env=base_env)

    second_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert second_sha != first_sha

    cwd = os.getcwd()
    try:
        os.chdir(str(repo))
        ok = verifier._revert_last_commit(
            expected_author=expected_author,
            task_start_ts=task_start,
            task_id="T-legit",
        )
    finally:
        os.chdir(cwd)

    assert ok is True
    after_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert after_sha == first_sha, (
        f"HEAD should have moved back to first commit; got {after_sha!r}, expected {first_sha!r}"
    )


def test_revert_refuses_when_head_info_unavailable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If git log fails to return HEAD info, the path refuses safely."""
    captured = _capture_run(monkeypatch)
    monkeypatch.setattr(verifier, "_get_head_commit_info", lambda: None)

    with caplog.at_level(logging.WARNING, logger="whilly.verifier"):
        ok = verifier._revert_last_commit(
            expected_author="orchestrator@whilly.local",
            task_start_ts=time.time() - 60,
            task_id="T-no-info",
        )

    assert ok is False
    git_resets = [c for c in captured if c[:2] == ["git", "reset"]]
    assert git_resets == []
    matching = [r for r in caplog.records if "refus" in r.getMessage().lower()]
    assert matching, "missing refusal warning when HEAD info unavailable"


# ─── verify_task plumbs through expected_author + task_start_ts ──────


def test_verify_task_failed_revert_passes_guard_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When verification fails, ``verify_task`` must not invoke ``git reset``
    if HEAD doesn't match the orchestrator's expected author/timestamp guard."""
    monkeypatch.setattr(verifier, "_get_changed_files", lambda: ["whilly/x.py"])
    monkeypatch.setattr(verifier, "_run_lint", lambda files: (False, "boom"))
    monkeypatch.setattr(verifier, "_run_tests", lambda files: (True, ""))
    monkeypatch.setattr(
        verifier,
        "_get_head_commit_info",
        lambda: ("attacker@evil.example", int(time.time())),
    )
    captured = _capture_run(monkeypatch)

    result = verifier.verify_task(
        "T-plumb",
        log_dir=tmp_path,
        revert_on_fail=True,
        expected_author="orchestrator@whilly.local",
        task_start_ts=time.time() - 60,
    )

    assert result.passed is False
    assert result.reverted is False
    git_resets = [c for c in captured if c[:2] == ["git", "reset"]]
    assert git_resets == [], f"verify_task must not run git reset on author mismatch: captured={captured!r}"
