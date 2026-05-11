"""Security primitives for Whilly v4 (M1).

This namespace hosts pure, stdlib-only helpers that defuse untrusted text
before it reaches an LLM prompt or a PR body. Modules under this package
must remain side-effect-free at import time and must not introduce any
non-stdlib runtime dependency — ``lint-imports`` enforces the layering.
"""

from __future__ import annotations
