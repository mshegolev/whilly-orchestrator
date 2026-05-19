"""Unit tests for :mod:`whilly.cli.worker_launch` — the
``whilly worker launch/list/remove`` subcommand surface.

PRD-post-auth-hardening §Epic B, Item 4. Covers every branch the PRD
description names plus auxiliary error paths:

* Launch with a new config (registers, writes config)
* Launch reuse path (existing config, no re-registration)
* Launch force-register (discards cache, re-registers)
* Launch register-only (cache + exit, no worker loop)
* Launch print-env (resolve env + exit, no worker loop)
* Launch missing plan / bootstrap / claude_bin → exit code 2
* List table output / --json output / empty config
* Remove single match / ambiguous (requires --connect) / --all
* Bootstrap token resolution: flag wins over env wins over .env
* .env parser: KEY=VALUE lines, ignores comments and blank lines

PRD mentions "three bootstrap-token decode paths (base64url, hex, utf-8
fallback)" but no such decode logic exists in worker_launch.py —
``_resolve_bootstrap_token`` is a pure string resolver. Tests follow the
actual code shape (flag → env → .env), not the PRD's idealised decode.

All tests use ``tmp_path`` for config isolation and monkeypatch
``_register`` + ``run_worker_command`` so no network or worker-loop
side effects escape the test process.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from whilly.cli import worker_launch


# ─── helpers ────────────────────────────────────────────────────────────────


def _stub_register_factory(worker_id: str = "w-test-001", token: str = "tk-test") -> Any:
    """Build an async stub matching the signature of ``_register``.

    Mirrors the production signature including the F18b ``tags`` arg
    (PRD-post-auth-hardening §Epic F Item 18); the stub ignores its
    inputs but accepting them keeps the call site source-compatible
    with the real function.
    """

    async def _stub(
        control_url: str,
        bootstrap_token: str,
        hostname: str,
        tags: list[str] | None = None,
    ) -> tuple[str, str]:
        return worker_id, token

    return _stub


def _patch_no_worker_loop(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace ``run_worker_command`` so launch doesn't try to start the loop.

    Returns a list that captures call args, so tests can assert the worker
    loop was/wasn't invoked.
    """
    calls: list[list[str]] = []

    def _fake_run_worker_command(argv: list[str]) -> int:
        calls.append(list(argv))
        return 0

    # The import is done inside ``run_launch_command`` (``from whilly.cli.worker
    # import run_worker_command``), so we patch the source module — the import
    # resolves at call time, so this gets picked up.
    import whilly.cli.worker as _cli_worker

    monkeypatch.setattr(_cli_worker, "run_worker_command", _fake_run_worker_command, raising=True)
    return calls


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    """Per-test config file path."""
    return tmp_path / "worker.json"


