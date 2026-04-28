"""Pure parser for Claude CLI ``--output-format json`` stdout (PRD FR-1.6, TC-7).

This module is the I/O-free half of the runner adapter pair (TASK-017a).
The subprocess wrapper (TASK-017b, ``claude_cli.py``) feeds the captured
stdout string into :func:`parse_output` and gets back a fully-populated
:class:`AgentResult`. Keeping the parsing logic on its own — with no
``subprocess``, ``asyncio`` or filesystem imports — means the unit tests can
exercise every JSON edge case without spawning a single process.

What we parse
-------------
Claude CLI in non-interactive mode (``claude -p "<prompt>" --output-format
json``) prints a single JSON object whose shape is, abridged::

    {
      "result": "<final assistant message text>",
      "total_cost_usd": 0.0042,
      "num_turns": 3,
      "duration_ms": 12345,
      "usage": {
         "input_tokens": 100,
         "output_tokens": 50,
         "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0
      }
    }

The completion handshake (PRD FR-1.6) is the literal string
``<promise>COMPLETE</promise>`` — emitted by the agent inside ``result``
whenever it considers a task done. We surface that as
:attr:`AgentResult.is_complete`.

Why ``parse_output`` accepts ``exit_code`` rather than parsing it
----------------------------------------------------------------
Exit code is a property of the *subprocess*, not of stdout. We thread it
through here so :class:`AgentResult` can stay a single immutable value object
that callers consume — no need to merge two fields after parsing. The
default of ``0`` keeps tests that only care about JSON parsing terse:
``parse_output(stdout)``.

Defensive parsing
-----------------
Production stdout is hostile: it may be empty (claude crashed before
writing), partial (timeout mid-stream), pure plaintext (an upstream error
page), or JSON without the ``usage`` key. None of those are programmer
errors, so we never raise: malformed input falls back to a zeroed
:class:`AgentUsage` and the raw stdout as ``output``. The subprocess wrapper
already encodes the failure via ``exit_code``; logging/alerting belongs
there, not in this pure helper.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

# Universal completion handshake — instructed by build_task_prompt
# (whilly.core.prompts) and detected here. Kept as a module constant so
# tests and callers reference one source of truth.
COMPLETION_MARKER: Final[str] = "<promise>COMPLETE</promise>"


@dataclass(frozen=True)
class AgentUsage:
    """Token / cost accounting for a single agent invocation.

    Frozen so the enclosing :class:`AgentResult` stays a value object end-to-end
    (PRD NFR-4 / TC-8). All fields default to zero so a malformed response
    still produces a valid ``AgentUsage`` rather than raising.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_create_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0


@dataclass(frozen=True)
class AgentResult:
    """Outcome of one agent invocation.

    Layout matches the TASK-017a AC: ``usage``, ``exit_code``, ``is_complete``
    and ``output`` (the assistant's final message text). Frozen + nested
    frozen ``AgentUsage`` so the value can be safely shared across asyncio
    tasks without defensive copying.

    Attributes
    ----------
    output:
        The assistant's final ``result`` text from the JSON envelope, OR the
        raw stdout if parsing failed (so logs always have something useful).
    usage:
        Token / cost accounting; zeroed when ``usage`` is missing.
    exit_code:
        Subprocess exit code threaded through by the wrapper. ``0`` means
        success in POSIX terms; non-zero is interpreted by the caller.
    is_complete:
        ``True`` when :data:`COMPLETION_MARKER` appears in ``output`` —
        the protocol-level "task done" signal.
    """

    output: str = ""
    usage: AgentUsage = field(default_factory=AgentUsage)
    exit_code: int = 0
    is_complete: bool = False


def _coerce_int(value: Any) -> int:
    """Coerce a JSON value to ``int`` defensively (``None`` / wrong-type → 0)."""
    if isinstance(value, bool):
        # ``bool`` is a subclass of ``int`` in Python; explicit guard prevents
        # ``True`` slipping through as ``1`` for fields that expect counters.
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _coerce_float(value: Any) -> float:
    """Coerce a JSON value to ``float`` defensively (``None`` / wrong-type → 0.0)."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _parse_usage(payload: Any) -> AgentUsage:
    """Build an :class:`AgentUsage` from the top-level JSON envelope.

    The envelope nests token counters inside ``"usage"`` but keeps cost,
    turn count and duration at the top level — we mirror that shape here.
    Anything missing or wrongly-typed yields the zero default for that
    field; we never raise.
    """
    if not isinstance(payload, dict):
        return AgentUsage()

    usage_block = payload.get("usage")
    if not isinstance(usage_block, dict):
        usage_block = {}

    return AgentUsage(
        input_tokens=_coerce_int(usage_block.get("input_tokens")),
        output_tokens=_coerce_int(usage_block.get("output_tokens")),
        cache_read_tokens=_coerce_int(usage_block.get("cache_read_input_tokens")),
        cache_create_tokens=_coerce_int(usage_block.get("cache_creation_input_tokens")),
        cost_usd=_coerce_float(payload.get("total_cost_usd")),
        num_turns=_coerce_int(payload.get("num_turns")),
        duration_ms=_coerce_int(payload.get("duration_ms")),
    )


def parse_output(stdout: str, exit_code: int = 0) -> AgentResult:
    """Parse Claude CLI stdout into an immutable :class:`AgentResult`.

    Pure: no subprocess, no clock, no env reads. The same input always
    yields the same :class:`AgentResult`, which makes this trivial to unit
    test.

    Parameters
    ----------
    stdout:
        The captured stdout of ``claude --output-format json -p "<prompt>"``.
        May be empty, malformed, or plain text — none of these raise.
    exit_code:
        Subprocess exit code; threaded through to :attr:`AgentResult.exit_code`
        so callers get a single value object back. Defaults to ``0`` for
        terse tests.

    Returns
    -------
    AgentResult
        Always a valid value, even on parse failure — see module docstring
        for the fallback contract.
    """
    if not stdout:
        return AgentResult(exit_code=exit_code)

    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        # Plaintext or partial JSON: surface the raw text so downstream logs
        # still capture *something*, but we cannot extract usage / completion
        # marker reliably from anything but the parsed ``result`` field.
        # Note: we still scan the raw stdout for the marker — agents that
        # crashed mid-stream may still have signalled completion before the
        # JSON envelope was emitted.
        return AgentResult(
            output=stdout,
            exit_code=exit_code,
            is_complete=COMPLETION_MARKER in stdout,
        )

    if not isinstance(payload, dict):
        # Top-level JSON wasn't an object (e.g. ``null``, an array, a bare
        # number). Nothing to do beyond surface-level fallback.
        return AgentResult(
            output=stdout,
            exit_code=exit_code,
            is_complete=COMPLETION_MARKER in stdout,
        )

    result_field = payload.get("result")
    output = result_field if isinstance(result_field, str) else ""
    if not output:
        # The envelope parsed but ``result`` was missing/non-string. Fall
        # back to the raw stdout so operators retain visibility.
        output = stdout

    return AgentResult(
        output=output,
        usage=_parse_usage(payload),
        exit_code=exit_code,
        is_complete=COMPLETION_MARKER in output,
    )
