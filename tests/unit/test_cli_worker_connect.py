"""Unit tests for ``whilly worker connect <url>`` (M1 worker bootstrap).

What we cover (per validation contract VAL-M1-CONNECT-* / VAL-M1-INSECURE-*):

- URL classification: scheme guard (https, http+loopback, http+non-loopback),
  port range, missing-scheme rejection, path rejection, trailing-slash
  canonicalisation.
- Loopback recognition matrix: 127.0.0.0/8, ::1, ``localhost`` accepted;
  ``0.0.0.0``, ``[::]``, RFC1918 ranges, ``localhost.evil.example``
  all rejected.
- Bootstrap-token resolution: ``--bootstrap-token`` flag wins over env;
  whitespace-only tokens rejected before any HTTP call.
- ``--no-keychain`` skips the keychain write but still emits the stdout
  contract and execs into ``whilly-worker``.
- Storage backend: keychain success path, file fallback when keyring
  raises, atomic write of the fallback file, parent-dir mode 0700 / file
  mode 0600, multi-URL coexistence (URL_A + URL_B both retrievable).
- Stdout contract: ``worker_id: ...\\ntoken: ...\\n`` with no banners
  between the two lines (pipeable through awk/grep).
- Diagnostics on stderr never leak the plaintext bearer.
- Exec contract: ``os.execvp`` is called with ``whilly-worker --connect
  --token --plan`` argv; ``--insecure`` propagates when set; missing
  ``whilly-worker`` (FileNotFoundError) surfaces as exit 1 with a
  pointer for manual recovery.

How we isolate from the network and the OS keyring:

- ``whilly.cli.worker._async_register`` is monkeypatched to return a
  canned :class:`RegisterResponse` (no httpx, no socket).
- ``os.execvp`` is monkeypatched to capture the argv and return without
  exec'ing (otherwise the test runner itself would be replaced).
- ``whilly.secrets._set_keyring_password`` /
  ``whilly.secrets._get_keyring_password`` are monkeypatched to talk to
  an in-memory dict — the real OS keyring is never touched.
- The fallback file lives under a ``tmp_path`` via
  ``XDG_CONFIG_HOME`` so each test starts clean.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from whilly import secrets as whilly_secrets
from whilly.adapters.transport.client import AuthError, ServerError
from whilly.adapters.transport.schemas import RegisterResponse
from whilly.cli import worker as cli_worker
from whilly.cli.worker import (
    BOOTSTRAP_TOKEN_ENV,
    EXIT_CONNECT_ERROR,
    EXIT_OK,
    InsecureSchemeError,
    UrlValidationError,
    _is_loopback_host,
    build_connect_parser,
    classify_control_url,
    enforce_scheme_guard,
    run_connect_command,
)


# ---------------------------------------------------------------------------
# Test helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_xdg(tmp_path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point XDG_CONFIG_HOME at a fresh tmp dir for fallback-file isolation."""
    cfg = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return str(cfg)


