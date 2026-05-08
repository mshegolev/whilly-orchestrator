"""CI polling primitives."""

from whilly.ci.events import CI_POLL_RESULT_EVENT, CI_POLL_STARTED_EVENT
from whilly.ci.github import GitHubCIPollAdapter
from whilly.ci.models import (
    CI_PROVIDER_GITHUB,
    CI_VERIFICATION_SOURCE,
    CICheckSummary,
    CIPollEvidence,
    CIPollResult,
    CIPollSpec,
)
from whilly.ci.verification import (
    CIPollRunner,
    ci_result_to_verification_result,
    ci_spec_from_verification_spec,
    run_ci_verification,
)

__all__ = [
    "CI_POLL_RESULT_EVENT",
    "CI_POLL_STARTED_EVENT",
    "CI_PROVIDER_GITHUB",
    "CI_VERIFICATION_SOURCE",
    "CICheckSummary",
    "CIPollEvidence",
    "CIPollResult",
    "CIPollRunner",
    "CIPollSpec",
    "GitHubCIPollAdapter",
    "ci_result_to_verification_result",
    "ci_spec_from_verification_spec",
    "run_ci_verification",
]
