"""Pure value objects for fail-closed swarm execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

__all__ = ["ExecutionBlocked", "ExecutionPolicy", "SandboxResult", "SWARM_PHASES"]

SWARM_PHASES = (
    "discussion",
    "planner",
    "escalation",
    "worker",
    "review",
    "verify",
    "git",
    "host_script",
    "publication",
)
MAX_OUTPUT_BYTES = 1024 * 1024


class ExecutionBlocked(RuntimeError):
    """Named blocker returned when guarded execution cannot be provided."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SandboxResult:
    """Pure result value returned by an execution adapter."""

    exit_code: int | None
    reason: str | None
    timed_out: bool
    cancelled: bool
    duration_seconds: float
    stdout_path: str
    stderr_path: str
    backend: str


@dataclass(frozen=True)
class ExecutionPolicy:
    """Immutable, host-approved capabilities for one child-process attempt."""

    phase: str
    read_roots: tuple[str, ...]
    write_roots: tuple[str, ...]
    denied_roots: tuple[str, ...]
    network: bool
    timeout_seconds: int
    max_output_bytes: int
    protected_write_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.phase, str) or self.phase not in SWARM_PHASES:
            raise ValueError(f"unknown execution phase: {self.phase!r}")
        if type(self.network) is not bool:
            raise ValueError("network must be a strict bool")
        if type(self.timeout_seconds) is not int or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if type(self.max_output_bytes) is not int or not 0 < self.max_output_bytes <= MAX_OUTPUT_BYTES:
            raise ValueError(f"max_output_bytes must be between 1 and {MAX_OUTPUT_BYTES}")
        if self.phase in {"verify", "git", "host_script"} and self.network:
            raise ValueError(f"network must be false for offline phase {self.phase!r}")
        for label, roots in (
            ("read_roots", self.read_roots),
            ("write_roots", self.write_roots),
            ("denied_roots", self.denied_roots),
            ("protected_write_roots", self.protected_write_roots),
        ):
            if not isinstance(roots, tuple):
                raise ValueError(f"{label} must be a tuple")
            for root in roots:
                if not isinstance(root, str) or not root or "\x00" in root:
                    raise ValueError(f"{label} contains an invalid root")
                if not root.startswith("/"):
                    raise ValueError(f"{label} roots must be absolute")
                if any(part == ".." for part in root.split("/")):
                    raise ValueError(f"{label} roots must not contain parent traversal")
        for root in self.write_roots:
            parts = tuple(part for part in root.split("/") if part)
            if len(parts) <= 2:
                raise ValueError(f"write root is too broad: {root!r}")

    def digest(self) -> str:
        """Return a stable fingerprint of every capability, including denials."""
        payload = {
            "phase": self.phase,
            "read_roots": self.read_roots,
            "write_roots": self.write_roots,
            "denied_roots": self.denied_roots,
            "network": self.network,
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "protected_write_roots": self.protected_write_roots,
        }
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
