"""Unit tests for ``whilly gitlab smoke`` CLI surface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from whilly.cli.gitlab import _resolve_gitlab_config_state, run_gitlab_command

# ---------------------------------------------------------------------------
# Sample constants
# ---------------------------------------------------------------------------

_GITLAB_URL = "https://gitlab.example.com"
_SAMPLE_TOKEN = "super-secret-token-abc123"
_REPO_URL = "https://gitlab.example.com/group/repo"

_USER_PAYLOAD: dict[str, Any] = {
    "id": 42,
    "username": "smoketest",
    "name": "Smoke Test",
}

_PROJECT_PAYLOAD: dict[str, Any] = {
    "id": 99,
    "path_with_namespace": "group/repo",
    "http_url_to_repo": "https://gitlab.example.com/group/repo.git",
    "name": "repo",
}


def _minimal_env() -> dict[str, str]:
    return {
        "GITLAB_URL": _GITLAB_URL,
        "GITLAB_TOKEN": _SAMPLE_TOKEN,
    }


def _make_getter(
    user: dict[str, Any] | None = None,
    project: dict[str, Any] | None = None,
) -> Any:
    """Return a fake gitlab_getter that dispatches by URL fragment."""
    _user = user if user is not None else _USER_PAYLOAD
    _project = project if project is not None else _PROJECT_PAYLOAD

    def getter(url: str, *, token: str, timeout: int = 15) -> dict[str, Any]:
        if "/api/v4/user" in url:
            return _user
        if "/api/v4/projects/" in url:
            return _project
        raise RuntimeError(f"Unexpected URL in fake getter: {url}")

    return getter


# ---------------------------------------------------------------------------
# Test 1: all checks pass, report written
# ---------------------------------------------------------------------------


def test_gitlab_smoke_auth_pass_writes_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth + project_access + repo_hint all pass → exit 0 + report file."""
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    rc = run_gitlab_command(
        ["smoke", "--repo-url", _REPO_URL],
        gitlab_getter=_make_getter(),
        environ=_minimal_env(),
    )

    assert rc == 0

    # Report file must exist under smoke/
    smoke_dir = tmp_path / "smoke"
    reports = list(smoke_dir.glob("gitlab-smoke-*.json"))
    assert len(reports) == 1, f"Expected 1 report, found {len(reports)}"

    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    summary = payload["summary"]
    assert summary["all_passed"] is True
    assert summary["total"] == 3
    assert summary["passed"] == 3


# ---------------------------------------------------------------------------
# Test 2: missing config → exit 2, getter never called
# ---------------------------------------------------------------------------


