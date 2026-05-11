"""Unit tests for the per-process one-shot ``--insecure`` warning.

VAL-M2-WORKER-INSECURE-901 / VAL-M2-WORKER-INSECURE-007 require the
plain-HTTP-to-non-loopback warning emitted by the worker CLI to fire at
most once per Python process. Two back-to-back invocations of
``whilly worker connect --insecure http://...`` (and the legacy
``whilly-worker --connect ...`` path) must produce ``warning_count == 1``
on stderr.

The test resets the module-level latch in a fixture so each test starts
clean.
"""

from __future__ import annotations

from typing import Any

import pytest

from whilly import secrets as whilly_secrets
from whilly.adapters.transport.schemas import RegisterResponse
from whilly.cli import worker as cli_worker
from whilly.cli.worker import EXIT_OK, run_connect_command, run_worker_command


@pytest.fixture(autouse=True)
def _reset_insecure_warn_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_worker, "_INSECURE_WARNING_EMITTED", False, raising=True)


@pytest.fixture
def patched_xdg(tmp_path, monkeypatch: pytest.MonkeyPatch) -> str:
    cfg = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return str(cfg)


class _FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    fk = _FakeKeyring()
    monkeypatch.setattr(whilly_secrets, "_set_keyring_password", fk.set_password)
    monkeypatch.setattr(whilly_secrets, "_get_keyring_password", fk.get_password)
    return fk


def _patch_register(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(connect_url: str, bootstrap_token: str, hostname: str) -> RegisterResponse:
        return RegisterResponse(worker_id="w-aaaaaaaa", token="bearer-xyz")

    monkeypatch.setattr(cli_worker, "_async_register", _fake)


def _patch_execvp(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(file: str, args: list[str]) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(cli_worker.os, "execvp", _fake)


def _patch_async_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(**_kwargs: Any) -> Any:
        from whilly.worker.remote import RemoteWorkerStats

        return RemoteWorkerStats()

    monkeypatch.setattr(cli_worker, "_async_worker", _fake)


def _count_insecure_warnings(stderr: str) -> int:
    return sum(1 for line in stderr.splitlines() if "warning" in line.lower() and "plain HTTP" in line)


def test_connect_insecure_warning_emits_once_per_process(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    _patch_register(monkeypatch)
    _patch_execvp(monkeypatch)

    argv = [
        "http://example.com:8000",
        "--bootstrap-token",
        "BT",
        "--plan",
        "demo",
        "--insecure",
    ]

    code1 = run_connect_command(argv)
    code2 = run_connect_command(argv)
    assert code1 == EXIT_OK
    assert code2 == EXIT_OK

    err = capsys.readouterr().err
    assert _count_insecure_warnings(err) == 1
    assert "example.com" in err


def test_connect_first_invocation_emits_full_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    _patch_register(monkeypatch)
    _patch_execvp(monkeypatch)
    code = run_connect_command(
        [
            "http://example.com:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
            "--insecure",
        ]
    )
    assert code == EXIT_OK
    err = capsys.readouterr().err
    assert "warning — using plain HTTP to non-loopback host 'example.com'" in err
    assert "(--insecure). Prefer HTTPS in production." in err


def test_legacy_worker_insecure_warning_emits_once_per_process(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_async_worker(monkeypatch)
    for var in ("WHILLY_CONTROL_URL", "WHILLY_WORKER_TOKEN", "WHILLY_PLAN_ID"):
        monkeypatch.delenv(var, raising=False)

    argv = [
        "--connect",
        "http://example.com:8000",
        "--token",
        "X",
        "--plan",
        "p",
        "--insecure",
    ]

    code1 = run_worker_command(argv)
    code2 = run_worker_command(argv)
    assert code1 == EXIT_OK
    assert code2 == EXIT_OK

    err = capsys.readouterr().err
    assert _count_insecure_warnings(err) == 1


def test_warning_latch_resets_between_tests_via_fixture(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    _patch_register(monkeypatch)
    _patch_execvp(monkeypatch)
    assert cli_worker._INSECURE_WARNING_EMITTED is False
    code = run_connect_command(
        [
            "http://example.com:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
            "--insecure",
        ]
    )
    assert code == EXIT_OK
    assert cli_worker._INSECURE_WARNING_EMITTED is True
    err = capsys.readouterr().err
    assert _count_insecure_warnings(err) == 1


def test_mixed_connect_then_legacy_only_warns_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    _patch_register(monkeypatch)
    _patch_execvp(monkeypatch)
    _patch_async_worker(monkeypatch)
    for var in ("WHILLY_CONTROL_URL", "WHILLY_WORKER_TOKEN", "WHILLY_PLAN_ID"):
        monkeypatch.delenv(var, raising=False)

    code1 = run_connect_command(
        [
            "http://example.com:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
            "--insecure",
        ]
    )
    code2 = run_worker_command(
        [
            "--connect",
            "http://example.com:8000",
            "--token",
            "X",
            "--plan",
            "p",
            "--insecure",
        ]
    )
    assert code1 == EXIT_OK
    assert code2 == EXIT_OK

    err = capsys.readouterr().err
    assert _count_insecure_warnings(err) == 1
