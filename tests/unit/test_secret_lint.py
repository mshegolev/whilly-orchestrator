"""Unit tests for the shared secret-lint contract."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("pattern_id", "secret"),
    [
        ("aws-access-key-id", "AKIAIOSFODNN7EXAMPLE"),
        ("github-token", "ghp_" + "A" * 36),
        ("slack-token", "xoxb-" + "1234567890"),
        ("openai-api-key", "sk-" + "B" * 24),
        ("anthropic-api-key", "sk-ant-" + "C" * 24),
        ("groq-api-key", "gsk_" + "D" * 24),
        ("private-key", "-----BEGIN PRIVATE KEY-----"),
        ("auth-header", "Authorization: Bearer " + "E" * 24),
        ("database-url", "postgres://user:password@example.test/db"),
    ],
)
def test_scan_text_returns_stable_pattern_ids(pattern_id: str, secret: str) -> None:
    from whilly.security.secret_lint import scan_text

    finding = scan_text("token " + secret, field_path="task.description")

    assert finding is not None
    assert finding.pattern_id == pattern_id
    assert finding.field_path == "task.description"
    assert secret not in finding.redacted_excerpt
    assert "[REDACTED:" in finding.redacted_excerpt


@pytest.mark.parametrize(
    "secret",
    [
        "AKIAIOSFODNN7EXAMPLE",
        "gho_" + "A" * 36,
        "ghu_" + "A" * 36,
        "ghs_" + "A" * 36,
        "ghr_" + "A" * 36,
        "xoxp-" + "1234567890",
        "xoxa-" + "1234567890",
        "xoxs-" + "1234567890",
        "sk-proj-" + "B" * 24,
        "sk-ant-" + "C" * 24,
        "gsk_" + "D" * 24,
        "-----BEGIN RSA PRIVATE KEY-----",
        "Proxy-Authorization: Basic " + "R" * 24,
        "mysql://user:password@example.test/db",
        "mongodb://user:password@example.test/db",
        "mongodb+srv://user:password@example.test/db",
        "redis://user:password@example.test/0",
    ],
)
def test_redact_secrets_removes_raw_values(secret: str) -> None:
    from whilly.security.secret_lint import redact_secrets

    out = redact_secrets("prefix " + secret + " suffix")

    assert secret not in out
    assert "[REDACTED:" in out
    assert "]" in out


def test_scan_text_payload_is_audit_safe() -> None:
    from whilly.security.secret_lint import SECRET_LINT_BLOCKED_EVENT_TYPE, scan_text

    fake_secret = "ghp_" + "F" * 36
    finding = scan_text("token " + fake_secret, field_path="task.description")

    assert finding is not None
    assert finding.field_path == "task.description"
    assert fake_secret not in finding.redacted_excerpt
    assert finding.event_payload(task_id="T-1", plan_id="P-1") == {
        "event_type": SECRET_LINT_BLOCKED_EVENT_TYPE,
        "pattern_id": "github-token",
        "field_path": "task.description",
        "task_id": "T-1",
        "plan_id": "P-1",
        "redacted_excerpt": finding.redacted_excerpt,
    }
    assert fake_secret not in str(finding.event_payload(task_id="T-1", plan_id="P-1"))


@pytest.mark.parametrize(
    "key",
    [
        "TOKEN",
        "client_secret",
        "db_PASSWORD",
        "service_API_KEY",
        "DATABASE_URL",
        "audit_dsn",
    ],
)
def test_scan_mapping_flags_sensitive_config_key_fragments(key: str) -> None:
    from whilly.security.secret_lint import scan_mapping

    finding = scan_mapping({key: "plaintext-value"}, field_path_prefix="config")

    assert finding is not None
    assert finding.pattern_id == "sensitive-config-key"
    assert finding.field_path == f"config.{key}"
    assert "plaintext-value" not in finding.redacted_excerpt


@pytest.mark.parametrize("reference", ["env:GITHUB_TOKEN", "keyring:whilly/github", "file:/tmp/whilly-token"])
def test_scan_mapping_allows_secret_reference_values(reference: str) -> None:
    from whilly.security.secret_lint import scan_mapping

    assert scan_mapping({"token": reference}, field_path_prefix="config") is None


def test_scan_mapping_detects_secret_values_even_for_plain_keys() -> None:
    from whilly.security.secret_lint import scan_mapping

    secret = "gsk_" + "G" * 24
    finding = scan_mapping({"description": "leak " + secret}, field_path_prefix="config")

    assert finding is not None
    assert finding.pattern_id == "groq-api-key"
    assert finding.field_path == "config.description"
    assert secret not in finding.redacted_excerpt


def test_first_secret_finding_scans_structured_surfaces() -> None:
    from whilly.security.secret_lint import first_secret_finding

    secret = "sk-ant-" + "H" * 24
    finding = first_secret_finding(
        {
            "task.description": "plain",
            "task.acceptance_criteria": ["still plain", "leak " + secret],
        }
    )

    assert finding is not None
    assert finding.pattern_id == "anthropic-api-key"
    assert finding.field_path == "task.acceptance_criteria[1]"
    assert secret not in finding.redacted_excerpt


def test_contains_secret_reflects_text_scan() -> None:
    from whilly.security.secret_lint import contains_secret

    assert contains_secret("Authorization: Bearer " + "I" * 24)
    assert not contains_secret("ordinary implementation notes")