def test_gitlab_smoke_missing_config_returns_2_and_never_calls_getter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty environ (no token, no glab) → exit 2; getter is never invoked."""
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    def _should_not_be_called(url: str, **_kwargs: Any) -> dict[str, Any]:
        pytest.fail(f"gitlab_getter was called with {url!r} — it must NOT be called on missing config")

    rc = run_gitlab_command(
        ["smoke", "--repo-url", _REPO_URL],
        gitlab_getter=_should_not_be_called,
        environ={},  # no GITLAB_URL, no GITLAB_TOKEN
    )

    assert rc == 2


# ---------------------------------------------------------------------------
# Test 3: repo-hint — report records parsed path and check results
# ---------------------------------------------------------------------------


def test_gitlab_smoke_report_records_repo_path_and_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report payload contains the repo path and named check entries."""
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    rc = run_gitlab_command(
        ["smoke", "--repo-url", _REPO_URL],
        gitlab_getter=_make_getter(),
        environ=_minimal_env(),
    )

    assert rc == 0

    smoke_dir = tmp_path / "smoke"
    reports = list(smoke_dir.glob("gitlab-smoke-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))

    # target must record the repo path
    assert "target" in payload
    assert payload["target"]["repo_path"] == "group/repo"

    # All three checks must be present and passed
    check_names = {c["name"] for c in payload["checks"]}
    assert "auth" in check_names
    assert "project_access" in check_names
    assert "repo_hint" in check_names
    for check in payload["checks"]:
        assert check["passed"] is True, f"Check {check['name']!r} unexpectedly failed"


# ---------------------------------------------------------------------------
# Test 4: raising getter → exit 1, hint in output, no Traceback
# ---------------------------------------------------------------------------


def test_gitlab_smoke_raising_getter_returns_1_with_hint_no_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A getter that raises RuntimeError → exit 1 with a hint and no traceback."""
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    def _failing_getter(url: str, *, token: str, timeout: int = 15) -> dict[str, Any]:
        raise RuntimeError("connection refused — cannot reach GitLab")

    rc = run_gitlab_command(
        ["smoke", "--repo-url", _REPO_URL],
        gitlab_getter=_failing_getter,
        environ=_minimal_env(),
    )

    assert rc == 1

    # No raw Python traceback must appear in stdout/stderr
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "Traceback" not in combined, "Raw traceback leaked to output"

    # A hint describing the failure must be in the report
    smoke_dir = tmp_path / "smoke"
    reports = list(smoke_dir.glob("gitlab-smoke-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["summary"]["all_passed"] is False

    # At least one check must carry a non-empty hint
    hints = [c["hint"] for c in payload["checks"] if c.get("hint")]
    assert hints, "Expected at least one check to carry an actionable hint"


# ---------------------------------------------------------------------------
# Test 5: no secrets in report
# ---------------------------------------------------------------------------


def test_gitlab_smoke_report_contains_no_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report JSON must not contain the sample token value, 'GITLAB_TOKEN', or 'postgres://'."""
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    rc = run_gitlab_command(
        ["smoke", "--repo-url", _REPO_URL],
        gitlab_getter=_make_getter(),
        environ=_minimal_env(),
    )

    assert rc == 0

    smoke_dir = tmp_path / "smoke"
    reports = list(smoke_dir.glob("gitlab-smoke-*.json"))
    assert len(reports) == 1
    content = reports[0].read_text(encoding="utf-8")

    assert _SAMPLE_TOKEN not in content, "Token value leaked into report"
    assert "GITLAB_TOKEN" not in content, "'GITLAB_TOKEN' literal in report"
    assert "postgres://" not in content, "'postgres://' DSN in report"


# ---------------------------------------------------------------------------
# Test 6: GITLAB_TOKEN precedence over GITLAB_API_TOKEN
# ---------------------------------------------------------------------------


def test_resolve_gitlab_config_state_prefers_gitlab_token(tmp_path: Path) -> None:
    """_resolve_gitlab_config_state picks GITLAB_TOKEN over GITLAB_API_TOKEN."""
    env = {
        "GITLAB_URL": _GITLAB_URL,
        "GITLAB_TOKEN": "token-from-GITLAB_TOKEN",
        "GITLAB_API_TOKEN": "token-from-GITLAB_API_TOKEN",
        "WHILLY_GITLAB_API_TOKEN": "token-from-WHILLY",
    }
    url, token = _resolve_gitlab_config_state(env, "gitlab.example.com")
    assert url == _GITLAB_URL
    assert token == "token-from-GITLAB_TOKEN"


# ---------------------------------------------------------------------------
# Test 7: GITLAB_API_TOKEN used when GITLAB_TOKEN absent
# ---------------------------------------------------------------------------


def test_resolve_gitlab_config_state_falls_back_to_gitlab_api_token() -> None:
    """_resolve_gitlab_config_state falls back to GITLAB_API_TOKEN when GITLAB_TOKEN absent."""
    env = {
        "GITLAB_URL": _GITLAB_URL,
        "GITLAB_API_TOKEN": "token-from-GITLAB_API_TOKEN",
        "WHILLY_GITLAB_API_TOKEN": "token-from-WHILLY",
    }
    _url, token = _resolve_gitlab_config_state(env, "gitlab.example.com")
    assert token == "token-from-GITLAB_API_TOKEN"
