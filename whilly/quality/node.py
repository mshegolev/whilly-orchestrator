"""Node / JavaScript / TypeScript quality gate.

Detection: ``package.json`` at the project root.

Stages are derived from ``package.json.scripts`` — we run ``npm test`` +
``npm run lint`` + ``npm run format:check`` only when the respective
scripts are defined. Missing scripts are NOT treated as failures (unlike
Python, where pytest/ruff absence is a red flag) — many Node projects
intentionally skip a linter or split checks differently.

Rationale: the JS ecosystem has half a dozen popular combinations
(vitest/jest, eslint/biome, prettier/rome). Reading ``package.json``
scripts is the one source of truth every project actually maintains.
"""

from __future__ import annotations

import json
from pathlib import Path

from whilly.quality._runner import combine, run_stage
from whilly.quality.base import GateResult, StageResult


class NodeQualityGate:
    kind = "node"

    # npm script name → stage label mapping
    _STAGES = [
        ("test", "npm test"),
        ("lint", "npm run lint"),
        ("format:check", "npm run format:check"),
        ("typecheck", "npm run typecheck"),
    ]

    def detect(self, cwd: Path) -> bool:
        return (cwd / "package.json").is_file()

    def _scripts(self, cwd: Path) -> dict[str, str]:
        try:
            data = json.loads((cwd / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(data.get("scripts") or {})

    def run(self, cwd: Path) -> GateResult:
        scripts = self._scripts(cwd)
        stages: list[StageResult] = []
        for script_name, label in self._STAGES:
            if script_name not in scripts:
                continue
            if script_name == "test":
                cmd = ["npm", "test", "--silent"]
            else:
                cmd = ["npm", "run", script_name, "--silent"]
            stages.append(run_stage(label, cmd, cwd))

        if not stages:
            # Nothing to run — treat as "no gate applies" rather than fail.
            # The multi-language runner will skip us in this case.
            return GateResult(
                gate_kind=self.kind,
                passed=True,
                summary=f"[{self.kind}] no test/lint scripts defined in package.json",
                stages=[],
            )

        passed = all(s.passed for s in stages)
        return GateResult(
            gate_kind=self.kind,
            passed=passed,
            summary=combine(self.kind, stages),
            stages=stages,
        )
