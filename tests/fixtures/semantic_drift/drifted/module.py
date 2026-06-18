"""Drifted fixture module.

The paired spec (``spec.md``) requires ``summarize`` to SHALL return a JSON
object (a ``dict``). This implementation instead returns a bare string, which
plainly contradicts that normative requirement. The violating ``return`` is on
its own line below so ``file:line`` evidence is unambiguous.
"""

from __future__ import annotations


def summarize(field: str, value: str) -> str:
    """Return a result summary.

    Spec says this SHALL return a JSON object; this returns a bare string.
    """
    return f"{field}={value}"  # VIOLATION: returns a bare string, not a JSON object