_TRACKED_ENV_VARS = (
    "WHILLY_WORKER_BOOTSTRAP_TOKEN",
    "WHILLY_CONTROL_URL",
    "WHILLY_PLAN_ID",
    "WHILLY_MODEL",
    "WHILLY_WORKER_ID",
    "WHILLY_WORKER_TOKEN",
    "WHILLY_AGENT_ALLOW_SHELL",
    "CLAUDE_BIN",
    "WHILLY_WORKER_CONFIG",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip pre-test env state AND restore on teardown.

    Critical: the launch happy path calls ``os.environ.update(resolved_env)``
    in the production code (see worker_launch.run_launch_command around the
    ``os.environ.update`` line). That bypasses :class:`pytest.MonkeyPatch`'s
    bookkeeping, so a stale ``WHILLY_MODEL=claude-haiku-...`` leaks into
    later tests that read it (notably ``test_llm_ops``). Snapshot + restore
    + post-yield delete fixes the cross-test contamination CI surfaced in
    PR #280 v1.
    """
    import os as _os

    snapshot = {var: _os.environ.get(var) for var in _TRACKED_ENV_VARS}
    for var in _TRACKED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    try:
        yield
    finally:
        for var, original in snapshot.items():
            if original is None:
                _os.environ.pop(var, None)
            else:
                _os.environ[var] = original


# ─── _read_dotenv parser ────────────────────────────────────────────────────


def test_read_dotenv_parses_keyvalue_lines_ignores_comments(tmp_path: Path) -> None:
    """Lines with #, blank lines, and lines without = are silently skipped."""
    p = tmp_path / ".env"
    p.write_text(
        "\n"
        "# comment line\n"
        "KEY1=value1\n"
        "KEY2 = value2 \n"
        "bare-line-no-equals\n"
        'KEY3="quoted-value"\n'
        "KEY4='single-quoted'\n",
        encoding="utf-8",
    )
    out = worker_launch._read_dotenv(p)
    assert out == {
        "KEY1": "value1",
        "KEY2": "value2",
        "KEY3": "quoted-value",
        "KEY4": "single-quoted",
    }


def test_read_dotenv_missing_file_returns_empty_dict(tmp_path: Path) -> None:
    """Missing .env is not an error — empty dict, no exception."""
    assert worker_launch._read_dotenv(tmp_path / "does-not-exist.env") == {}


# ─── _resolve_bootstrap_token resolution chain ──────────────────────────────


def test_resolve_bootstrap_token_flag_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHILLY_WORKER_BOOTSTRAP_TOKEN", "from-env")
    assert worker_launch._resolve_bootstrap_token("from-flag") == "from-flag"


def test_resolve_bootstrap_token_env_falls_back_to_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No flag, no env var → reads ./.env from cwd."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("WHILLY_WORKER_BOOTSTRAP_TOKEN=from-dotenv\n", encoding="utf-8")
    assert worker_launch._resolve_bootstrap_token(None) == "from-dotenv"


def test_resolve_bootstrap_token_no_sources_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    assert worker_launch._resolve_bootstrap_token(None) is None


# ─── run_launch_command: fresh path → register + write config ───────────────


def test_launch_with_new_config_registers_and_writes_config(monkeypatch: pytest.MonkeyPatch, cfg_path: Path) -> None:
    """Empty config → calls _register, writes worker.json with the result."""
    monkeypatch.setattr(worker_launch, "_register", _stub_register_factory("w-new", "tk-new"))

    rc = worker_launch.run_launch_command(
        [
            "demo-plan",
            "--connect",
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "boot-tk",
            "--claude-bin",
            "/usr/bin/claude",
            "--register-only",
            "--config",
            str(cfg_path),
        ]
    )
    assert rc == worker_launch.EXIT_OK
    config = json.loads(cfg_path.read_text())
    cache_key = worker_launch._config_key("http://127.0.0.1:8000", "demo-plan")
    assert cache_key in config["workers"]
    entry = config["workers"][cache_key]
    assert entry["worker_id"] == "w-new"
    assert entry["token"] == "tk-new"
    assert entry["plan_id"] == "demo-plan"
    assert config["last_plan_id"] == "demo-plan"
    assert config["default_control_url"] == "http://127.0.0.1:8000"


# ─── run_launch_command: reuse path → no re-registration ───────────────────


def test_launch_reuses_cached_credentials_no_re_registration(monkeypatch: pytest.MonkeyPatch, cfg_path: Path) -> None:
    """Pre-seeded config → _register MUST NOT be called."""

    register_called = False

    async def _trap(*args: Any, **kwargs: Any) -> tuple[str, str]:
        nonlocal register_called
        register_called = True
        return "should-not-be-used", "should-not-be-used"

    monkeypatch.setattr(worker_launch, "_register", _trap)
    cache_key = worker_launch._config_key("http://127.0.0.1:8000", "demo-plan")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "workers": {
                    cache_key: {
                        "worker_id": "w-existing",
                        "token": "tk-existing",
                        "plan_id": "demo-plan",
                        "control_url": "http://127.0.0.1:8000",
                    }
                }
            }
        )
    )
    rc = worker_launch.run_launch_command(
        [
            "demo-plan",
            "--connect",
            "http://127.0.0.1:8000",
            "--claude-bin",
            "/usr/bin/claude",
            "--register-only",
            "--config",
            str(cfg_path),
        ]
    )
    assert rc == worker_launch.EXIT_OK
    assert register_called is False, "reuse path must not call _register"


