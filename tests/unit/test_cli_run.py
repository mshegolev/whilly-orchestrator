"""Unit tests for :mod:`whilly.cli.run` — the ``whilly run`` subcommand (TASK-019c).

What we cover
-------------
- Argparse surface: ``--plan`` is required; the optional flags
  (``--max-iterations``, ``--idle-wait``, ``--heartbeat-interval``,
  ``--worker-id``) all parse cleanly.
- DSN resolution: missing ``WHILLY_DATABASE_URL`` exits 2 with a
  diagnostic message rather than crashing inside asyncio.
- Worker-id resolution precedence: CLI flag > env var > auto-generated
  ``<hostname>-<short-uuid>``.
- Plan-not-found path: a missing ``plan_id`` surfaces as exit 2 with a
  diagnostic message — the AC's "При отсутствии плана — exit code 2".
- Dispatcher wiring: ``whilly.cli.main(["run", ...])`` routes into
  :func:`run_run_command` rather than the legacy v3 parser.

What we deliberately *don't* cover here
---------------------------------------
End-to-end pool + worker behaviour belongs in
:mod:`tests.integration.test_local_worker`, which spins up a real
Postgres via testcontainers. These unit tests stop at the boundary
where ``asyncio.run`` would invoke ``create_pool`` — anything past that
needs a real DB.

How we isolate from asyncpg
---------------------------
The DSN-missing test never reaches ``asyncio.run`` (the env check is
synchronous). The plan-not-found test patches
:func:`whilly.cli.run._async_run` to raise ``_PlanNotFoundError``
directly, so we exercise the sync exit-code mapping without spinning up
a connection pool. Patching at module level (not at the source) matches
the resolution rule pytest uses with monkeypatch — we patch the symbol
in :mod:`whilly.cli.run` because that's where ``run_run_command`` looks
it up.
"""

from __future__ import annotations

import asyncio

import pytest

from whilly.cli import run as cli_run
from whilly.cli.run import (
    DATABASE_URL_ENV,
    EXIT_ENVIRONMENT_ERROR,
    EXIT_OK,
    WORKER_ID_ENV,
    _PlanNotFoundError,
    _resolve_worker_id,
    build_run_parser,
    run_run_command,
)
from whilly.worker.local import WorkerStats


# ─── argparse surface ────────────────────────────────────────────────────


def test_build_run_parser_requires_plan() -> None:
    """``--plan`` is mandatory; argparse exits with code 2 (its convention).

    Argparse's ``required=True`` raises :class:`SystemExit` with code 2 on
    missing flags — same numbering the rest of the v4 CLI uses for
    environment failures, but argparse owns that path. Pinning the
    behaviour here means a future refactor that loosens ``required=True``
    is loud at test time.
    """
    parser = build_run_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([])
    assert exc_info.value.code == 2


def test_build_run_parser_accepts_all_optional_flags() -> None:
    """All optional flags parse without choking — surface check, not behaviour.

    The test exists so a typo in ``add_argument`` (``--idle_wait`` vs
    ``--idle-wait``) is caught without spinning up the rest of the
    machinery. argparse normalises dashes to underscores for the dest, so
    we assert against the dest names the run handler reads.
    """
    parser = build_run_parser()
    args = parser.parse_args(
        [
            "--plan",
            "P-1",
            "--max-iterations",
            "5",
            "--idle-wait",
            "0.1",
            "--heartbeat-interval",
            "0.5",
            "--worker-id",
            "test-worker-x",
        ]
    )
    assert args.plan_id == "P-1"
    assert args.max_iterations == 5
    assert args.idle_wait == pytest.approx(0.1)
    assert args.heartbeat_interval == pytest.approx(0.5)
    assert args.worker_id == "test-worker-x"


# ─── worker-id resolution ────────────────────────────────────────────────


def test_resolve_worker_id_prefers_cli_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI flag wins even when the env var is set — most-explicit wins.

    The precedence chain is documented in :func:`_resolve_worker_id`'s
    docstring; pinning it here means a regression that flips CLI <-> env
    priority is caught immediately. Operators rely on the CLI flag taking
    precedence for one-off overrides.
    """
    monkeypatch.setenv(WORKER_ID_ENV, "from-env")
    assert _resolve_worker_id("from-cli") == "from-cli"


def test_resolve_worker_id_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CLI flag → env var wins; generated id is the last resort."""
    monkeypatch.setenv(WORKER_ID_ENV, "env-worker-007")
    assert _resolve_worker_id(None) == "env-worker-007"


