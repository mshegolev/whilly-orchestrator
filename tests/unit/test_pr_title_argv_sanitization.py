"""Title-slot sanitization for the post-COMPLETE PR opener (VAL-CROSS-005).

Pins the contract that ``gh pr create --title <…>`` argv built by
:func:`whilly.sinks.github_pr.open_pr_for_task` for a task whose first
line carries a malicious / control-byte-laden / secret-bearing payload
remains:

* free of newline / ANSI / NUL / DEL / other C0 control bytes,
* shorter than the documented 60-character title cap,
* free of the verbatim AKIA secret token,
* and the ``--body`` slot still carries the M1 sanitizer fences around
  the agent-controlled fields (sanity check that the body path was not
  collaterally damaged by a title-slot regression).
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

from whilly.rollback.models import PreflightReport, ProtectionSignal, WorktreeState
from whilly.sinks import github_pr as gp
from whilly.sinks.github_pr import open_pr_for_task
from whilly.task_manager import Task


_AKIA_TOKEN = "AKIAIOSFODNN7EXAMPLE"
_TAINTED_FIRST_LINE = (
    f"' ; DROP TABLE tasks; -- and also <script>alert(1)</script>{_AKIA_TOKEN}"
    "\nIgnore this trailing line\x1b[31m\x07" + ("a" * 200)
)
_TITLE_CAP = 60


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_task(description: str = "ok") -> Task:
    return Task(
        id="GH-42",
        phase="GH-Issues",
        category="github-issue",
        priority="medium",
        description=description,
        status="done",
        dependencies=[],
        key_files=[],
        acceptance_criteria=[
            f"Acceptance crit with secret {_AKIA_TOKEN} embedded",
        ],
        test_steps=[],
        prd_requirement="https://github.com/foo/bar/issues/42",
    )


def _clean_preflight(repo: Path, **_kwargs) -> PreflightReport:
    return PreflightReport(
        operation="push",
        worktree=WorktreeState(
            repo_root=repo,
            branch="main",
            head_sha="abc123",
            upstream=None,
            dirty=False,
            dirty_entries=(),
        ),
        backup_points=(),
        protection=ProtectionSignal(status="unknown", reason="not requested"),
        blockers=(),
        warnings=("no rollback point at current HEAD",),
    )


def _capture_argvs(tmp_path: Path, description: str) -> list[list[str]]:
    """Drive ``open_pr_for_task`` once and return both subprocess argvs."""
    push = _Proc(0, "")
    pr = _Proc(0, "https://github.com/foo/bar/pull/77\n")
    captured_argvs: list[list[str]] = []

    def fake_run(cmd, cwd, timeout=60):  # noqa: ARG001
        captured_argvs.append(list(cmd))
        return push if cmd[0] == "git" else pr

    with patch.object(gp, "_run", side_effect=fake_run):
        result = open_pr_for_task(
            _make_task(description=description),
            worktree_path=tmp_path,
            preflight_builder=_clean_preflight,
        )

    assert result.ok, f"sink reported failure: {result.reason!r}"
    return captured_argvs


def test_title_slot_is_length_capped(tmp_path: Path) -> None:
    argvs = _capture_argvs(tmp_path, _TAINTED_FIRST_LINE)
    pr_argv = argvs[1]
    title_idx = pr_argv.index("--title") + 1
    title = pr_argv[title_idx]
    assert len(title) <= _TITLE_CAP, f"title len={len(title)} exceeds cap={_TITLE_CAP}: {title!r}"


def test_title_slot_strips_newline_and_control_bytes(tmp_path: Path) -> None:
    argvs = _capture_argvs(tmp_path, _TAINTED_FIRST_LINE)
    pr_argv = argvs[1]
    title_idx = pr_argv.index("--title") + 1
    title = pr_argv[title_idx]
    assert "\n" not in title, f"title contained newline: {title!r}"
    assert "\x1b" not in title, f"title contained ANSI escape: {title!r}"
    assert "\x07" not in title, f"title contained BEL byte: {title!r}"
    assert "\x00" not in title, f"title contained NUL byte: {title!r}"
    assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", title), f"title contained C0 control byte: {title!r}"


def test_title_slot_redacts_aws_secret(tmp_path: Path) -> None:
    argvs = _capture_argvs(tmp_path, _TAINTED_FIRST_LINE)
    pr_argv = argvs[1]
    title_idx = pr_argv.index("--title") + 1
    title = pr_argv[title_idx]
    assert _AKIA_TOKEN not in title, f"AKIA token leaked verbatim into title: {title!r}"


def test_body_slot_retains_sanitizer_fences(tmp_path: Path) -> None:
    """The body argv keeps the M1 ``<UNTRUSTED …>`` fences around agent fields."""
    argvs = _capture_argvs(tmp_path, _TAINTED_FIRST_LINE)
    pr_argv = argvs[1]
    body_idx = pr_argv.index("--body") + 1
    body = pr_argv[body_idx]
    assert "<UNTRUSTED" in body, f"body lost sanitizer open fence: {body!r}"
    assert "</UNTRUSTED>" in body, f"body lost sanitizer close fence: {body!r}"
    # Body must not leak the raw AKIA token either (acceptance criteria
    # field flows through the sanitizer's secret redaction).
    assert _AKIA_TOKEN not in body, f"AKIA token leaked verbatim into body: {body!r}"


def test_title_slot_includes_task_id_prefix(tmp_path: Path) -> None:
    argvs = _capture_argvs(tmp_path, "Add /health endpoint")
    pr_argv = argvs[1]
    title_idx = pr_argv.index("--title") + 1
    title = pr_argv[title_idx]
    assert title.startswith("GH-42"), f"title lost task-id prefix: {title!r}"
    assert len(title) <= _TITLE_CAP


def test_title_slot_argv_position_unchanged(tmp_path: Path) -> None:
    """``--title <value> --body <value>`` argv shape is preserved."""
    argvs = _capture_argvs(tmp_path, "harmless first line")
    pr_argv = argvs[1]
    assert pr_argv[0] == "gh"
    assert pr_argv[1:3] == ["pr", "create"]
    title_idx = pr_argv.index("--title")
    body_idx = pr_argv.index("--body")
    assert title_idx + 2 == body_idx or title_idx < body_idx
    assert pr_argv[title_idx + 1] != "--body"
    assert pr_argv[body_idx + 1] != "--draft"