# ─── run_launch_command: --force-register discards cache ────────────────────


def test_launch_force_register_discards_cached_creds(monkeypatch: pytest.MonkeyPatch, cfg_path: Path) -> None:
    """Cached creds present + --force-register → _register IS called; cache overwritten."""
    monkeypatch.setattr(worker_launch, "_register", _stub_register_factory("w-fresh", "tk-fresh"))
    cache_key = worker_launch._config_key("http://127.0.0.1:8000", "demo-plan")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "workers": {
                    cache_key: {
                        "worker_id": "w-stale",
                        "token": "tk-stale",
                        "plan_id": "demo-plan",
                        "control_url": "http://127.0.0.1:8000",
                    }
                }
            }
        )
    )
    rc = worker_launch.run_launch_command(
        [
            "demo-plan",
            "--connect",
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "boot-tk",
            "--claude-bin",
            "/usr/bin/claude",
            "--force-register",
            "--register-only",
            "--config",
            str(cfg_path),
        ]
    )
    assert rc == worker_launch.EXIT_OK
    config = json.loads(cfg_path.read_text())
    assert config["workers"][cache_key]["worker_id"] == "w-fresh"


# ─── run_launch_command: --print-env exits after printing ───────────────────


def test_launch_print_env_outputs_export_lines_and_exits(
    monkeypatch: pytest.MonkeyPatch, cfg_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(worker_launch, "_register", _stub_register_factory("w-1", "tk-1"))
    rc = worker_launch.run_launch_command(
        [
            "demo-plan",
            "--connect",
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "boot-tk",
            "--claude-bin",
            "/usr/bin/claude",
            "--print-env",
            "--config",
            str(cfg_path),
        ]
    )
    assert rc == worker_launch.EXIT_OK
    captured = capsys.readouterr()
    out = captured.out
    assert "export WHILLY_WORKER_ID=" in out
    assert "export WHILLY_PLAN_ID=" in out
    assert "export CLAUDE_BIN=" in out


# ─── run_launch_command: error paths ────────────────────────────────────────


def test_launch_missing_bootstrap_token_returns_environment_error(
    monkeypatch: pytest.MonkeyPatch, cfg_path: Path, tmp_path: Path
) -> None:
    """Fresh register without a bootstrap token (anywhere) → exit 2."""
    monkeypatch.chdir(tmp_path)  # no .env in cwd
    rc = worker_launch.run_launch_command(
        [
            "demo-plan",
            "--connect",
            "http://127.0.0.1:8000",
            "--claude-bin",
            "/usr/bin/claude",
            "--register-only",
            "--config",
            str(cfg_path),
        ]
    )
    assert rc == worker_launch.EXIT_ENVIRONMENT_ERROR


def test_launch_missing_claude_bin_returns_environment_error(monkeypatch: pytest.MonkeyPatch, cfg_path: Path) -> None:
    """No --claude-bin, no CLAUDE_BIN env, no `claude` on PATH → exit 2."""
    monkeypatch.setattr(worker_launch.shutil, "which", lambda name: None)
    rc = worker_launch.run_launch_command(
        [
            "demo-plan",
            "--connect",
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "boot-tk",
            "--register-only",
            "--config",
            str(cfg_path),
        ]
    )
    assert rc == worker_launch.EXIT_ENVIRONMENT_ERROR


def test_launch_missing_plan_id_returns_environment_error(monkeypatch: pytest.MonkeyPatch, cfg_path: Path) -> None:
    """No plan_id positional, no config default, no interactive answer → exit 2."""
    # Force the interactive picker to return empty.
    monkeypatch.setattr(worker_launch.sys, "stdin", io.StringIO("\n"))
    rc = worker_launch.run_launch_command(
        [
            "--connect",
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "boot-tk",
            "--claude-bin",
            "/usr/bin/claude",
            "--register-only",
            "--config",
            str(cfg_path),
        ]
    )
    assert rc == worker_launch.EXIT_ENVIRONMENT_ERROR


# ─── --tags wiring (PRD F18 Item 18) ────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        pytest.param(None, [], id="none"),
        pytest.param("", [], id="empty"),
        pytest.param("   ", [], id="whitespace-only"),
        pytest.param("gpu", ["gpu"], id="single"),
        pytest.param("gpu,signing", ["gpu", "signing"], id="two-no-spaces"),
        pytest.param("gpu, signing", ["gpu", "signing"], id="two-with-spaces"),
        pytest.param(" gpu , signing ", ["gpu", "signing"], id="padded"),
        pytest.param("gpu,,signing", ["gpu", "signing"], id="drops-empty-middle"),
        pytest.param("gpu,signing,", ["gpu", "signing"], id="trailing-comma"),
    ],
)
def test_parse_tags_arg_handles_real_world_shapes(raw: str | None, expected: list[str]) -> None:
    """The CLI helper is tolerant of operator-typed shapes (spaces, trailing commas)."""
    assert worker_launch._parse_tags_arg(raw) == expected


