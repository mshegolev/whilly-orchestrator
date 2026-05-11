from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from tests import conftest as _conftest


def _patch_docker_probe(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> list[list[str]]:
    captured: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        if name in {"docker", "colima"}:
            return f"/usr/local/bin/{name}"
        return None

    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.append(list(args))
        return handler(list(args))

    monkeypatch.setattr(_conftest.shutil, "which", fake_which)
    monkeypatch.setattr(_conftest.subprocess, "run", fake_run)
    monkeypatch.setattr(_conftest.sys, "platform", "darwin")
    monkeypatch.setattr(_conftest, "_DOCKER_PROVIDER_START_ATTEMPTED", False)
    return captured


def test_docker_available_starts_colima_when_active_context_is_colima(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_info_calls = 0

    def handler(args: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal docker_info_calls
        if args == ["docker", "info"]:
            docker_info_calls += 1
            if docker_info_calls == 1:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="daemon down")
            return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")
        if args == ["docker", "context", "show"]:
            return subprocess.CompletedProcess(args, 0, stdout="colima\n", stderr="")
        if args[:4] == ["docker", "context", "inspect", "--format"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="unix:///Users/test/.colima/default/docker.sock\n",
                stderr="",
            )
        if args == ["colima", "start"]:
            return subprocess.CompletedProcess(args, 0, stdout="started", stderr="")
        raise AssertionError(f"unexpected command: {args!r}")

    captured = _patch_docker_probe(monkeypatch, handler)

    assert _conftest.docker_available() is True
    assert ["colima", "start"] in captured
    assert docker_info_calls == 2


def test_docker_availability_reason_names_colima_command_when_autostart_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHILLY_TEST_DOCKER_AUTOSTART", "0")

    def handler(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args == ["docker", "info"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="Cannot connect")
        if args == ["docker", "context", "show"]:
            return subprocess.CompletedProcess(args, 0, stdout="colima\n", stderr="")
        if args[:4] == ["docker", "context", "inspect", "--format"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="unix:///Users/test/.colima/default/docker.sock\n",
                stderr="",
            )
        raise AssertionError(f"unexpected command: {args!r}")

    captured = _patch_docker_probe(monkeypatch, handler)

    availability = _conftest.docker_availability()

    assert availability.available is False
    assert ["colima", "start"] not in captured
    assert "colima start" in availability.reason
    assert "docker ps" in availability.reason
