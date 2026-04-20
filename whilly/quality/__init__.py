"""Language-agnostic quality gate — Protocol-driven, auto-detected per repo.

Ships today with four concrete gates (Python, Node, Go, Rust); more drop
in as sibling modules without touching callers. Callers use the two
high-level helpers:

    from whilly.quality import detect_gates, run_detected

    # Direct run — detects, composes, executes.
    result = run_detected(Path.cwd())
    if not result.passed:
        print(result.summary)

    # Introspection (unit tests, dashboards, PR-body renderers).
    gates = detect_gates(Path.cwd())

Extending to a new language:

    # whilly/quality/kotlin.py
    class KotlinQualityGate:
        kind = "kotlin"
        def detect(self, cwd):
            return (cwd / "build.gradle.kts").exists()
        def run(self, cwd):
            ...

    # __init__.py
    from whilly.quality.kotlin import KotlinQualityGate
    _REGISTRY["kotlin"] = KotlinQualityGate

Design rationale and alternatives considered in ADR-016.
"""

from __future__ import annotations

from pathlib import Path

from whilly.quality.base import GateResult, QualityGate, StageResult
from whilly.quality.go import GoQualityGate
from whilly.quality.multi import run_all
from whilly.quality.node import NodeQualityGate
from whilly.quality.python import PythonQualityGate
from whilly.quality.rust import RustQualityGate

__all__ = [
    "QualityGate",
    "GateResult",
    "StageResult",
    "PythonQualityGate",
    "NodeQualityGate",
    "GoQualityGate",
    "RustQualityGate",
    "available_gates",
    "get_gate",
    "detect_gates",
    "run_detected",
]


_REGISTRY: dict[str, type[QualityGate]] = {
    "python": PythonQualityGate,
    "node": NodeQualityGate,
    "go": GoQualityGate,
    "rust": RustQualityGate,
}


def available_gates() -> list[str]:
    """Every gate kind registered in this build."""
    return sorted(_REGISTRY.keys())


def get_gate(name: str) -> QualityGate:
    """Resolve a gate by name.

    Raises:
        ValueError: when *name* is not registered.
    """
    key = (name or "").strip().lower()
    if key not in _REGISTRY:
        raise ValueError(f"Unknown quality gate {name!r}. Available: {', '.join(available_gates())}")
    return _REGISTRY[key]()


def detect_gates(cwd: Path | str | None = None) -> list[QualityGate]:
    """Return gate instances whose :meth:`detect` is True for *cwd*.

    Order follows :func:`available_gates` — deterministic for tests.
    """
    base = Path(cwd) if cwd is not None else Path.cwd()
    detected: list[QualityGate] = []
    for kind in available_gates():
        gate = _REGISTRY[kind]()
        if gate.detect(base):
            detected.append(gate)
    return detected


def run_detected(cwd: Path | str | None = None) -> GateResult:
    """Detect + run every applicable gate, returning one aggregate result.

    The one-liner the pipeline calls. When nothing applies, returns
    ``passed=True`` with an informational summary — "no gates" is not
    treated as an error (polyglot repos, docs-only changes, etc.).
    """
    base = Path(cwd) if cwd is not None else Path.cwd()
    return run_all(detect_gates(base), base)
