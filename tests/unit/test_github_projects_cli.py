from __future__ import annotations

import json
from typing import Any

from whilly.cli import github_projects as cli


class _FakeConverter:
    instances: list["_FakeConverter"] = []

    def __init__(self, *, sync_config: Any, check_gh_cli: bool = True) -> None:
        self.sync_config = sync_config
        self.check_gh_cli = check_gh_cli
        self.calls: list[tuple[Any, ...]] = []
        _FakeConverter.instances.append(self)

    def sync_todo_items(
        self,
        project_url: str,
        owner: str,
        repo: str,
        *,
        output_file: str,
        create_draft_issues: bool = True,
    ) -> dict[str, int]:
        self.calls.append(("sync_todo_items", project_url, owner, repo, output_file, create_draft_issues))
        return {"created_count": 1, "skipped_count": 0, "synced_count": 1, "total_todo_items": 1}

    def sync_status_changes(self, issue_number: int, status: str) -> bool:
        self.calls.append(("sync_status_changes", issue_number, status))
        return True

    def get_sync_status(self) -> dict[str, str]:
        self.calls.append(("get_sync_status",))
        return {"project_url": "https://github.com/users/test/projects/1"}


def _install_fake_converter(monkeypatch) -> None:
    _FakeConverter.instances = []
    monkeypatch.setattr(cli, "GitHubProjectsConverter", _FakeConverter)


def test_sync_todo_routes_to_converter(monkeypatch, tmp_path, capsys) -> None:
    _install_fake_converter(monkeypatch)
    state_file = tmp_path / "sync.json"

    rc = cli.run_github_projects_command(
        [
            "--state-file",
            str(state_file),
            "sync-todo",
            "https://github.com/users/test/projects/1",
            "--repo",
            "test/repo",
            "--output",
            "tasks.json",
            "--existing-only",
        ]
    )

    assert rc == 0
    fake = _FakeConverter.instances[0]
    assert fake.sync_config.sync_state_file == str(state_file)
    assert fake.calls == [
        ("sync_todo_items", "https://github.com/users/test/projects/1", "test", "repo", "tasks.json", False)
    ]
    assert json.loads(capsys.readouterr().out)["created_count"] == 1


def test_sync_status_routes_to_converter(monkeypatch, tmp_path) -> None:
    _install_fake_converter(monkeypatch)

    rc = cli.run_github_projects_command(["--state-file", str(tmp_path / "sync.json"), "sync-status", "123", "Done"])

    assert rc == 0
    assert _FakeConverter.instances[0].calls == [("sync_status_changes", 123, "Done")]


def test_status_prints_sync_status(monkeypatch, capsys) -> None:
    _install_fake_converter(monkeypatch)

    rc = cli.run_github_projects_command(["status"])

    assert rc == 0
    assert _FakeConverter.instances[0].check_gh_cli is False
    assert json.loads(capsys.readouterr().out)["project_url"] == "https://github.com/users/test/projects/1"