def test_launch_with_tags_forwards_to_register_and_persists_in_config(
    monkeypatch: pytest.MonkeyPatch, cfg_path: Path
) -> None:
    """``whilly worker launch --tags gpu,signing`` round-trips into config + register call.

    Pins the two PRD F18 Item 18 AC bullets for the CLI side:
      * tags persist to the config file under the per-worker entry, and
      * tags are forwarded to ``_register`` (which in turn ships them
        in the wire body via ``client.register(..., tags=...)``).

    A capturing stub records the kwarg so a regression that silently
    dropped ``tags`` on the way from CLI to client.register would fail
    here — not just at runtime against a live server.
    """
    captured_kwargs: dict[str, Any] = {}

    async def _capturing_stub(
        control_url: str,
        bootstrap_token: str,
        hostname: str,
        tags: list[str] | None = None,
    ) -> tuple[str, str]:
        captured_kwargs["control_url"] = control_url
        captured_kwargs["hostname"] = hostname
        captured_kwargs["tags"] = tags
        return "w-tagged", "tk-tagged"

    monkeypatch.setattr(worker_launch, "_register", _capturing_stub)

    rc = worker_launch.run_launch_command(
        [
            "demo-plan",
            "--connect",
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "boot-tk",
            "--claude-bin",
            "/usr/bin/claude",
            "--tags",
            "gpu, signing",
            "--register-only",
            "--config",
            str(cfg_path),
        ]
    )
    assert rc == worker_launch.EXIT_OK
    # _register received the parsed list — not the raw string.
    assert captured_kwargs["tags"] == ["gpu", "signing"]
    # Tags persisted under the worker cache entry so subsequent
    # ``whilly worker list`` (or another invocation that wants to know
    # what this worker advertised) can read them without a server
    # round-trip.
    config = json.loads(cfg_path.read_text())
    cache_key = worker_launch._config_key("http://127.0.0.1:8000", "demo-plan")
    entry = config["workers"][cache_key]
    assert entry["tags"] == ["gpu", "signing"]


def test_launch_without_tags_persists_empty_list_in_config(monkeypatch: pytest.MonkeyPatch, cfg_path: Path) -> None:
    """Omitting ``--tags`` records an explicit empty list, not a missing key.

    Pinning the empty-list shape matters because :func:`run_list_command`
    branches on ``isinstance(tags, list)`` to render the chip column —
    a regression that wrote ``None`` (or omitted the key entirely)
    would change the worker-list rendering for every legacy launch.
    """
    captured_kwargs: dict[str, Any] = {}

    async def _capturing_stub(
        control_url: str,
        bootstrap_token: str,
        hostname: str,
        tags: list[str] | None = None,
    ) -> tuple[str, str]:
        captured_kwargs["tags"] = tags
        return "w-plain", "tk-plain"

    monkeypatch.setattr(worker_launch, "_register", _capturing_stub)

    rc = worker_launch.run_launch_command(
        [
            "demo-plan",
            "--connect",
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "boot-tk",
            "--claude-bin",
            "/usr/bin/claude",
            "--register-only",
            "--config",
            str(cfg_path),
        ]
    )
    assert rc == worker_launch.EXIT_OK
    assert captured_kwargs["tags"] == []
    config = json.loads(cfg_path.read_text())
    cache_key = worker_launch._config_key("http://127.0.0.1:8000", "demo-plan")
    entry = config["workers"][cache_key]
    assert entry["tags"] == []


