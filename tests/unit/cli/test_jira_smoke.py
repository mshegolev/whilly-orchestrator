"""Unit tests for ``whilly jira smoke`` CLI action (_run_jira_smoke)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from whilly.cli.jira import run_jira_command
from whilly.jira_watch import JiraWorkSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jira_env() -> dict[str, str]:
    return {
        "JIRA_SERVER_URL": "https://company.atlassian.net",
        "JIRA_USERNAME": "dev@example.com",
        "JIRA_API_TOKEN": "jira-token",
    }


def _full_snapshot() -> JiraWorkSnapshot:
    """Build a complete fake JiraWorkSnapshot with all fields populated."""
    return JiraWorkSnapshot(
        issue_key="ABC-123",
        summary="Fix ETL job",
        description="desc",
        comments=({"id": "20001", "body": "first comment"},),
        changelog_ids=("10001", "10002"),
        links=({"url": "https://gitlab.company/platform/etl/-/merge_requests/7"},),
        repo_targets=({"id": "gitlab:platform/etl"},),
        context_hashes={"combined_hash": "hash"},
        classification={"kind": "bug", "urgency": "normal"},
        comment_commands=({"action": "plan", "value": "", "raw": "/whilly plan"},),
        last_seen_comment_id="20001",
    )


# ---------------------------------------------------------------------------
# Test 1: all checks pass with full snapshot and valid Jira env returns 0
# ---------------------------------------------------------------------------


def test_jira_smoke_all_checks_pass_returns_zero_and_writes_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """All six checks pass → exit 0, report file written under WHILLY_LOG_DIR/smoke/."""
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    snapshot = _full_snapshot()
    rc = run_jira_command(
        ["smoke", "--issue", "ABC-123"],
        snapshot_collector=lambda ref, timeout=15: snapshot,
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
        stdin_isatty=lambda: False,
    )

    assert rc == 0, f"Expected exit 0, got {rc}"

    # Report file must exist under tmp_path/smoke/
    smoke_dir = tmp_path / "smoke"
    assert smoke_dir.is_dir(), "smoke/ dir must be created"
    reports = list(smoke_dir.glob("jira-smoke-*.json"))
    assert len(reports) == 1, f"Expected 1 report file, found {len(reports)}"

    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["summary"]["all_passed"] is True
    assert payload["summary"]["total"] == 6
    assert payload["summary"]["passed"] == 6
    assert payload["issue_key"] == "ABC-123"
    assert payload["project_key"] == "ABC"

    stdout = capsys.readouterr().out
    assert "PASS" in stdout
    assert "ABC-123" in stdout

    # Field checks must record what was actually verified (no fabricated pass flags).
    hints = {c["name"]: c["hint"] for c in payload["checks"]}
    assert hints["comments"] == "fetched 1 comments"
    assert hints["changelog"] == "fetched 2 changelog entries"
    assert hints["remote_links"] == "fetched 1 remote links"


# ---------------------------------------------------------------------------
# CR-03 regression: comments/changelog/remote_links checks must be falsifiable
# ---------------------------------------------------------------------------


def test_jira_smoke_field_checks_fail_on_malformed_snapshot_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed field shapes → comments/changelog/remote_links each fail independently."""
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    snapshot = JiraWorkSnapshot(
        issue_key="ABC-123",
        summary="Fix ETL job",
        description="desc",
        comments=("not-a-dict",),  # type: ignore[arg-type] — wrong element type
        changelog_ids=("", "10001"),  # empty id string is invalid
        links=[{"url": "https://x"}],  # type: ignore[arg-type] — list, not tuple
        repo_targets=(),
        context_hashes={},
        classification={"kind": "bug"},
    )

    rc = run_jira_command(
        ["smoke", "--issue", "ABC-123"],
        snapshot_collector=lambda ref, timeout=15: snapshot,
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
        stdin_isatty=lambda: False,
    )

    assert rc == 1, f"Expected exit 1 (field checks failed), got {rc}"

    reports = list((tmp_path / "smoke").glob("jira-smoke-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    results = {c["name"]: c["passed"] for c in payload["checks"]}

    # auth/issue_fetch/classify still pass — the failures are independent.
    assert results["auth"] is True
    assert results["issue_fetch"] is True
    assert results["classify"] is True
    assert results["comments"] is False
    assert results["changelog"] is False
    assert results["remote_links"] is False


# ---------------------------------------------------------------------------
# Test 2: empty environ (no Jira config) → exit 2, collector never called
# ---------------------------------------------------------------------------


def test_jira_smoke_missing_config_returns_2_and_never_calls_collector(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No Jira config in environ → exit 2, snapshot_collector never invoked."""

    def _fail_collector(ref: str, timeout: int = 15) -> JiraWorkSnapshot:
        pytest.fail("snapshot_collector must not be called when config is missing")

    rc = run_jira_command(
        ["smoke", "--issue", "ABC-123"],
        snapshot_collector=_fail_collector,
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ={},
        stdin_isatty=lambda: False,
    )

    assert rc == 2, f"Expected exit 2 (config missing), got {rc}"
    err = capsys.readouterr().err
    assert "Jira config is incomplete" in err or "JIRA" in err


# ---------------------------------------------------------------------------
# Test 3: collector raises RuntimeError → exit 1 with actionable hint, no Traceback
# ---------------------------------------------------------------------------


def test_jira_smoke_raising_collector_returns_1_with_actionable_hint_no_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Collector raises RuntimeError → exit 1, hint mentions JIRA_SERVER_URL, no 'Traceback'."""
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    def _raising_collector(ref: str, timeout: int = 15) -> JiraWorkSnapshot:
        raise RuntimeError("boom — connection refused")

    rc = run_jira_command(
        ["smoke", "--issue", "ABC-123"],
        snapshot_collector=_raising_collector,
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
        stdin_isatty=lambda: False,
    )

    assert rc == 1, f"Expected exit 1 (check failed), got {rc}"

    # Check all output channels for hint quality
    captured = capsys.readouterr()
    combined_output = captured.out + captured.err
    # Must NOT expose raw traceback
    assert "Traceback" not in combined_output, "Raw traceback must not appear in CLI output"

    # The report file contains the actionable hint (check report payload)
    smoke_dir = tmp_path / "smoke"
    reports = list(smoke_dir.glob("jira-smoke-*.json"))
    assert len(reports) == 1, "Report must still be written even on failure"
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["summary"]["all_passed"] is False

    # Find the auth check — it should have an actionable hint
    auth_check = next((c for c in payload["checks"] if c["name"] == "auth"), None)
    assert auth_check is not None
    assert auth_check["passed"] is False
    hint = auth_check["hint"]
    # Hint must mention at least one recognizable credential or project key signal
    assert any(s in hint for s in ("JIRA_SERVER_URL", "JIRA_API_TOKEN", "project key", "ABC")), (
        f"Actionable hint expected, got: {hint!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: classify check uses snapshot.classification (no extra call)
# ---------------------------------------------------------------------------


def test_jira_smoke_classify_uses_snapshot_classification_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """classify check reads snapshot.classification directly — no additional Jira call."""
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    classification_data = {"kind": "feature", "urgency": "low", "confidence": "high"}
    snapshot = JiraWorkSnapshot(
        issue_key="XY-99",
        summary="some task",
        description="",
        comments=(),
        changelog_ids=("c1",),
        links=(),
        repo_targets=(),
        context_hashes={},
        classification=classification_data,
    )

    collector_calls: list[str] = []

    def _tracking_collector(ref: str, timeout: int = 15) -> JiraWorkSnapshot:
        collector_calls.append(ref)
        return snapshot

    env = {
        "JIRA_SERVER_URL": "https://jira.example.com",
        "JIRA_USERNAME": "user@example.com",
        "JIRA_API_TOKEN": "token-xyz",
    }
    rc = run_jira_command(
        ["smoke", "--issue", "XY-99"],
        snapshot_collector=_tracking_collector,
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=env,
        stdin_isatty=lambda: False,
    )

    assert rc == 0, f"Expected exit 0, got {rc}"
    # Collector called exactly once (no extra classify call)
    assert collector_calls == ["XY-99"], f"Expected one collector call, got {collector_calls}"

    # Report confirms classify check passed
    smoke_dir = tmp_path / "smoke"
    reports = list(smoke_dir.glob("jira-smoke-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    classify_check = next((c for c in payload["checks"] if c["name"] == "classify"), None)
    assert classify_check is not None
    assert classify_check["passed"] is True, "classify check must pass when snapshot.classification is non-empty"


# ---------------------------------------------------------------------------
# Test 5: report contains no sample token, no postgres DSN
# ---------------------------------------------------------------------------


def test_jira_smoke_report_contains_no_token_or_dsn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Written report must not contain the sample token value, JIRA_API_TOKEN literal, or postgres://."""
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    snapshot = _full_snapshot()
    env = {
        "JIRA_SERVER_URL": "https://sample-jira.example.com",
        "JIRA_USERNAME": "testuser@example.com",
        "JIRA_API_TOKEN": "secret-token-should-not-appear",
        "WHILLY_DATABASE_URL": "postgres://dbuser:dbpass@dbhost:5432/mydb",
    }

    rc = run_jira_command(
        ["smoke", "--issue", "ABC-123"],
        snapshot_collector=lambda ref, timeout=15: snapshot,
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=env,
        stdin_isatty=lambda: False,
    )

    assert rc == 0

    smoke_dir = tmp_path / "smoke"
    reports = list(smoke_dir.glob("jira-smoke-*.json"))
    assert len(reports) == 1
    raw = reports[0].read_text(encoding="utf-8")

    # Security assertions: no secrets in the report file
    assert "secret-token-should-not-appear" not in raw, "Sample token value must not appear in report"
    assert "JIRA_API_TOKEN" not in raw, "Token key literal must not appear in report"
    assert "postgres://" not in raw, "Database DSN must not appear in report"

    # Sanity: redacted host and project key ARE present
    parsed = json.loads(raw)
    assert "sample-jira.example.com" in parsed.get("target_host", "")
    assert parsed["project_key"] == "ABC"
