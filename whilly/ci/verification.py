"""CI polling integration with the verification runner contract."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from whilly.ci.models import CI_VERIFICATION_SOURCE, CIPollEvidence, CIPollResult, CIPollSpec

if TYPE_CHECKING:
    from whilly.pipeline.verification import VerificationCommandResult, VerificationCommandSpec


class CIPollRunner(Protocol):
    """Callable surface for provider-specific CI poll adapters."""

    async def __call__(self, spec: CIPollSpec) -> CIPollResult: ...


def ci_spec_from_verification_spec(spec: "VerificationCommandSpec") -> CIPollSpec:
    """Convert a verification command item into a CI poll spec."""

    target = spec.command
    return CIPollSpec(
        name=spec.name,
        provider=_provider_from_target(target),
        target=target,
        required=spec.required,
    )


def ci_result_to_verification_result(result: CIPollResult) -> "VerificationCommandResult":
    """Map CI poll evidence into existing verification result semantics."""

    from whilly.pipeline.verification import (  # noqa: PLC0415
        VERIFICATION_FAILED_EVENT,
        VERIFICATION_SUCCEEDED_EVENT,
        VERIFICATION_WARNING_EVENT,
        VerificationCommandResult,
    )

    warning = not result.required and not result.succeeded
    event_name = (
        VERIFICATION_SUCCEEDED_EVENT
        if result.succeeded
        else VERIFICATION_WARNING_EVENT
        if warning
        else VERIFICATION_FAILED_EVENT
    )
    return VerificationCommandResult(
        name=result.name,
        command=result.target,
        required=result.required,
        succeeded=result.succeeded,
        warning=warning,
        event_name=event_name,
        returncode=None,
        stdout="",
        stderr=result.reason,
        duration_s=result.duration_s,
        source=CI_VERIFICATION_SOURCE,
        timed_out=result.timed_out,
    )


async def run_ci_verification(
    spec: "VerificationCommandSpec",
    *,
    runner: CIPollRunner | None,
) -> tuple[CIPollEvidence, "VerificationCommandResult"]:
    """Run or synthesize one CI verification result without shell execution."""

    ci_spec = ci_spec_from_verification_spec(spec)
    ci_result = await runner(ci_spec) if runner is not None else _runner_not_configured_result(ci_spec)
    evidence = CIPollEvidence(spec=ci_spec, result=ci_result)
    return evidence, ci_result_to_verification_result(ci_result)


def _provider_from_target(target: str) -> str:
    if not target.startswith("ci://"):
        return "unknown"
    rest = target.removeprefix("ci://")
    provider, _sep, _tail = rest.partition("/")
    return provider or "unknown"


def _runner_not_configured_result(spec: CIPollSpec) -> CIPollResult:
    return CIPollResult(
        name=spec.name,
        provider=spec.provider,
        target=spec.target,
        state="unavailable",
        conclusion="unavailable",
        required=spec.required,
        attempts=0,
        max_attempts=spec.max_attempts,
        timeout_s=spec.timeout_s,
        unavailable=True,
        reason="ci_poll_runner_not_configured",
    )


__all__ = [
    "CIPollRunner",
    "ci_result_to_verification_result",
    "ci_spec_from_verification_spec",
    "run_ci_verification",
]