def test_list_renders_tags_column(cfg_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``whilly worker list`` surfaces advertised tags so operators can sanity-check capability mappings."""
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    key = worker_launch._config_key("http://127.0.0.1:8000", "demo")
    cfg_path.write_text(
        json.dumps(
            {
                "workers": {
                    key: {
                        "worker_id": "w-gpu",
                        "plan_id": "demo",
                        "control_url": "http://127.0.0.1:8000",
                        "registered_at": 1_700_000_000,
                        "hostname": "gpu-box",
                        "tags": ["gpu", "signing"],
                    }
                },
            }
        )
    )
    rc = worker_launch.run_list_command(["--config", str(cfg_path)])
    assert rc == worker_launch.EXIT_OK
    out = capsys.readouterr().out
    # The header advertises the new column and the data row prints the
    # comma-joined values — defensive against future column reorders.
    assert "tags" in out
    assert "gpu,signing" in out


def test_list_renders_em_dash_for_legacy_entries_without_tags(
    cfg_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pre-F18b worker entries (no ``tags`` key) fall back to ``—`` in the table."""
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    key = worker_launch._config_key("http://127.0.0.1:8000", "legacy")
    cfg_path.write_text(
        json.dumps(
            {
                "workers": {
                    key: {
                        "worker_id": "w-legacy",
                        "plan_id": "legacy",
                        "control_url": "http://127.0.0.1:8000",
                        "registered_at": 1_700_000_000,
                        "hostname": "old-box",
                    }
                },
            }
        )
    )
    rc = worker_launch.run_list_command(["--config", str(cfg_path)])
    assert rc == worker_launch.EXIT_OK
    out = capsys.readouterr().out
    # The em-dash placeholder is the legacy-row signal — fine-grained
    # assertion would couple too tightly to column widths.
    assert "—" in out


# ─── run_list_command ───────────────────────────────────────────────────────


def test_list_empty_config_returns_ok(cfg_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = worker_launch.run_list_command(["--config", str(cfg_path)])
    assert rc == worker_launch.EXIT_OK
    assert "no cached workers" in capsys.readouterr().out


def test_list_table_output_with_workers(cfg_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    key = worker_launch._config_key("http://127.0.0.1:8000", "demo")
    cfg_path.write_text(
        json.dumps(
            {
                "default_control_url": "http://127.0.0.1:8000",
                "last_plan_id": "demo",
                "workers": {
                    key: {
                        "worker_id": "w-1",
                        "plan_id": "demo",
                        "control_url": "http://127.0.0.1:8000",
                        "registered_at": 1_700_000_000,
                        "hostname": "host-1",
                    }
                },
            }
        )
    )
    rc = worker_launch.run_list_command(["--config", str(cfg_path)])
    assert rc == worker_launch.EXIT_OK
    out = capsys.readouterr().out
    assert "w-1" in out
    assert "demo" in out
    assert "host-1" in out


def test_list_json_output_dumps_raw_config(cfg_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data = {"workers": {"k": {"worker_id": "wj"}}, "last_plan_id": "p"}
    cfg_path.write_text(json.dumps(data))
    rc = worker_launch.run_list_command(["--config", str(cfg_path), "--json"])
    assert rc == worker_launch.EXIT_OK
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == data


# ─── run_remove_command ─────────────────────────────────────────────────────


def test_remove_single_match_drops_entry_and_returns_ok(cfg_path: Path) -> None:
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    key = worker_launch._config_key("http://127.0.0.1:8000", "demo")
    cfg_path.write_text(
        json.dumps(
            {
                "workers": {
                    key: {
                        "worker_id": "w-1",
                        "plan_id": "demo",
                        "control_url": "http://127.0.0.1:8000",
                    }
                },
                "last_plan_id": "demo",
            }
        )
    )
    rc = worker_launch.run_remove_command(["demo", "--config", str(cfg_path)])
    assert rc == worker_launch.EXIT_OK
    config = json.loads(cfg_path.read_text())
    assert config["workers"] == {}
    assert "last_plan_id" not in config


def test_remove_ambiguous_without_connect_returns_error(cfg_path: Path) -> None:
    """Same plan_id under two control URLs + no --connect → exit 2."""
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    k1 = worker_launch._config_key("http://a", "demo")
    k2 = worker_launch._config_key("http://b", "demo")
    cfg_path.write_text(
        json.dumps(
            {
                "workers": {
                    k1: {"worker_id": "w-a", "plan_id": "demo", "control_url": "http://a"},
                    k2: {"worker_id": "w-b", "plan_id": "demo", "control_url": "http://b"},
                }
            }
        )
    )
    rc = worker_launch.run_remove_command(["demo", "--config", str(cfg_path)])
    assert rc == worker_launch.EXIT_ENVIRONMENT_ERROR
    # Nothing was removed — both entries should still be there.
    config = json.loads(cfg_path.read_text())
    assert set(config["workers"].keys()) == {k1, k2}


def test_remove_ambiguous_with_connect_disambiguates(cfg_path: Path) -> None:
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    k1 = worker_launch._config_key("http://a", "demo")
    k2 = worker_launch._config_key("http://b", "demo")
    cfg_path.write_text(
        json.dumps(
            {
                "workers": {
                    k1: {"worker_id": "w-a", "plan_id": "demo", "control_url": "http://a"},
                    k2: {"worker_id": "w-b", "plan_id": "demo", "control_url": "http://b"},
                }
            }
        )
    )
    rc = worker_launch.run_remove_command(["demo", "--connect", "http://a", "--config", str(cfg_path)])
    assert rc == worker_launch.EXIT_OK
    config = json.loads(cfg_path.read_text())
    assert k1 not in config["workers"]
    assert k2 in config["workers"]


def test_remove_all_wipes_workers_section(cfg_path: Path) -> None:
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "workers": {
                    "k1": {"worker_id": "w-1"},
                    "k2": {"worker_id": "w-2"},
                },
                "last_plan_id": "demo",
            }
        )
    )
    rc = worker_launch.run_remove_command(["--all", "--config", str(cfg_path)])
    assert rc == worker_launch.EXIT_OK
    config = json.loads(cfg_path.read_text())
    assert config["workers"] == {}
    assert "last_plan_id" not in config


def test_remove_nonexistent_plan_returns_error(cfg_path: Path) -> None:
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"workers": {"k": {"worker_id": "w", "plan_id": "other"}}}))
    rc = worker_launch.run_remove_command(["does-not-exist", "--config", str(cfg_path)])
    assert rc == worker_launch.EXIT_ENVIRONMENT_ERROR


