"""Unit tests for the pure verification command runner."""

from __future__ import annotations

import sys

import pytest

from whilly.pipeline.verification import (
    CLI_VERIFICATION_SOURCE,
    PROFILE_VERIFICATION_SOURCE,
    VERIFICATION_FAILED_EVENT,
    VerificationCommandResult,
    VerificationCommandSpec,
    make_verification_result_event,
    resolve_verification_specs,
    run_verification_commands,
)
from whilly.core.models import VerificationCommand
from whilly.project_config.models import VerificationCommandConfig


def _python(command: str) -> str:
    return f"{sys.executable} -c {command!r}"


def test_resolve_verification_specs_orders_profile_then_required_then_optional_cli() -> None:
    specs = resolve_verification_specs(
        profile_commands=(
            VerificationCommand(
                name="profile-unit",
                command="pytest -q tests/unit",
                required=True,
            ),
        ),
        required_cli=("cli-required=pytest -q",),
        optional_cli=("cli-optional=ruff check whilly",),
    )

    assert [spec.name for spec in specs] == ["profile-unit", "cli-required", "cli-optional"]
    assert [spec.source for spec in specs] == [
        PROFILE_VERIFICATION_SOURCE,
        CLI_VERIFICATION_SOURCE,
        CLI_VERIFICATION_SOURCE,
    ]
    assert [spec.required for spec in specs] == [True, True, False]


@pytest.mark.asyncio
async def test_required_command_success_emits_succeeded_event(tmp_path):
    outcome = await run_verification_commands(
        [
            VerificationCommandSpec(
                name="unit",
                command=_python("print('ok')"),
                required=True,
                source=PROFILE_VERIFICATION_SOURCE,
            )
        ],
        cwd=tmp_path,
    )

    assert outcome.succeeded is True
    assert outcome.required_failed is False
    assert outcome.event_names == ("verification.started", "verification.succeeded")
    assert outcome.results[0].succeeded is True
    assert outcome.results[0].stdout == "ok\n"
    assert outcome.results[0].stderr == ""
    assert outcome.results[0].source == PROFILE_VERIFICATION_SOURCE


@pytest.mark.asyncio
async def test_required_command_failure_emits_failed_event(tmp_path):
    outcome = await run_verification_commands(
        [
            VerificationCommandConfig(
                name="unit",
                command=_python("import sys; print('bad'); sys.exit(7)"),
                required=True,
            )
        ],
        cwd=tmp_path,
    )

    result = outcome.results[0]
    assert outcome.succeeded is False
    assert outcome.required_failed is True
    assert outcome.event_names == ("verification.started", "verification.failed")
    assert result.succeeded is False
    assert result.warning is False
    assert result.returncode == 7
    assert result.stdout == "bad\n"


@pytest.mark.asyncio
async def test_optional_command_failure_is_warning_not_required_failure(tmp_path):
    outcome = await run_verification_commands(
        [VerificationCommandSpec(name="lint", command=_python("import sys; sys.exit(2)"), required=False)],
        cwd=tmp_path,
    )

    result = outcome.results[0]
    assert outcome.succeeded is True
    assert outcome.required_failed is False
    assert outcome.warning_count == 1
    assert outcome.event_names == ("verification.started", "verification.warning")
    assert result.succeeded is False
    assert result.warning is True
    assert result.returncode == 2


@pytest.mark.asyncio
async def test_blocked_required_command_fails_without_execution(tmp_path):
    marker = tmp_path / "should-not-exist"

    outcome = await run_verification_commands(
        [
            VerificationCommandSpec(
                name="cleanup",
                command="rm -rf / ",
                required=True,
                source=PROFILE_VERIFICATION_SOURCE,
            )
        ],
        cwd=tmp_path,
    )

    result = outcome.results[0]
    assert outcome.succeeded is False
    assert outcome.event_names == ("verification.started", "verification.failed")
    assert result.blocked is True
    assert result.source == PROFILE_VERIFICATION_SOURCE
    assert result.pattern_matched == "rm-rf-root"
    assert result.returncode is None
    assert marker.exists() is False


