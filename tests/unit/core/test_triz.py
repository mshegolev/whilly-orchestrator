"""Unit tests for the per-task TRIZ analyzer (TASK-104b).

The contract under test is in :mod:`whilly.core.triz`; the
assertions below mirror VAL-TRIZ-003 (claude absent), VAL-TRIZ-005
(malformed JSON), VAL-TRIZ-006 (subprocess invocation shape),
VAL-TRIZ-012 (one warning per failure mode), and VAL-TRIZ-016
(structured ``record.event`` field).

Every test mocks ``subprocess.run`` and ``shutil.which`` — none of
the unit tests reach the network or the real ``claude`` CLI. The
live-smoke counterpart lives in
``tests/integration/test_triz_live.py`` and is gated by
``WHILLY_RUN_LIVE_LLM=1``.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

import pytest

from whilly.core.models import Priority, Task, TaskStatus
from whilly.core.triz import (
    CLAUDE_BIN,
    LOG_EVENT_CLAUDE_MISSING,
    LOG_EVENT_PARSE_ERROR,
    LOG_EVENT_TIMEOUT,
    TIMEOUT_SECONDS,
    TrizFinding,
    analyze_contradiction,
    analyze_contradiction_with_outcome,
)


# ─── helpers ─────────────────────────────────────────────────────────────


def _make_task(
    *,
    description: str = (
        "The cache must be both fully consistent and fully eventually-consistent across every regional read replica."
    ),
    acceptance_criteria: tuple[str, ...] = ("Reads return the latest write within 10ms",),
    task_id: str = "T-TRIZ-001",
) -> Task:
    """Build a default task whose description encodes a TRIZ contradiction."""
    return Task(
        id=task_id,
        status=TaskStatus.PENDING,
        priority=Priority.MEDIUM,
        description=description,
        acceptance_criteria=acceptance_criteria,
        test_steps=("step-1",),
    )


class _RecordingRun:
    """Subprocess.run double that records its argv/kwargs and returns canned output."""

    def __init__(self, *, stdout: str = "", returncode: int = 0, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []
        self._stdout = stdout
        self._returncode = returncode
        self._raises = raises

    def __call__(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        if self._raises is not None:
            raise self._raises
        # subprocess.run accepts cmd as first positional or as kwarg
        cmd = args[0] if args else kwargs.get("args")
        return subprocess.CompletedProcess(args=cmd, returncode=self._returncode, stdout=self._stdout, stderr="")


def _patch_claude_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``shutil.which("claude")`` to resolve to a deterministic path."""
    monkeypatch.setattr("whilly.core.triz.shutil.which", lambda name: "/usr/bin/claude" if name == CLAUDE_BIN else None)


# ─── VAL-TRIZ-003: claude CLI absent → graceful skip + structured warning ─


def test_analyze_returns_none_when_claude_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``shutil.which`` returns None → return None and emit one structured WARNING."""
    monkeypatch.setattr("whilly.core.triz.shutil.which", lambda _name: None)
    # subprocess.run must NOT be called when claude is absent.
    runner = _RecordingRun(stdout="should not run")
    monkeypatch.setattr("whilly.core.triz.subprocess.run", runner)

    with caplog.at_level(logging.WARNING, logger="whilly.core.triz"):
        finding = analyze_contradiction(_make_task())

    assert finding is None
    assert runner.calls == []  # claude never invoked

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "whilly.core.triz"]
    assert len(warning_records) == 1, "expected exactly one warning per failure mode"
    assert "claude" in warning_records[0].getMessage().lower()
    # VAL-TRIZ-016: structured ``record.event`` matches the documented enum.
    assert getattr(warning_records[0], "event", None) == LOG_EVENT_CLAUDE_MISSING


def test_filenotfound_during_subprocess_is_treated_as_claude_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A defensive guard: if ``shutil.which`` lies and ``subprocess.run`` raises ``FileNotFoundError``."""
    _patch_claude_present(monkeypatch)
    runner = _RecordingRun(raises=FileNotFoundError("claude vanished"))
    monkeypatch.setattr("whilly.core.triz.subprocess.run", runner)

    with caplog.at_level(logging.WARNING, logger="whilly.core.triz"):
        finding = analyze_contradiction(_make_task())

    assert finding is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "whilly.core.triz"]
    assert len(warnings) == 1
    assert getattr(warnings[0], "event", None) == LOG_EVENT_CLAUDE_MISSING


# ─── VAL-TRIZ-004 / VAL-TRIZ-006: timeout → return None + outcome.error_reason="timeout" ─


