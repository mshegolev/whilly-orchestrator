"""Live-LLM smoke test for the per-task TRIZ analyzer (TASK-104b, VAL-TRIZ-013).

Gated by both:

* ``WHILLY_RUN_LIVE_LLM=1`` — operator opts in (the regular CI run does
  not subprocess to a real model).
* ``@pytest.mark.live_llm`` marker — selectable via ``-m live_llm``
  on the command line (``WHILLY_RUN_LIVE_LLM=1 pytest -m live_llm``).

When ``WHILLY_RUN_LIVE_LLM`` is unset, the test is skipped with a
reason that names the gate variable (VAL-TRIZ-014). When the variable
is set but ``claude`` is absent from PATH, the test fails with a clear
message rather than silently passing.
"""

from __future__ import annotations

import os
import shutil
import time

import pytest

from whilly.core.models import Priority, Task, TaskStatus
from whilly.core.triz import TIMEOUT_SECONDS, TrizFinding, analyze_contradiction


def _live_llm_enabled() -> bool:
    return os.environ.get("WHILLY_RUN_LIVE_LLM") == "1"


# Marker plus skip-if so users see the actual reason text on plain `pytest`.
pytestmark = [
    pytest.mark.live_llm,
    pytest.mark.skipif(
        not _live_llm_enabled(),
        reason="live_llm gate: set WHILLY_RUN_LIVE_LLM=1 to run the live Claude smoke test",
    ),
]


def _canonical_contradiction_task() -> Task:
    """Build a task whose description encodes a textbook TRIZ contradiction."""
    return Task(
        id="T-LIVE-001",
        status=TaskStatus.IN_PROGRESS,
        priority=Priority.HIGH,
        description=(
            "Design a distributed cache that must be both fully strongly consistent "
            "and fully eventually-consistent at the same time across every regional "
            "read replica."
        ),
        acceptance_criteria=(
            "Reads return the latest write within 10 ms",
            "Writes scale linearly to 100 regions",
        ),
        test_steps=("pytest -k cache",),
    )


def test_smoke() -> None:
    """Real Claude returns a plausible TrizFinding within the 25 s budget.

    Asserts the live AC contract from VAL-TRIZ-013:

    * ``finding is not None``
    * ``contradiction_type`` is a non-empty string
    * ``reason`` is at least 20 characters of natural language
    * single-call wall clock ≤ 25 seconds
    """
    if shutil.which("claude") is None:
        # When the gate variable is set but claude is missing the test
        # FAILS rather than silently passing (VAL-TRIZ-014).
        pytest.fail(
            "WHILLY_RUN_LIVE_LLM=1 is set but the `claude` CLI is not on PATH; "
            "install Claude CLI or unset the gate variable."
        )

    task = _canonical_contradiction_task()
    t_start = time.monotonic()
    finding = analyze_contradiction(task)
    elapsed = time.monotonic() - t_start

    # Hard timeout invariant — VAL-TRIZ-013 / VAL-TRIZ-007.
    assert elapsed <= TIMEOUT_SECONDS, f"analyze_contradiction took {elapsed:.1f}s > {TIMEOUT_SECONDS}s budget"

    # Live model returned a plausible verdict.
    assert finding is not None, "live Claude must surface a contradiction for the canonical task"
    assert isinstance(finding, TrizFinding)
    assert isinstance(finding.contradiction_type, str)
    assert len(finding.contradiction_type) > 0
    assert len(finding.reason) >= 20
