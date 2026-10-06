"""Bounded subprocesses for swarm agents and verification commands.

Every child runs in its own process group (``start_new_session=True``) with
stdout/stderr streamed to retained log files. On timeout, cancellation or
coordinator stop the whole group receives ``SIGTERM`` and, after a grace
period, ``SIGKILL``; after a normal exit any leftover group members are
killed as well so a finished agent cannot keep editing its worktree in the
background.

Claude CLI tool flags are *permissions*, not OS isolation. Read-only
planner/reviewer calls pass an explicit ``--tools Read,Grep,Glob`` set, an
empty strict MCP configuration and no inherited setting sources so a
permission-bypassing wrapper cannot re-enable write tools or MCP servers
through user settings.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from whilly.adapters.runner.result_parser import AgentResult, parse_output
from whilly.swarm.registry import AgentConfig
from whilly.swarm.engines import EngineResult, build_engine_argv, parse_engine_output

__all__ = [
    "READ_ONLY_TOOLS",
    "AgentRun",
    "ProcessOutcome",
    "build_agent_argv",
    "kill_process_group",
    "process_group_alive",
    "run_agent",
    "run_engine",
    "run_bounded",
]

READ_ONLY_TOOLS = "Read,Grep,Glob"
TERMINATE_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class ProcessOutcome:
    argv: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    duration_seconds: float
    stdout_path: str
    stderr_path: str
    spawn_error: str | None = None
    reason: str | None = None
    backend: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.exit_code == 0
            and not self.timed_out
            and not self.cancelled
            and self.spawn_error is None
            and self.reason is None
        )

    def describe(self) -> str:
        if self.spawn_error:
            return f"spawn failed: {self.spawn_error}"
        if self.reason:
            return self.reason
        if self.timed_out:
            return f"timed out after {self.duration_seconds:.0f}s"
        if self.cancelled:
            return "cancelled"
        return f"exit code {self.exit_code}"


@dataclass(frozen=True)
class AgentRun:
    process: ProcessOutcome
    result: AgentResult

    @property
    def cost_usd(self) -> float:
        return float(self.result.usage.cost_usd or 0.0)


def process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_process_group(pgid: int, sig: int = signal.SIGKILL) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def _terminate_group(proc: asyncio.subprocess.Process, pgid: int) -> None:
    kill_process_group(pgid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=TERMINATE_GRACE_SECONDS)
    except asyncio.TimeoutError:
        pass
    kill_process_group(pgid, signal.SIGKILL)
    try:
        await asyncio.wait_for(proc.wait(), timeout=TERMINATE_GRACE_SECONDS)
    except asyncio.TimeoutError:  # pragma: no cover — SIGKILL is not ignorable
        pass


async def run_bounded(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    timeout_seconds: float,
    stdout_path: str | os.PathLike[str],
    stderr_path: str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    stdin_path: str | os.PathLike[str] | None = None,
    on_start: Callable[[int], Awaitable[None]] | None = None,
) -> ProcessOutcome:
    """Run ``argv`` in a new process group with a hard timeout.

    ``CancelledError`` is re-raised after the process group has been killed,
    so a cancelled coordinator never leaves a live child behind.
    """
    Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    stdin_handle = open(stdin_path, "rb") if stdin_path is not None else None  # noqa: SIM115
    try:
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(cwd),
                    env=dict(env) if env is not None else None,
                    stdin=stdin_handle if stdin_handle is not None else asyncio.subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                )
            except OSError as exc:
                return ProcessOutcome(
                    argv=tuple(argv),
                    exit_code=None,
                    timed_out=False,
                    cancelled=False,
                    duration_seconds=0.0,
                    stdout_path=str(stdout_path),
                    stderr_path=str(stderr_path),
                    spawn_error=f"{type(exc).__name__}: {exc}",
                )
            pgid = proc.pid  # start_new_session makes the child its own group leader
            try:
                if on_start is not None:
                    await on_start(pgid)
                try:
                    exit_code = await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
                except asyncio.TimeoutError:
                    await _terminate_group(proc, pgid)
                    return ProcessOutcome(
                        argv=tuple(argv),
                        exit_code=proc.returncode,
                        timed_out=True,
                        cancelled=False,
                        duration_seconds=time.monotonic() - started,
                        stdout_path=str(stdout_path),
                        stderr_path=str(stderr_path),
                    )
            except BaseException:
                # The child already exists here. Failure to persist its PGID
                # is just as important as cancellation: never leave it running
                # after the owner has failed or lost its database connection.
                await asyncio.shield(_terminate_group(proc, pgid))
                raise
            # Leader exited; reap anything it left running in its group.
            kill_process_group(pgid, signal.SIGKILL)
            return ProcessOutcome(
                argv=tuple(argv),
                exit_code=exit_code,
                timed_out=False,
                cancelled=False,
                duration_seconds=time.monotonic() - started,
                stdout_path=str(stdout_path),
                stderr_path=str(stderr_path),
            )
    finally:
        if stdin_handle is not None:
            stdin_handle.close()


def build_agent_argv(
    config: AgentConfig,
    *,
    mode: str,
    max_turns: int,
    budget_usd: float,
    empty_mcp_config: str,
    add_dirs: Sequence[str] = (),
) -> list[str]:
    """Build the Claude CLI argv. The prompt is always supplied on stdin.

    ``mode`` is ``"worker"`` (configured worker permissions, local edits in
    its own worktree) or ``"read_only"`` (planner / reviewer).
    """
    if mode not in {"worker", "read_only"}:
        raise ValueError(f"unknown agent mode {mode!r}")
    argv = [
        *config.executable,
        "-p",
        "--output-format",
        "json",
        "--max-turns",
        str(max_turns),
        "--max-budget-usd",
        f"{budget_usd:.2f}",
    ]
    if config.model:
        argv += ["--model", config.model]
    if mode == "read_only":
        argv += [
            "--tools",
            READ_ONLY_TOOLS,
            "--strict-mcp-config",
            "--mcp-config",
            empty_mcp_config,
            "--setting-sources",
            "",
        ]
    else:
        argv += list(config.worker_args)
        if not config.inherit_mcp:
            argv += ["--strict-mcp-config", "--mcp-config", empty_mcp_config]
    for directory in add_dirs:
        argv += ["--add-dir", directory]
    return argv


def ensure_empty_mcp_config(state_dir: Path) -> str:
    path = state_dir / "empty-mcp.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    return str(path)


async def run_agent(
    argv: Sequence[str],
    *,
    prompt: str,
    cwd: str | os.PathLike[str],
    log_dir: Path,
    name: str,
    timeout_seconds: float,
    env: Mapping[str, str] | None = None,
    on_start: Callable[[int], Awaitable[None]] | None = None,
    policy=None,
    executor=None,
) -> AgentRun:
    """Write the prompt, run the agent, and parse its JSON envelope."""
    log_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = log_dir / f"{name}.prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    (log_dir / f"{name}.argv.json").write_text(json.dumps(list(argv), indent=2), encoding="utf-8")
    if policy is None:
        raise ValueError("execution_policy_required")
    if executor is None:
        from whilly.swarm.execution import GuardedExecutor

        executor = GuardedExecutor()
    outcome = await executor.run(
        argv,
        phase=policy.phase,
        policy=policy,
        environment=env or {},
        cwd=cwd,
        log_dir=log_dir,
        stdin_path=prompt_path,
        on_start=on_start,
    )
    stdout = Path(outcome.stdout_path).read_text(encoding="utf-8", errors="replace")
    exit_code = outcome.exit_code if outcome.exit_code is not None else -1
    return AgentRun(process=outcome, result=parse_output(stdout, exit_code))


async def run_engine(
    engine: str,
    config,
    *,
    mode: str,
    prompt: str,
    cwd: str | os.PathLike[str],
    log_dir: Path,
    name: str,
    timeout_seconds: float,
    max_turns: int | None = None,
    budget_usd: float | None = None,
    empty_mcp_config: str | None = None,
    add_dirs: Sequence[str] = (),
    auth_mode: str | None = None,
    env: Mapping[str, str] | None = None,
    on_start: Callable[[int], Awaitable[None]] | None = None,
    policy=None,
    executor=None,
) -> tuple[ProcessOutcome, EngineResult]:
    """Run either backend through the same process-group/timeout machinery."""
    argv = build_engine_argv(
        engine,
        config,
        mode=mode,
        max_turns=max_turns,
        budget_usd=budget_usd,
        empty_mcp_config=empty_mcp_config,
        add_dirs=add_dirs,
        auth_mode=auth_mode,
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = log_dir / f"{name}.prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    (log_dir / f"{name}.argv.json").write_text(json.dumps(argv, indent=2), encoding="utf-8")
    if policy is None:
        raise ValueError("execution_policy_required")
    if executor is None:
        from whilly.swarm.execution import GuardedExecutor

        executor = GuardedExecutor()
    outcome = await executor.run(
        argv,
        phase=policy.phase,
        policy=policy,
        environment=env or {},
        cwd=cwd,
        log_dir=log_dir,
        stdin_path=prompt_path,
        on_start=on_start,
    )
    stdout = Path(outcome.stdout_path).read_text(encoding="utf-8", errors="replace")
    return outcome, parse_engine_output(
        engine, stdout, exit_code=outcome.exit_code if outcome.exit_code is not None else -1
    )