class _FakeKeyring:
    """In-memory replacement for the OS keyring (per-test instance)."""

    def __init__(self, *, raise_on_set: bool = False) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.set_calls: list[tuple[str, str]] = []
        self.raise_on_set = raise_on_set

    def set_password(self, service: str, username: str, password: str) -> None:
        self.set_calls.append((service, username))
        if self.raise_on_set:
            raise RuntimeError("simulated keyring backend unavailable")
        self.store[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    fk = _FakeKeyring()
    monkeypatch.setattr(whilly_secrets, "_set_keyring_password", fk.set_password)
    monkeypatch.setattr(whilly_secrets, "_get_keyring_password", fk.get_password)
    return fk


@pytest.fixture
def fake_keyring_failing(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    fk = _FakeKeyring(raise_on_set=True)
    monkeypatch.setattr(whilly_secrets, "_set_keyring_password", fk.set_password)
    monkeypatch.setattr(whilly_secrets, "_get_keyring_password", fk.get_password)
    return fk


def _patch_register(
    monkeypatch: pytest.MonkeyPatch,
    *,
    worker_id: str = "w-12345678",
    token: str = "bearer-deadbeef",
) -> dict[str, Any]:
    """Replace ``cli_worker._async_register`` with a coroutine returning a canned response."""
    captured: dict[str, Any] = {}

    async def _fake(connect_url: str, bootstrap_token: str, hostname: str) -> RegisterResponse:
        captured["connect_url"] = connect_url
        captured["bootstrap_token"] = bootstrap_token
        captured["hostname"] = hostname
        return RegisterResponse(worker_id=worker_id, token=token)

    monkeypatch.setattr(cli_worker, "_async_register", _fake)
    return captured


def _patch_execvp(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    """Replace ``os.execvp`` (via ``whilly.cli.worker.os.execvp``) with a recorder.

    Production calls ``os.execvp`` to replace the process; in tests we
    capture the argv tuple and return so the test runner survives.
    """
    calls: list[tuple[str, list[str]]] = []

    def _fake(file: str, args: list[str]) -> None:  # noqa: ARG001
        calls.append((file, list(args)))

    monkeypatch.setattr(cli_worker.os, "execvp", _fake)
    return calls


def _patch_execvp_missing(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    """Replace ``os.execvp`` with a stub that raises FileNotFoundError."""
    calls: list[tuple[str, list[str]]] = []

    def _fake(file: str, args: list[str]) -> None:
        calls.append((file, list(args)))
        raise FileNotFoundError(f"[Errno 2] No such file or directory: {file!r}")

    monkeypatch.setattr(cli_worker.os, "execvp", _fake)
    return calls


# ---------------------------------------------------------------------------
# URL classification + loopback detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.10.20.30",
        "::1",
        "[::1]",
        "localhost",
        "LOCALHOST",
    ],
)
def test_loopback_hosts_recognised(host: str) -> None:
    assert _is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "::",
        "[::]",
        "192.168.1.10",
        "10.0.0.1",
        "172.16.0.1",
        "169.254.42.42",
        "localhost.evil.example",
        "control",
        "8.8.8.8",
        "",
    ],
)
def test_non_loopback_hosts_not_exempted(host: str) -> None:
    assert _is_loopback_host(host) is False


def test_classify_control_url_happy_path() -> None:
    scheme, host, port = classify_control_url("http://127.0.0.1:8000")
    assert scheme == "http"
    assert host == "127.0.0.1"
    assert port == 8000


def test_classify_control_url_strips_trailing_slash() -> None:
    scheme, host, port = classify_control_url("http://127.0.0.1:8000/")
    assert (scheme, host, port) == ("http", "127.0.0.1", 8000)


def test_classify_control_url_rejects_path() -> None:
    with pytest.raises(UrlValidationError, match="must not include a path"):
        classify_control_url("http://127.0.0.1:8000/api")


def test_classify_control_url_rejects_missing_scheme() -> None:
    with pytest.raises(UrlValidationError, match="missing a scheme"):
        classify_control_url("127.0.0.1:8000")


def test_classify_control_url_rejects_empty() -> None:
    with pytest.raises(UrlValidationError, match="empty"):
        classify_control_url("   ")


def test_classify_control_url_rejects_out_of_range_port() -> None:
    with pytest.raises(UrlValidationError):
        classify_control_url("http://127.0.0.1:99999")


def test_enforce_scheme_guard_rejects_plain_http_to_remote() -> None:
    with pytest.raises(InsecureSchemeError, match="--insecure"):
        enforce_scheme_guard("http://192.0.2.10:8000", insecure=False)


def test_enforce_scheme_guard_allows_plain_http_with_insecure() -> None:
    scheme, host, port = enforce_scheme_guard("http://192.0.2.10:8000", insecure=True)
    assert scheme == "http"
    assert host == "192.0.2.10"
    assert port == 8000


def test_enforce_scheme_guard_allows_https_anywhere() -> None:
    scheme, host, port = enforce_scheme_guard("https://example.com:443", insecure=False)
    assert scheme == "https"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"])
def test_enforce_scheme_guard_allows_loopback_without_insecure(url: str) -> None:
    enforce_scheme_guard(url, insecure=False)  # no exception


