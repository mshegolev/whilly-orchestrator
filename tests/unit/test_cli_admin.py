"""Unit tests for ``whilly admin ...`` argparse + parsing helpers (M2 mission, m2-admin-cli).

These tests exercise the pure / fast surface of :mod:`whilly.cli.admin` —
argparse tree shape, ``--expires-in`` parsing, malformed-input
diagnostics, missing ``WHILLY_DATABASE_URL`` envelope. The DB-touching
end-to-end coverage lives in
``tests/integration/test_cli_admin_e2e.py``.
"""

from __future__ import annotations

import io
from datetime import timedelta

import pytest

from whilly.cli import admin


# ---------------------------------------------------------------------------
# argparse tree
# ---------------------------------------------------------------------------


def test_build_admin_parser_exposes_bootstrap_and_worker_namespaces() -> None:
    parser = admin.build_admin_parser()
    args = parser.parse_args(["bootstrap", "mint", "--owner", "alice@example.com"])
    assert args.namespace == "bootstrap"
    assert args.action == "mint"
    assert args.owner == "alice@example.com"
    assert args.admin is False
    assert args.expires_in is None

    args = parser.parse_args(["worker", "revoke", "w-123"])
    assert args.namespace == "worker"
    assert args.action == "revoke"
    assert args.worker_id == "w-123"


def test_admin_help_lists_bootstrap_and_worker(capsys: pytest.CaptureFixture[str]) -> None:
    parser = admin.build_admin_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])
    out = capsys.readouterr().out
    assert "bootstrap" in out
    assert "worker" in out


def test_bootstrap_subcommands_present(capsys: pytest.CaptureFixture[str]) -> None:
    parser = admin.build_admin_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["bootstrap", "--help"])
    out = capsys.readouterr().out
    assert "mint" in out
    assert "revoke" in out
    assert "list" in out


def test_missing_namespace_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    parser = admin.build_admin_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([])
    assert exc.value.code == 2


def test_bootstrap_revoke_requires_prefix(capsys: pytest.CaptureFixture[str]) -> None:
    parser = admin.build_admin_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["bootstrap", "revoke"])
    assert exc.value.code == 2


def test_worker_revoke_requires_worker_id(capsys: pytest.CaptureFixture[str]) -> None:
    parser = admin.build_admin_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["worker", "revoke"])
    assert exc.value.code == 2


def test_bootstrap_list_supports_include_revoked_and_json() -> None:
    parser = admin.build_admin_parser()
    args = parser.parse_args(["bootstrap", "list", "--include-revoked", "--json"])
    assert args.include_revoked is True
    assert args.json_output is True


def test_bootstrap_mint_admin_flag() -> None:
    parser = admin.build_admin_parser()
    args = parser.parse_args(
        [
            "bootstrap",
            "mint",
            "--owner",
            "admin@example.com",
            "--admin",
            "--expires-in",
            "30d",
        ]
    )
    assert args.admin is True
    assert args.expires_in == "30d"


# ---------------------------------------------------------------------------
# ``--expires-in`` parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("30d", timedelta(days=30)),
        ("2w", timedelta(weeks=2)),
        ("12h", timedelta(hours=12)),
        ("45m", timedelta(minutes=45)),
        ("90s", timedelta(seconds=90)),
        ("  3D  ", timedelta(days=3)),
    ],
)
def test_parse_expires_in_accepts_canonical_forms(value: str, expected: timedelta) -> None:
    assert admin.parse_expires_in(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "   ", "30", "abc", "0d", "-5d", "10x", "1.5d", "5y"],
)
def test_parse_expires_in_rejects_malformed(value: str) -> None:
    with pytest.raises(admin.ExpiresInError):
        admin.parse_expires_in(value)


# ---------------------------------------------------------------------------
# Missing WHILLY_DATABASE_URL envelope (no DB touched)
# ---------------------------------------------------------------------------


def test_bootstrap_list_returns_2_when_dsn_unset(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(admin.DATABASE_URL_ENV, raising=False)
    rc = admin.run_admin_command(["bootstrap", "list"])
    assert rc == admin.EXIT_ENVIRONMENT_ERROR
    captured = capsys.readouterr()
    assert admin.DATABASE_URL_ENV in captured.err


def test_bootstrap_mint_invalid_owner_exits_1_without_dsn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Owner-shape gate fires before the DSN check, so DSN absence is irrelevant."""
    monkeypatch.delenv(admin.DATABASE_URL_ENV, raising=False)
    rc = admin.run_admin_command(["bootstrap", "mint", "--owner", "not-an-email"])
    assert rc == admin.EXIT_OPERATION_ERROR
    captured = capsys.readouterr()
    assert "not a valid email shape" in captured.err


def test_bootstrap_mint_missing_owner_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(admin.DATABASE_URL_ENV, raising=False)
    rc = admin.run_admin_command(["bootstrap", "mint"])
    assert rc == admin.EXIT_OPERATION_ERROR
    err = capsys.readouterr().err
    assert "--owner is required" in err


def test_bootstrap_mint_bad_expires_in_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(admin.DATABASE_URL_ENV, raising=False)
    rc = admin.run_admin_command(
        [
            "bootstrap",
            "mint",
            "--owner",
            "alice@example.com",
            "--expires-in",
            "garbage",
        ]
    )
    assert rc == admin.EXIT_OPERATION_ERROR
    err = capsys.readouterr().err
    assert "--expires-in" in err


def test_bootstrap_revoke_short_prefix_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(admin.DATABASE_URL_ENV, raising=False)
    rc = admin.run_admin_command(["bootstrap", "revoke", "abc12"])
    assert rc == admin.EXIT_OPERATION_ERROR
    assert "too short" in capsys.readouterr().err


def test_bootstrap_revoke_non_hex_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(admin.DATABASE_URL_ENV, raising=False)
    rc = admin.run_admin_command(["bootstrap", "revoke", "zzzzzzzzzz"])
    assert rc == admin.EXIT_OPERATION_ERROR
    assert "hex" in capsys.readouterr().err


def test_worker_revoke_empty_id_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(admin.DATABASE_URL_ENV, raising=False)
    rc = admin.run_admin_command(["worker", "revoke", "   "])
    assert rc == admin.EXIT_OPERATION_ERROR
    assert "non-empty" in capsys.readouterr().err


def test_dispatch_via_main_routes_admin_subcommand(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`whilly admin ...` flows through the v4 dispatcher in :mod:`whilly.cli`."""
    from whilly.cli import main

    monkeypatch.delenv(admin.DATABASE_URL_ENV, raising=False)
    rc = main(["admin", "bootstrap", "list"])
    assert rc == admin.EXIT_ENVIRONMENT_ERROR
    err = capsys.readouterr().err
    assert admin.DATABASE_URL_ENV in err


def test_admin_help_shown_via_v4_help_block() -> None:
    """The top-level v4 help block mentions the admin command."""
    from whilly.cli import _HELP_TEXT

    assert "admin" in _HELP_TEXT


def test_emit_kv_writes_line_oriented_pairs() -> None:
    buf = io.StringIO()
    admin._emit_kv(buf, token="abc", owner="alice@example.com")
    assert buf.getvalue() == "token: abc\nowner: alice@example.com\n"