@pytest.mark.asyncio
async def test_blocked_optional_command_warns_without_execution(tmp_path):
    outcome = await run_verification_commands(
        [VerificationCommandSpec(name="cleanup", command="rm -rf /", required=False)],
        cwd=tmp_path,
    )

    result = outcome.results[0]
    assert outcome.succeeded is True
    assert outcome.event_names == ("verification.started", "verification.warning")
    assert result.blocked is True
    assert result.warning is True
    assert result.pattern_matched == "rm-rf-root"


@pytest.mark.asyncio
async def test_env_allowlist_exposes_only_requested_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("WHILLY_VISIBLE", "yes")
    monkeypatch.setenv("WHILLY_HIDDEN", "no")
    command = _python("import os; print(os.getenv('WHILLY_VISIBLE')); print(os.getenv('WHILLY_HIDDEN'))")

    outcome = await run_verification_commands(
        [VerificationCommandSpec(name="env", command=command, required=True)],
        cwd=tmp_path,
        env_allowlist=("WHILLY_VISIBLE",),
    )

    assert outcome.succeeded is True
    assert outcome.results[0].stdout == "yes\nNone\n"


@pytest.mark.asyncio
async def test_timeout_kills_process_and_marks_required_failed(tmp_path):
    outcome = await run_verification_commands(
        [
            VerificationCommandSpec(
                name="slow",
                command=_python("import time; time.sleep(5)"),
                required=True,
                source=PROFILE_VERIFICATION_SOURCE,
            )
        ],
        cwd=tmp_path,
        timeout_s=0.05,
    )

    result = outcome.results[0]
    assert outcome.succeeded is False
    assert result.timed_out is True
    assert result.source == PROFILE_VERIFICATION_SOURCE
    assert result.returncode is None
    assert "timed out" in result.stderr


@pytest.mark.asyncio
async def test_stdout_and_stderr_are_capped(tmp_path):
    outcome = await run_verification_commands(
        [
            VerificationCommandSpec(
                name="chatty",
                command=_python("import sys; print('abcdef'); print('ghijkl', file=sys.stderr)"),
                required=True,
            )
        ],
        cwd=tmp_path,
        output_limit=4,
    )

    result = outcome.results[0]
    assert result.stdout == "def\n"
    assert result.stderr == "jkl\n"
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True


@pytest.mark.asyncio
async def test_stdout_and_stderr_are_redacted_before_result_persistence(tmp_path):
    fake_secret = "sk-ant-" + "V" * 32
    outcome = await run_verification_commands(
        [
            VerificationCommandSpec(
                name="secret-output",
                command=_python(f"import sys; print({fake_secret!r}); print({fake_secret!r}, file=sys.stderr)"),
                required=True,
            )
        ],
        cwd=tmp_path,
    )

    result = outcome.results[0]
    assert fake_secret not in result.stdout
    assert fake_secret not in result.stderr
    assert "[REDACTED:anthropic-api-key]" in result.stdout
    assert "[REDACTED:anthropic-api-key]" in result.stderr


def test_verification_result_event_redacts_command_and_detail() -> None:
    fake_secret = "sk-ant-" + "E" * 32
    result = VerificationCommandResult(
        name="secret-event",
        command="echo " + fake_secret,
        required=True,
        source=PROFILE_VERIFICATION_SOURCE,
        succeeded=False,
        warning=False,
        event_name=VERIFICATION_FAILED_EVENT,
        returncode=1,
        stdout="stdout " + fake_secret,
        stderr="stderr " + fake_secret,
        duration_s=0.1,
    )

    event = make_verification_result_event("T-secret-event", result, plan_id="P-secret")

    assert fake_secret not in str(event.payload)
    assert fake_secret not in str(event.detail)
    assert event.payload["command"] == "echo [REDACTED:anthropic-api-key]"
    assert event.payload["source"] == PROFILE_VERIFICATION_SOURCE
    assert event.detail == {
        "stdout": "stdout [REDACTED:anthropic-api-key]",
        "stderr": "stderr [REDACTED:anthropic-api-key]",
    }
