"""Unit tests for :mod:`whilly.adapters.runner.result_parser` (TASK-017a, PRD FR-1.6, TC-7).

These tests cover the AC for TASK-017a:

- :class:`AgentResult` is a frozen dataclass exposing ``output``, ``usage``,
  ``exit_code`` and ``is_complete``.
- :func:`parse_output` is a **pure** function — same input always yields the
  same :class:`AgentResult`, no subprocess / clock / filesystem reads.
- The completion handshake — literal ``<promise>COMPLETE</promise>`` — flips
  :attr:`AgentResult.is_complete` to ``True``.
- The Claude CLI ``--output-format json`` envelope is parsed into the right
  fields (``result`` → ``output``; nested ``usage`` → :class:`AgentUsage`).
- Hostile inputs (empty stdout, plaintext, JSON arrays, missing keys, wrong
  types, ``None`` values) never raise — they fall back to a sensible empty
  :class:`AgentResult` with the raw stdout as ``output`` so operators retain
  visibility.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from whilly.adapters.runner import AgentResult, AgentUsage, COMPLETION_MARKER, parse_output


# ---------------------------------------------------------------------------
# Constant + dataclass shape
# ---------------------------------------------------------------------------


def test_completion_marker_is_canonical() -> None:
    """The marker must match the literal the agent emits — never paraphrased."""
    assert COMPLETION_MARKER == "<promise>COMPLETE</promise>"


def test_agent_usage_defaults_are_zero() -> None:
    usage = AgentUsage()
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cache_read_tokens == 0
    assert usage.cache_create_tokens == 0
    assert usage.cost_usd == 0.0
    assert usage.num_turns == 0
    assert usage.duration_ms == 0


def test_agent_result_defaults() -> None:
    result = AgentResult()
    assert result.output == ""
    assert result.usage == AgentUsage()
    assert result.exit_code == 0
    assert result.is_complete is False


def test_agent_result_is_frozen() -> None:
    """Mutating an :class:`AgentResult` must raise — immutability is the contract."""
    result = AgentResult()
    with pytest.raises(FrozenInstanceError):
        result.exit_code = 1  # type: ignore[misc]


def test_agent_usage_is_frozen() -> None:
    usage = AgentUsage()
    with pytest.raises(FrozenInstanceError):
        usage.input_tokens = 5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Empty / fallback inputs
# ---------------------------------------------------------------------------


def test_empty_stdout_yields_default_result() -> None:
    result = parse_output("")
    assert result == AgentResult(exit_code=0)


def test_empty_stdout_with_nonzero_exit_code() -> None:
    """An empty stdout must still propagate the subprocess exit code."""
    result = parse_output("", exit_code=137)
    assert result.exit_code == 137
    assert result.output == ""
    assert result.is_complete is False


def test_plaintext_stdout_falls_back_to_raw() -> None:
    """Non-JSON stdout is preserved verbatim as ``output`` (operator visibility)."""
    raw = "API Error: 503 Service Unavailable\nRetry later."
    result = parse_output(raw)
    assert result.output == raw
    assert result.usage == AgentUsage()
    assert result.is_complete is False


def test_plaintext_with_completion_marker_is_complete() -> None:
    """Even when JSON parsing fails, a marker in raw stdout still signals success.

    Justified by the runner contract: agents that crash mid-stream may have
    already emitted the marker before the JSON envelope. Better to honour it
    than lose the completion signal to a parse error.
    """
    raw = f"agent crashed but had emitted {COMPLETION_MARKER} earlier"
    result = parse_output(raw)
    assert result.output == raw
    assert result.is_complete is True


def test_partial_json_falls_back_safely() -> None:
    raw = '{"result": "half-written'  # missing closing brace + quote
    result = parse_output(raw)
    assert result.output == raw
    assert result.usage == AgentUsage()


@pytest.mark.parametrize("raw", ["null", "[1, 2, 3]", "42", '"just a string"', "true"])
def test_top_level_non_object_json_falls_back(raw: str) -> None:
    """JSON that parses but isn't an object must not crash — we only know how
    to read the dict envelope, anything else is treated as opaque stdout."""
    result = parse_output(raw)
    assert result.output == raw
    assert result.usage == AgentUsage()
    assert result.is_complete is False


# ---------------------------------------------------------------------------
# Happy-path JSON envelope
# ---------------------------------------------------------------------------


def _envelope(**overrides: object) -> str:
    """Build a Claude-CLI-shaped JSON envelope for tests."""
    base: dict[str, object] = {
        "result": "All tasks finished. " + COMPLETION_MARKER,
        "total_cost_usd": 0.0123,
        "num_turns": 4,
        "duration_ms": 12345,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 10,
            "cache_creation_input_tokens": 20,
        },
    }
    base.update(overrides)
    return json.dumps(base)


def test_full_envelope_is_parsed_into_all_fields() -> None:
    result = parse_output(_envelope())
    assert result.output == "All tasks finished. " + COMPLETION_MARKER
    assert result.is_complete is True
    assert result.exit_code == 0
    assert result.usage == AgentUsage(
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=10,
        cache_create_tokens=20,
        cost_usd=0.0123,
        num_turns=4,
        duration_ms=12345,
    )


def test_envelope_without_marker_is_not_complete() -> None:
    result = parse_output(_envelope(result="No completion signal here."))
    assert result.is_complete is False
    assert result.output == "No completion signal here."


def test_envelope_marker_outside_result_field_is_not_complete() -> None:
    """The marker must live inside ``result`` — placing it elsewhere in the JSON
    body must NOT count, otherwise a stray mention in a tool argument or log
    line would falsely mark the task done."""
    raw = json.dumps(
        {
            "result": "still working",
            "metadata": {"comment": COMPLETION_MARKER},
        }
    )
    result = parse_output(raw)
    assert result.is_complete is False
    assert result.output == "still working"


def test_envelope_propagates_exit_code() -> None:
    result = parse_output(_envelope(), exit_code=7)
    assert result.exit_code == 7
    # exit_code does not affect parsing of other fields
    assert result.is_complete is True


# ---------------------------------------------------------------------------
# Defensive parsing — missing / wrongly-typed fields
# ---------------------------------------------------------------------------


def test_envelope_missing_usage_block_yields_zero_usage() -> None:
    raw = json.dumps({"result": "done", "total_cost_usd": 0.5})
    result = parse_output(raw)
    assert result.output == "done"
    assert result.usage == AgentUsage(cost_usd=0.5)


def test_envelope_with_nonstring_result_falls_back_to_raw() -> None:
    """If ``result`` exists but isn't a string (e.g. ``null``), we surface the
    full stdout so logs aren't truncated to an empty string."""
    raw = json.dumps({"result": None, "total_cost_usd": 0.1})
    result = parse_output(raw)
    assert result.output == raw  # raw preserved
    assert result.usage.cost_usd == pytest.approx(0.1)
    assert result.is_complete is False