def test_analyze_returns_none_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Subprocess raises TimeoutExpired → ``analyze_contradiction`` returns None."""
    _patch_claude_present(monkeypatch)
    runner = _RecordingRun(raises=subprocess.TimeoutExpired(cmd=[CLAUDE_BIN], timeout=TIMEOUT_SECONDS))
    monkeypatch.setattr("whilly.core.triz.subprocess.run", runner)

    with caplog.at_level(logging.WARNING, logger="whilly.core.triz"):
        finding = analyze_contradiction(_make_task())

    assert finding is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "whilly.core.triz"]
    assert len(warnings) == 1
    assert getattr(warnings[0], "event", None) == LOG_EVENT_TIMEOUT


def test_outcome_carries_timeout_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """The richer ``analyze_contradiction_with_outcome`` exposes ``error_reason='timeout'``.

    The repository hook keys on this to write a ``triz.error`` event row
    (VAL-TRIZ-004).
    """
    _patch_claude_present(monkeypatch)
    runner = _RecordingRun(raises=subprocess.TimeoutExpired(cmd=[CLAUDE_BIN], timeout=TIMEOUT_SECONDS))
    monkeypatch.setattr("whilly.core.triz.subprocess.run", runner)

    outcome = analyze_contradiction_with_outcome(_make_task())
    assert outcome.finding is None
    assert outcome.error_reason == "timeout"


# ─── VAL-TRIZ-005: malformed JSON → safe-parse to None ──────────────────


@pytest.mark.parametrize(
    "stdout",
    [
        "<<not json>>\n",
        "",
        "this is just plain prose, sorry",
        '{"contradictory": "yes"}',  # wrong type for ``contradictory``
        '{"contradictory": true}',  # missing required keys
        '{"contradictory": true, "contradiction_type": "", "reason": "x"}',  # empty type
        '["not", "an", "object"]',  # top-level not a dict
    ],
)
def test_analyze_returns_none_on_malformed_or_shape_mismatched_output(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stdout: str,
) -> None:
    """Malformed JSON / shape-mismatched payload → return None + one parse_error warning."""
    _patch_claude_present(monkeypatch)
    runner = _RecordingRun(stdout=stdout, returncode=0)
    monkeypatch.setattr("whilly.core.triz.subprocess.run", runner)

    with caplog.at_level(logging.WARNING, logger="whilly.core.triz"):
        finding = analyze_contradiction(_make_task())

    assert finding is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "whilly.core.triz"]
    assert len(warnings) == 1
    assert getattr(warnings[0], "event", None) == LOG_EVENT_PARSE_ERROR


def test_analyze_returns_none_on_claude_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-zero returncode → parse_error soft-fail (no event row)."""
    _patch_claude_present(monkeypatch)
    runner = _RecordingRun(stdout="ignored", returncode=1)
    monkeypatch.setattr("whilly.core.triz.subprocess.run", runner)

    with caplog.at_level(logging.WARNING, logger="whilly.core.triz"):
        finding = analyze_contradiction(_make_task())
    assert finding is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "whilly.core.triz"]
    assert len(warnings) == 1
    assert getattr(warnings[0], "event", None) == LOG_EVENT_PARSE_ERROR


# ─── VAL-TRIZ-006: subprocess invocation shape ──────────────────────────


def test_subprocess_invocation_uses_claude_binary_with_25s_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recording stub captures ``argv[0]=='claude'`` and ``timeout==25``."""
    _patch_claude_present(monkeypatch)
    payload = json.dumps(
        {
            "contradictory": True,
            "contradiction_type": "technical",
            "reason": "improving consistency worsens latency by an order of magnitude",
        }
    )
    runner = _RecordingRun(stdout=payload, returncode=0)
    monkeypatch.setattr("whilly.core.triz.subprocess.run", runner)

    finding = analyze_contradiction(_make_task())

    assert isinstance(finding, TrizFinding)
    assert len(runner.calls) == 1
    args, kwargs = runner.calls[0]
    cmd = args[0] if args else kwargs.get("args")
    assert isinstance(cmd, list) and cmd, "subprocess.run must receive a list argv"
    assert cmd[0] == CLAUDE_BIN, f"argv[0] must be {CLAUDE_BIN!r}"
    assert kwargs.get("timeout") == TIMEOUT_SECONDS
    assert kwargs["timeout"] < 30  # HARD constraint: must stay below 30s claim visibility-timeout


# ─── VAL-TRIZ-001 (mock variant): contradiction → TrizFinding ───────────


def test_returns_finding_on_positive_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-formed positive verdict round-trips into a TrizFinding."""
    _patch_claude_present(monkeypatch)
    payload = json.dumps(
        {
            "contradictory": True,
            "contradiction_type": "physical",
            "reason": (
                "Cache must be both consistent and eventually-consistent — "
                "two opposing properties at the same logical instant."
            ),
        }
    )
    monkeypatch.setattr("whilly.core.triz.subprocess.run", _RecordingRun(stdout=payload, returncode=0))

    finding = analyze_contradiction(_make_task())
    assert isinstance(finding, TrizFinding)
    assert finding.contradiction_type == "physical"
    assert "Cache" in finding.reason
    assert len(finding.reason) >= 20


