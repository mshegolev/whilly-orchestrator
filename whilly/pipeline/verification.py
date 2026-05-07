"""Async verification command runner for worker integration."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from whilly.core.agent_runner import scan_command
from whilly.pipeline.events import PipelineTaskEvent

VERIFICATION_STARTED_EVENT = "verification.started"
VERIFICATION_SUCCEEDED_EVENT = "verification.succeeded"
VERIFICATION_FAILED_EVENT = "verification.failed"
VERIFICATION_WARNING_EVENT = "verification.warning"

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
    timed_out: bool = False
    blocked: bool = False
    pattern_matched: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False


@dataclass(frozen=True)
class VerificationRunOutcome:
    """Aggregate verification outcome across all configured commands."""

    results: tuple[VerificationCommandResult, ...]

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
        "command": result.command,
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
        detail={"stdout": result.stdout, "stderr": result.stderr},
    )


async def run_verification_commands(
    commands: list[VerificationCommandLike] | tuple[VerificationCommandLike, ...],
    *,
    cwd: str | Path,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    env_allowlist: tuple[str, ...] = (),
    output_limit: int = DEFAULT_OUTPUT_LIMIT,
) -> VerificationRunOutcome:
    """Run verification commands sequentially and return structured outcomes.

    Environment inheritance is intentionally allowlisted. Variables absent from
    the parent environment are omitted from the child environment.
    """

    command_specs = tuple(
        VerificationCommandSpec(name=command.name, command=command.command, required=command.required)
        for command in commands
    )
    cwd_path = Path(cwd)
    env = _allowed_env(env_allowlist)
    results = []
    for spec in command_specs:
        results.append(await _run_one(spec, cwd=cwd_path, timeout_s=timeout_s, env=env, output_limit=output_limit))
    return VerificationRunOutcome(results=tuple(results))


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
    "VERIFICATION_FAILED_EVENT",
    "VERIFICATION_STARTED_EVENT",
    "VERIFICATION_SUCCEEDED_EVENT",
    "VERIFICATION_WARNING_EVENT",
    "VerificationCommandResult",
    "VerificationCommandSpec",
    "VerificationRunOutcome",
    "make_verification_result_event",
    "make_verification_started_event",
    "run_verification_commands",
]
