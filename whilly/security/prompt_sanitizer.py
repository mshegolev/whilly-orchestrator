"""Sanitize untrusted text before it lands in a worker prompt or PR body.

The single public entry point is :func:`sanitize_external_text`. It wraps the
input in ``<UNTRUSTED kind={scope}>...</UNTRUSTED>`` fences, redacts secrets
matching the configured patterns (AWS access keys, GitHub PATs, Slack tokens,
OpenAI ``sk-`` keys), strips C0 control bytes (NUL, BEL, BS, ESC, ...) while
preserving ``\\n`` and ``\\t``, neutralizes embedded ``</UNTRUSTED>`` substrings
so the wrapper close marker is unambiguous, enforces a hard length cap with a
single ``[truncated]`` indicator, and is idempotent.

Pure stdlib. No I/O. Importable without side effects. The module is the
canonical sanitizer used by the worker prompt builder, every external-content
ingestion site, and the PR body renderer.
"""

from __future__ import annotations

import re

__all__ = ["sanitize_external_text"]


_OPEN_FENCE_TEMPLATE = "<UNTRUSTED kind={scope}>"
_CLOSE_FENCE = "</UNTRUSTED>"

_TRUNCATION_MARKER = " [truncated]"

_NEUTRALIZED_CLOSE_FENCE = "<!--UNTRUSTED-CLOSE-->"

_C0_CONTROL_RX = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_SECRET_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:AWS_KEY]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), "[REDACTED:GH_TOKEN]"),
    (re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"), "[REDACTED:SLACK_TOKEN]"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED:OPENAI_KEY]"),
)


def sanitize_external_text(text: str, *, scope: str, max_chars: int = 8000) -> str:
    """Wrap, redact, strip, and length-cap untrusted ``text`` for safe interpolation.

    Args:
        text: Untrusted input — issue body, Jira description, PR review comment,
            PRD content, etc.
        scope: Short identifier (``issue_body``, ``jira_description``,
            ``pr_review_comment``, ...) interpolated into the open fence.
        max_chars: Maximum allowed length of the payload (excluding fence
            markers). Default 8000.

    Returns:
        ``"<UNTRUSTED kind={scope}>...payload...</UNTRUSTED>"`` with all
        invariants documented in the module docstring enforced.
    """
    open_fence = _OPEN_FENCE_TEMPLATE.format(scope=scope)

    if _is_already_sanitized(text, open_fence, max_chars):
        return text

    payload = text

    if not payload.strip():
        return open_fence + _CLOSE_FENCE

    payload = _C0_CONTROL_RX.sub("", payload)

    for pattern, replacement in _SECRET_REPLACEMENTS:
        payload = pattern.sub(replacement, payload)

    if _CLOSE_FENCE in payload:
        payload = payload.replace(_CLOSE_FENCE, _NEUTRALIZED_CLOSE_FENCE)

    if len(payload) > max_chars:
        cap = max_chars - len(_TRUNCATION_MARKER)
        if cap < 0:
            cap = 0
        payload = payload[:cap] + _TRUNCATION_MARKER

    return open_fence + payload + _CLOSE_FENCE


def _is_already_sanitized(text: str, open_fence: str, max_chars: int) -> bool:
    """True when ``text`` is byte-identical to a previous ``sanitize_external_text`` output.

    The fast path keeps :func:`sanitize_external_text` strictly idempotent
    without re-walking the payload: if ``text`` already opens with the expected
    fence, closes with exactly one ``</UNTRUSTED>``, and the inner payload is
    free of all the bytes / patterns the function would otherwise rewrite, we
    can return it verbatim.
    """
    if not text.startswith(open_fence) or not text.endswith(_CLOSE_FENCE):
        return False
    if text.count(_CLOSE_FENCE) != 1:
        return False

    payload = text[len(open_fence) : -len(_CLOSE_FENCE)]
    if len(payload) > max_chars:
        return False
    if _CLOSE_FENCE in payload:
        return False
    if _C0_CONTROL_RX.search(payload):
        return False
    for pattern, _ in _SECRET_REPLACEMENTS:
        if pattern.search(payload):
            return False
    return True