def test_returns_none_on_no_contradiction_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """``contradictory: false`` is the no-contradiction happy path → None, no warning."""
    _patch_claude_present(monkeypatch)
    payload = json.dumps({"contradictory": False, "contradiction_type": "", "reason": ""})
    monkeypatch.setattr("whilly.core.triz.subprocess.run", _RecordingRun(stdout=payload, returncode=0))

    finding = analyze_contradiction(_make_task())
    assert finding is None


def test_returns_finding_when_output_wrapped_in_markdown_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tolerant parser strips ```json fences before json.loads."""
    _patch_claude_present(monkeypatch)
    inner = json.dumps(
        {
            "contradictory": True,
            "contradiction_type": "technical",
            "reason": "improving throughput worsens tail latency at the 99.9th percentile",
        }
    )
    fenced = f"```json\n{inner}\n```"
    monkeypatch.setattr("whilly.core.triz.subprocess.run", _RecordingRun(stdout=fenced, returncode=0))

    finding = analyze_contradiction(_make_task())
    assert isinstance(finding, TrizFinding)
    assert finding.contradiction_type == "technical"


# ─── VAL-TRIZ-016 (set-membership): each failure mode emits its own enum ─


def test_each_failure_mode_emits_distinct_event_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Across the three failure modes, the captured ``record.event`` set matches the documented enum."""
    _patch_claude_present(monkeypatch)

    observed: set[str] = set()

    # Claude missing
    monkeypatch.setattr("whilly.core.triz.shutil.which", lambda _name: None)
    monkeypatch.setattr("whilly.core.triz.subprocess.run", _RecordingRun(stdout=""))
    triz_mod_logger = logging.getLogger("whilly.core.triz")
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Handler(level=logging.WARNING)
    triz_mod_logger.addHandler(handler)
    try:
        analyze_contradiction(_make_task())
    finally:
        triz_mod_logger.removeHandler(handler)
    observed.update({getattr(r, "event", None) for r in records if getattr(r, "event", None)})

    # Timeout
    _patch_claude_present(monkeypatch)
    monkeypatch.setattr(
        "whilly.core.triz.subprocess.run",
        _RecordingRun(raises=subprocess.TimeoutExpired(cmd=[CLAUDE_BIN], timeout=TIMEOUT_SECONDS)),
    )
    records.clear()
    handler2 = _Handler(level=logging.WARNING)
    triz_mod_logger.addHandler(handler2)
    try:
        analyze_contradiction(_make_task())
    finally:
        triz_mod_logger.removeHandler(handler2)
    observed.update({getattr(r, "event", None) for r in records if getattr(r, "event", None)})

    # Parse error
    _patch_claude_present(monkeypatch)
    monkeypatch.setattr("whilly.core.triz.subprocess.run", _RecordingRun(stdout="<not json>", returncode=0))
    records.clear()
    handler3 = _Handler(level=logging.WARNING)
    triz_mod_logger.addHandler(handler3)
    try:
        analyze_contradiction(_make_task())
    finally:
        triz_mod_logger.removeHandler(handler3)
    observed.update({getattr(r, "event", None) for r in records if getattr(r, "event", None)})

    assert observed == {LOG_EVENT_CLAUDE_MISSING, LOG_EVENT_TIMEOUT, LOG_EVENT_PARSE_ERROR}


# ─── analyze_contradiction never re-raises (foundation for VAL-TRIZ-015) ─


@pytest.mark.parametrize(
    "side_effect_kwargs",
    [
        # Each tuple: (which-result, run-result-or-exception)
        ("missing", None),
        ("present", subprocess.TimeoutExpired(cmd=[CLAUDE_BIN], timeout=TIMEOUT_SECONDS)),
        ("present", "garbage stdout"),
        ("present", FileNotFoundError("vanished")),
    ],
)
def test_analyze_never_re_raises(
    monkeypatch: pytest.MonkeyPatch,
    side_effect_kwargs: tuple[str, Any],
) -> None:
    """Foundation for VAL-TRIZ-015: analyze never re-raises into the caller."""
    which_result, run_result = side_effect_kwargs
    if which_result == "missing":
        monkeypatch.setattr("whilly.core.triz.shutil.which", lambda _name: None)
    else:
        _patch_claude_present(monkeypatch)

    if isinstance(run_result, BaseException):
        monkeypatch.setattr("whilly.core.triz.subprocess.run", _RecordingRun(raises=run_result))
    elif isinstance(run_result, str):
        monkeypatch.setattr("whilly.core.triz.subprocess.run", _RecordingRun(stdout=run_result, returncode=0))

    # Must not raise any exception type listed in VAL-TRIZ-015.
    finding = analyze_contradiction(_make_task())
    assert finding is None