@pytest.mark.parametrize("url", ["http://0.0.0.0:8000", "http://[::]:8000"])
def test_enforce_scheme_guard_rejects_wildcard_binds(url: str) -> None:
    with pytest.raises(InsecureSchemeError):
        enforce_scheme_guard(url, insecure=False)


# ---------------------------------------------------------------------------
# argparse surface
# ---------------------------------------------------------------------------


def test_build_connect_parser_accepts_minimum_args() -> None:
    parser = build_connect_parser()
    args = parser.parse_args(["http://127.0.0.1:8000"])
    assert args.control_url == "http://127.0.0.1:8000"
    assert args.bootstrap_token is None
    assert args.plan_id is None
    assert args.hostname is None
    assert args.no_keychain is False
    assert args.insecure is False


def test_build_connect_parser_accepts_all_flags() -> None:
    parser = build_connect_parser()
    args = parser.parse_args(
        [
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
            "--hostname",
            "h",
            "--no-keychain",
            "--keychain-service",
            "whilly-test",
            "--insecure",
        ]
    )
    assert args.bootstrap_token == "BT"
    assert args.plan_id == "demo"
    assert args.hostname == "h"
    assert args.no_keychain is True
    assert args.keychain_service == "whilly-test"
    assert args.insecure is True


# ---------------------------------------------------------------------------
# Validation: missing bootstrap token / plan / URL errors
# ---------------------------------------------------------------------------


def test_missing_bootstrap_token_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No --bootstrap-token and no env → exit 1, stderr names the env var, no HTTP call."""
    monkeypatch.delenv(BOOTSTRAP_TOKEN_ENV, raising=False)
    captured_register = _patch_register(monkeypatch)
    code = run_connect_command(
        ["http://127.0.0.1:8000", "--plan", "demo"],
    )
    assert code == EXIT_CONNECT_ERROR
    err = capsys.readouterr().err
    assert BOOTSTRAP_TOKEN_ENV in err
    assert "--bootstrap-token" in err
    assert captured_register == {}


def test_whitespace_only_bootstrap_token_rejected_before_http(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Whitespace-only token is treated as missing (VAL-M1-CONNECT-022)."""
    monkeypatch.delenv(BOOTSTRAP_TOKEN_ENV, raising=False)
    captured_register = _patch_register(monkeypatch)
    code = run_connect_command(
        [
            "http://127.0.0.1:8000",
            "--plan",
            "demo",
            "--bootstrap-token",
            "    ",
        ],
    )
    assert code == EXIT_CONNECT_ERROR
    err = capsys.readouterr().err
    assert "non-empty" in err
    assert captured_register == {}, "no HTTP call should have happened"


