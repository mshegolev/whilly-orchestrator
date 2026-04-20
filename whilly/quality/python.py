"""Python quality gate — pytest + ruff check + ruff format --check.

Detection: presence of ``pyproject.toml`` OR ``setup.py`` OR ``setup.cfg`` OR
``requirements.txt`` at the project root. That covers every mainstream Python
project layout without requiring a specific build backend.

Stages:

* ``pytest`` — runs the full suite. We do *not* pass ``-q`` so the
  failure stage summary shows the actual failing test names rather than
  a single ``1 failed`` line.
* ``ruff check`` — linting.
* ``ruff format --check`` — formatting (non-fix, we want the gate to fail
  loudly on unformatted code, not silently rewrite under review).

Any stage whose binary is missing returns ``passed=False`` — a Python
project without pytest/ruff in its dev deps should fail the gate so the
user notices (rather than silently skipping).
"""

from __future__ import annotations

from pathlib import Path

from whilly.quality._runner import combine, run_stage
from whilly.quality.base import GateResult, StageResult


class PythonQualityGate:
    kind = "python"

    _MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")

    def detect(self, cwd: Path) -> bool:
        return any((cwd / m).exists() for m in self._MARKERS)

    def run(self, cwd: Path) -> GateResult:
        stages: list[StageResult] = [
            run_stage("pytest", ["pytest", "-q", "--no-header"], cwd),
            run_stage("ruff check", ["ruff", "check", "."], cwd),
            run_stage("ruff format --check", ["ruff", "format", "--check", "."], cwd),
        ]
        passed = all(s.passed for s in stages)
        return GateResult(
            gate_kind=self.kind,
            passed=passed,
            summary=combine(self.kind, stages),
            stages=stages,
        )
