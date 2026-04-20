"""Composite quality gate — runs every gate that applies to *cwd*.

Most self-hosting projects are single-language; multi-language monorepos
exist (a Python core + a Node tooling sidecar, for example) and we want
the pipeline to "just work" in that case — run both gates, fail if either
fails, render both summaries.

Not itself registered as a gate ``kind`` — it's the top-level entry point
for :func:`whilly.quality.run_detected` and returns a synthesised
``gate_kind="multi"`` result aggregating the individual stages.
"""

from __future__ import annotations

from pathlib import Path

from whilly.quality.base import GateResult, QualityGate, StageResult


def run_all(gates: list[QualityGate], cwd: Path) -> GateResult:
    """Run every *gate* in order and aggregate their results.

    An empty ``gates`` list is still a valid outcome — we return
    ``passed=True`` with a summary saying "no gates detected" so callers
    (the pipeline) can log it and continue, rather than crashing. Silent
    success when literally nothing applies.
    """
    if not gates:
        return GateResult(
            gate_kind="multi",
            passed=True,
            summary="[multi] no language gates detected — skipping quality check",
            stages=[],
        )

    all_stages: list[StageResult] = []
    summaries: list[str] = []
    all_passed = True
    for gate in gates:
        result = gate.run(cwd)
        all_passed = all_passed and result.passed
        all_stages.extend(result.stages)
        summaries.append(result.summary)

    return GateResult(
        gate_kind="multi",
        passed=all_passed,
        summary="\n\n".join(summaries),
        stages=all_stages,
    )
