"""Async verification command runner for worker integration."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from whilly.ci.models import CI_VERIFICATION_SOURCE, CIPollEvidence
from whilly.core.agent_runner import scan_command
from whilly.pipeline.events import PipelineTaskEvent
from whilly.security.secret_lint import redact_secrets

if TYPE_CHECKING:
    from whilly.ci.verification import CIPollRunner

VERIFICATION_STARTED_EVENT = "verification.started"
VERIFICATION_SUCCEEDED_EVENT = "verification.succeeded"
VERIFICATION_FAILED_EVENT = "verification.failed"
VERIFICATION_WARNING_EVENT = "verification.warning"

PROFILE_VERIFICATION_SOURCE = "profile"
CLI_VERIFICATION_SOURCE = "cli"

DEFAULT_TIMEOUT_S = 600.0
DEFAULT_OUTPUT_LIMIT = 20_000


class VerificationCommandLike(Protocol):
    name: str
    command: str
    required: bool


@dataclass(frozen=True)
class VerificationCommandSpec:
    """One shell command to run after agent work completes."""

    name: str
    command: str
    required: bool = True
    source: str = CLI_VERIFICATION_SOURCE


@dataclass(frozen=True)
class VerificationCommandResult:
    """Result data the worker can convert into verification events."""

    name: str
    command: str
    required: bool
    succeeded: bool
    warning: bool
    event_name: str
    returncode: int | None
    stdout: str
    stderr: str
    duration_s: float
    source: str = CLI_VERIFICATION_SOURCE
    timed_out: bool = False
    blocked: bool = False
    pattern_matched: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False


@dataclass(frozen=True)
class VerificationRunOutcome:
    """Aggregate verification outcome across all configured commands."""

    results: tuple[VerificationCommandResult, ...]
    ci_polls: tuple[CIPollEvidence, ...] = ()

    @property
    def succeeded(self) -> bool:
        return not self.required_failed

    @property
    def required_failed(self) -> bool:
        return any(result.required and not result.succeeded for result in self.results)

    @property
    def warning_count(self) -> int:
        return sum(1 for result in self.results if result.warning)

    @property
    def event_names(self) -> tuple[str, ...]:
        return (VERIFICATION_STARTED_EVENT, *(result.event_name for result in self.results))


def make_verification_started_event(task_id: str, *, plan_id: str = "") -> PipelineTaskEvent:
    """Build the audit event emitted before configured verification starts."""

    payload: dict[str, Any] = {"task_id": task_id}
    if plan_id:
        payload["plan_id"] = plan_id
    return PipelineTaskEvent(task_id=task_id, event_type=VERIFICATION_STARTED_EVENT, payload=payload)


def make_verification_result_event(
    task_id: str,
    result: VerificationCommandResult,
    *,
    plan_id: str = "",
) -> PipelineTaskEvent:
    """Build the audit event for one verification command result."""

    payload: dict[str, Any] = {
        "task_id": task_id,
        "name": result.name,
        "source": result.source,
        "command": redact_secrets(result.command),
        "required": result.required,
        "succeeded": result.succeeded,
        "warning": result.warning,
        "returncode": result.returncode,
        "duration_s": result.duration_s,
        "timed_out": result.timed_out,
        "blocked": result.blocked,
    }
    if plan_id:
        payload["plan_id"] = plan_id
    if result.pattern_matched:
        payload["pattern_matched"] = result.pattern_matched
    if result.stdout_truncated:
        payload["stdout_truncated"] = True
    if result.stderr_truncated:
        payload["stderr_truncated"] = True
    return PipelineTaskEvent(
        task_id=task_id,
        event_type=result.event_name,
        payload=payload,
        detail={"stdout": redact_secrets(result.stdout), "stderr": redact_secrets(result.stderr)},
    )


async def run_verification_commands(
    commands: list[VerificationCommandLike] | tuple[VerificationCommandLike, ...],
    *,
    cwd: str | Path,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    env_allowlist: tuple[str, ...] = (),
    output_limit: int = DEFAULT_OUTPUT_LIMIT,
    ci_poll_runner: "CIPollRunner | None" = None,
) -> VerificationRunOutcome:
    """Run verification commands sequentially and return structured outcomes.

    Environment inheritance is intentionally allowlisted. Variables absent from
    the parent environment are omitted from the child environment.
    """

    command_specs = tuple(_command_like_to_spec(command) for command in commands)
    cwd_path = Path(cwd)
    env = _allowed_env(env_allowlist)
    results = []
    ci_polls = []
    for spec in command_specs:
        if spec.source == CI_VERIFICATION_SOURCE:
            from whilly.ci.verification import run_ci_verification  # noqa: PLC0415

            ci_evidence, ci_result = await run_ci_verification(spec, runner=ci_poll_runner)
            ci_polls.append(ci_evidence)
            results.append(ci_result)
            continue
        results.append(await _run_one(spec, cwd=cwd_path, timeout_s=timeout_s, env=env, output_limit=output_limit))
    return VerificationRunOutcome(results=tuple(results), ci_polls=tuple(ci_polls))


def resolve_verification_specs(
    *,
    profile_commands: Sequence[VerificationCommandLike],
    required_cli: Sequence[str] = (),
    optional_cli: Sequence[str] = (),
) -> tuple[VerificationCommandSpec, ...]:
    """Resolve profile-native and explicit CLI verification commands in runtime order."""

    specs: list[VerificationCommandSpec] = [
        VerificationCommandSpec(
            name=command.name,
            command=command.command,
            required=command.required,
            source=PROFILE_VERIFICATION_SOURCE,
        )
        for command in profile_commands
    ]
    for raw in required_cli:
        name, command = _split_verification_command(raw)
        specs.append(
            VerificationCommandSpec(
                name=name,
                command=command,
                required=True,
                source=CLI_VERIFICATION_SOURCE,
            )
        )
    for raw in optional_cli:
        name, command = _split_verification_command(raw)
        specs.append(
            VerificationCommandSpec(
                name=name,
                command=command,
                required=False,
                source=CLI_VERIFICATION_SOURCE,
            )
        )
    return tuple(specs)


def _command_like_to_spec(command: VerificationCommandLike) -> VerificationCommandSpec:
    return VerificationCommandSpec(
        name=command.name,
        command=command.command,
        required=command.required,
        source=getattr(command, "source", CLI_VERIFICATION_SOURCE),
    )


async def _run_one(
    spec: VerificationCommandSpec,
    *,
    cwd: Path,
    timeout_s: float,
    env: dict[str, str],
    output_limit: int,
) -> VerificationCommandResult:
    started = time.monotonic()
    scan = scan_command(spec.command)
    if scan.blocked:
        return _blocked_result(spec, started=started, scan_pattern=scan.pattern_matched)

    proc = await asyncio.create_subprocess_shell(
        spec.command,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.CancelledError:
        _kill_process_group(proc)
        await proc.communicate()
        raise
    except asyncio.TimeoutError:
        _kill_process_group(proc)
        await proc.communicate()
        return _timeout_result(spec, started=started, timeout_s=timeout_s)

    stdout, stdout_truncated = _decode_and_cap(stdout_bytes, output_limit)
    stderr, stderr_truncated = _decode_and_cap(stderr_bytes, output_limit)
    stdout = redact_secrets(stdout)
    stderr = redact_secrets(stderr)
    succeeded = proc.returncode == 0
    warning = not spec.required and not succeeded
    event_name = _event_name(required=spec.required, succeeded=succeeded, warning=warning)
    return VerificationCommandResult(
        name=spec.name,
        command=spec.command,
        required=spec.required,
        succeeded=succeeded,
        warning=warning,
        event_name=event_name,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
        duration_s=time.monotonic() - started,
        source=spec.source,
        pattern_matched=scan.pattern_matched,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def _blocked_result(
    spec: VerificationCommandSpec,
    *,
    started: float,
    scan_pattern: str | None,
) -> VerificationCommandResult:
    warning = not spec.required
    return VerificationCommandResult(
        name=spec.name,
        command=spec.command,
        required=spec.required,
        succeeded=False,
        warning=warning,
        event_name=VERIFICATION_WARNING_EVENT if warning else VERIFICATION_FAILED_EVENT,
        returncode=None,
        stdout="",
        stderr=f"blocked by shell policy: {scan_pattern or 'unknown'}",
        duration_s=time.monotonic() - started,
        source=spec.source,
        blocked=True,
        pattern_matched=scan_pattern,
    )


def _timeout_result(
    spec: VerificationCommandSpec,
    *,
    started: float,
    timeout_s: float,
) -> VerificationCommandResult:
    warning = not spec.required
    return VerificationCommandResult(
        name=spec.name,
        command=spec.command,
        required=spec.required,
        succeeded=False,
        warning=warning,
        event_name=VERIFICATION_WARNING_EVENT if warning else VERIFICATION_FAILED_EVENT,
        returncode=None,
        stdout="",
        stderr=f"timed out after {timeout_s:g}s",
        duration_s=time.monotonic() - started,
        source=spec.source,
        timed_out=True,
    )


def _event_name(*, required: bool, succeeded: bool, warning: bool) -> str:
    if warning:
        return VERIFICATION_WARNING_EVENT
    if succeeded:
        return VERIFICATION_SUCCEEDED_EVENT
    return VERIFICATION_FAILED_EVENT if required else VERIFICATION_WARNING_EVENT


def _allowed_env(env_allowlist: tuple[str, ...]) -> dict[str, str]:
    return {name: os.environ[name] for name in env_allowlist if name in os.environ}


def _split_verification_command(raw: str) -> tuple[str, str]:
    name, _sep, command = raw.partition("=")
    return name.strip(), command.strip()


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if hasattr(os, "killpg") and proc.pid is not None:
        os.killpg(proc.pid, signal.SIGKILL)
        return
    proc.kill()


def _decode_and_cap(payload: bytes, output_limit: int) -> tuple[str, bool]:
    text = payload.decode("utf-8", errors="replace")
    if output_limit < 0 or len(text) <= output_limit:
        return text, False
    return text[-output_limit:], True


__all__ = [
    "DEFAULT_OUTPUT_LIMIT",
    "DEFAULT_TIMEOUT_S",
    "CI_VERIFICATION_SOURCE",
    "CLI_VERIFICATION_SOURCE",
    "PROFILE_VERIFICATION_SOURCE",
    "VERIFICATION_FAILED_EVENT",
    "VERIFICATION_STARTED_EVENT",
    "VERIFICATION_SUCCEEDED_EVENT",
    "VERIFICATION_WARNING_EVENT",
    "VerificationCommandResult",
    "VerificationCommandSpec",
    "VerificationRunOutcome",
    "make_verification_result_event",
    "make_verification_started_event",
    "resolve_verification_specs",
    "run_verification_commands",
]