def test_envelope_with_missing_result_falls_back_to_raw() -> None:
    raw = json.dumps({"total_cost_usd": 0.1})
    result = parse_output(raw)
    assert result.output == raw
    assert result.is_complete is False


def test_envelope_with_none_token_counts_yields_zero() -> None:
    """``null`` for any counter must coerce to 0, not crash on ``int(None)``."""
    raw = json.dumps(
        {
            "result": "ok",
            "total_cost_usd": None,
            "num_turns": None,
            "duration_ms": None,
            "usage": {
                "input_tokens": None,
                "output_tokens": None,
                "cache_read_input_tokens": None,
                "cache_creation_input_tokens": None,
            },
        }
    )
    result = parse_output(raw)
    assert result.usage == AgentUsage()
    assert result.output == "ok"


def test_envelope_with_wrongly_typed_usage_block_yields_zero() -> None:
    """``usage`` must be a dict; any other type is treated as missing."""
    raw = json.dumps({"result": "ok", "usage": "not-a-dict"})
    result = parse_output(raw)
    assert result.usage == AgentUsage()


def test_envelope_with_wrongly_typed_counters_yields_zero() -> None:
    raw = json.dumps(
        {
            "result": "ok",
            "total_cost_usd": "not-a-number",
            "num_turns": [1, 2, 3],
            "usage": {
                "input_tokens": "100",  # string instead of int
                "output_tokens": {"nested": True},
            },
        }
    )
    result = parse_output(raw)
    assert result.usage == AgentUsage()


def test_envelope_accepts_int_cost_and_float_token_counts() -> None:
    """``total_cost_usd`` may legitimately be an int (e.g. ``0``) and
    occasional float token counts have been observed; both must coerce."""
    raw = json.dumps(
        {
            "result": "ok",
            "total_cost_usd": 0,  # int
            "usage": {"input_tokens": 100.0, "output_tokens": 50.7},  # floats
        }
    )
    result = parse_output(raw)
    assert result.usage.cost_usd == 0.0
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 50  # int() truncates


def test_envelope_with_bool_counters_does_not_silently_pass_through_as_int() -> None:
    """``True`` is technically ``int`` in Python, so explicit guards keep the
    coercion well-defined. We accept it (=1) but document the behaviour."""
    raw = json.dumps(
        {
            "result": "ok",
            "num_turns": True,  # JSON true
            "usage": {"input_tokens": False},
        }
    )
    result = parse_output(raw)
    assert result.usage.num_turns == 1
    assert result.usage.input_tokens == 0


# ---------------------------------------------------------------------------
# Purity / determinism
# ---------------------------------------------------------------------------


def test_parse_output_is_deterministic() -> None:
    raw = _envelope()
    assert parse_output(raw) == parse_output(raw)


def test_parse_output_is_deterministic_for_failures() -> None:
    raw = "definitely not json"
    assert parse_output(raw, exit_code=2) == parse_output(raw, exit_code=2)


def test_parse_output_does_not_mutate_input_string() -> None:
    """Strings are immutable in Python, but we re-assert the contract via id()
    to make any future refactor that interpolates / replaces the raw string
    visible in the diff."""
    raw = _envelope()
    snapshot = raw
    parse_output(raw)
    assert raw == snapshot


@pytest.mark.parametrize("exit_code", [-2, -1, 0, 1, 2, 137, 255])
def test_exit_code_round_trips_unchanged(exit_code: int) -> None:
    """Negative codes are used by the wrapper to encode timeout / missing-binary
    cases (see TASK-017b ``claude_cli.py``); they must pass through verbatim."""
    result = parse_output(_envelope(), exit_code=exit_code)
    assert result.exit_code == exit_code
