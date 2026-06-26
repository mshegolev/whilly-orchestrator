"""Clean fixture module.

The paired spec (``spec.md``) requires ``summarize`` to SHALL return a JSON
object (a ``dict``). This implementation does exactly that, so a competent
reviewer should report no drift (zero HIGH findings) for this control.
"""

from __future__ import annotations


def summarize(field: str, value: str) -> dict:
    """Return a result summary as a JSON object (a dict), matching the spec."""
    return {field: value}
