"""Provider-neutral CI polling contracts."""

from __future__ import annotations

from dataclasses import dataclass

CI_VERIFICATION_SOURCE = "ci"
CI_PROVIDER_GITHUB = "github"


@dataclass(frozen=True, slots=True)
class CIPollSpec:
    """One configured CI polling request."""

    name: str
    provider: str
    target: str
    required: bool = True
    timeout_s: float = 60.0
    poll_interval_s: float = 0.0
    max_attempts: int = 1


@dataclass(frozen=True, slots=True)
class CICheckSummary:
    """Bounded check-level evidence from a CI provider."""

    name: str
    state: str
    conclusion: str
    details_url: str | None = None


@dataclass(frozen=True, slots=True)
class CIPollResult:
    """Result evidence from one bounded CI poll."""

    name: str
    provider: str
    target: str
    state: str
    conclusion: str
    required: bool
    attempts: int = 1
    max_attempts: int = 1
    timeout_s: float = 60.0
    duration_s: float = 0.0
    details_url: str | None = None
    checks: tuple[CICheckSummary, ...] = ()
    timed_out: bool = False
    unavailable: bool = False
    unauthenticated: bool = False
    reason: str = ""

    @property
    def succeeded(self) -> bool:
        return (
            self.state == "completed"
            and self.conclusion == "success"
            and not self.timed_out
            and not self.unavailable
            and not self.unauthenticated
        )

    @property
    def blocking(self) -> bool:
        return self.required and not self.succeeded


@dataclass(frozen=True, slots=True)
class CIPollEvidence:
    """Pair the original CI poll budget with provider result evidence."""

    spec: CIPollSpec
    result: CIPollResult


__all__ = [
    "CI_PROVIDER_GITHUB",
    "CI_VERIFICATION_SOURCE",
    "CICheckSummary",
    "CIPollEvidence",
    "CIPollResult",
    "CIPollSpec",
]
