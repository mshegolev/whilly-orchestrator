from __future__ import annotations

from whilly.cli.update import run_update_command
from whilly.update import UpdateCheckResult, UpdateInstallResult


def test_update_check_reports_newer_version_without_installing(capsys) -> None:
    def checker() -> UpdateCheckResult:
        return UpdateCheckResult("4.6.4", "4.7.0", True)

    rc = run_update_command(["check"], checker=checker)

    assert rc == 0
    captured = capsys.readouterr()
    assert "whilly 4.6.4 -> 4.7.0 available" in captured.out
    assert "whilly update install" in captured.out


def test_update_check_reports_unavailable_package_index(capsys) -> None:
    def checker() -> UpdateCheckResult:
        return UpdateCheckResult("4.6.4", None, None, error="network down")

    rc = run_update_command(["check"], checker=checker)

    assert rc == 1
    err = capsys.readouterr().err
    assert "could not check latest version" in err
    assert "network down" in err
    assert "python -m pip install --upgrade whilly-orchestrator" in err


def test_update_install_dry_run_prints_command_without_running(capsys) -> None:
    called = False

    def installer(*, dry_run: bool, installer: str) -> UpdateInstallResult:
        nonlocal called
        called = True
        assert dry_run is True
        assert installer == "pip"
        return UpdateInstallResult(
            command=("python", "-m", "pip", "install", "--upgrade", "whilly-orchestrator"),
            returncode=0,
            stdout="",
            stderr="",
            dry_run=True,
        )

    rc = run_update_command(["install", "--dry-run", "--installer", "pip"], installer=installer)

    assert called is True
    assert rc == 0
    assert "Would run: python -m pip install --upgrade whilly-orchestrator" in capsys.readouterr().out


def test_update_install_apply_reports_failure_with_command(capsys) -> None:
    def installer(*, dry_run: bool, installer: str) -> UpdateInstallResult:
        assert dry_run is False
        assert installer == "pip"
        return UpdateInstallResult(
            command=("python", "-m", "pip", "install", "--upgrade", "whilly-orchestrator"),
            returncode=23,
            stdout="",
            stderr="permission denied",
            dry_run=False,
        )

    rc = run_update_command(["install", "--installer", "pip"], installer=installer)

    assert rc == 23
    err = capsys.readouterr().err
    assert "update command failed" in err
    assert "permission denied" in err


def test_update_auto_off_does_not_check_or_install(monkeypatch, capsys) -> None:
    monkeypatch.delenv("WHILLY_UPDATE_MODE", raising=False)

    def checker() -> UpdateCheckResult:
        raise AssertionError("checker should not run")

    rc = run_update_command(["auto"], checker=checker)

    assert rc == 0
    assert "Automatic updates are off" in capsys.readouterr().out


def test_update_auto_install_policy_installs_when_newer(monkeypatch, capsys) -> None:
    monkeypatch.setenv("WHILLY_UPDATE_MODE", "install")
    installed: list[bool] = []

    def checker() -> UpdateCheckResult:
        return UpdateCheckResult("4.6.4", "4.7.0", True)

    def installer(*, dry_run: bool, installer: str) -> UpdateInstallResult:
        installed.append(dry_run)
        assert installer == "auto"
        return UpdateInstallResult(
            command=("python", "-m", "pip", "install", "--upgrade", "whilly-orchestrator"),
            returncode=0,
            stdout="updated",
            stderr="",
            dry_run=dry_run,
        )

    rc = run_update_command(["auto"], checker=checker, installer=installer)

    assert rc == 0
    assert installed == [False]
    assert "Auto-update installed whilly 4.7.0" in capsys.readouterr().out


def test_update_auto_check_policy_only_reports(monkeypatch, capsys) -> None:
    monkeypatch.setenv("WHILLY_UPDATE_MODE", "check")

    def checker() -> UpdateCheckResult:
        return UpdateCheckResult("4.6.4", "4.7.0", True)

    def installer(*, dry_run: bool, installer: str) -> UpdateInstallResult:
        raise AssertionError("installer should not run")

    rc = run_update_command(["auto"], checker=checker, installer=installer)

    assert rc == 0
    out = capsys.readouterr().out
    assert "Auto-update check: whilly 4.6.4 -> 4.7.0 available" in out
    assert "WHILLY_UPDATE_MODE=install" in out
