"""Unit tests for ``whilly gitlab smoke`` CLI surface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from whilly.cli.gitlab import _extract_host_from_url, _resolve_gitlab_config_state, run_gitlab_command

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
# CR-01 regression: credentialed GITLAB_URL must never leak into report/stdout
# ---------------------------------------------------------------------------


def test_gitlab_smoke_credentialed_url_secret_never_leaks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """GITLAB_URL with embedded oauth2:glpat-SECRET@ creds + failing getter → secret nowhere.

    The getter mimics _gitlab_get by embedding the request URL in its error
    message; the credential must never surface in the report JSON or output.
    """
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))
    secret = "glpat-SECRET123"
    env = {
        "GITLAB_URL": f"https://oauth2:{secret}@gitlab.example.com",
        "GITLAB_TOKEN": secret,
    }

    def _failing_getter(url: str, *, token: str, timeout: int = 15) -> dict[str, Any]:
        # urllib-style failure that embeds the full request URL it was given.
        raise RuntimeError(f"GitLab GET {url!r} failed: HTTP 401 — unauthorized")

    rc = run_gitlab_command(
        ["smoke", "--repo-url", _REPO_URL],
        gitlab_getter=_failing_getter,
        environ=env,
    )

    assert rc == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert secret not in combined, "Token value leaked into CLI output"

    reports = list((tmp_path / "smoke").glob("gitlab-smoke-*.json"))
    assert len(reports) == 1
    content = reports[0].read_text(encoding="utf-8")
    assert secret not in content, "Token value leaked into report JSON"
    assert "oauth2:" not in content, "URL userinfo leaked into report JSON"
    # The clean hostname is still allowed (and useful) in hints.
    assert "gitlab.example.com" in content


def test_gitlab_get_error_messages_redact_credentialed_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_gitlab_get RuntimeError text must contain the redacted URL, not the credential."""
    from urllib.error import URLError

    from whilly.cli.gitlab import _gitlab_get

    def _raise(req: Any, timeout: int = 0) -> Any:
        raise URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _raise)

    with pytest.raises(RuntimeError) as excinfo:
        _gitlab_get("https://oauth2:glpat-SECRET123@gitlab.example.com/api/v4/user", token="t")

    message = str(excinfo.value)
    assert "glpat-SECRET123" not in message, "Credential leaked into error message"
    assert "oauth2" not in message, "URL userinfo leaked into error message"
    assert "gitlab.example.com" in message


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
# WR-05 regression: _extract_host_from_url handles userinfo and SSH forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("repo_url", "expected_host"),
    [
        ("https://gitlab.example.com/group/repo", "gitlab.example.com"),
        ("https://user:pass@gitlab.example.com/group/repo", "gitlab.example.com"),
        ("https://user@gitlab.example.com/group/repo", "gitlab.example.com"),
        ("https://gitlab.example.com:8443/group/repo", "gitlab.example.com"),
        ("git@gitlab.example.com:group/repo.git", "gitlab.example.com"),
        ("not a url", ""),
    ],
)
def test_extract_host_from_url_handles_userinfo_and_ssh(repo_url: str, expected_host: str) -> None:
    """Userinfo must never pollute the extracted host; SSH clone form still works."""
    assert _extract_host_from_url(repo_url) == expected_host


# ---------------------------------------------------------------------------
# WR-06 regression: SSH-style --repo-url rejected up front
# ---------------------------------------------------------------------------


def test_gitlab_smoke_rejects_ssh_repo_url_early(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """git@host:path --repo-url → exit 2 with a clear hint, getter never called."""
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path))

    def _should_not_be_called(url: str, **_kwargs: Any) -> dict[str, Any]:
        pytest.fail(f"gitlab_getter was called with {url!r} — SSH URLs must be rejected up front")

    rc = run_gitlab_command(
        ["smoke", "--repo-url", "git@gitlab.example.com:group/repo.git"],
        gitlab_getter=_should_not_be_called,
        environ=_minimal_env(),
    )

    assert rc == 2
    err = capsys.readouterr().err
    assert "http" in err
    assert "git@" in err or "SSH" in err


# ---------------------------------------------------------------------------
# WR-04 regression: glab fallback host comes from GITLAB_URL, never hardcoded
# ---------------------------------------------------------------------------


def test_resolve_gitlab_config_state_glab_fallback_uses_gitlab_url_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty extracted host → glab lookup targets the GITLAB_URL host."""
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = "glab-token\n"

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _Result:
        calls.append(list(cmd))
        return _Result()

    monkeypatch.setattr("subprocess.run", _fake_run)

    url, token = _resolve_gitlab_config_state({"GITLAB_URL": _GITLAB_URL}, "")

    assert url == _GITLAB_URL
    assert token == "glab-token"
    assert calls == [["glab", "config", "get", "token", "-h", "gitlab.example.com"]]


def test_resolve_gitlab_config_state_skips_glab_without_any_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No extracted host and no GITLAB_URL → glab is never invoked."""

    def _fail_run(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("glab must not be invoked when no host can be derived")

    monkeypatch.setattr("subprocess.run", _fail_run)

    url, token = _resolve_gitlab_config_state({}, "")

    assert url == ""
    assert token == ""


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