def test_remove_with_no_workers_in_config_returns_error(cfg_path: Path) -> None:
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({}))
    rc = worker_launch.run_remove_command(["any-plan", "--config", str(cfg_path)])
    assert rc == worker_launch.EXIT_ENVIRONMENT_ERROR


# ─── _patch_no_worker_loop helper smoke test (also exercises the full
#     launch happy path that DOES call run_worker_command) ──────────────────


# ─── H21: --model / --connect override on reuse path ───────────────────────


def test_launch_model_flag_updates_cached_default_model(monkeypatch: pytest.MonkeyPatch, cfg_path: Path) -> None:
    """H21 AC: second launch with --model overwrites the cached default_model.

    Pre-populate a config with default_model=X and a cached entry, then
    re-run launch with --model NEW. After the second call, default_model
    must be NEW (not X).
    """
    monkeypatch.setattr(worker_launch, "_register", _stub_register_factory("w1", "tk1"))
    cache_key = worker_launch._config_key("http://127.0.0.1:8000", "demo-plan")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "default_control_url": "http://127.0.0.1:8000",
                "default_model": "claude-old-model",
                "last_plan_id": "demo-plan",
                "workers": {
                    cache_key: {
                        "worker_id": "w1",
                        "token": "tk1",
                        "plan_id": "demo-plan",
                        "control_url": "http://127.0.0.1:8000",
                    }
                },
            }
        )
    )
    rc = worker_launch.run_launch_command(
        [
            "demo-plan",
            "--connect",
            "http://127.0.0.1:8000",
            "--claude-bin",
            "/usr/bin/claude",
            "--model",
            "claude-new-model",  # explicit override
            "--register-only",
            "--config",
            str(cfg_path),
        ]
    )
    assert rc == worker_launch.EXIT_OK
    config = json.loads(cfg_path.read_text())
    assert config["default_model"] == "claude-new-model"


