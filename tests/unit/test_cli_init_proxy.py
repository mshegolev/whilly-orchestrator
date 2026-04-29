"""Unit tests for ``whilly init`` proxy CLI flags + probe wiring (TASK-109-4).

Pins PRD ``docs/PRD-v41-claude-proxy.md`` FR-6 + SC-1 + SC-3:

* ``--claude-proxy URL`` overrides ``WHILLY_CLAUDE_PROXY_URL`` env.
* ``--no-claude-proxy`` opts out even when env is set.
* Probe runs once on startup if proxy is active and
  ``WHILLY_CLAUDE_PROXY_PROBE`` is not ``0``.
* Probe failure → exit 2 (env error) with friendly message.

Strategy: feed a fake headless_runner / tasks_builder / plan_inserter
through the keyword seams of ``run_init_command`` (same pattern as
``test_cli_init.py``) so the suite never spawns a real subprocess and
never hits Postgres. The probe itself is pinned by exercising it
against real listening / closed sockets — no monkey-patching of the
probe function, just the env it consults.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from whilly.adapters.runner.proxy import WHILLY_PROXY_PROBE_ENV, WHILLY_PROXY_URL_ENV
from whilly.cli.init import (
    EXIT_ENVIRONMENT_ERROR,
    EXIT_OK,
    _build_parser,
    run_init_command,
)


# ─── argparse layout (FR-6) ────────────────────────────────────────────────


def test_parser_advertises_proxy_flags() -> None:
    """Both flags appear in --help output."""
    parser = _build_parser()
    help_text = parser.format_help()
    assert "--claude-proxy" in help_text
    assert "--no-claude-proxy" in help_text


def test_parser_proxy_flags_mutually_exclusive() -> None:
    """``--claude-proxy URL --no-claude-proxy`` should be rejected by argparse."""
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["idea", "--claude-proxy", "http://x:1", "--no-claude-proxy"])


def test_parser_proxy_default_none() -> None:
    """Without flags, args.claude_proxy is None and args.no_claude_proxy False."""
    parser = _build_parser()
    args = parser.parse_args(["some idea"])
    assert args.claude_proxy is None
    assert args.no_claude_proxy is False


# ─── helpers (mirror test_cli_init.py shape) ──────────────────────────────


# ``listening_port`` / ``closed_port`` fixtures are auto-discovered from
# ``tests/unit/conftest.py``.


def _make_fake_runner_writes_prd(prd_text: str = "# PRD\n\nfake\n") -> Any:
    def runner(*, idea: str, slug: str, output_dir: Path, model: str) -> int:
        path = Path(output_dir).resolve() / f"PRD-{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prd_text, encoding="utf-8")
        return 0

    return runner


def _fake_tasks_builder() -> Any:
    def builder(*, prd_path: Path, plan_id: str, model: str) -> dict[str, Any]:
        return {
            "project": "Fake",
            "plan_id": plan_id,
            "tasks": [
                {
                    "id": "TASK-001",
                    "status": "pending",
                    "priority": "high",
                    "description": "Synth",
                }
            ],
        }

    return builder


def _fake_plan_inserter() -> Any:
    def inserter(*, payload: dict[str, Any], plan_id: str, dsn: str) -> int:
        return len(payload["tasks"])

    return inserter


# ─── probe wiring (FR-3, SC-1, SC-3) ──────────────────────────────────────


def test_init_probe_runs_when_proxy_set_and_succeeds(
    listening_port: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listening proxy → probe ok → init proceeds to wizard + import."""
    monkeypatch.setenv("WHILLY_DATABASE_URL", "postgresql://fake/test")
    proxy_url = f"http://127.0.0.1:{listening_port}"

    rc = run_init_command(
        ["idea", "--headless", "--claude-proxy", proxy_url, "--output-dir", str(tmp_path)],
        headless_runner=_make_fake_runner_writes_prd(),
        tasks_builder=_fake_tasks_builder(),
        plan_inserter=_fake_plan_inserter(),
    )
    assert rc == EXIT_OK


