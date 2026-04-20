"""QualityGate Protocol + shared dataclasses.

A *quality gate* is whatever check the project's language/toolchain runs
before a change is considered mergeable — tests, linters, formatters, type
checkers. Whilly self-hosting pipelines call the gate right before opening
a PR; a failure flips the issue card to ``failed`` and leaves the PR for
human triage.

Design mirrors :class:`whilly.agents.base.AgentBackend` and
:class:`whilly.workflow.base.BoardSink`: narrow Protocol surface
(:meth:`detect`, :meth:`run`), small value-only dataclasses, no exceptions
on expected failures (returned in :class:`StageResult`).

Extending to a new language:

1. Implement the Protocol — ``kind`` attribute, ``detect()`` reading
   marker files from *cwd*, ``run()`` returning :class:`GateResult`.
2. Register under a short name in ``whilly/quality/__init__.py``
   ``_REGISTRY`` (``"python"``, ``"node"``, …).
3. Three tests: Protocol conformance, detection positive/negative, one
   run path with a mocked subprocess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class StageResult:
    """Outcome of a single stage inside a gate (one test run, one lint pass).

    Named *stage* (not *check* or *step*) so event vocabulary stays
    consistent with the pipeline JSONL — which uses "stage" for the
    multi-stage pipeline too.
    """

    name: str  # human label — "pytest", "ruff check", "go vet", …
    passed: bool
    summary: str = ""  # truncated stdout/stderr for reviewers to read
    duration_s: float = 0.0


@dataclass
class GateResult:
    """Outcome of a full quality-gate run for one language toolchain.

    ``passed`` is True iff every stage passed. ``summary`` is a
    human-readable string (suitable for PR body + dashboards) assembled
    from the stages — impls build this once, callers never re-render.
    """

    gate_kind: str
    passed: bool
    summary: str = ""
    stages: list[StageResult] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.passed


class QualityGate(Protocol):
    """Stable contract for language-specific quality gates.

    Adapters:

    * set ``kind`` — short registry key (``"python"``, ``"node"``, ``"go"``, …).
    * implement :meth:`detect` — return True when the gate is applicable
      to the project rooted at *cwd* (presence of ``pyproject.toml``,
      ``package.json``, ``go.mod``, …).
    * implement :meth:`run` — execute the stages and return a
      :class:`GateResult`. Never raises on test/lint failure — those are
      communicated through ``passed=False`` on the returned value.

    Transport errors (binary missing, timeout) are returned as a failed
    :class:`StageResult` with a human summary — we don't raise because
    the pipeline's caller is already doing partial-success accounting
    and would have to wrap everything in try/except otherwise.
    """

    kind: str

    def detect(self, cwd: Path) -> bool:
        """Return True when this gate applies to the project at *cwd*."""
        ...

    def run(self, cwd: Path) -> GateResult:
        """Run every applicable stage for this language, return a
        consolidated :class:`GateResult`. Never raises."""
        ...
