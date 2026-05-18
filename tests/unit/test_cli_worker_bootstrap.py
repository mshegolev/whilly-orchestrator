"""Unit tests for ``whilly worker bootstrap`` (PRD §Epic H Item 22)."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from whilly.cli import worker_launch

_TRACKED_ENV = (
    "WHILLY_SERVER_URL",
    "WHILLY_WORKER_BOOTSTRAP_TOKEN",
    "WHILLY_PLAN_ID",
    "WHILLY_CONTROL_URL",
    "WHILLY_MODEL",
    "CLAUDE_BIN",
    "WHILLY_WORKER_CONFIG",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import os as _os

    snap = {k: _os.environ.get(k) for k in _TRACKED_ENV}
    for k in _TRACKED_ENV:
        monkeypatch.delenv(k, raising=False)
    try:
        yield
    finally:
        for k, v in snap.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    return tmp_path / "worker.json"


def _stub_register(*args: Any, **kwargs: Any) -> Any:
    async def _impl(control_url: str, bootstrap_token: str, hostname: str) -> tuple[str, str]:
        return "w-bootstrap-test", "tk-bootstrap-test"

    return _impl


def test_bootstrap_non_interactive_with_env_vars_succeeds(
    monkeypatch: pytest.MonkeyPatch, cfg_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC: --non-interactive accepts inputs via env vars."""
    monkeypatch.setenv("WHILLY_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("WHILLY_WORKER_BOOTSTRAP_TOKEN", "boot-tk")
    monkeypatch.setenv("WHILLY_PLAN_ID", "demo-plan")
    monkeypatch.setattr(worker_launch, "_register", _stub_register())
    # _resolve_claude_bin needs SOME claude binary; patch shutil.which.
    monkeypatch.setattr(worker_launch.shutil, "which", lambda name: "/usr/bin/claude")
    rc = worker_launch.run_bootstrap_command(["--non-interactive", "--config", str(cfg_path)])
    assert rc == worker_launch.EXIT_OK
    config = json.loads(cfg_path.read_text())
    cache_key = worker_launch._config_key("http://127.0.0.1:8000", "demo-plan")
    assert cache_key in config["workers"]
    out = capsys.readouterr().out
    assert "bootstrap complete" in out
    assert "demo-plan" in out


def test_bootstrap_non_interactive_missing_env_fails(cfg_path: Path) -> None:
    """--non-interactive without env vars surfaces clear error."""
    rc = worker_launch.run_bootstrap_command(["--non-interactive", "--config", str(cfg_path)])
    assert rc == worker_launch.EXIT_ENVIRONMENT_ERROR


def test_bootstrap_existing_config_without_force_refuses(cfg_path: Path) -> None:
    """AC: second run on configured box detects existing config and refuses
    without --force.
    """
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"workers": {"k": {"worker_id": "w1"}}}))
    rc = worker_launch.run_bootstrap_command(["--non-interactive", "--config", str(cfg_path)])
    assert rc == worker_launch.EXIT_ENVIRONMENT_ERROR


def test_bootstrap_force_overwrites_existing_config(monkeypatch: pytest.MonkeyPatch, cfg_path: Path) -> None:
    """--force lets bootstrap proceed even when config exists."""
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"workers": {"k": {"worker_id": "w-old"}}}))
    monkeypatch.setenv("WHILLY_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("WHILLY_WORKER_BOOTSTRAP_TOKEN", "boot-tk")
    monkeypatch.setenv("WHILLY_PLAN_ID", "demo-plan")
    monkeypatch.setattr(worker_launch, "_register", _stub_register())
    monkeypatch.setattr(worker_launch.shutil, "which", lambda name: "/usr/bin/claude")
    rc = worker_launch.run_bootstrap_command(["--non-interactive", "--force", "--config", str(cfg_path)])
    assert rc == worker_launch.EXIT_OK


def test_bootstrap_interactive_reads_from_stdin(monkeypatch: pytest.MonkeyPatch, cfg_path: Path) -> None:
    """Without --non-interactive, missing values are prompted from stdin."""
    fake_stdin = io.StringIO("http://127.0.0.1:8000\nboot-tk-from-prompt\ndemo-plan\n")
    monkeypatch.setattr(worker_launch.sys, "stdin", fake_stdin)
    monkeypatch.setattr(worker_launch, "_register", _stub_register())
    monkeypatch.setattr(worker_launch.shutil, "which", lambda name: "/usr/bin/claude")
    rc = worker_launch.run_bootstrap_command(["--config", str(cfg_path)])
    assert rc == worker_launch.EXIT_OK
    config = json.loads(cfg_path.read_text())
    assert "workers" in config


def test_bootstrap_with_explicit_plan_id_positional(monkeypatch: pytest.MonkeyPatch, cfg_path: Path) -> None:
    """A positional plan_id overrides any prompt / env."""
    monkeypatch.setenv("WHILLY_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("WHILLY_WORKER_BOOTSTRAP_TOKEN", "boot-tk")
    # Set a different plan_id in env to verify the positional wins.
    monkeypatch.setenv("WHILLY_PLAN_ID", "env-plan")
    monkeypatch.setattr(worker_launch, "_register", _stub_register())
    monkeypatch.setattr(worker_launch.shutil, "which", lambda name: "/usr/bin/claude")
    rc = worker_launch.run_bootstrap_command(["explicit-plan", "--non-interactive", "--config", str(cfg_path)])
    assert rc == worker_launch.EXIT_OK
    config = json.loads(cfg_path.read_text())
    assert worker_launch._config_key("http://127.0.0.1:8000", "explicit-plan") in config["workers"]


# ─── End-to-end: subcommand dispatch via the main CLI ───────────────────────


def test_bootstrap_dispatched_via_main_cli(monkeypatch: pytest.MonkeyPatch, cfg_path: Path) -> None:
    """Verify `whilly worker bootstrap` routes to run_bootstrap_command."""
    monkeypatch.setenv("WHILLY_SERVER_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("WHILLY_WORKER_BOOTSTRAP_TOKEN", "boot-tk")
    monkeypatch.setattr(worker_launch, "_register", _stub_register())
    monkeypatch.setattr(worker_launch.shutil, "which", lambda name: "/usr/bin/claude")
    from whilly.cli import main as cli_main

    rc = cli_main(["worker", "bootstrap", "demo-plan", "--non-interactive", "--config", str(cfg_path)])
    assert rc == worker_launch.EXIT_OK


# Silence unused import lint
_ = patch
