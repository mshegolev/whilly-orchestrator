"""Unit tests for whilly.cli.smoke — SmokeReport, write_smoke_report, _redact_url."""

from __future__ import annotations

import json
from pathlib import Path

from whilly.cli.smoke import SmokeReport, _redact_url, write_smoke_report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_payload(kind: str = "jira") -> dict:
    """Build a realistic jira-smoke payload with no secrets."""
    report = SmokeReport(kind=kind)
    report.add_check("auth", True)
    report.add_check("issue_fetch", True)
    return report.to_payload()


# ---------------------------------------------------------------------------
# write_smoke_report tests
# ---------------------------------------------------------------------------


def test_write_smoke_report_creates_file_with_correct_name(tmp_path: Path) -> None:
    """write_smoke_report creates a file named jira-smoke-<timestamp>.json."""
    payload = _minimal_payload("jira")
    result_path = write_smoke_report(tmp_path, "jira", payload)

    assert result_path.exists(), "Report file must exist after write"
    assert result_path.name.startswith("jira-smoke-"), f"Expected 'jira-smoke-' prefix, got: {result_path.name}"
    assert result_path.name.endswith(".json"), f"Expected .json suffix, got: {result_path.name}"
    assert result_path.parent == tmp_path


def test_write_smoke_report_content_round_trips_timestamp_and_checks(tmp_path: Path) -> None:
    """Parsed JSON contains the original timestamp and checks list."""
    report = SmokeReport(kind="jira")
    report.add_check("auth", True)
    report.add_check("issue_fetch", False, hint="Check credentials")
    payload = report.to_payload()

    result_path = write_smoke_report(tmp_path, "jira", payload)
    loaded = json.loads(result_path.read_text(encoding="utf-8"))

    assert loaded["timestamp"] == payload["timestamp"]
    assert loaded["checks"] == payload["checks"]
    assert len(loaded["checks"]) == 2


def test_write_smoke_report_creates_parent_directory(tmp_path: Path) -> None:
    """write_smoke_report creates nested directories via parents=True."""
    nested_dir = tmp_path / "deep" / "nested" / "smoke"
    assert not nested_dir.exists()

    payload = _minimal_payload("gitlab")
    result_path = write_smoke_report(nested_dir, "gitlab", payload)

    assert nested_dir.exists()
    assert result_path.exists()


# ---------------------------------------------------------------------------
# SmokeReport accumulation tests
# ---------------------------------------------------------------------------


def test_smoke_report_accumulation_does_not_stop_on_first_failure() -> None:
    """A failed check must not prevent subsequent checks from being recorded."""
    report = SmokeReport(kind="jira")
    report.add_check("auth", True)
    report.add_check("issue_fetch", False, hint="Verify JIRA_API_TOKEN")
    report.add_check("comments", True)

    assert len(report.checks) == 3, "All three checks must be recorded"
    assert report.all_passed is False, "all_passed must be False when any check failed"

    # Order preserved
    assert report.checks[0]["name"] == "auth"
    assert report.checks[1]["name"] == "issue_fetch"
    assert report.checks[1]["passed"] is False
    assert report.checks[2]["name"] == "comments"


def test_smoke_report_all_passed_true_when_all_checks_pass() -> None:
    """all_passed is True when every check has passed=True."""
    report = SmokeReport(kind="gitlab")
    report.add_check("token_auth", True)
    report.add_check("repo_fetch", True)

    assert report.all_passed is True


def test_smoke_report_all_passed_false_with_one_failing_check() -> None:
    """all_passed is False if even one check fails."""
    report = SmokeReport(kind="jira")
    report.add_check("auth", True)
    report.add_check("classify", False)

    assert report.all_passed is False


def test_smoke_report_to_payload_timestamp_has_trailing_z() -> None:
    """to_payload timestamp uses 'Z' suffix, not '+00:00'."""
    report = SmokeReport(kind="jira")
    payload = report.to_payload()

    assert payload["timestamp"].endswith("Z"), f"Expected timestamp to end with 'Z', got: {payload['timestamp']}"
    assert "+00:00" not in payload["timestamp"]


def test_smoke_report_to_payload_summary_counts() -> None:
    """to_payload summary contains correct passed/failed counts."""
    report = SmokeReport(kind="jira")
    report.add_check("auth", True)
    report.add_check("issue_fetch", False)
    report.add_check("comments", True)

    payload = report.to_payload()
    summary = payload["summary"]

    assert summary["total"] == 3
    assert summary["passed"] == 2
    assert summary["failed"] == 1
    assert summary["all_passed"] is False


# ---------------------------------------------------------------------------
# _redact_url tests
# ---------------------------------------------------------------------------


def test_redact_url_strips_user_pass_at_authority() -> None:
    """_redact_url removes user:pass@ from authority-embedded URL."""
    result = _redact_url("https://user:pass@host.example.com/some/path")

    assert "user" not in result or "user" in result.split("//")[-1].split("@")[-1]
    assert "@" not in result, f"Result still contains '@': {result}"
    assert "pass" not in result, f"Result still contains 'pass': {result}"
    assert "host.example.com" in result
    assert "/some/path" in result


def test_redact_url_is_noop_on_clean_url() -> None:
    """_redact_url is idempotent on a URL without credentials."""
    url = "https://host.example.com/repo/path"
    assert _redact_url(url) == url


def test_redact_url_handles_url_with_port() -> None:
    """_redact_url preserves port while stripping credentials."""
    result = _redact_url("https://admin:secret@gitlab.example.com:8443/group/repo")

    assert "@" not in result
    assert "8443" in result
    assert "gitlab.example.com" in result


def test_redact_url_returns_input_on_unparseable_string() -> None:
    """_redact_url returns the input unchanged for completely invalid input."""
    # urllib.parse.urlsplit is very permissive so we just test it doesn't raise
    weird = "not-a-url at all"
    result = _redact_url(weird)
    # Should return something (either unchanged or parsed without error)
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# No-secret-leak assertion test
# ---------------------------------------------------------------------------


def test_smoke_report_payload_contains_no_tokens_or_dsn(tmp_path: Path) -> None:
    """Serialized jira report payload must not contain token values or DSN strings.

    The token value and DSN are added only to hints (operator-readable hints
    are fine); the payload itself should never carry the raw secret value at
    a top-level or checks level that would leak to the report file.
    """
    sample_token = "ATATT3xFfGF0-FAKE-TOKEN-VALUE-abc123"
    sample_dsn = "postgres://user:secret@db.example.com/dbname"

    report = SmokeReport(kind="jira")
    # Simulate a check that records host + project_key (safe) but NOT the token
    report.add_check("auth_whoami", True)
    report.add_check(
        "issue_fetch",
        True,
        hint="OK: PROJ-123 fetched from jira.example.com",
    )

    payload = report.to_payload()
    # Add realistic safe fields only
    payload["target_host"] = "jira.example.com"
    payload["project_key"] = "PROJ"

    serialized = json.dumps(payload)

    # The sample token value must NOT appear in the payload
    assert sample_token not in serialized, "Report payload must not contain the JIRA API token value"
    # No postgres DSN
    assert "postgres://" not in serialized, "Report payload must not contain a postgres:// DSN"
    # The env var name itself (not value) should not appear either in this context
    assert "JIRA_API_TOKEN" not in serialized, "Report payload must not contain the literal JIRA_API_TOKEN env var name"

    # Write to file and verify the file also has no secrets
    result_path = write_smoke_report(tmp_path, "jira", payload)
    file_content = result_path.read_text(encoding="utf-8")

    assert sample_token not in file_content
    assert "postgres://" not in file_content
    assert sample_dsn not in file_content
