"""Go quality gate — go test + go vet + gofmt.

Detection: ``go.mod`` at the project root.

Stages:

* ``go test ./...`` — full module test sweep.
* ``go vet ./...`` — static analysis.
* ``gofmt -l .`` — format check. gofmt exits 0 whether formatted or not;
  the check is "stdout must be empty" (it lists files needing format).
  We rewrap via a shell one-liner so a non-empty stdout fails the stage.

Kept minimal in v1 — no ``golangci-lint`` / ``staticcheck`` because those
are optional deps and detection becomes "is it installed AND configured",
which is a bigger surface than this gate needs.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from whilly.quality._runner import combine, run_stage
from whilly.quality.base import GateResult, StageResult


class GoQualityGate:
    kind = "go"

    def detect(self, cwd: Path) -> bool:
        return (cwd / "go.mod").is_file()

    def _gofmt_stage(self, cwd: Path) -> StageResult:
        """gofmt -l prints files that would be reformatted; empty stdout = pass."""
        start = time.monotonic()
        if shutil.which("gofmt") is None:
            return StageResult(name="gofmt -l", passed=False, summary="'gofmt' not found on PATH")
        try:
            proc = subprocess.run(
                ["gofmt", "-l", "."],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return StageResult(name="gofmt -l", passed=False, summary=str(exc))

        dirty = (proc.stdout or "").strip()
        passed = proc.returncode == 0 and not dirty
        summary = "" if passed else f"Files needing formatting:\n{dirty or proc.stderr}"
        return StageResult(name="gofmt -l", passed=passed, summary=summary, duration_s=time.monotonic() - start)

    def run(self, cwd: Path) -> GateResult:
        stages = [
            run_stage("go test", ["go", "test", "./..."], cwd),
            run_stage("go vet", ["go", "vet", "./..."], cwd),
            self._gofmt_stage(cwd),
        ]
        passed = all(s.passed for s in stages)
        return GateResult(
            gate_kind=self.kind,
            passed=passed,
            summary=combine(self.kind, stages),
            stages=stages,
        )
