"""Shared smoke-report foundation for ``whilly jira smoke`` and ``whilly gitlab smoke``.

Provides:
- SmokeReport accumulator dataclass
- write_smoke_report() — writes a timestamped, secret-free JSON file
- _redact_url() — strips user:pass@ authority from a URL
- Exit code constants EXIT_OK, EXIT_CHECK_FAILED, EXIT_CONFIG_MISSING
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from whilly.llm_ops import DEFAULT_LOG_DIR, LOG_DIR_ENV, _log_dir

# ---------------------------------------------------------------------------
# Exit code constants (shared by jira smoke and gitlab smoke commands)
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_CONFIG_MISSING = 2


# ---------------------------------------------------------------------------
# Report directory helper
# ---------------------------------------------------------------------------


def _smoke_report_dir() -> Path:
    """Return the smoke-report directory, honouring WHILLY_LOG_DIR."""
    return _log_dir() / "smoke"


# ---------------------------------------------------------------------------
# URL redaction
# ---------------------------------------------------------------------------


def _redact_url(url: str) -> str:
    """Return *url* with any ``user:pass@`` authority stripped.

    Only the host (and optional port, path, query, fragment) survive.
    Returns the input unchanged on parse failure rather than raising.

    >>> _redact_url("https://user:pass@host/path")
    'https://host/path'
    >>> _redact_url("https://host/path")
    'https://host/path'
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:  # noqa: BLE001
        return url

    if not parsed.username and not parsed.password:
        # Already clean — idempotent fast-path.
        return url

    # Rebuild netloc as host-only (drop username / password).
    host = parsed.hostname or ""
    port = parsed.port
    netloc = f"{host}:{port}" if port else host

    clean = urllib.parse.SplitResult(
        scheme=parsed.scheme,
        netloc=netloc,
        path=parsed.path,
        query=parsed.query,
        fragment=parsed.fragment,
    )
    return clean.geturl()


# ---------------------------------------------------------------------------
# SmokeReport accumulator
# ---------------------------------------------------------------------------


@dataclass
class SmokeReport:
    """Accumulator for per-check smoke results.

    Checks are recorded via :meth:`add_check`; a ``False`` result never
    raises so subsequent checks still execute (Pitfall 2 — never stop on
    first failure).
    """

    kind: str
    checks: list[dict[str, Any]] = field(default_factory=list)

    def add_check(self, name: str, passed: bool, hint: str = "") -> None:
        """Append a check result.

        :param name: Human-readable check identifier.
        :param passed: ``True`` if the check succeeded.
        :param hint: Optional operator hint for failed checks; may be empty.
        """
        # add_check is for local/instant checks where 0.0 is the real
        # duration; network-bound checks must use add_timed_check with a
        # measured duration (WR-09).
        self.checks.append(
            {
                "name": name,
                "passed": passed,
                "hint": hint,
                "duration_seconds": 0.0,
            }
        )

    def add_timed_check(self, name: str, passed: bool, duration_seconds: float, hint: str = "") -> None:
        """Append a check result with an explicit duration."""
        self.checks.append(
            {
                "name": name,
                "passed": passed,
                "hint": hint,
                "duration_seconds": round(duration_seconds, 3),
            }
        )

    @property
    def all_passed(self) -> bool:
        """``True`` only when every recorded check has ``passed is True``."""
        return all(c["passed"] is True for c in self.checks)

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serialisable report dict.

        The ``timestamp`` key uses UTC ISO-8601 with a trailing ``Z``
        (not ``+00:00``) for operator readability.
        """
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        passed_count = sum(1 for c in self.checks if c["passed"] is True)
        failed_count = len(self.checks) - passed_count
        return {
            "timestamp": ts,
            "kind": self.kind,
            "checks": list(self.checks),
            "summary": {
                "total": len(self.checks),
                "passed": passed_count,
                "failed": failed_count,
                "all_passed": self.all_passed,
            },
        }


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------


def write_smoke_report(
    report_dir: Path,
    kind: str,
    payload: dict[str, Any],
) -> Path:
    """Write *payload* as a timestamped JSON file under *report_dir*.

    Creates *report_dir* (including parents) as needed. Returns the
    path of the written file.

    :param report_dir: Directory to write the report into.
    :param kind: Report kind label (``"jira"`` or ``"gitlab"``).
    :param payload: Report dict produced by :meth:`SmokeReport.to_payload`
        (must contain a ``"timestamp"`` key).
    :returns: Path of the written report file.
    """
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = payload["timestamp"]
    filename = f"{kind}-smoke-{ts}.json"
    path = report_dir / filename
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


__all__ = [
    "DEFAULT_LOG_DIR",
    "LOG_DIR_ENV",
    "EXIT_OK",
    "EXIT_CHECK_FAILED",
    "EXIT_CONFIG_MISSING",
    "SmokeReport",
    "write_smoke_report",
    "_redact_url",
    "_smoke_report_dir",
]