def test_empty_plan_rejected_before_http(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--plan "   "`` rejected before any network call (VAL-M1-CONNECT-905)."""
    captured_register = _patch_register(monkeypatch)
    code = run_connect_command(
        [
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "   ",
        ],
    )
    assert code == EXIT_CONNECT_ERROR
    err = capsys.readouterr().err
    assert "--plan" in err
    assert captured_register == {}


def test_invalid_url_rejected_before_http(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing-scheme URL rejected before any network call (VAL-M1-CONNECT-903)."""
    captured_register = _patch_register(monkeypatch)
    code = run_connect_command(
        [
            "127.0.0.1:8000",  # no scheme
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
        ],
    )
    assert code == EXIT_CONNECT_ERROR
    err = capsys.readouterr().err
    assert "http://" in err or "https://" in err
    assert captured_register == {}


def test_url_with_path_rejected_before_http(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """URL with explicit path is rejected (VAL-M1-CONNECT-902 option b)."""
    captured_register = _patch_register(monkeypatch)
    code = run_connect_command(
        [
            "http://127.0.0.1:8000/api",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
        ],
    )
    assert code == EXIT_CONNECT_ERROR
    err = capsys.readouterr().err
    assert "path" in err.lower()
    assert captured_register == {}


def test_out_of_range_port_rejected(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Port > 65535 rejected before HTTP (VAL-M1-CONNECT-904)."""
    captured_register = _patch_register(monkeypatch)
    code = run_connect_command(
        [
            "http://127.0.0.1:99999",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
        ],
    )
    assert code == EXIT_CONNECT_ERROR
    err = capsys.readouterr().err
    assert "port" in err.lower()
    assert captured_register == {}


def test_plain_http_to_non_loopback_rejected_before_http(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Connect to plain HTTP non-loopback without --insecure rejected (VAL-M1-INSECURE-009)."""
    captured_register = _patch_register(monkeypatch)
    code = run_connect_command(
        [
            "http://192.0.2.10:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
        ],
    )
    assert code == EXIT_CONNECT_ERROR
    err = capsys.readouterr().err
    assert "--insecure" in err
    assert captured_register == {}


def test_insecure_warning_emitted_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """``--insecure`` for non-loopback http emits a stderr warning (M1 description requirement)."""
    _patch_register(monkeypatch)
    _patch_execvp(monkeypatch)
    code = run_connect_command(
        [
            "http://192.0.2.10:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
            "--insecure",
        ]
    )
    assert code == EXIT_OK
    err = capsys.readouterr().err
    assert "warning" in err.lower()
    assert "192.0.2.10" in err


# ---------------------------------------------------------------------------
# Happy path: keychain stores bearer + execvp into whilly-worker
# ---------------------------------------------------------------------------


def test_happy_path_stores_in_keychain_and_execs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """Default flow: keyring stores bearer, stdout has the contract lines, execvp called."""
    register_capture = _patch_register(monkeypatch, worker_id="w-deadbeef", token="secret-bearer")
    exec_calls = _patch_execvp(monkeypatch)

    code = run_connect_command(
        [
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
            "--hostname",
            "host-a",
        ]
    )
    assert code == EXIT_OK

    # Stdout contract (VAL-M1-CONNECT-007): two lines, no banners between.
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == "worker_id: w-deadbeef"
    assert lines[1] == "token: secret-bearer"

    # Register call captured the right inputs.
    assert register_capture["connect_url"] == "http://127.0.0.1:8000"
    assert register_capture["bootstrap_token"] == "BT"
    assert register_capture["hostname"] == "host-a"

    # Keyring received exactly one set call under service="whilly".
    assert len(fake_keyring.set_calls) == 1
    service, username = fake_keyring.set_calls[0]
    assert service == "whilly"
    assert username == "http://127.0.0.1:8000"
    assert fake_keyring.store[(service, username)] == "secret-bearer"

    # execvp called with the correct argv shape.
    assert len(exec_calls) == 1
    file_, argv = exec_calls[0]
    assert file_ == "whilly-worker"
    assert argv[:7] == [
        "whilly-worker",
        "--connect",
        "http://127.0.0.1:8000",
        "--token",
        "secret-bearer",
        "--plan",
        "demo",
    ]
    assert "--insecure" not in argv


def test_happy_path_canonicalises_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """Trailing slash is stripped from the keychain key (VAL-M1-CONNECT-901)."""
    _patch_register(monkeypatch, token="bearer-x")
    _patch_execvp(monkeypatch)
    code = run_connect_command(
        [
            "http://127.0.0.1:8000/",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
        ]
    )
    assert code == EXIT_OK
    # Key is the slash-stripped form.
    assert ("whilly", "http://127.0.0.1:8000") in fake_keyring.store
    assert ("whilly", "http://127.0.0.1:8000/") not in fake_keyring.store


def test_bootstrap_token_env_var_fallback(
    monkeypatch: pytest.MonkeyPatch,
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """Env var ``WHILLY_WORKER_BOOTSTRAP_TOKEN`` is read when the flag is absent."""
    monkeypatch.setenv(BOOTSTRAP_TOKEN_ENV, "ENV-BT")
    register_capture = _patch_register(monkeypatch)
    _patch_execvp(monkeypatch)
    code = run_connect_command(
        [
            "http://127.0.0.1:8000",
            "--plan",
            "demo",
        ]
    )
    assert code == EXIT_OK
    assert register_capture["bootstrap_token"] == "ENV-BT"


def test_flag_overrides_env_for_bootstrap_token(
    monkeypatch: pytest.MonkeyPatch,
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """``--bootstrap-token`` flag wins over env (VAL-M1-CONNECT-003)."""
    monkeypatch.setenv(BOOTSTRAP_TOKEN_ENV, "ENV-BAD")
    register_capture = _patch_register(monkeypatch)
    _patch_execvp(monkeypatch)
    code = run_connect_command(
        [
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "FLAG-GOOD",
            "--plan",
            "demo",
        ]
    )
    assert code == EXIT_OK
    assert register_capture["bootstrap_token"] == "FLAG-GOOD"


def test_hostname_default_is_socket_gethostname(
    monkeypatch: pytest.MonkeyPatch,
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """Default hostname comes from ``socket.gethostname`` (VAL-M1-CONNECT-004)."""
    monkeypatch.setattr(cli_worker.socket, "gethostname", lambda: "default-host")
    register_capture = _patch_register(monkeypatch)
    _patch_execvp(monkeypatch)
    code = run_connect_command(
        [
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
        ]
    )
    assert code == EXIT_OK
    assert register_capture["hostname"] == "default-host"


# ---------------------------------------------------------------------------
# --no-keychain: skip keyring, still print + exec
# ---------------------------------------------------------------------------


def test_no_keychain_skips_keyring(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """``--no-keychain`` does NOT call keyring.set_password (VAL-M1-CONNECT-006)."""
    _patch_register(monkeypatch, token="bearer-y")
    exec_calls = _patch_execvp(monkeypatch)
    code = run_connect_command(
        [
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
            "--no-keychain",
        ]
    )
    assert code == EXIT_OK
    assert fake_keyring.set_calls == []
    assert fake_keyring.store == {}
    # Stdout still has the contract lines.
    out = capsys.readouterr().out
    assert "token: bearer-y" in out
    # Still execs.
    assert len(exec_calls) == 1


# ---------------------------------------------------------------------------
# File-fallback path
# ---------------------------------------------------------------------------


def test_file_fallback_when_keyring_raises(
    monkeypatch: pytest.MonkeyPatch,
    fake_keyring_failing: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """Keyring raising → fallback file written with mode 0600 / parent 0700 (VAL-M1-CONNECT-013, -911)."""
    _patch_register(monkeypatch, token="bearer-fb")
    _patch_execvp(monkeypatch)
    code = run_connect_command(
        [
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
        ]
    )
    assert code == EXIT_OK

    cred_path = whilly_secrets.credentials_file_path()
    assert cred_path.is_file(), f"fallback file not written at {cred_path}"
    # File mode 0600.
    file_mode = cred_path.stat().st_mode & 0o777
    assert file_mode == 0o600, f"expected 0o600, got {oct(file_mode)}"
    # Parent dir mode 0700.
    parent_mode = cred_path.parent.stat().st_mode & 0o777
    assert parent_mode == 0o700, f"expected 0o700, got {oct(parent_mode)}"
    # JSON content has the URL → bearer mapping.
    data = json.loads(cred_path.read_text(encoding="utf-8"))
    assert data == {"http://127.0.0.1:8000": "bearer-fb"}


def test_file_fallback_atomic_multi_url_coexistence(
    monkeypatch: pytest.MonkeyPatch,
    fake_keyring_failing: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """Connecting to URL_A then URL_B preserves both entries (VAL-M1-CONNECT-909, -014)."""
    _patch_register(monkeypatch, token="bearer-A")
    _patch_execvp(monkeypatch)
    code_a = run_connect_command(
        [
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
        ]
    )
    assert code_a == EXIT_OK

    _patch_register(monkeypatch, token="bearer-B")
    _patch_execvp(monkeypatch)
    code_b = run_connect_command(
        [
            "http://127.0.0.1:9000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
        ]
    )
    assert code_b == EXIT_OK

    cred_path = whilly_secrets.credentials_file_path()
    data = json.loads(cred_path.read_text(encoding="utf-8"))
    assert data == {
        "http://127.0.0.1:8000": "bearer-A",
        "http://127.0.0.1:9000": "bearer-B",
    }


def test_keychain_url_a_url_b_coexist_in_keyring(
    monkeypatch: pytest.MonkeyPatch,
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """Two URLs on the keyring path coexist independently (VAL-M1-CONNECT-909)."""
    _patch_register(monkeypatch, token="bearer-A")
    _patch_execvp(monkeypatch)
    run_connect_command(["http://127.0.0.1:8000", "--bootstrap-token", "BT", "--plan", "demo"])

    _patch_register(monkeypatch, token="bearer-B")
    _patch_execvp(monkeypatch)
    run_connect_command(["http://127.0.0.1:9000", "--bootstrap-token", "BT", "--plan", "demo"])

    assert fake_keyring.store[("whilly", "http://127.0.0.1:8000")] == "bearer-A"
    assert fake_keyring.store[("whilly", "http://127.0.0.1:9000")] == "bearer-B"


def test_keychain_re_register_overwrites(
    monkeypatch: pytest.MonkeyPatch,
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """Re-running for the same URL overwrites the bearer (VAL-M1-CONNECT-016)."""
    _patch_register(monkeypatch, token="bearer-1")
    _patch_execvp(monkeypatch)
    run_connect_command(["http://127.0.0.1:8000", "--bootstrap-token", "BT", "--plan", "demo"])

    _patch_register(monkeypatch, token="bearer-2")
    _patch_execvp(monkeypatch)
    run_connect_command(["http://127.0.0.1:8000", "--bootstrap-token", "BT", "--plan", "demo"])

    # Final stored value is the second bearer.
    assert fake_keyring.store[("whilly", "http://127.0.0.1:8000")] == "bearer-2"


# ---------------------------------------------------------------------------
# Error mappings: 401 / network / server / missing exec
# ---------------------------------------------------------------------------


def test_register_auth_error_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """Bootstrap-token mismatch → exit 1, no keyring write, no exec (VAL-M1-CONNECT-009)."""

    async def _raise(*_a: Any, **_kw: Any) -> Any:
        raise AuthError("401 from /workers/register", status_code=401, response_body="bad token")

    monkeypatch.setattr(cli_worker, "_async_register", _raise)
    exec_calls = _patch_execvp(monkeypatch)

    code = run_connect_command(
        [
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "WRONG",
            "--plan",
            "demo",
        ]
    )
    assert code == EXIT_CONNECT_ERROR
    err = capsys.readouterr().err
    assert "401" in err
    assert "bootstrap" in err.lower() or "token" in err.lower()
    assert fake_keyring.set_calls == []
    assert exec_calls == []


def test_control_plane_unreachable_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """Connection refused / timeout → exit 1 with a helpful diagnostic (VAL-M1-CONNECT-010)."""

    async def _raise(*_a: Any, **_kw: Any) -> Any:
        raise ConnectionRefusedError("nope")

    monkeypatch.setattr(cli_worker, "_async_register", _raise)
    exec_calls = _patch_execvp(monkeypatch)

    code = run_connect_command(
        [
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
        ]
    )
    assert code == EXIT_CONNECT_ERROR
    err = capsys.readouterr().err
    assert "unreachable" in err.lower() or "connectionrefused" in err.lower()
    assert fake_keyring.set_calls == []
    assert exec_calls == []


def test_control_plane_5xx_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """5xx after retry budget → exit 1 with server-error diagnostic."""

    async def _raise(*_a: Any, **_kw: Any) -> Any:
        raise ServerError("503 service unavailable", status_code=503, response_body="upstream down")

    monkeypatch.setattr(cli_worker, "_async_register", _raise)
    exec_calls = _patch_execvp(monkeypatch)

    code = run_connect_command(
        [
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
        ]
    )
    assert code == EXIT_CONNECT_ERROR
    err = capsys.readouterr().err
    assert "server" in err.lower()
    assert exec_calls == []


def test_missing_whilly_worker_after_register_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """``whilly-worker`` not on PATH → exit 1 with recovery hint, but bearer IS persisted (VAL-M1-CONNECT-908)."""
    _patch_register(monkeypatch, token="bearer-after-register")
    _patch_execvp_missing(monkeypatch)

    code = run_connect_command(
        [
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
        ]
    )
    assert code == EXIT_CONNECT_ERROR
    err = capsys.readouterr().err
    assert "whilly-worker" in err
    # Bearer was persisted before the exec attempt.
    assert fake_keyring.store[("whilly", "http://127.0.0.1:8000")] == "bearer-after-register"


# ---------------------------------------------------------------------------
# Stdout / stderr never leak the plaintext bearer to stderr
# ---------------------------------------------------------------------------


def test_plaintext_bearer_never_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """The plaintext bearer is only emitted on stdout (VAL-M1-CONNECT-912)."""
    bearer = "supersecret-bearer-XYZ"
    _patch_register(monkeypatch, token=bearer)
    _patch_execvp(monkeypatch)
    code = run_connect_command(
        [
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
        ]
    )
    assert code == EXIT_OK
    out_err = capsys.readouterr()
    assert bearer in out_err.out
    assert bearer not in out_err.err


# ---------------------------------------------------------------------------
# --insecure forwarded into whilly-worker argv
# ---------------------------------------------------------------------------


def test_insecure_propagates_to_worker_argv(
    monkeypatch: pytest.MonkeyPatch,
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """``--insecure`` on connect adds ``--insecure`` to the exec'd whilly-worker argv."""
    _patch_register(monkeypatch)
    exec_calls = _patch_execvp(monkeypatch)
    code = run_connect_command(
        [
            "http://192.0.2.10:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
            "--insecure",
        ]
    )
    assert code == EXIT_OK
    _, argv = exec_calls[0]
    assert "--insecure" in argv


# ---------------------------------------------------------------------------
# Dispatcher: `whilly worker connect` reaches run_connect_command
# ---------------------------------------------------------------------------


def test_main_routes_connect_to_run_connect_command(
    monkeypatch: pytest.MonkeyPatch,
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """``whilly-worker connect <url> ...`` reaches :func:`run_connect_command`."""
    _patch_register(monkeypatch)
    exec_calls = _patch_execvp(monkeypatch)
    code = cli_worker.main(
        [
            "connect",
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
        ]
    )
    assert code == EXIT_OK
    assert len(exec_calls) == 1


def test_dispatcher_routes_whilly_worker_connect(
    monkeypatch: pytest.MonkeyPatch,
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """``whilly worker connect ...`` (top-level dispatcher) reaches the handler (VAL-M1-CONNECT-019)."""
    from whilly.cli import main as whilly_main

    _patch_register(monkeypatch)
    exec_calls = _patch_execvp(monkeypatch)
    code = whilly_main(
        [
            "worker",
            "connect",
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "BT",
            "--plan",
            "demo",
        ]
    )
    assert code == EXIT_OK
    assert len(exec_calls) == 1


# ---------------------------------------------------------------------------
# whilly-worker --insecure (worker loop) — scheme guard reuse
# ---------------------------------------------------------------------------


def test_worker_loop_rejects_plain_http_to_remote_without_insecure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``whilly-worker --connect http://nonloop:8000 --token X --plan p`` exits 1 without --insecure."""
    # Guarantee no env interference.
    for var in ("WHILLY_CONTROL_URL", "WHILLY_WORKER_TOKEN", "WHILLY_PLAN_ID"):
        monkeypatch.delenv(var, raising=False)
    code = cli_worker.run_worker_command(
        [
            "--connect",
            "http://192.0.2.10:8000",
            "--token",
            "X",
            "--plan",
            "p",
        ]
    )
    assert code == EXIT_CONNECT_ERROR
    err = capsys.readouterr().err
    assert "--insecure" in err


def test_worker_loop_accepts_https_without_insecure(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTPS goes through the scheme guard fine (VAL-M1-INSECURE-003)."""
    captured: list[dict[str, Any]] = []

    async def _fake(**kwargs: Any) -> Any:
        captured.append(kwargs)
        from whilly.worker.remote import RemoteWorkerStats

        return RemoteWorkerStats()

    monkeypatch.setattr(cli_worker, "_async_worker", _fake)
    for var in ("WHILLY_CONTROL_URL", "WHILLY_WORKER_TOKEN", "WHILLY_PLAN_ID"):
        monkeypatch.delenv(var, raising=False)
    code = cli_worker.run_worker_command(
        [
            "--connect",
            "https://example.com:443",
            "--token",
            "X",
            "--plan",
            "p",
        ]
    )
    assert code == EXIT_OK
    assert captured[0]["connect_url"] == "https://example.com:443"


def test_worker_loop_accepts_loopback_without_insecure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loopback http always passes (VAL-M1-INSECURE-004/005/006)."""

    async def _fake(**_kwargs: Any) -> Any:
        from whilly.worker.remote import RemoteWorkerStats

        return RemoteWorkerStats()

    monkeypatch.setattr(cli_worker, "_async_worker", _fake)
    for var in ("WHILLY_CONTROL_URL", "WHILLY_WORKER_TOKEN", "WHILLY_PLAN_ID"):
        monkeypatch.delenv(var, raising=False)
    for url in ("http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"):
        code = cli_worker.run_worker_command(["--connect", url, "--token", "X", "--plan", "p"])
        assert code == EXIT_OK, url


def test_worker_loop_rejects_wildcard_bind(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``0.0.0.0`` is treated as non-loopback (VAL-M1-INSECURE-901)."""
    for var in ("WHILLY_CONTROL_URL", "WHILLY_WORKER_TOKEN", "WHILLY_PLAN_ID"):
        monkeypatch.delenv(var, raising=False)
    code = cli_worker.run_worker_command(
        [
            "--connect",
            "http://0.0.0.0:8000",
            "--token",
            "X",
            "--plan",
            "p",
        ]
    )
    assert code == EXIT_CONNECT_ERROR
    err = capsys.readouterr().err
    assert "--insecure" in err


def test_worker_loop_warns_when_insecure_used(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--insecure`` for non-loopback http emits a stderr warning (description requirement)."""

    async def _fake(**_kwargs: Any) -> Any:
        from whilly.worker.remote import RemoteWorkerStats

        return RemoteWorkerStats()

    monkeypatch.setattr(cli_worker, "_async_worker", _fake)
    for var in ("WHILLY_CONTROL_URL", "WHILLY_WORKER_TOKEN", "WHILLY_PLAN_ID"):
        monkeypatch.delenv(var, raising=False)
    code = cli_worker.run_worker_command(
        [
            "--connect",
            "http://192.0.2.10:8000",
            "--token",
            "X",
            "--plan",
            "p",
            "--insecure",
        ]
    )
    assert code == EXIT_OK
    err = capsys.readouterr().err
    assert "warning" in err.lower()


# ---------------------------------------------------------------------------
# Help text mentions env var (VAL-M1-OPERABILITY-903) and required flags (VAL-M1-CONNECT-018)
# ---------------------------------------------------------------------------


def test_connect_help_mentions_env_and_flags(capsys: pytest.CaptureFixture[str]) -> None:
    """``whilly worker connect --help`` mentions all flags + the env var."""
    parser = build_connect_parser()
    help_text = parser.format_help()
    assert "--bootstrap-token" in help_text
    assert "--plan" in help_text
    assert "--hostname" in help_text
    assert "--no-keychain" in help_text
    assert BOOTSTRAP_TOKEN_ENV in help_text


def test_load_worker_credential_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    fake_keyring: _FakeKeyring,
    patched_xdg: str,
) -> None:
    """``store_worker_credential`` then ``load_worker_credential`` returns the same bearer."""
    _patch_register(monkeypatch, token="bearer-rt")
    _patch_execvp(monkeypatch)
    run_connect_command(["http://127.0.0.1:8000", "--bootstrap-token", "BT", "--plan", "demo"])
    assert whilly_secrets.load_worker_credential("http://127.0.0.1:8000") == "bearer-rt"
    # Trailing-slash variant resolves to the same value.
    assert whilly_secrets.load_worker_credential("http://127.0.0.1:8000/") == "bearer-rt"
