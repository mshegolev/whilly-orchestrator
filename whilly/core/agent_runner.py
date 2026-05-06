"""Pure shell-command guard used before agent subprocess execution."""

from __future__ import annotations

import base64
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from whilly.core.models import Task

SHELL_COMMAND_BLOCKED_EVENT_TYPE: Final[str] = "shell_command_blocked"
SHELL_COMMAND_WARN_EVENT_TYPE: Final[str] = "shell_command_warn"
SHELL_COMMAND_FAIL_REASON: Final[str] = "shell_command_blocked"
SHELL_DENY_ENV: Final[str] = "WHILLY_SHELL_DENY_PATTERNS"

_MAX_EXCERPT: Final[int] = 120

_BASELINE_DENY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("rm-rf-root", r"\brm\s+-[A-Za-z]*r[A-Za-z]*f[A-Za-z]*\s+/(?:\s|$|\*)"),
    ("git-force-push", r"\bgit\s+push\b(?:\s+\S+)*\s+(?:--force|-f|\+\S+)"),
    ("dd-raw-disk", r"\bdd\b(?=[^\n]*\bof=/dev/(?:sd|nvme|disk|xvd)\S*)"),
    ("fork-bomb", r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
    ("curl-pipe-shell", r"\b(?:curl|wget)\b[^|\n]*\|\s*(?:sh|bash)\b"),
)

_WARN_PATTERNS: tuple[tuple[str, str], ...] = (
    ("encoded-pipe-shell", r"\bbase64\s+(?:-d|--decode)\b[^|\n]*\|\s*(?:sh|bash)\b"),
)


@dataclass(frozen=True)
class ShellScanResult:
    blocked: bool
    warning: bool = False
    pattern_matched: str | None = None
    redacted_command_excerpt: str = ""

    @property
    def event_type(self) -> str | None:
        if self.blocked:
            return SHELL_COMMAND_BLOCKED_EVENT_TYPE
        if self.warning:
            return SHELL_COMMAND_WARN_EVENT_TYPE
        return None

    def event_payload(self, *, task_id: str, plan_id: str) -> dict[str, str]:
        return {
            "event_type": self.event_type or "",
            "pattern_matched": self.pattern_matched or "",
            "task_id": task_id,
            "plan_id": plan_id,
            "redacted_command_excerpt": self.redacted_command_excerpt,
        }


def normalize_shell_command(command: str) -> str:
    """Normalize cheap shell-obfuscation forms before deny-list matching."""

    text = unicodedata.normalize("NFKC", command)
    text = re.sub(r"\$\(\s*echo\s+(['\"])?\s+\1?\s*\)", " ", text)
    text = text.replace("\\\n", " ")
    text = text.replace("\\ ", " ")
    text = text.replace("'", "").replace('"', "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return text.strip()


def _custom_patterns() -> tuple[tuple[str, str], ...]:
    raw = os.environ.get(SHELL_DENY_ENV, "")
    patterns: list[tuple[str, str]] = []
    for idx, fragment in enumerate(raw.split(","), start=1):
        fragment = fragment.strip()
        if fragment:
            patterns.append((f"custom-{idx}", fragment))
    return tuple(patterns)


def _compiled(patterns: tuple[tuple[str, str], ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple((name, re.compile(pattern, re.IGNORECASE | re.MULTILINE)) for name, pattern in patterns)


def _redacted_excerpt(command: str, match: re.Match[str] | None = None) -> str:
    if match is None:
        start = 0
        end = min(len(command), _MAX_EXCERPT)
    else:
        start = max(0, match.start() - 32)
        end = min(len(command), match.end() + 32)
    excerpt = command[start:end]
    excerpt = re.sub(r"([A-Z0-9_]*(?:TOKEN|SECRET|KEY)[A-Z0-9_]*=)\S+", r"\1***REDACTED***", excerpt)
    excerpt = re.sub(r"(?<!\S)/(?:Users|home|opt|var|tmp)/\S+", "/<path>", excerpt)
    excerpt = " ".join(excerpt.split())
    if len(excerpt) <= _MAX_EXCERPT:
        return excerpt
    return excerpt[: _MAX_EXCERPT - 3].rstrip() + "..."


def _decoded_base64_payloads(command: str) -> tuple[str, ...]:
    payloads: list[str] = []
    for match in re.finditer(
        r"\b(?:echo|printf)\s+([A-Za-z0-9+/=_-]{12,})(?=\s|$)[^\n|]*\|\s*base64\s+(?:-d|--decode)",
        command,
    ):
        raw = match.group(1)
        try:
            decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=False)
        except Exception:  # noqa: BLE001 - malformed payload is just not decoded.
            continue
        text = decoded.decode("utf-8", errors="replace").strip()
        if text:
            payloads.append(text)
    return tuple(payloads)


def scan_command(command: str) -> ShellScanResult:
    """Scan one shell command string for v6 A2 deny-list patterns."""

    normalized = normalize_shell_command(command)
    for decoded in _decoded_base64_payloads(normalized):
        decoded_result = scan_command(decoded)
        if decoded_result.blocked:
            return ShellScanResult(
                blocked=True,
                pattern_matched=f"base64:{decoded_result.pattern_matched}",
                redacted_command_excerpt=_redacted_excerpt(normalized),
            )

    for pattern_name, pattern in _compiled((*_BASELINE_DENY_PATTERNS, *_custom_patterns())):
        match = pattern.search(normalized)
        if match is not None:
            return ShellScanResult(
                blocked=True,
                pattern_matched=pattern_name,
                redacted_command_excerpt=_redacted_excerpt(normalized, match),
            )

    for pattern_name, pattern in _compiled(_WARN_PATTERNS):
        match = pattern.search(normalized)
        if match is not None:
            return ShellScanResult(
                blocked=False,
                warning=True,
                pattern_matched=pattern_name,
                redacted_command_excerpt=_redacted_excerpt(normalized, match),
            )

    return ShellScanResult(blocked=False)


def scan_task_command_surface(task: Task) -> ShellScanResult:
    """Scan task-authored text fields before handing them to an agent runner."""

    surface = "\n".join((task.description, *task.acceptance_criteria, *task.test_steps))
    return scan_command(surface)


__all__ = [
    "SHELL_COMMAND_BLOCKED_EVENT_TYPE",
    "SHELL_COMMAND_FAIL_REASON",
    "SHELL_COMMAND_WARN_EVENT_TYPE",
    "SHELL_DENY_ENV",
    "ShellScanResult",
    "normalize_shell_command",
    "scan_command",
    "scan_task_command_surface",
]