def test_resolve_worker_id_generates_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CLI, no env → auto-generated ``<hostname>-<8-hex>``.

    Asserts the *shape*, not exact bytes — uuid4 is non-deterministic.
    The hostname half is delegated to :func:`socket.gethostname` (we
    don't mock it; the real value is fine for shape checking).
    """
    monkeypatch.delenv(WORKER_ID_ENV, raising=False)
    generated = _resolve_worker_id(None)
    host, _, suffix = generated.rpartition("-")
    assert host, f"generated id has no hostname half: {generated!r}"
    assert len(suffix) == 8, f"suffix length is not 8 hex chars: {suffix!r}"
    assert all(c in "0123456789abcdef" for c in suffix), f"suffix is not lowercase hex: {suffix!r}"


# ─── DSN-missing exit path ───────────────────────────────────────────────


def test_run_run_command_exits_2_when_dsn_unset(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing ``WHILLY_DATABASE_URL`` → exit 2 with a stderr diagnostic.

    Mirrors :func:`whilly.cli.plan._run_import`'s behaviour so the v4 CLI
    is uniform: the operator sees the same message shape regardless of
    which subcommand they ran without a DSN. The diagnostic must mention
    the env var name so a fresh user can fix it without grep-ing the
    source.
    """
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    code = run_run_command(["--plan", "P-1"])
    assert code == EXIT_ENVIRONMENT_ERROR
    captured = capsys.readouterr()
    assert DATABASE_URL_ENV in captured.err
    assert "whilly run" in captured.err


# ─── plan-not-found exit path ────────────────────────────────────────────


def test_run_run_command_exits_2_when_plan_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A plan id absent from Postgres → exit 2 with a diagnostic mentioning the id.

    We patch ``_async_run`` to raise ``_PlanNotFoundError`` directly so
    the test never opens a connection pool. The patch site is
    :mod:`whilly.cli.run` (not the source) because that's where
    ``run_run_command`` looks up the symbol.
    """
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://user@127.0.0.1/whilly")

    async def _fake_async_run(**kwargs: object) -> WorkerStats:
        raise _PlanNotFoundError(str(kwargs["plan_id"]))

    monkeypatch.setattr(cli_run, "_async_run", _fake_async_run)

    code = run_run_command(["--plan", "P-MISSING"])
    assert code == EXIT_ENVIRONMENT_ERROR
    captured = capsys.readouterr()
    assert "P-MISSING" in captured.err
    assert "not found" in captured.err.lower()


def test_run_run_command_returns_zero_on_normal_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The happy path: ``_async_run`` returns stats → exit 0 + stats summary.

    Pins the contract that a clean worker termination (max_iterations
    reached or stop set) maps to ``EXIT_OK``. The summary line goes to
    stderr so callers piping stdout (none today, but the import command
    does) keep the discipline of "data on stdout, diagnostics on
    stderr".
    """
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://user@127.0.0.1/whilly")

    async def _fake_async_run(**kwargs: object) -> WorkerStats:
        return WorkerStats(iterations=3, completed=2, failed=0, idle_polls=1)

    monkeypatch.setattr(cli_run, "_async_run", _fake_async_run)

    code = run_run_command(["--plan", "P-OK", "--worker-id", "w-test"])
    assert code == EXIT_OK
    captured = capsys.readouterr()
    # The summary line carries the stats so operators see at-a-glance how
    # the run went without scraping logs.
    assert "iterations=3" in captured.err
    assert "completed=2" in captured.err
    assert "w-test" in captured.err


# ─── dispatcher wiring ───────────────────────────────────────────────────


def test_main_dispatches_run_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    """``whilly run ...`` reaches :func:`run_run_command` rather than the legacy parser.

    Regression guard for the dispatcher — without this, a refactor that
    forgot to add the ``run`` branch in :func:`whilly.cli.main` would
    silently route ``whilly run --plan X`` into the v3 argument parser,
    which would then complain about an unknown argument with a
    confusing diagnostic.
    """
    captured: dict[str, object] = {}

    def _fake_run_run_command(
        argv: object,
        *,
        runner: object | None = None,
        install_signal_handlers: bool = True,
    ) -> int:
        captured["argv"] = list(argv) if isinstance(argv, list) else argv
        return 0

    monkeypatch.setattr(cli_run, "run_run_command", _fake_run_run_command)

    from whilly.cli import main as dispatch_main

    code = dispatch_main(["run", "--plan", "P-D"])
    assert code == 0
    assert captured["argv"] == ["--plan", "P-D"]


# ─── runner injection seam ───────────────────────────────────────────────


def test_run_run_command_forwards_injected_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test-supplied ``runner`` reaches ``_async_run`` instead of ``run_task``.

    The injection seam is the only way unit tests can stub the agent
    layer; without this, every ``whilly run`` test would need a real
    Claude binary. We assert the same callable lands in ``_async_run`` —
    the ``runner`` kwarg is the contract the integration test relies on
    later for the fake-agent fixture.
    """
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://user@127.0.0.1/whilly")
    seen_runner: list[object] = []

    async def _stub_runner(task: object, prompt: str) -> object:  # pragma: no cover — never invoked
        return None

    async def _fake_async_run(**kwargs: object) -> WorkerStats:
        seen_runner.append(kwargs["runner"])
        return WorkerStats()

    monkeypatch.setattr(cli_run, "_async_run", _fake_async_run)

    code = run_run_command(["--plan", "P-INJ"], runner=_stub_runner)
    assert code == EXIT_OK
    assert seen_runner == [_stub_runner], "runner kwarg did not reach _async_run unchanged"


def test_asyncio_run_is_used_for_async_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_run_command`` keeps a sync surface even though the work is async.

    The call graph is sync → ``asyncio.run`` → ``_async_run``. Smoke-test
    that ``asyncio.run`` is what bridges them: a regression that swapped
    in ``loop.run_until_complete`` would break callers in environments
    that already own a running loop (Jupyter, FastAPI lifespan).
    """
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://user@127.0.0.1/whilly")

    async def _fake_async_run(**kwargs: object) -> WorkerStats:
        return WorkerStats()

    monkeypatch.setattr(cli_run, "_async_run", _fake_async_run)

    seen_calls: list[object] = []
    original_run = asyncio.run

    def _spy_run(coro: object) -> object:
        seen_calls.append(coro)
        return original_run(coro)  # type: ignore[arg-type]

    monkeypatch.setattr(cli_run.asyncio, "run", _spy_run)

    code = run_run_command(["--plan", "P-ASY"])
    assert code == EXIT_OK
    assert len(seen_calls) == 1, "asyncio.run was not called exactly once"
