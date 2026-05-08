"""Integration tests for the ``whilly rollback`` CLI surface."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from whilly.cli.rollback import EXIT_BLOCKED, EXIT_OK, run_rollback_command
from whilly.rollback.service import build_preflight_report, confirmation_phrase


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, filename: str, content: str, message: str) -> str:
    target = repo / filename
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _git(repo, "branch", "-M", "main")
    _git(repo, "config", "user.name", "Whilly Test")
    _git(repo, "config", "user.email", "whilly-test@example.invalid")
    _commit(repo, "app.txt", "base\n", "initial commit")
    return repo


def _json_output(capsys: pytest.CaptureFixture[str]) -> object:
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def _create_point(repo: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    rc = run_rollback_command(["create", "--repo", str(repo), "--operation", "merge", "--json"])
    assert rc == EXIT_OK
    payload = _json_output(capsys)
    assert isinstance(payload, dict)
    return payload


def test_create_and_list_rollback_points(git_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    point = _create_point(git_repo, capsys)
    assert str(point["name"]).startswith("whilly/rollback/main/")
    assert point["target_sha"] == _git(git_repo, "rev-parse", "HEAD")
    assert point["branch"] == "main"

    rc = run_rollback_command(["list", "--repo", str(git_repo), "--json"])
    assert rc == EXIT_OK
    payload = _json_output(capsys)
    assert isinstance(payload, list)
    assert [entry["name"] for entry in payload] == [point["name"]]


def test_create_passes_custom_message_to_annotated_tag(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    message = "operator supplied rollback note"
    rc = run_rollback_command(["create", "--repo", str(git_repo), "--message", message, "--json"])
    assert rc == EXIT_OK
    point = _json_output(capsys)
    assert isinstance(point, dict)

    tag_message = _git(git_repo, "for-each-ref", f"refs/tags/{point['name']}", "--format=%(contents)")
    assert message in tag_message


def test_preflight_json_reports_dirty_blocker(git_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (git_repo / "app.txt").write_text("dirty\n", encoding="utf-8")

    rc = run_rollback_command(["preflight", "push", "--repo", str(git_repo), "--json"])
    assert rc == EXIT_BLOCKED
    payload = _json_output(capsys)
    assert isinstance(payload, dict)
    assert payload["ok"] is False
    assert payload["dirty"] is True
    assert "dirty worktree" in payload["blockers"]


def test_restore_dry_run_outputs_confirmation_phrase(git_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    point = _create_point(git_repo, capsys)
    original_head = str(point["target_sha"])
    latest_head = _commit(git_repo, "app.txt", "later\n", "later commit")

    rc = run_rollback_command(["restore", str(point["name"]), "--repo", str(git_repo), "--dry-run", "--json"])
    assert rc == EXIT_OK
    payload = _json_output(capsys)
    assert isinstance(payload, dict)
    assert payload["dry_run"] is True
    assert payload["reset_performed"] is False
    assert payload["target_sha"] == original_head
    assert payload["confirmation_phrase"] == f"restore {original_head[:12]} to main"
    assert _git(git_repo, "rev-parse", "HEAD") == latest_head


def test_restore_requires_exact_confirmation(git_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    point = _create_point(git_repo, capsys)
    latest_head = _commit(git_repo, "app.txt", "later\n", "later commit")

    rc = run_rollback_command(["restore", str(point["name"]), "--repo", str(git_repo)])
    captured = capsys.readouterr()
    assert rc == EXIT_BLOCKED
    assert captured.out == ""
    assert "rollback restore: confirmation required" in captured.err
    assert _git(git_repo, "rev-parse", "HEAD") == latest_head

    rc = run_rollback_command(["restore", str(point["name"]), "--repo", str(git_repo), "--confirm", "yes"])
    captured = capsys.readouterr()
    assert rc == EXIT_BLOCKED
    assert captured.out == ""
    assert "confirmation required" in captured.err
    assert _git(git_repo, "rev-parse", "HEAD") == latest_head


def test_restore_exact_confirmation_resets_head_when_clean(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    point = _create_point(git_repo, capsys)
    original_head = str(point["target_sha"])
    _commit(git_repo, "app.txt", "later\n", "later commit")
    expected_phrase = confirmation_phrase(
        build_preflight_report(git_repo, operation="restore", target_ref=str(point["name"])),
        original_head,
    )

    rc = run_rollback_command(
        ["restore", str(point["name"]), "--repo", str(git_repo), "--confirm", expected_phrase, "--json"]
    )
    assert rc == EXIT_OK
    payload = _json_output(capsys)
    assert isinstance(payload, dict)
    assert payload["reset_performed"] is True
    assert payload["target_sha"] == original_head
    assert _git(git_repo, "rev-parse", "HEAD") == original_head
