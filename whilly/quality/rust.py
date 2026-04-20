"""Rust quality gate — cargo test + cargo clippy + cargo fmt --check.

Detection: ``Cargo.toml`` at the project root.

Stages:

* ``cargo test`` — full test sweep.
* ``cargo clippy -- -D warnings`` — linter, warnings promoted to errors
  so the gate flags them. Skipped silently if clippy isn't installed
  (it's a rustup component that not all repos enable).
* ``cargo fmt --all -- --check`` — format check. Non-zero exit when any
  file would be reformatted.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from whilly.quality._runner import combine, run_stage
from whilly.quality.base import GateResult, StageResult


class RustQualityGate:
    kind = "rust"

    def detect(self, cwd: Path) -> bool:
        return (cwd / "Cargo.toml").is_file()

    def run(self, cwd: Path) -> GateResult:
        stages: list[StageResult] = [
            run_stage("cargo test", ["cargo", "test"], cwd),
        ]
        # clippy is a rustup component — skip silently when absent.
        if shutil.which("cargo") and self._has_clippy():
            stages.append(run_stage("cargo clippy", ["cargo", "clippy", "--", "-D", "warnings"], cwd))
        stages.append(run_stage("cargo fmt --check", ["cargo", "fmt", "--all", "--", "--check"], cwd))

        passed = all(s.passed for s in stages)
        return GateResult(
            gate_kind=self.kind,
            passed=passed,
            summary=combine(self.kind, stages),
            stages=stages,
        )

    @staticmethod
    def _has_clippy() -> bool:
        """Heuristic — ``cargo clippy --version`` exits 0 when installed."""
        import subprocess

        try:
            return (
                subprocess.run(
                    ["cargo", "clippy", "--version"],
                    capture_output=True,
                    timeout=5,
                    check=False,
                ).returncode
                == 0
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
