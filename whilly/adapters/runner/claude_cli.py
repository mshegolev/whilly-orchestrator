"""Async subprocess wrapper for the Claude CLI (TASK-017b, PRD FR-1.6, TC-7).

This is the I/O half of the runner adapter pair. Its sibling
:mod:`whilly.adapters.runner.result_parser` (TASK-017a) takes the captured
stdout string and turns it into an :class:`AgentResult`; this module's only
job is to spawn the ``claude`` binary, wait for it, and feed parsing.

Why ``asyncio.create_subprocess_exec`` and not ``subprocess.run``?
-----------------------------------------------------------------
The v4 worker loop (TASK-019) is async-first: a single worker may run a
heartbeat task, a poll loop and the agent invocation cooperatively. Using
sync ``subprocess.run`` would block the event loop for the duration of the
agent call (often minutes), starving the heartbeat and triggering the
visibility timeout (FR-1.4) — the worker would lose its own task while
still working on it. The async spawn primitive releases the loop between
syscalls so siblings keep running. We pass argv as a list (not a shell
string) so there is no shell interpretation and no command-injection risk
from the prompt content.

Retry policy
------------
On retriable errors (non-zero exit, ``API Error: 5xx``, ``"type":"error"``)
we sleep through the schedule ``5/10/20/40/60`` seconds — the same shape as
v3 so operators recognise the cadence. After all five backoffs the last
:class:`AgentResult` is returned verbatim so the caller can mark the task
``FAILED`` with the real error text rather than a synthetic "exhausted"
message.

Auth errors (``failed to authenticate`` / ``403 Forbidden``) are permanent:
no amount of waiting will fix a missing API key, so we return immediately
without consuming retries — the worker should mark the task failed and
move on (or shut down depending on policy; that decision lives in the
worker loop, not here).

Negative exit codes
-------------------
A few subprocess-level failures have no JSON envelope to attach to:
binary not found, EAGAIN spawn rejection. We encode them as negative exit
codes (``-2`` and ``-3``) which collide with no POSIX status — the parser
threads them through verbatim (`test_exit_code_round_trips_unchanged` in
the result_parser test suite asserts this).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence
from typing import Final

from whilly.adapters.runner.result_parser import AgentResult, parse_output
from whilly.core.models import Task

log = logging.getLogger(__name__)

DEFAULT_BIN: Final[str] = "claude"
DEFAULT_MODEL: Final[str] = "claude-opus-4-6[1m]"

# Exponential backoff for API errors (PRD: 5/10/20/40/60s). Five retries
# total; the first attempt has no preceding sleep, so total attempts =
# len(BACKOFF_SCHEDULE) + 1 == 6.
BACKOFF_SCHEDULE: Final[tuple[int, ...]] = (5, 10, 20, 40, 60)

# Negative exit codes for subprocess-level failures the wrapper itself
# encountered (no JSON envelope available). Negative range is reserved by
# convention so they can't collide with any POSIX-legitimate code.
EXIT_BINARY_NOT_FOUND: Final[int] = -2
EXIT_SPAWN_BLOCKED: Final[int] = -3

# Permanent-failure markers — present in stdout/stderr means we should NOT
# retry. Lower-cased before matching.
_AUTH_ERROR_SUBSTRINGS: Final[tuple[str, ...]] = ("failed to authenticate",)

# Retriable upstream errors. Match the shape Claude CLI prints when an
# upstream API request fails: ``API Error: 503`` or ``"type":"error"``
# inside a JSON event. Lower-cased before matching.
_API_ERROR_SUBSTRINGS: Final[tuple[str, ...]] = (
    "api error: 403",
    "api error: 500",
    "api error: 502",
    "api error: 503",
    "api error: 529",
    '"type":"error"',
)


def _claude_bin() -> str:
    """Resolve the binary path, honouring ``CLAUDE_BIN`` override."""
    return os.environ.get("CLAUDE_BIN") or DEFAULT_BIN


def _resolve_model(model: str | None) -> str:
    """Pick the model in priority: explicit arg → ``WHILLY_MODEL`` env → default."""
    if model:
        return model
    return os.environ.get("WHILLY_MODEL") or DEFAULT_MODEL


def _permission_args() -> list[str]:
    """Return the permission-related Claude CLI flags.

    Mirrors v3 default: agents run unattended via
    ``--dangerously-skip-permissions``. Operators wanting an attended TTY
    flow set ``WHILLY_CLAUDE_SAFE=1`` and get ``--permission-mode acceptEdits``
    instead — the same env knob the v3 backend honours so the operational
    contract is unchanged.
    """
    if os.environ.get("WHILLY_CLAUDE_SAFE") in ("1", "true", "yes"):
        return ["--permission-mode", "acceptEdits"]
    return ["--dangerously-skip-permissions"]


def build_command(prompt: str, model: str) -> list[str]:
    """Build the ``claude`` argv for the resolved binary, model and prompt.

    Exposed (not underscore-prefixed) because it's useful for debugging and
    testing: a unit test can assert on the argv shape without spawning the
    binary. ``--output-format json`` is required because the sibling
    :func:`parse_output` only understands the single-envelope shape (v3's
    ``stream-json`` JSONL stream is intentionally not used here — the
    server-side worker doesn't need live progress, and JSONL would force
    parsing complexity into the pure parser).
    """
    return [
        _claude_bin(),
        *_permission_args(),
        "--output-format",
        "json",
        "--model",
        model,
        "-p",
        prompt,
    ]


def _is_auth_error(result: AgentResult) -> bool:
    """Detect permanent auth failures — these must NOT trigger retries."""
    text = result.output.lower()
    if any(needle in text for needle in _AUTH_ERROR_SUBSTRINGS):
        return True
    # "403 Forbidden" anywhere in the output: permanent.
    return "403" in text and "forbidden" in text


def _is_retriable_error(result: AgentResult) -> bool:
    """Decide whether a retry is justified for this :class:`AgentResult`.

    Successful completion (``is_complete=True``) is never retried regardless
    of exit code — the agent already signalled it's done its job. Auth
    errors are never retried (waiting won't grant credentials). Otherwise:
    non-zero exit OR a known retriable substring in the output.
    """
    if result.is_complete:
        return False
    if _is_auth_error(result):
        return False
    if result.exit_code != 0:
        return True
    text = result.output.lower()
    return any(needle in text for needle in _API_ERROR_SUBSTRINGS)


async def _spawn_and_collect(prompt: str, model: str) -> AgentResult:
    """Spawn ``claude`` once and return the parsed :class:`AgentResult`.

    All known subprocess-level failures (missing binary, EAGAIN) are
    converted into an :class:`AgentResult` with a negative exit code so the
    caller never has to catch exceptions itself. stdout is decoded as UTF-8
    with replacement for any byte that fails decoding (Claude CLI is
    UTF-8 but agents have been observed to interleave invalid bytes from
    embedded shell tools — we don't want that to crash the wrapper).
    """
    cmd = build_command(prompt, model)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.error("claude binary not found at %r — set CLAUDE_BIN to override", _claude_bin())
        return AgentResult(
            output=f"{_claude_bin()} CLI not found",
            exit_code=EXIT_BINARY_NOT_FOUND,
        )
    except BlockingIOError as exc:
        # macOS RLIMIT_NPROC can momentarily reject spawn (errno 35). Surface
        # the failure rather than retrying inline — the outer retry loop
        # treats this as a retriable error via the negative exit code.
        log.warning("spawn blocked (errno %s): %s", exc.errno, exc)
        return AgentResult(
            output=f"spawn blocked: {exc}",
            exit_code=EXIT_SPAWN_BLOCKED,
        )

    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    if not stdout and stderr_bytes:
        # If the JSON envelope never made it to stdout, fall back to stderr
        # so logs aren't empty. Parser will treat it as plaintext and
        # surface the raw text as ``output``.
        stdout = stderr_bytes.decode("utf-8", errors="replace")

    exit_code = proc.returncode if proc.returncode is not None else 0
    return parse_output(stdout, exit_code=exit_code)


async def run_task(
    task: Task,
    prompt: str,
    model: str | None = None,
    *,
    backoff_schedule: Sequence[int] = BACKOFF_SCHEDULE,
) -> AgentResult:
    """Run ``claude`` for *task* with retry on transient API errors.

    Parameters
    ----------
    task:
        The :class:`~whilly.core.models.Task` being executed. Only used for
        log context today (``task.id`` appears in retry warnings) — accepting
        the full object keeps the door open for future per-task knobs (e.g.
        a per-task timeout) without an API change.
    prompt:
        The agent prompt — typically built by
        :func:`whilly.core.prompts.build_task_prompt`.
    model:
        Explicit model override; falls back to ``WHILLY_MODEL`` env then
        :data:`DEFAULT_MODEL`.
    backoff_schedule:
        Exponential backoff in seconds between retries. Defaults to
        :data:`BACKOFF_SCHEDULE` (5/10/20/40/60). Tests pass ``(0, 0, 0, 0, 0)``
        to exercise retry behaviour without sleeping; an empty tuple
        disables retries entirely.

    Returns
    -------
    AgentResult
        On success — the agent's final :class:`AgentResult`.
        On retriable failure exhausting the schedule — the *last* result
        observed (so the caller has the real error text, not a synthetic
        "max retries" message).
        On permanent auth failure — the first result, immediately.
    """
    resolved_model = _resolve_model(model)
    total_attempts = len(backoff_schedule) + 1
    last_result: AgentResult | None = None

    for attempt in range(total_attempts):
        if attempt > 0:
            delay = backoff_schedule[attempt - 1]
            log.warning(
                "task=%s attempt=%d/%d retriable error — sleeping %ds before retry",
                task.id,
                attempt + 1,
                total_attempts,
                delay,
            )
            await asyncio.sleep(delay)

        result = await _spawn_and_collect(prompt, resolved_model)
        last_result = result

        if not _is_retriable_error(result):
            return result

    # Exhausted retries: return the last attempt verbatim. ``last_result``
    # is guaranteed non-None because the loop runs at least once.
    assert last_result is not None
    log.error(
        "task=%s exhausted %d retries — last exit_code=%s",
        task.id,
        len(backoff_schedule),
        last_result.exit_code,
    )
    return last_result


__all__ = [
    "BACKOFF_SCHEDULE",
    "DEFAULT_BIN",
    "DEFAULT_MODEL",
    "EXIT_BINARY_NOT_FOUND",
    "EXIT_SPAWN_BLOCKED",
    "build_command",
    "run_task",
]
