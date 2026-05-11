"""Unit tests for ``whilly quick-setup`` local bootstrap command."""

from __future__ import annotations

from pathlib import Path

import pytest

from whilly.cli import main as dispatch_main


def test_top_level_whilly_help_lists_quick_setup(capsys: pytest.CaptureFixture[str]) -> None:
    code = dispatch_main(["--help"])

    assert code == 0
    captured = capsys.readouterr()
    assert "quick-setup" in captured.out


def _next_secret_factory(*values: str):
    items = iter(values)

    def _factory(_nbytes: int) -> str:
        return next(items)

    return _factory


def test_quick_setup_writes_env_files_with_generated_secrets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from whilly.cli.quick_setup import run_quick_setup_command

    code = run_quick_setup_command(
        ["--yes", "--repo-root", str(tmp_path), "--compose-command", "docker-compose"],
        token_factory=_next_secret_factory("bootstrap-secret", "postgres-secret"),
    )

    assert code == 0
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    worker_env = (tmp_path / ".env.worker").read_text(encoding="utf-8")
    assert "WHILLY_WORKER_BOOTSTRAP_TOKEN=bootstrap-secret" in env
    assert "WHILLY_WORKER_BOOTSTRAP_TOKEN=bootstrap-secret" in worker_env
    assert "POSTGRES_PASSWORD=postgres-secret" in env
    assert "demo-bootstrap" not in env + worker_env

    captured = capsys.readouterr()
    assert "docker-compose --env-file .env -f docker-compose.control-plane.yml up -d" in captured.out
    assert "docker-compose --env-file .env.worker -f docker-compose.worker.yml up -d" in captured.out
    assert "bootstrap-secret" not in captured.out
    assert "postgres-secret" not in captured.out


def test_quick_setup_refuses_to_overwrite_existing_env_without_force(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from whilly.cli.quick_setup import run_quick_setup_command

    existing = tmp_path / ".env"
    existing.write_text("EXISTING=1\n", encoding="utf-8")

    code = run_quick_setup_command(
        ["--yes", "--repo-root", str(tmp_path)],
        compose_probe=lambda: "docker compose",
        token_factory=_next_secret_factory("bootstrap-secret", "postgres-secret"),
    )

    assert code == 2
    assert existing.read_text(encoding="utf-8") == "EXISTING=1\n"
    assert not (tmp_path / ".env.worker").exists()
    captured = capsys.readouterr()
    assert "--force" in captured.err
    assert ".env" in captured.err


def test_quick_setup_print_only_writes_nothing_and_prints_redacted_preview(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from whilly.cli.quick_setup import run_quick_setup_command

    code = run_quick_setup_command(
        ["--print-only", "--repo-root", str(tmp_path), "--compose-command", "docker compose"],
        token_factory=_next_secret_factory("bootstrap-secret", "postgres-secret"),
    )

    assert code == 0
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / ".env.worker").exists()
    captured = capsys.readouterr()
    assert "# .env" in captured.out
    assert "WHILLY_WORKER_BOOTSTRAP_TOKEN=<generated>" in captured.out
    assert "docker compose --env-file .env -f docker-compose.control-plane.yml up -d" in captured.out
    assert "bootstrap-secret" not in captured.out
    assert "postgres-secret" not in captured.out


def test_quick_setup_uses_detected_docker_compose_fallback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from whilly.cli.quick_setup import run_quick_setup_command

    code = run_quick_setup_command(
        ["--yes", "--repo-root", str(tmp_path)],
        compose_probe=lambda: "docker-compose",
        token_factory=_next_secret_factory("bootstrap-secret", "postgres-secret"),
    )

    assert code == 0
    captured = capsys.readouterr()
    assert "Compose command: docker-compose" in captured.out
    assert "docker-compose --env-file .env -f docker-compose.control-plane.yml up -d" in captured.out


def test_quick_setup_prints_colima_guidance(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from whilly.cli.quick_setup import run_quick_setup_command

    code = run_quick_setup_command(
        ["--print-only", "--repo-root", str(tmp_path), "--docker-provider", "colima"],
        compose_probe=lambda: "docker compose",
        token_factory=_next_secret_factory("bootstrap-secret", "postgres-secret"),
    )

    assert code == 0
    captured = capsys.readouterr()
    assert "colima start" in captured.out
    assert "docker context use colima" in captured.out
    assert "TESTCONTAINERS_RYUK_DISABLED=true" in captured.out


def test_main_dispatches_quick_setup_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    from whilly.cli import quick_setup as cli_quick_setup

    captured: dict[str, object] = {}

    def _fake_runner(argv: object) -> int:
        captured["argv"] = list(argv) if isinstance(argv, list) else argv
        return 0

    monkeypatch.setattr(cli_quick_setup, "run_quick_setup_command", _fake_runner)

    code = dispatch_main(["quick-setup", "--yes"])

    assert code == 0
    assert captured["argv"] == ["--yes"]