def test_init_probe_fails_with_friendly_error(
    closed_port: int,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Closed proxy port → exit 2 (env error), no Claude call attempted."""
    proxy_url = f"http://127.0.0.1:{closed_port}"

    # Fake runner deliberately marks itself as called; we'll assert it
    # never fired because the probe should have failed first.
    runner_calls: list[str] = []

    def runner_should_not_fire(**kwargs: Any) -> int:
        runner_calls.append("called")
        return 0

    rc = run_init_command(
        ["idea", "--headless", "--claude-proxy", proxy_url, "--output-dir", str(tmp_path)],
        headless_runner=runner_should_not_fire,
        tasks_builder=_fake_tasks_builder(),
        plan_inserter=_fake_plan_inserter(),
    )

    assert rc == EXIT_ENVIRONMENT_ERROR
    assert runner_calls == [], "wizard should not have run after failed probe"
    err = capsys.readouterr().err
    assert "Claude proxy unreachable" in err
    assert "ssh -fN -L" in err  # actionable hint per FR-3
    assert "WHILLY_CLAUDE_PROXY_PROBE=0" in err  # opt-out hint


def test_init_probe_skipped_when_env_disables(
    closed_port: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``WHILLY_CLAUDE_PROXY_PROBE=0`` skips the probe even on dead port.

    Useful for proxies that legitimately reject bare TCP probes (some
    corporate setups). The user pays the price of a slower failure
    later if the tunnel is genuinely down — that's the trade-off they
    chose by setting the opt-out.
    """
    monkeypatch.setenv("WHILLY_DATABASE_URL", "postgresql://fake/test")
    monkeypatch.setenv(WHILLY_PROXY_PROBE_ENV, "0")
    proxy_url = f"http://127.0.0.1:{closed_port}"

    rc = run_init_command(
        ["idea", "--headless", "--claude-proxy", proxy_url, "--output-dir", str(tmp_path)],
        headless_runner=_make_fake_runner_writes_prd(),
        tasks_builder=_fake_tasks_builder(),
        plan_inserter=_fake_plan_inserter(),
    )
    # No probe ran → fake runner wrote PRD → import succeeded.
    assert rc == EXIT_OK


def test_init_no_proxy_skips_probe_entirely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without any proxy signal, no probe runs (regression-free path)."""
    monkeypatch.setenv("WHILLY_DATABASE_URL", "postgresql://fake/test")
    monkeypatch.delenv(WHILLY_PROXY_URL_ENV, raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)

    rc = run_init_command(
        ["idea", "--headless", "--output-dir", str(tmp_path)],
        headless_runner=_make_fake_runner_writes_prd(),
        tasks_builder=_fake_tasks_builder(),
        plan_inserter=_fake_plan_inserter(),
    )
    assert rc == EXIT_OK


def test_init_no_claude_proxy_flag_disables_probe(
    closed_port: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-claude-proxy`` opts out even with WHILLY_CLAUDE_PROXY_URL set."""
    monkeypatch.setenv("WHILLY_DATABASE_URL", "postgresql://fake/test")
    proxy_url = f"http://127.0.0.1:{closed_port}"
    monkeypatch.setenv(WHILLY_PROXY_URL_ENV, proxy_url)

    rc = run_init_command(
        ["idea", "--headless", "--no-claude-proxy", "--output-dir", str(tmp_path)],
        headless_runner=_make_fake_runner_writes_prd(),
        tasks_builder=_fake_tasks_builder(),
        plan_inserter=_fake_plan_inserter(),
    )
    # Even though env points at a closed port, --no-claude-proxy
    # disabled the proxy entirely → no probe → init succeeds.
    assert rc == EXIT_OK


def test_init_cli_flag_overrides_env_url(
    listening_port: int,
    closed_port: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--claude-proxy`` to a working port wins over env pointing at a dead port.

    Same priority chain as resolve_proxy_settings (CLI > env), exercised
    end-to-end through the run_init_command argv path.
    """
    monkeypatch.setenv("WHILLY_DATABASE_URL", "postgresql://fake/test")
    # Env points at closed port → would fail probe.
    monkeypatch.setenv(WHILLY_PROXY_URL_ENV, f"http://127.0.0.1:{closed_port}")
    # CLI flag points at listening port → wins, probe succeeds.
    cli_url = f"http://127.0.0.1:{listening_port}"

    rc = run_init_command(
        ["idea", "--headless", "--claude-proxy", cli_url, "--output-dir", str(tmp_path)],
        headless_runner=_make_fake_runner_writes_prd(),
        tasks_builder=_fake_tasks_builder(),
        plan_inserter=_fake_plan_inserter(),
    )
    assert rc == EXIT_OK


# ─── CLI → downstream env propagation (regression for review-found bug) ────


def test_init_cli_url_propagates_to_os_environ_for_downstream_spawn(
    listening_port: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--claude-proxy URL`` must be visible to downstream re-resolves.

    ``_call_claude`` / ``_spawn_and_collect`` re-read ``os.environ`` to
    decide whether to inject ``HTTPS_PROXY`` into the spawn. If
    ``run_init_command`` only used the flag for the probe (early bug) and
    never propagated, the spawned Claude would skip the proxy. This test
    pins the propagation by inspecting ``os.environ`` from inside the
    fake runner — that's the exact moment downstream would re-resolve.
    """
    import os

    monkeypatch.setenv("WHILLY_DATABASE_URL", "postgresql://fake/test")
    monkeypatch.delenv(WHILLY_PROXY_URL_ENV, raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    cli_url = f"http://127.0.0.1:{listening_port}"

    seen_in_runner: dict[str, str | None] = {}

    def runner_inspecting_env(*, idea: str, slug: str, output_dir: Path, model: str) -> int:
        seen_in_runner["proxy_url_env"] = os.environ.get(WHILLY_PROXY_URL_ENV)
        path = Path(output_dir).resolve() / f"PRD-{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# PRD\n", encoding="utf-8")
        return 0

    rc = run_init_command(
        ["idea", "--headless", "--claude-proxy", cli_url, "--output-dir", str(tmp_path)],
        headless_runner=runner_inspecting_env,
        tasks_builder=_fake_tasks_builder(),
        plan_inserter=_fake_plan_inserter(),
    )
    assert rc == EXIT_OK
    assert seen_in_runner["proxy_url_env"] == cli_url


def test_init_no_claude_proxy_clears_inherited_env_for_downstream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-claude-proxy`` must remove inherited proxy vars before runner runs.

    Otherwise downstream ``resolve_proxy_settings`` (which only reads
    ``os.environ``) would re-introduce the proxy via the
    ``HTTPS_PROXY`` fallback against the operator's explicit opt-out.
    """
    import os

    monkeypatch.setenv("WHILLY_DATABASE_URL", "postgresql://fake/test")
    monkeypatch.setenv(WHILLY_PROXY_URL_ENV, "http://will-be-cleared:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://also-cleared:1")

    seen_in_runner: dict[str, str | None] = {}

    def runner_inspecting_env(*, idea: str, slug: str, output_dir: Path, model: str) -> int:
        seen_in_runner["proxy_url_env"] = os.environ.get(WHILLY_PROXY_URL_ENV)
        seen_in_runner["https_proxy"] = os.environ.get("HTTPS_PROXY")
        path = Path(output_dir).resolve() / f"PRD-{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# PRD\n", encoding="utf-8")
        return 0

    rc = run_init_command(
        ["idea", "--headless", "--no-claude-proxy", "--output-dir", str(tmp_path)],
        headless_runner=runner_inspecting_env,
        tasks_builder=_fake_tasks_builder(),
        plan_inserter=_fake_plan_inserter(),
    )
    assert rc == EXIT_OK
    assert seen_in_runner["proxy_url_env"] is None
    assert seen_in_runner["https_proxy"] is None
