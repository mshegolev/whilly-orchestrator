"""Shared secret linting and redaction helpers.

Pure stdlib. No I/O. Importable without side effects.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Final

SECRET_LINT_BLOCKED_EVENT_TYPE: Final[str] = "secret_lint_blocked"
SECRET_LINT_FAIL_REASON: Final[str] = "secret_lint_blocked"
SECRET_KEY_NAME_FRAGMENTS: tuple[str, ...] = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "DATABASE_URL", "DSN")

_SECRET_REFERENCE_PREFIXES: Final[tuple[str, ...]] = ("env:", "keyring:", "file:")
_EXCERPT_RADIUS: Final[int] = 80


@dataclass(frozen=True)
class SecretPattern:
    pattern_id: str
    regex: re.Pattern[str]
    replacement: str


@dataclass(frozen=True)
class SecretFinding:
    pattern_id: str
    field_path: str
    redacted_excerpt: str

    def event_payload(self, *, task_id: str, plan_id: str) -> dict[str, str]:
        return {
            "event_type": SECRET_LINT_BLOCKED_EVENT_TYPE,
            "pattern_id": self.pattern_id,
            "field_path": self.field_path,
            "task_id": task_id,
            "plan_id": plan_id,
            "redacted_excerpt": self.redacted_excerpt,
        }


SECRET_PATTERNS: Final[tuple[SecretPattern, ...]] = (
    SecretPattern(
        pattern_id="aws-access-key-id",
        regex=re.compile(r"AKIA[0-9A-Z]{16}"),
        replacement="[REDACTED:aws-access-key-id]",
    ),
    SecretPattern(
        pattern_id="github-token",
        regex=re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
        replacement="[REDACTED:github-token]",
    ),
    SecretPattern(
        pattern_id="slack-token",
        regex=re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"),
        replacement="[REDACTED:slack-token]",
    ),
    SecretPattern(
        pattern_id="anthropic-api-key",
        regex=re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
        replacement="[REDACTED:anthropic-api-key]",
    ),
    SecretPattern(
        pattern_id="groq-api-key",
        regex=re.compile(r"gsk_[A-Za-z0-9_-]{20,}"),
        replacement="[REDACTED:groq-api-key]",
    ),
    SecretPattern(
        pattern_id="openai-api-key",
        regex=re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
        replacement="[REDACTED:openai-api-key]",
    ),
    SecretPattern(
        pattern_id="private-key",
        regex=re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
        replacement="[REDACTED:private-key]",
    ),
    SecretPattern(
        pattern_id="auth-header",
        regex=re.compile(
            r"\b(?:proxy-authorization|authorization)\b\s*[:=]\s*(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}",
            re.IGNORECASE,
        ),
        replacement="[REDACTED:auth-header]",
    ),
    SecretPattern(
        pattern_id="database-url",
        regex=re.compile(
            r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^:\s/@]+:[^@\s]+@[^\s]+",
            re.IGNORECASE,
        ),
        replacement="[REDACTED:database-url]",
    ),
)


def redact_secrets(text: str) -> str:
    redacted = text
    for secret_pattern in SECRET_PATTERNS:
        redacted = secret_pattern.regex.sub(secret_pattern.replacement, redacted)
    return redacted


def scan_text(text: str, *, field_path: str) -> SecretFinding | None:
    for secret_pattern in SECRET_PATTERNS:
        match = secret_pattern.regex.search(text)
        if match is None:
            continue
        return SecretFinding(
            pattern_id=secret_pattern.pattern_id,
            field_path=field_path,
            redacted_excerpt=_redacted_excerpt(text, match.start(), match.end()),
        )
    return None


def scan_mapping(mapping: Mapping[str, object], *, field_path_prefix: str = "config") -> SecretFinding | None:
    for field_path, value in _iter_values(mapping, field_path_prefix):
        if not isinstance(value, str):
            continue

        text_finding = scan_text(value, field_path=field_path)
        if text_finding is not None:
            return text_finding

        if _is_sensitive_config_path(field_path) and _is_plaintext_config_value(value):
            return SecretFinding(
                pattern_id="sensitive-config-key",
                field_path=field_path,
                redacted_excerpt="[REDACTED:sensitive-config-key]",
            )
    return None


def first_secret_finding(surfaces: Mapping[str, object]) -> SecretFinding | None:
    for field_path, value in _iter_values(surfaces, ""):
        if not isinstance(value, str):
            continue
        finding = scan_text(value, field_path=field_path)
        if finding is not None:
            return finding
    return None


def contains_secret(text: str) -> bool:
    return scan_text(text, field_path="text") is not None


def _redacted_excerpt(text: str, start: int, end: int) -> str:
    excerpt_start = max(0, start - _EXCERPT_RADIUS)
    excerpt_end = min(len(text), end + _EXCERPT_RADIUS)
    excerpt = redact_secrets(text[excerpt_start:excerpt_end])
    if excerpt_start > 0:
        excerpt = "..." + excerpt
    if excerpt_end < len(text):
        excerpt += "..."
    return excerpt


def _iter_values(value: object, field_path: str) -> list[tuple[str, object]]:
    values: list[tuple[str, object]] = []
    if isinstance(value, Mapping):
        for raw_key, nested_value in value.items():
            key = str(raw_key)
            child_path = f"{field_path}.{key}" if field_path else key
            values.extend(_iter_values(nested_value, child_path))
    elif isinstance(value, (list, tuple)):
        for index, nested_value in enumerate(value):
            child_path = f"{field_path}[{index}]" if field_path else f"[{index}]"
            values.extend(_iter_values(nested_value, child_path))
    else:
        values.append((field_path, value))
    return values


def _is_sensitive_config_path(field_path: str) -> bool:
    upper_path = field_path.upper()
    return any(fragment in upper_path for fragment in SECRET_KEY_NAME_FRAGMENTS)


def _is_plaintext_config_value(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    return not stripped.startswith(_SECRET_REFERENCE_PREFIXES)
