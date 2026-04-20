"""Tests for :mod:`whilly.quality` — Protocol conformance, per-impl detection,
multi-language composite, registry.

Subprocess interactions are mocked at the ``whilly.quality._runner.run_stage``
level so each test runs in milliseconds and doesn't require pytest/npm/go/cargo
to exist on the runner.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from whilly.quality import (
    GoQualityGate,
    NodeQualityGate,
    PythonQualityGate,
    QualityGate,
    RustQualityGate,
    available_gates,
    detect_gates,
    get_gate,
    run_detected,
)
from whilly.quality.base import GateResult, StageResult
from whilly.quality.multi import run_all


# ── Registry + Protocol conformance ───────────────────────────────────────────


class TestRegistry:
    def test_available_lists_all_built_in(self):
        assert available_gates() == ["go", "node", "python", "rust"]

    def test_get_gate_resolves_each_kind(self):
        for name in available_gates():
            gate = get_gate(name)
            assert gate.kind == name
            assert callable(getattr(gate, "detect", None))
            assert callable(getattr(gate, "run", None))

    def test_get_gate_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown quality gate"):
            get_gate("cobol")

    @pytest.mark.parametrize("cls", [PythonQualityGate, NodeQualityGate, GoQualityGate, RustQualityGate])
    def test_each_satisfies_protocol(self, cls):
        gate: QualityGate = cls()  # type-checks at runtime via duck-typing
        assert isinstance(gate.kind, str) and gate.kind


# ── Detection ────────────────────────────────────────────────────────────────


class TestDetection:
    def _touch(self, path: Path, name: str, content: str = "") -> None:
        (path / name).write_text(content or "# placeholder\n")

    def test_python_detects_pyproject(self, tmp_path):
        self._touch(tmp_path, "pyproject.toml")
        assert PythonQualityGate().detect(tmp_path) is True

    def test_python_detects_setup_py(self, tmp_path):
        self._touch(tmp_path, "setup.py")
        assert PythonQualityGate().detect(tmp_path) is True

    def test_python_detects_requirements_txt(self, tmp_path):
        self._touch(tmp_path, "requirements.txt")
        assert PythonQualityGate().detect(tmp_path) is True

    def test_python_no_markers_means_no_detection(self, tmp_path):
        assert PythonQualityGate().detect(tmp_path) is False

    def test_node_detects_package_json(self, tmp_path):
        self._touch(tmp_path, "package.json", "{}")
        assert NodeQualityGate().detect(tmp_path) is True

    def test_node_no_marker_no_detection(self, tmp_path):
        assert NodeQualityGate().detect(tmp_path) is False

    def test_go_detects_go_mod(self, tmp_path):
        self._touch(tmp_path, "go.mod", "module x\n")
        assert GoQualityGate().detect(tmp_path) is True

    def test_rust_detects_cargo_toml(self, tmp_path):
        self._touch(tmp_path, "Cargo.toml", "[package]\n")
        assert RustQualityGate().detect(tmp_path) is True

    def test_detect_gates_composes(self, tmp_path):
        self._touch(tmp_path, "pyproject.toml")
        self._touch(tmp_path, "package.json", "{}")
        detected = detect_gates(tmp_path)
        kinds = {g.kind for g in detected}
        assert kinds == {"python", "node"}

    def test_detect_gates_empty_repo(self, tmp_path):
        assert detect_gates(tmp_path) == []


# ── Python gate run ──────────────────────────────────────────────────────────


class TestPythonGate:
    def test_all_pass(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "whilly.quality.python.run_stage",
            lambda name, cmd, cwd: StageResult(name=name, passed=True),
        )
        result = PythonQualityGate().run(tmp_path)
        assert result.passed is True
        assert len(result.stages) == 3
        assert all(s.passed for s in result.stages)
        assert result.gate_kind == "python"

    def test_pytest_failure_fails_gate(self, tmp_path, monkeypatch):
        def fake(name, cmd, cwd):
            return StageResult(
                name=name, passed=(name != "pytest"), summary="AssertionError: x != y" if name == "pytest" else ""
            )

        monkeypatch.setattr("whilly.quality.python.run_stage", fake)
        result = PythonQualityGate().run(tmp_path)
        assert result.passed is False
        assert "AssertionError" in result.summary

    def test_format_check_failure_fails_gate(self, tmp_path, monkeypatch):
        def fake(name, cmd, cwd):
            return StageResult(name=name, passed=(name != "ruff format --check"))

        monkeypatch.setattr("whilly.quality.python.run_stage", fake)
        result = PythonQualityGate().run(tmp_path)
        assert result.passed is False


# ── Node gate run ────────────────────────────────────────────────────────────


class TestNodeGate:
    def _pkg(self, tmp_path, scripts):
        import json as _json

        (tmp_path / "package.json").write_text(_json.dumps({"scripts": scripts}))

    def test_no_scripts_treats_as_no_op_pass(self, tmp_path):
        self._pkg(tmp_path, {})
        result = NodeQualityGate().run(tmp_path)
        assert result.passed is True
        assert result.stages == []
        assert "no test/lint scripts" in result.summary

    def test_runs_only_defined_scripts(self, tmp_path, monkeypatch):
        self._pkg(tmp_path, {"test": "vitest", "lint": "eslint ."})
        called = []

        def fake(name, cmd, cwd):
            called.append(name)
            return StageResult(name=name, passed=True)

        monkeypatch.setattr("whilly.quality.node.run_stage", fake)
        result = NodeQualityGate().run(tmp_path)
        assert result.passed is True
        assert called == ["npm test", "npm run lint"]
        assert len(result.stages) == 2

    def test_any_failure_fails_gate(self, tmp_path, monkeypatch):
        self._pkg(tmp_path, {"test": "jest", "lint": "eslint ."})

        def fake(name, cmd, cwd):
            return StageResult(
                name=name,
                passed=(name != "npm run lint"),
                summary="eslint: 3 problems" if name == "npm run lint" else "",
            )

        monkeypatch.setattr("whilly.quality.node.run_stage", fake)
        result = NodeQualityGate().run(tmp_path)
        assert result.passed is False
        assert "eslint: 3 problems" in result.summary


# ── Go gate run ──────────────────────────────────────────────────────────────


class TestGoGate:
    def test_gofmt_dirty_files_fails(self, tmp_path, monkeypatch):
        # go test + go vet pass; gofmt lists a dirty file.
        monkeypatch.setattr(
            "whilly.quality.go.run_stage",
            lambda name, cmd, cwd: StageResult(name=name, passed=True),
        )

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = "main.go\n"
        fake_proc.stderr = ""
        with (
            patch("whilly.quality.go.shutil.which", return_value="/usr/bin/gofmt"),
            patch("whilly.quality.go.subprocess.run", return_value=fake_proc),
        ):
            result = GoQualityGate().run(tmp_path)
        assert result.passed is False
        assert "main.go" in result.summary

    def test_all_pass(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "whilly.quality.go.run_stage",
            lambda name, cmd, cwd: StageResult(name=name, passed=True),
        )
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = ""  # no dirty files
        fake_proc.stderr = ""
        with (
            patch("whilly.quality.go.shutil.which", return_value="/usr/bin/gofmt"),
            patch("whilly.quality.go.subprocess.run", return_value=fake_proc),
        ):
            result = GoQualityGate().run(tmp_path)
        assert result.passed is True


# ── Multi / run_all ──────────────────────────────────────────────────────────


class TestMulti:
    def test_empty_gates_returns_pass_with_summary(self, tmp_path):
        result = run_all([], tmp_path)
        assert result.passed is True
        assert result.gate_kind == "multi"
        assert "no language gates detected" in result.summary

    def test_single_pass(self, tmp_path):
        gate = MagicMock()
        gate.kind = "python"
        gate.run.return_value = GateResult(
            gate_kind="python", passed=True, summary="[python] 3/3", stages=[StageResult("pytest", True)]
        )
        result = run_all([gate], tmp_path)
        assert result.passed is True
        assert "[python] 3/3" in result.summary

    def test_any_failure_fails_whole(self, tmp_path):
        ok = MagicMock(kind="python")
        ok.run.return_value = GateResult(gate_kind="python", passed=True, summary="[python] OK")
        bad = MagicMock(kind="node")
        bad.run.return_value = GateResult(gate_kind="node", passed=False, summary="[node] FAIL")
        result = run_all([ok, bad], tmp_path)
        assert result.passed is False
        assert "[python] OK" in result.summary
        assert "[node] FAIL" in result.summary


# ── run_detected (integration) ───────────────────────────────────────────────


class TestRunDetected:
    def test_empty_repo_is_pass(self, tmp_path):
        result = run_detected(tmp_path)
        assert result.passed is True
        assert "no language gates" in result.summary

    def test_python_repo_detected_and_run(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        monkeypatch.setattr(
            "whilly.quality.python.run_stage",
            lambda name, cmd, cwd: StageResult(name=name, passed=True),
        )
        result = run_detected(tmp_path)
        assert result.passed is True
        # Multi wraps — gate_kind is "multi" but summary includes python header.
        assert result.gate_kind == "multi"
        assert "[python]" in result.summary
