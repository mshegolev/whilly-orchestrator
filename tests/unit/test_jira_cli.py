"""Tests for the ``whilly jira`` CLI surface."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from whilly.cli.jira import JIRA_CLOUD_API_TOKEN_URL, build_jira_parser, run_jira_command
from whilly.jira_watch import JiraWorkSnapshot


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


def test_jira_poll_prints_snapshot_summary(capsys: pytest.CaptureFixture[str]) -> None:
    snapshot = JiraWorkSnapshot(
        issue_key="ABC-123",
        summary="Fix ETL job",
        description="desc",
        comments=({"id": "20001", "body": "/whilly plan"},),
        changelog_ids=("10001",),
        links=({"url": "https://gitlab.company/platform/etl/-/merge_requests/7"},),
        repo_targets=({"id": "gitlab:platform/etl"},),
        context_hashes={"combined_hash": "hash"},
        classification={"kind": "bug", "urgency": "normal"},
        comment_commands=({"action": "plan", "value": "", "raw": "/whilly plan"},),
        last_seen_comment_id="20001",
    )

    rc = run_jira_command(
        ["poll", "ABC-123"],
        snapshot_collector=lambda ref, timeout=15: snapshot,
    )

    assert rc == 0
    stdout = capsys.readouterr().out
    assert "whilly jira poll: issue=ABC-123" in stdout
    assert "comments=1" in stdout
    assert "changelog=1" in stdout
    assert "repo_targets=1" in stdout


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


def test_jira_intake_save_writes_gitlab_repo_target_from_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, Path, int]] = []
    out = tmp_path / "jira-plan.json"
    repo_url = "git@gitlab.company.test:platform/etl-service.git"

    rc = run_jira_command(
        [
            "intake",
            "ABC-123",
            "--out",
            str(out),
            "--repo-url",
            repo_url,
            "--action",
            "save",
        ],
        fetcher=_fake_fetcher(calls),
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
        stdin_isatty=lambda: False,
    )

    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["plan_id"] == "jira-abc-123"
    assert data["repo_targets"] == [
        {
            "id": "gitlab:platform/etl-service",
            "provider": "gitlab",
            "repo_full_name": "platform/etl-service",
            "clone_url": repo_url,
            "default_branch": "main",
        }
    ]
    assert data["tasks"][0]["repo_target_id"] == "gitlab:platform/etl-service"
    stdout = capsys.readouterr().out
    assert "whilly jira intake: loaded ABC-123" in stdout
    assert "repo_target=gitlab:platform/etl-service" in stdout


def test_jira_intake_interactive_prd_writes_context_and_links_task(tmp_path: Path) -> None:
    calls: list[tuple[str, Path, int]] = []
    out = tmp_path / "jira-plan.json"
    answers = iter(
        [
            "3",
            "https://gitlab.company.test/platform/etl-service.git",
            "1",
        ]
    )

    rc = run_jira_command(
        ["intake", "ABC-123", "--out", str(out)],
        fetcher=_fake_fetcher(calls),
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
        stdin_isatty=lambda: True,
        prompt=lambda _label: next(answers),
    )

    assert rc == 0
    context_path = tmp_path / "jira-ABC-123-context.md"
    assert context_path.read_text(encoding="utf-8").startswith("# Jira ABC-123 Context")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["repo_targets"][0]["id"] == "gitlab:platform/etl-service"
    assert data["tasks"][0]["repo_target_id"] == "gitlab:platform/etl-service"
    assert data["origin"]["prd_file"] == str(context_path)


def test_jira_intake_same_repo_uses_detected_origin(tmp_path: Path) -> None:
    calls: list[tuple[str, Path, int]] = []
    out = tmp_path / "jira-plan.json"

    rc = run_jira_command(
        [
            "intake",
            "ABC-123",
            "--out",
            str(out),
            "--repo-kind",
            "same",
            "--action",
            "save",
        ],
        fetcher=_fake_fetcher(calls),
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
        stdin_isatty=lambda: False,
        repo_detector=lambda: "https://gitlab.company.test/platform/main-app.git",
    )

    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["repo_targets"][0]["id"] == "gitlab:platform/main-app"
    assert data["tasks"][0]["repo_target_id"] == "gitlab:platform/main-app"


def test_jira_intake_writes_work_classification_metadata(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def fetch_feature(key: str, out_path: str | Path, *, timeout: int = 15) -> tuple[Path, SimpleNamespace]:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "project": "jira",
                    "name": "Add ETL export wizard",
                    "origin": {"system": "jira_issue", "ref": key},
                    "tasks": [
                        {
                            "id": f"JIRA-{key}",
                            "status": "PENDING",
                            "priority": "medium",
                            "description": "Implement a new ETL export wizard for analysts.",
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

    out = tmp_path / "jira-plan.json"

    rc = run_jira_command(
        [
            "intake",
            "ABC-123",
            "--out",
            str(out),
            "--repo-kind",
            "skip",
            "--action",
            "save",
        ],
        fetcher=fetch_feature,
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
        stdin_isatty=lambda: False,
    )

    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["jira_work"]["classification"]["kind"] == "feature"
    assert data["jira_work"]["classification"]["recommended_flow"] == "feature_prd"
    assert data["jira_work"]["context_hashes"]["combined_hash"]
    assert data["origin"]["content_hash"] == data["jira_work"]["context_hashes"]["combined_hash"]
    stdout = capsys.readouterr().out
    assert "classification=feature" in stdout
    assert "flow=feature_prd" in stdout


def test_jira_intake_run_blocks_when_readiness_repo_has_no_tests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, Path, int]] = []
    plan_calls: list[list[str]] = []
    run_calls: list[list[str]] = []
    out = tmp_path / "jira-plan.json"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text('{"scripts":{"test":"vitest run"}}\n', encoding="utf-8")

    rc = run_jira_command(
        [
            "intake",
            "ABC-123",
            "--out",
            str(out),
            "--repo-url",
            "https://github.com/acme/app.git",
            "--readiness-repo-path",
            str(repo),
            "--action",
            "run",
        ],
        fetcher=_fake_fetcher(calls),
        plan_runner=lambda argv: plan_calls.append(list(argv)) or 0,
        runner=lambda argv: run_calls.append(list(argv)) or 0,
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
        stdin_isatty=lambda: False,
    )

    assert rc == 1
    assert plan_calls == []
    assert run_calls == []
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["jira_work"]["readiness"]["verdict"] == "needs_test_plan"
    assert "unit_tests" in data["jira_work"]["readiness"]["missing_context"]
    captured = capsys.readouterr()
    assert "readiness=needs_test_plan" in captured.out
    assert "readiness gate failed" in captured.err


def test_jira_intake_run_imports_plan_then_runs_worker(tmp_path: Path) -> None:
    calls: list[tuple[str, Path, int]] = []
    plan_calls: list[list[str]] = []
    run_calls: list[list[str]] = []
    out = tmp_path / "jira-plan.json"

    rc = run_jira_command(
        [
            "intake",
            "ABC-123",
            "--out",
            str(out),
            "--repo-url",
            "https://github.com/acme/app.git",
            "--action",
            "run",
            "--max-iterations",
            "1",
        ],
        fetcher=_fake_fetcher(calls),
        plan_runner=lambda argv: plan_calls.append(list(argv)) or 0,
        runner=lambda argv: run_calls.append(list(argv)) or 0,
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
        stdin_isatty=lambda: False,
    )

    assert rc == 0
    assert plan_calls == [["apply", str(out), "--strict"]]
    assert run_calls == [["--plan", "jira-abc-123", "--max-iterations", "1"]]
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["repo_targets"][0]["id"] == "github:acme/app"


def test_jira_intake_run_stops_when_strict_apply_fails(tmp_path: Path) -> None:
    calls: list[tuple[str, Path, int]] = []
    plan_calls: list[list[str]] = []
    run_calls: list[list[str]] = []
    out = tmp_path / "jira-plan.json"

    rc = run_jira_command(
        [
            "intake",
            "ABC-123",
            "--out",
            str(out),
            "--repo-url",
            "https://github.com/acme/app.git",
            "--action",
            "run",
        ],
        fetcher=_fake_fetcher(calls),
        plan_runner=lambda argv: plan_calls.append(list(argv)) or 9,
        runner=lambda argv: run_calls.append(list(argv)) or 0,
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
        stdin_isatty=lambda: False,
    )

    assert rc == 9
    assert plan_calls == [["apply", str(out), "--strict"]]
    assert run_calls == []


def test_jira_intake_plan_action_runs_apply_then_triz(tmp_path: Path) -> None:
    calls: list[tuple[str, Path, int]] = []
    plan_calls: list[list[str]] = []
    out = tmp_path / "jira-plan.json"

    rc = run_jira_command(
        [
            "intake",
            "ABC-123",
            "--out",
            str(out),
            "--repo-url",
            "https://github.com/acme/app.git",
            "--action",
            "plan",
        ],
        fetcher=_fake_fetcher(calls),
        plan_runner=lambda argv: plan_calls.append(list(argv)) or 0,
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
        stdin_isatty=lambda: False,
    )

    assert rc == 0
    assert plan_calls == [
        ["apply", str(out), "--strict"],
        ["triz", "jira-abc-123", "--strict"],
    ]


def test_jira_intake_plan_action_stops_when_strict_apply_fails(tmp_path: Path) -> None:
    calls: list[tuple[str, Path, int]] = []
    plan_calls: list[list[str]] = []
    out = tmp_path / "jira-plan.json"

    rc = run_jira_command(
        [
            "intake",
            "ABC-123",
            "--out",
            str(out),
            "--repo-url",
            "https://github.com/acme/app.git",
            "--action",
            "plan",
        ],
        fetcher=_fake_fetcher(calls),
        plan_runner=lambda argv: plan_calls.append(list(argv)) or 8,
        config_loader=lambda: None,
        config_reader=lambda: {},
        environ=_jira_env(),
        stdin_isatty=lambda: False,
    )

    assert rc == 8
    assert plan_calls == [["apply", str(out), "--strict"]]


# ---------------------------------------------------------------------------
# watch + watch-status subparser and dispatch tests
# ---------------------------------------------------------------------------


def test_watch_subparser_has_required_flags() -> None:
    """watch subparser must carry all required flags including --interactive-config."""
    parser = build_jira_parser()
    # Find the watch subparser by parsing a watch invocation
    args = parser.parse_args(
        [
            "watch",
            "--issue",
            "ABC-123",
            "--interval",
            "60",
            "--timeout",
            "30",
            "--dispatch",
            "--readiness-repo-path",
            "/tmp/repo",
            "--allow-unready-run",
            "--interactive-config",
        ]
    )
    assert args.issues == ["ABC-123"]
    assert args.interval == 60
    assert args.timeout == 30
    assert args.dispatch is True
    assert args.readiness_repo_path == "/tmp/repo"
    assert args.allow_unready_run is True
    assert args.interactive_config is True

    # --no-interactive-config also present
    args2 = parser.parse_args(["watch", "--issue", "X-1", "--no-interactive-config"])
    assert args2.no_interactive_config is True


def test_watch_dispatch_invokes_run_jira_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_jira_command watch routes to _run_jira_watch via lazy import."""
    from whilly.cli import jira_watch_loop

    watch_calls: list[object] = []

    def _spy(args: object, **kwargs: object) -> int:
        watch_calls.append(args)
        return 0

    monkeypatch.setattr(jira_watch_loop, "_run_jira_watch", _spy)

    rc = run_jira_command(
        ["watch", "--issue", "ABC-123", "--interval", "0"],
        environ=_jira_env(),
        config_loader=lambda: None,
        config_reader=lambda: {},
        stdin_isatty=lambda: False,
    )

    assert rc == 0
    assert len(watch_calls) == 1


