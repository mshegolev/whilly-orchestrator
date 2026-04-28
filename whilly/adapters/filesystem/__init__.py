"""Filesystem adapters for Whilly v4.0 (PRD TC-8 / SC-6, FR-2.5).

Owns serialisation and deserialisation of v4 plan JSON files. All file I/O
lives here rather than in :mod:`whilly.core` so the core layer stays pure
(import-linter ``core-purity`` contract). Higher layers — the ``whilly plan
import`` CLI in TASK-010b and the round-trip integration test in TASK-010c —
build on top of :func:`~whilly.adapters.filesystem.plan_io.parse_plan` and
:func:`~whilly.adapters.filesystem.plan_io.serialize_plan`.
"""

from whilly.adapters.filesystem.plan_io import (
    PlanParseError,
    parse_plan,
    serialize_plan,
)

__all__ = ["PlanParseError", "parse_plan", "serialize_plan"]
