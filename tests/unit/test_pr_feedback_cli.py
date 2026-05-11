"""Unit tests for ``whilly pr-feedback`` CLI subcommand (VAL-PR-019, VAL-PR-020, VAL-PR-026).

Covers:

* ``whilly --help`` lists ``pr-feedback`` as a registered subcommand.
* ``whilly pr-feedback --help`` lists ``poll`` and exits 0.
* ``whilly pr-feedback poll --plan <id>`` with a mocked-success poll
  exits 0 and prints a single-line summary mentioning the plan id and
  the number of PRs polled (VAL-PR-019).
* ``whilly pr-feedback poll --plan <id>`` with ``WHILLY_DATABASE_URL``
  unset exits non-zero with a stderr diagnostic naming the missing env
  var (VAL-PR-020).
* The dispatcher in :mod:`whilly.cli` routes ``pr-feedback`` into
  :func:`whilly.cli.pr_feedback.run_pr_feedback_command`.
"""

from __future__ import annotations

import pytest

from whilly.cli import main as dispatch_main
from whilly.cli import pr_feedback as cli_pr_feedback
from whilly.cli.pr_feedback import (
    DATABASE_URL_ENV,
    EXIT_ENVIRONMENT_ERROR,
    EXIT_OK,
    run_pr_feedback_command,
)


# ── Help surface ──────────────────────────────────────────────────────


def test_top_level_whilly_help_lists_pr_feedback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = dispatch_main(["--help"])
    assert code == 0
    captured = capsys.readouterr()
    assert "pr-feedback" in captured.out


def test_pr_feedback_help_lists_poll_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = dispatch_main(["pr-feedback", "--help"])
    assert code == 0
    captured = capsys.readouterr()
    assert "poll" in captured.out


def test_pr_feedback_poll_help_runs_clean(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = dispatch_main(["pr-feedback", "poll", "--help"])
    assert code == 0
    captured = capsys.readouterr()
    assert "--plan" in captured.out


# ── DSN-missing exit path (VAL-PR-020) ────────────────────────────────


def test_poll_exits_nonzero_when_dsn_unset(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    code = run_pr_feedback_command(["poll", "--plan", "P-1"])
    assert code != 0
    assert code == EXIT_ENVIRONMENT_ERROR
    captured = capsys.readouterr()
    assert DATABASE_URL_ENV in captured.err


def test_poll_does_not_silently_no_op_without_dsn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """VAL-PR-020: missing DSN must produce a non-zero exit AND a
    diagnostic — never silent success."""
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    code = run_pr_feedback_command(["poll", "--plan", "P-1"])
    captured = capsys.readouterr()
    assert code != 0
    # Must produce stderr output mentioning the env var name.
    assert captured.err.strip(), "stderr was empty — silent failure"
    assert DATABASE_URL_ENV in captured.err


# ── Mocked-success poll (VAL-PR-019) ──────────────────────────────────


def test_poll_exits_zero_on_mocked_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://user@127.0.0.1/whilly")

    async def _fake_async_poll(*, dsn: str, plan_id: str) -> int:
        assert dsn == "postgresql://user@127.0.0.1/whilly"
        assert plan_id == "P-OK"
        return 3

    monkeypatch.setattr(cli_pr_feedback, "_async_poll_one_cycle", _fake_async_poll)

    code = run_pr_feedback_command(["poll", "--plan", "P-OK"])
    assert code == EXIT_OK
    captured = capsys.readouterr()
    # Single-line summary mentions the plan id and the count.
    summary = captured.out.strip() or captured.err.strip()
    summary_lines = [line for line in summary.splitlines() if line.strip()]
    assert summary_lines, "summary line missing"
    joined = "\n".join(summary_lines)
    assert "P-OK" in joined
    assert "3" in joined


def test_poll_summary_reports_zero_when_no_open_prs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://user@127.0.0.1/whilly")

    async def _fake_async_poll(*, dsn: str, plan_id: str) -> int:
        return 0

    monkeypatch.setattr(cli_pr_feedback, "_async_poll_one_cycle", _fake_async_poll)

    code = run_pr_feedback_command(["poll", "--plan", "P-EMPTY"])
    assert code == EXIT_OK
    captured = capsys.readouterr()
    assert "P-EMPTY" in (captured.out + captured.err)
    assert "0" in (captured.out + captured.err)


# ── Required --plan flag ──────────────────────────────────────────────


def test_poll_requires_plan_flag() -> None:
    with pytest.raises(SystemExit) as exc_info:
        run_pr_feedback_command(["poll"])
    # argparse's required=True exits 2 on missing flag.
    assert exc_info.value.code == 2


# ── Dispatcher wiring (VAL-PR-026) ────────────────────────────────────


def test_main_dispatches_pr_feedback_subcommand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``whilly pr-feedback ...`` reaches :func:`run_pr_feedback_command`."""
    captured: dict[str, object] = {}

    def _fake_runner(argv: object) -> int:
        captured["argv"] = list(argv) if isinstance(argv, list) else argv
        return 0

    monkeypatch.setattr(cli_pr_feedback, "run_pr_feedback_command", _fake_runner)

    code = dispatch_main(["pr-feedback", "poll", "--plan", "P-D"])
    assert code == 0
    assert captured["argv"] == ["poll", "--plan", "P-D"]


def test_unknown_pr_feedback_subcommand_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = run_pr_feedback_command(["bogus"])
    assert code != 0
    captured = capsys.readouterr()
    assert "bogus" in captured.err or "unknown" in captured.err.lower()