def test_launch_without_model_flag_does_not_overwrite_cached_default_model(
    monkeypatch: pytest.MonkeyPatch, cfg_path: Path
) -> None:
    """H21 AC: launch with no --model leaves cached default_model alone."""
    monkeypatch.setattr(worker_launch, "_register", _stub_register_factory("w1", "tk1"))
    cache_key = worker_launch._config_key("http://127.0.0.1:8000", "demo-plan")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "default_control_url": "http://127.0.0.1:8000",
                "default_model": "claude-preserved",
                "last_plan_id": "demo-plan",
                "workers": {
                    cache_key: {
                        "worker_id": "w1",
                        "token": "tk1",
                        "plan_id": "demo-plan",
                        "control_url": "http://127.0.0.1:8000",
                    }
                },
            }
        )
    )
    rc = worker_launch.run_launch_command(
        [
            "demo-plan",
            "--connect",
            "http://127.0.0.1:8000",
            "--claude-bin",
            "/usr/bin/claude",
            "--register-only",
            "--config",
            str(cfg_path),
        ]
    )
    assert rc == worker_launch.EXIT_OK
    config = json.loads(cfg_path.read_text())
    assert config["default_model"] == "claude-preserved"


def test_launch_connect_flag_updates_cached_default_control_url(
    monkeypatch: pytest.MonkeyPatch, cfg_path: Path
) -> None:
    """H21 AC: --connect on reuse path updates the cached default_control_url."""
    monkeypatch.setattr(worker_launch, "_register", _stub_register_factory("w1", "tk1"))
    cache_key = worker_launch._config_key("http://NEW", "demo-plan")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "default_control_url": "http://OLD",
                "last_plan_id": "demo-plan",
                "workers": {
                    cache_key: {"worker_id": "w1", "token": "tk1", "plan_id": "demo-plan", "control_url": "http://NEW"}
                },
            }
        )
    )
    rc = worker_launch.run_launch_command(
        [
            "demo-plan",
            "--connect",
            "http://NEW",
            "--claude-bin",
            "/usr/bin/claude",
            "--register-only",
            "--config",
            str(cfg_path),
        ]
    )
    assert rc == worker_launch.EXIT_OK
    config = json.loads(cfg_path.read_text())
    assert config["default_control_url"] == "http://NEW"


def test_launch_full_path_invokes_worker_loop_after_register(monkeypatch: pytest.MonkeyPatch, cfg_path: Path) -> None:
    """Without --register-only or --print-env, launch should hand off to
    run_worker_command. With the loop stubbed out it returns immediately.
    """
    monkeypatch.setattr(worker_launch, "_register", _stub_register_factory("w-loop", "tk-loop"))
    worker_calls = _patch_no_worker_loop(monkeypatch)
    rc = worker_launch.run_launch_command(
        [
            "demo-plan",
            "--connect",
            "http://127.0.0.1:8000",
            "--bootstrap-token",
            "boot-tk",
            "--claude-bin",
            "/usr/bin/claude",
            "--config",
            str(cfg_path),
        ]
    )
    assert rc == worker_launch.EXIT_OK
    assert worker_calls == [[]]  # one call, no extra argv
