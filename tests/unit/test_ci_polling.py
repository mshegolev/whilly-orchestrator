"""Unit tests for CI polling primitives and verification mapping."""

from __future__ import annotations

import pytest

from whilly.ci import CI_PROVIDER_GITHUB
from whilly.ci.events import make_ci_poll_result_event, make_ci_poll_started_event
from whilly.ci.github import GitHubCIPollAdapter
from whilly.ci.models import CICheckSummary, CIPollEvidence, CIPollResult, CIPollSpec
from whilly.ci.verification import ci_result_to_verification_result
from whilly.pipeline.verification import VERIFICATION_FAILED_EVENT, VERIFICATION_WARNING_EVENT


def test_ci_poll_started_event_payload_has_target_provider_and_budget() -> None:
    spec = CIPollSpec(
        name="github-ci",
        provider=CI_PROVIDER_GITHUB,
        target="ci://github/acme/widgets#pr-42",
        required=True,
        timeout_s=45.0,
        poll_interval_s=3.0,
        max_attempts=5,
    )

    event = make_ci_poll_started_event("T-1", spec, plan_id="P-1")

    assert event.event_type == "ci.poll.started"
    assert event.task_id == "T-1"
    assert event.payload == {
        "task_id": "T-1",
        "plan_id": "P-1",
        "name": "github-ci",
        "provider": "github",
        "target": "ci://github/acme/widgets#pr-42",
        "required": True,
        "timeout_s": 45.0,
        "poll_interval_s": 3.0,
        "max_attempts": 5,
    }


def test_ci_poll_evidence_preserves_original_spec_and_result() -> None:
    spec = CIPollSpec(
        name="github-ci",
        provider=CI_PROVIDER_GITHUB,
        target="ci://github/acme/widgets#pr-42",
    )
    result = CIPollResult(
        name="github-ci",
        provider=CI_PROVIDER_GITHUB,
        target="ci://github/acme/widgets#pr-42",
        state="completed",
        conclusion="success",
        required=True,
    )

    evidence = CIPollEvidence(spec=spec, result=result)

    assert evidence.spec is spec
    assert evidence.result is result


def test_required_unavailable_ci_is_not_success() -> None:
    result = CIPollResult(
        name="github-ci",
        provider=CI_PROVIDER_GITHUB,
        target="ci://github/acme/widgets#pr-42",
        state="unavailable",
        conclusion="unavailable",
        required=True,
        unavailable=True,
        reason="provider unavailable",
    )

    event = make_ci_poll_result_event("T-1", result, plan_id="P-1")

    assert result.succeeded is False
    assert result.blocking is True
    assert event.event_type == "ci.poll.result"
    assert event.payload["succeeded"] is False
    assert event.payload["blocking"] is True
    assert event.payload["unavailable"] is True


def test_optional_failed_ci_is_nonblocking() -> None:
    result = CIPollResult(
        name="github-ci",
        provider=CI_PROVIDER_GITHUB,
        target="ci://github/acme/widgets#pr-42",
        state="completed",
        conclusion="failure",
        required=False,
        checks=(CICheckSummary(name="unit", state="completed", conclusion="failure", details_url="https://ci.test"),),
    )

    event = make_ci_poll_result_event("T-1", result)

    assert result.succeeded is False
    assert result.blocking is False
    assert event.payload["blocking"] is False
    assert event.detail == {
        "checks": [
            {
                "name": "unit",
                "state": "completed",
                "conclusion": "failure",
                "details_url": "https://ci.test",
            }
        ]
    }


@pytest.mark.asyncio
async def test_github_adapter_reports_unauthenticated_without_success(monkeypatch) -> None:
    async def fake_probe(self: GitHubCIPollAdapter, spec: CIPollSpec, owner: str, repo: str, pr_number: int):
        return 1, "", "not authenticated; run gh auth login"

    monkeypatch.setattr(GitHubCIPollAdapter, "_probe", fake_probe)
    adapter = GitHubCIPollAdapter(gh_bin="gh", timeout_s=1.0)

    result = await adapter(
        CIPollSpec(
            name="github-ci",
            provider=CI_PROVIDER_GITHUB,
            target="ci://github/acme/widgets#pr-42",
            required=True,
        )
    )

    assert result.unauthenticated is True
    assert result.unavailable is False
    assert result.succeeded is False
    assert result.blocking is True
    assert result.reason == "github_authentication_required"


def test_ci_result_maps_to_required_verification_failure() -> None:
    ci_result = CIPollResult(
        name="github-ci",
        provider=CI_PROVIDER_GITHUB,
        target="ci://github/acme/widgets#pr-42",
        state="unavailable",
        conclusion="unavailable",
        required=True,
        unavailable=True,
        reason="ci provider unavailable",
    )

    result = ci_result_to_verification_result(ci_result)

    assert result.event_name == VERIFICATION_FAILED_EVENT
    assert result.source == "ci"
    assert result.command == "ci://github/acme/widgets#pr-42"
    assert result.required is True
    assert result.succeeded is False
    assert result.warning is False
    assert result.stderr == "ci provider unavailable"


def test_ci_result_maps_optional_failure_to_warning() -> None:
    ci_result = CIPollResult(
        name="github-ci",
        provider=CI_PROVIDER_GITHUB,
        target="ci://github/acme/widgets#pr-42",
        state="completed",
        conclusion="failure",
        required=False,
        reason="ci failed",
    )

    result = ci_result_to_verification_result(ci_result)

    assert result.event_name == VERIFICATION_WARNING_EVENT
    assert result.source == "ci"
    assert result.required is False
    assert result.succeeded is False
    assert result.warning is True
