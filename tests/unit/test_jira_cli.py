"""Tests for the ``whilly jira`` CLI surface."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from whilly.cli.jira import JIRA_CLOUD_API_TOKEN_URL, run_jira_command


def _jira_env() -> dict[str, str]:
    return {
        "JIRA_SERVER_URL": "https://company.atlassian.net",
        "JIRA_USERNAME": "dev@example.com",
        "JIRA_API_TOKEN": "jira-token",
    }


def _fake_fetcher(calls: list[tuple[str, Path, int]]) -> Callable[..., tuple[Path, SimpleNamespace]]:
    def fetcher(key: str, out_path: str | Path, *, timeout: int = 15) -> tuple[Path, SimpleNamespace]:
        path = Path(out_path)
        calls.append((key, path, timeout))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "project": "jira",
                    "plan_id": "stale",
                    "tasks": [
                        {
                            "id": f"JIRA-{key}",
                            "status": "PENDING",
                            "priority": "medium",
                            "description": "Demo Jira task",
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return path, SimpleNamespace(new=1, updated=0)

    return fetcher


def test_jira_import_fetches_issue_and_writes_plan_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[str, Path, int]] = []
    out = tmp_path / "jira-plan.json"

    rc = run_jira_command(
        ["import", "abc-123", "--out", str(out)],
        fetcher=_fake_fetcher(calls),
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
    )

    assert rc == 0
    assert calls == [("ABC-123", out, 15)]
    assert json.loads(out.read_text(encoding="utf-8"))["plan_id"] == "jira-abc-123"
    stdout = capsys.readouterr().out
    assert f"plan={out}" in stdout
    assert "plan_id=jira-abc-123" in stdout
    assert "new=1" in stdout
    assert "updated=0" in stdout


def test_jira_import_defaults_to_out_file_under_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Path, int]] = []
    monkeypatch.chdir(tmp_path)

    rc = run_jira_command(
        ["import", "ABC-123"],
        fetcher=_fake_fetcher(calls),
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
    )

    assert rc == 0
    assert calls == [("ABC-123", Path("out/jira-ABC-123.json"), 15)]
    assert json.loads((tmp_path / "out/jira-ABC-123.json").read_text(encoding="utf-8"))["plan_id"] == ("jira-abc-123")


def test_jira_import_can_import_written_plan_to_database(tmp_path: Path) -> None:
    calls: list[tuple[str, Path, int]] = []
    imported: list[str] = []
    out = tmp_path / "jira-plan.json"

    rc = run_jira_command(
        ["import", "ABC-123", "--out", str(out), "--plan-id", "release-hotfix", "--import-db"],
        fetcher=_fake_fetcher(calls),
        importer=lambda plan_file: imported.append(plan_file) or 0,
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
    )

    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["plan_id"] == "release-hotfix"
    assert imported == [str(out)]


def test_jira_import_run_imports_plan_then_runs_worker(tmp_path: Path) -> None:
    calls: list[tuple[str, Path, int]] = []
    imported: list[str] = []
    run_calls: list[list[str]] = []
    out = tmp_path / "jira-plan.json"

    rc = run_jira_command(
        [
            "import",
            "ABC-123",
            "--out",
            str(out),
            "--run",
            "--max-iterations",
            "1",
            "--worker-id",
            "dev-worker",
            "--verify-command",
            "unit=pytest -q",
            "--optional-verify-command",
            "lint=ruff check .",
            "--verify-timeout",
            "5",
        ],
        fetcher=_fake_fetcher(calls),
        importer=lambda plan_file: imported.append(plan_file) or 0,
        runner=lambda argv: run_calls.append(list(argv)) or 7,
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
    )

    assert rc == 7
    assert imported == [str(out)]
    assert run_calls == [
        [
            "--plan",
            "jira-abc-123",
            "--max-iterations",
            "1",
            "--worker-id",
            "dev-worker",
            "--verify-command",
            "unit=pytest -q",
            "--optional-verify-command",
            "lint=ruff check .",
            "--verify-timeout",
            "5",
        ]
    ]


def test_jira_import_run_stops_when_database_import_fails(tmp_path: Path) -> None:
    calls: list[tuple[str, Path, int]] = []
    run_calls: list[list[str]] = []
    out = tmp_path / "jira-plan.json"

    rc = run_jira_command(
        ["import", "ABC-123", "--out", str(out), "--run"],
        fetcher=_fake_fetcher(calls),
        importer=lambda _plan_file: 2,
        runner=lambda argv: run_calls.append(list(argv)) or 0,
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
    )

    assert rc == 2
    assert run_calls == []


def test_jira_import_rejects_invalid_reference(capsys: pytest.CaptureFixture[str]) -> None:
    rc = run_jira_command(
        ["import", "not-a-jira-key"],
        fetcher=lambda *_args, **_kwargs: pytest.fail("fetcher should not be called"),
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
    )

    assert rc == 1
    assert "Cannot parse Jira reference" in capsys.readouterr().err


def test_jira_import_noninteractive_reports_missing_config_without_fetching(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = run_jira_command(
        ["import", "ABC-123"],
        fetcher=lambda *_args, **_kwargs: pytest.fail("fetcher should not be called"),
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ={},
        stdin_isatty=lambda: False,
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "Jira config is incomplete" in err
    assert "JIRA_SERVER_URL" in err
    assert "JIRA_USERNAME" in err
    assert "JIRA_API_TOKEN" in err
    assert JIRA_CLOUD_API_TOKEN_URL in err
    assert "--interactive-config" in err


def test_jira_import_interactive_config_prompts_missing_values_and_opens_pat_page(tmp_path: Path) -> None:
    calls: list[tuple[str, Path, int]] = []
    prompts: list[str] = []
    opened: list[str] = []
    answers = iter(["https://company.atlassian.net", "dev@example.com"])
    env: dict[str, str] = {}
    out = tmp_path / "jira-plan.json"

    rc = run_jira_command(
        ["import", "ABC-123", "--out", str(out)],
        fetcher=_fake_fetcher(calls),
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=env,
        stdin_isatty=lambda: True,
        prompt=lambda label: prompts.append(label) or next(answers),
        secret_prompt=lambda label: prompts.append(label) or "jira-token",
        browser_opener=lambda url: opened.append(url) or True,
    )

    assert rc == 0
    assert calls == [("ABC-123", out, 15)]
    assert env["JIRA_SERVER_URL"] == "https://company.atlassian.net"
    assert env["JIRA_USERNAME"] == "dev@example.com"
    assert env["JIRA_API_TOKEN"] == "jira-token"
    assert opened == [JIRA_CLOUD_API_TOKEN_URL]
    assert any("Jira server URL" in prompt for prompt in prompts)
    assert any("Jira username/email" in prompt for prompt in prompts)
    assert any("Jira API token / PAT" in prompt for prompt in prompts)


def test_jira_import_no_interactive_config_suppresses_tty_prompts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompts: list[str] = []

    rc = run_jira_command(
        ["import", "ABC-123", "--no-interactive-config"],
        fetcher=lambda *_args, **_kwargs: pytest.fail("fetcher should not be called"),
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ={},
        stdin_isatty=lambda: True,
        prompt=lambda label: prompts.append(label) or "",
        secret_prompt=lambda label: prompts.append(label) or "",
        browser_opener=lambda _url: pytest.fail("browser should not be opened"),
    )

    assert rc == 1
    assert prompts == []
    assert "Jira config is incomplete" in capsys.readouterr().err


def test_jira_import_bearer_auth_does_not_require_username(tmp_path: Path) -> None:
    calls: list[tuple[str, Path, int]] = []
    env = {
        "JIRA_SERVER_URL": "https://jira.example.test",
        "JIRA_API_TOKEN": "data-center-pat",
        "JIRA_AUTH_SCHEME": "bearer",
    }

    rc = run_jira_command(
        ["import", "ABC-123", "--out", str(tmp_path / "jira-plan.json")],
        fetcher=_fake_fetcher(calls),
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=env,
    )

    assert rc == 0
    assert calls[0][0] == "ABC-123"