def test_watch_dispatch_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without --dispatch, the dispatch_runner injected into _run_jira_watch is None."""
    from whilly.cli import jira_watch_loop

    captured_kwargs: list[dict[str, object]] = []

    def _capture(args: object, **kwargs: object) -> int:
        captured_kwargs.append(kwargs)
        return 0

    monkeypatch.setattr(jira_watch_loop, "_run_jira_watch", _capture)

    run_jira_command(
        ["watch", "--issue", "ABC-123"],
        environ=_jira_env(),
        config_loader=lambda: None,
        config_reader=lambda: {},
        stdin_isatty=lambda: False,
    )

    assert captured_kwargs, "spy was not called"
    kw = captured_kwargs[0]
    # Without --dispatch the production dispatch_runner must be None
    assert kw.get("dispatch_runner") is None


def _capture_production_dispatch_runner(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    *,
    plan_runner_calls: list[list[str]],
):
    """Run `whilly jira watch --dispatch ...` with only _run_jira_watch spied,
    so the REAL production dispatch closure is constructed, and return it."""
    from whilly.cli import jira_watch_loop

    captured: list[dict[str, object]] = []

    def _spy(args: object, **kwargs: object) -> int:
        captured.append({"args": args, **kwargs})
        return 0

    monkeypatch.setattr(jira_watch_loop, "_run_jira_watch", _spy)

    rc = run_jira_command(
        argv,
        environ=_jira_env(),
        config_loader=lambda: None,
        config_reader=lambda: {},
        stdin_isatty=lambda: False,
        plan_runner=lambda cmd: plan_runner_calls.append(list(cmd)) or 0,
    )
    assert rc == 0
    assert captured, "_run_jira_watch was not reached"
    runner = captured[0].get("dispatch_runner")
    assert callable(runner), "--dispatch must wire a production dispatch_runner"
    return captured[0]["args"], runner


def test_production_dispatch_closure_builds_complete_run_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The REAL dispatch closure must build a complete `jira run` argument set
    from the watch namespace — no AttributeError on the missing pass-through
    flags (CR-01) — and hand _run_plan_worker a valid argv."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path / "logs"))

    from whilly.cli import jira as jira_module

    worker_argv: list[list[str]] = []
    monkeypatch.setattr(jira_module, "_run_plan_worker", lambda argv: worker_argv.append(list(argv)) or 0)

    plan_runner_calls: list[list[str]] = []
    watch_args, runner = _capture_production_dispatch_runner(
        monkeypatch,
        ["watch", "--issue", "ABC-123", "--interval", "0", "--dispatch", "--allow-unready-run"],
        plan_runner_calls=plan_runner_calls,
    )

    rc = runner(watch_args, "ABC-123")

    assert rc == 0
    assert plan_runner_calls == [["apply", str(Path("out") / "jira-ABC-123.json"), "--strict"]]
    assert worker_argv == [["--plan", "jira-abc-123"]], "watch dispatch must produce a complete run argv"


def test_production_dispatch_closure_normalizes_browse_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browse-URL --issue value is normalized via parse_jira_key for plan id
    and plan path (WR-07); garbage refs are refused, not crashed on."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path / "logs"))

    from whilly.cli import jira as jira_module

    worker_argv: list[list[str]] = []
    monkeypatch.setattr(jira_module, "_run_plan_worker", lambda argv: worker_argv.append(list(argv)) or 0)

    plan_runner_calls: list[list[str]] = []
    url = "https://jira.example/browse/ABC-123"
    watch_args, runner = _capture_production_dispatch_runner(
        monkeypatch,
        ["watch", "--issue", url, "--interval", "0", "--dispatch", "--allow-unready-run"],
        plan_runner_calls=plan_runner_calls,
    )

    rc = runner(watch_args, url)
    assert rc == 0
    assert plan_runner_calls == [["apply", str(Path("out") / "jira-ABC-123.json"), "--strict"]]
    assert worker_argv == [["--plan", "jira-abc-123"]]

    # Unparseable ref → refused with rc 1, no worker run
    rc_bad = runner(watch_args, "not a jira ref")
    assert rc_bad == 1
    assert len(worker_argv) == 1, "no worker run for an unparseable issue ref"


def test_production_dispatch_closure_refuses_unready_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The closure's plan-level readiness gate refuses a plan that declares
    itself unready, and fails closed on a garbled plan JSON."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path / "logs"))

    from whilly.cli import jira as jira_module

    worker_argv: list[list[str]] = []
    monkeypatch.setattr(jira_module, "_run_plan_worker", lambda argv: worker_argv.append(list(argv)) or 0)

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "jira-ABC-123.json").write_text(
        json.dumps({"jira_work": {"readiness": {"verdict": "needs_test_plan", "missing_context": ["unit_tests"]}}}),
        encoding="utf-8",
    )

    plan_runner_calls: list[list[str]] = []
    watch_args, runner = _capture_production_dispatch_runner(
        monkeypatch,
        ["watch", "--issue", "ABC-123", "--interval", "0", "--dispatch"],
        plan_runner_calls=plan_runner_calls,
    )

    assert runner(watch_args, "ABC-123") == 1
    assert plan_runner_calls == [], "unready plan must not reach apply --strict"
    assert worker_argv == []

    # Garbled plan JSON → fail closed, not crash
    (out_dir / "jira-ABC-123.json").write_text("{not json", encoding="utf-8")
    assert runner(watch_args, "ABC-123") == 1
    assert worker_argv == []


def test_watch_missing_config_exits_config_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """watch with missing Jira config exits EXIT_CONFIG_MISSING (2) before
    the loop starts; the loop itself must never be entered (WR-03)."""
    from whilly.cli import jira_watch_loop
    from whilly.cli.smoke import EXIT_CONFIG_MISSING

    def _never(*args: object, **kwargs: object) -> int:
        raise AssertionError("_run_jira_watch must not run with missing config")

    monkeypatch.setattr(jira_watch_loop, "_run_jira_watch", _never)

    rc = run_jira_command(
        ["watch", "--issue", "ABC-123", "--no-interactive-config"],
        environ={},  # no Jira settings at all
        config_loader=lambda: None,
        config_reader=lambda: {},
        stdin_isatty=lambda: False,
    )

    assert rc == EXIT_CONFIG_MISSING
    err = capsys.readouterr().err
    assert "Jira config is incomplete" in err


def test_watch_status_missing_file_returns_ok(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """watch-status with no status file prints a friendly message and exits 0."""
    monkeypatch.setenv("WHILLY_LOG_DIR", str(tmp_path / "logs"))

    rc = run_jira_command(["watch-status"], environ={})

    assert rc == 0
    err = capsys.readouterr().err
    assert "no watcher status found" in err


def test_watch_status_prints_human_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """watch-status prints key fields from the status file in human-readable form."""
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("WHILLY_LOG_DIR", str(log_dir))

    import os

    watch_dir = log_dir / "watch"
    watch_dir.mkdir(parents=True)
    live_pid = os.getpid()  # a definitely-live PID so state stays "running"
    status = {
        "state": "running",
        "pid": live_pid,
        "cycle_count": 7,
        "error_count": 1,
        "last_poll_at": "2026-06-12T10:00:00Z",
        "backoff_seconds": 0,
    }
    (watch_dir / "jira-watch-status.json").write_text(json.dumps(status), encoding="utf-8")

    rc = run_jira_command(["watch-status"], environ={})

    assert rc == 0
    out = capsys.readouterr().out
    assert "state=running" in out
    assert str(live_pid) in out
    assert "cycle_count" in out or "7" in out


def test_watch_status_reports_stale_for_dead_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A status file claiming state=running with a dead PID is reported as
    stale instead of trusted (WR-05) — in both human and --json output."""
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("WHILLY_LOG_DIR", str(log_dir))

    watch_dir = log_dir / "watch"
    watch_dir.mkdir(parents=True)
    status = {"state": "running", "pid": 99999999, "cycle_count": 7}
    (watch_dir / "jira-watch-status.json").write_text(json.dumps(status), encoding="utf-8")

    from whilly.cli import jira_watch_loop

    def _dead_kill(pid: int, sig: int) -> None:
        raise ProcessLookupError("no such process")

    monkeypatch.setattr(jira_watch_loop.os, "kill", _dead_kill)

    rc = run_jira_command(["watch-status"], environ={})
    assert rc == 0
    out = capsys.readouterr().out
    assert "state=stale (pid 99999999 not running)" in out

    rc = run_jira_command(["watch-status", "--json"], environ={})
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["state"] == "stale (pid 99999999 not running)"


def test_watch_status_handles_non_dict_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A status file holding a JSON array/scalar prints a friendly message
    instead of an AttributeError traceback (IN-04)."""
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("WHILLY_LOG_DIR", str(log_dir))

    watch_dir = log_dir / "watch"
    watch_dir.mkdir(parents=True)
    (watch_dir / "jira-watch-status.json").write_text("[1, 2, 3]", encoding="utf-8")

    rc = run_jira_command(["watch-status"], environ={})
    assert rc == 0
    err = capsys.readouterr().err
    assert "could not read status file" in err


def test_watch_status_json_flag_prints_valid_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """watch-status --json prints the status as valid JSON."""
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("WHILLY_LOG_DIR", str(log_dir))

    watch_dir = log_dir / "watch"
    watch_dir.mkdir(parents=True)
    status = {"state": "stopped", "cycle_count": 3}
    (watch_dir / "jira-watch-status.json").write_text(json.dumps(status), encoding="utf-8")

    rc = run_jira_command(["watch-status", "--json"], environ={})

    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["state"] == "stopped"
    assert parsed["cycle_count"] == 3
