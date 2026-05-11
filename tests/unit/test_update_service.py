from __future__ import annotations

import io
import json
import subprocess

from whilly.update import (
    InstallerKind,
    UpdateCheckResult,
    UpdateMode,
    build_install_command,
    check_for_update,
    compare_versions,
    fetch_latest_version,
    resolve_update_mode,
    run_package_update,
)


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> io.BytesIO:
        return io.BytesIO(self._body)

    def __exit__(self, *_exc: object) -> None:
        return None


def test_fetch_latest_version_parses_pypi_json_payload() -> None:
    def opener(url: str, *, timeout: float) -> _Response:
        assert url == "https://pypi.org/pypi/whilly-orchestrator/json"
        assert timeout == 5.0
        return _Response({"info": {"version": "4.7.0"}})

    assert fetch_latest_version(opener=opener) == "4.7.0"


def test_compare_versions_handles_double_digit_minor_versions() -> None:
    assert compare_versions("4.10.0", "4.9.9") > 0
    assert compare_versions("4.6.4", "4.6.4") == 0
    assert compare_versions("4.6.3", "4.6.4") < 0


def test_check_for_update_reports_newer_version_without_mutating_environment() -> None:
    result = check_for_update(installed_version="4.6.4", latest_version_loader=lambda: "4.7.0")

    assert result == UpdateCheckResult(
        installed_version="4.6.4",
        latest_version="4.7.0",
        update_available=True,
        error=None,
    )


def test_check_for_update_returns_error_when_index_is_unavailable() -> None:
    def boom() -> str:
        raise OSError("network down")

    result = check_for_update(installed_version="4.6.4", latest_version_loader=boom)

    assert result.installed_version == "4.6.4"
    assert result.latest_version is None
    assert result.update_available is None
    assert "network down" in result.error


def test_build_install_command_prefers_pipx_when_running_inside_pipx() -> None:
    command = build_install_command(
        installer="auto",
        environ={"PIPX_HOME": "/tmp/pipx"},
        python_executable="/venv/bin/python",
        pipx_executable="/usr/local/bin/pipx",
    )

    assert command.kind is InstallerKind.PIPX
    assert command.argv == ("/usr/local/bin/pipx", "upgrade", "whilly-orchestrator")


def test_build_install_command_defaults_to_current_python_pip() -> None:
    command = build_install_command(
        installer="auto",
        environ={},
        python_executable="/venv/bin/python",
        pipx_executable=None,
    )

    assert command.kind is InstallerKind.PIP
    assert command.argv == ("/venv/bin/python", "-m", "pip", "install", "--upgrade", "whilly-orchestrator")


def test_run_package_update_dry_run_does_not_spawn_process() -> None:
    called = False

    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        raise AssertionError("runner should not be called")

    result = run_package_update(
        dry_run=True,
        installer="pip",
        python_executable="/venv/bin/python",
        runner=runner,
    )

    assert called is False
    assert result.dry_run is True
    assert result.returncode == 0
    assert result.command == ("/venv/bin/python", "-m", "pip", "install", "--upgrade", "whilly-orchestrator")


def test_run_package_update_apply_invokes_subprocess_runner() -> None:
    captured: list[tuple[str, ...]] = []

    def runner(args: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="updated", stderr="")

    result = run_package_update(
        dry_run=False,
        installer="pip",
        python_executable="/venv/bin/python",
        runner=runner,
    )

    assert captured == [("/venv/bin/python", "-m", "pip", "install", "--upgrade", "whilly-orchestrator")]
    assert result.returncode == 0
    assert result.stdout == "updated"


def test_resolve_update_mode_defaults_to_off_and_rejects_unknown_values() -> None:
    assert resolve_update_mode({}, explicit_mode=None) is UpdateMode.OFF
    assert resolve_update_mode({"WHILLY_UPDATE_MODE": "check"}, explicit_mode=None) is UpdateMode.CHECK
    assert resolve_update_mode({}, explicit_mode="install") is UpdateMode.INSTALL
    assert resolve_update_mode({"WHILLY_UPDATE_MODE": "garbage"}, explicit_mode=None) is UpdateMode.OFF
