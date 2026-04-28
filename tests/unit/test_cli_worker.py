"""Unit tests for :mod:`whilly.cli.worker` — the ``whilly-worker`` console script (TASK-022c).

What we cover
-------------
- Argparse surface: every documented flag parses cleanly without
  ``required=True`` (the diagnostics live in :func:`run_worker_command`,
  which we exercise below). Pinning the surface here means a typo in
  ``add_argument`` (``--once_mode`` vs ``--once``) is caught without
  spinning up the rest of the machinery.
- Env / flag resolution precedence: CLI flag > env var > error/auto-gen.
  Same precedence chain as :mod:`whilly.cli.run`; pinned here so a
  regression that flipped CLI <-> env priority can't slip through.
- Missing-required diagnostics: each of ``--connect`` / ``--token`` /
  ``--plan`` exits ``EXIT_ENVIRONMENT_ERROR`` (= 2) with a stderr message
  that names the env var. The token branch is the AC-load-bearing one
  ("Отсутствие токена → exit 2 с подсказкой") — the others mirror the
  same shape so operators see one consistent diagnostic.
- ``--once`` translation: the flag wires through to
  :func:`run_remote_worker_with_heartbeat`'s ``max_processed=1``. We
  exercise this by patching the internal ``_async_worker`` and asserting
  on the kwargs the CLI forwards.
- Worker-id resolution: CLI flag > env > auto-generated
  ``<hostname>-<8-hex>``. Same shape as :mod:`whilly.cli.run`.
- Runner injection seam: a test-supplied runner reaches ``_async_worker``
  unchanged. The seam is what lets unit tests stub the agent layer
  without spawning the Claude binary.
- Console-script entry: :func:`main` returns the same int code
  :func:`run_worker_command` produces.

What we deliberately *don't* cover here
---------------------------------------
End-to-end HTTP behaviour belongs in
:mod:`tests.integration.test_remote_worker_*`, which spins up a real
testcontainers Postgres + FastAPI app. These unit tests stop at the
boundary where ``asyncio.run`` would invoke the actual transport client
— anything past that needs the server.

How we isolate from the network
-------------------------------
Tests patch :func:`whilly.cli.worker._async_worker` to skip the httpx
client construction entirely. The synchronous wrapper
(``run_worker_command``) is what owns env validation and exit-code
mapping; ``_async_worker`` is the place where the real RPC stack would
fire, and substituting an in-process stub is the cleanest way to exercise
the wrapper without booting anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from whilly.cli import worker as cli_worker
from whilly.cli.worker import (
    CONTROL_URL_ENV,
    EXIT_ENVIRONMENT_ERROR,
    EXIT_OK,
    PLAN_ID_ENV,
    WORKER_ID_ENV,
    WORKER_TOKEN_ENV,
    _resolve_worker_id,
    build_worker_parser,
    main,
    run_worker_command,
)
from whilly.worker.remote import DEFAULT_HEARTBEAT_INTERVAL, RemoteWorkerStats

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _clear_worker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe every env var the worker CLI consults.

    Centralised so each test starts from a known-empty env — without this,
    a test that ran before us in the same process could leave a
    ``WHILLY_WORKER_TOKEN`` behind and silently turn a ``--token``
    omission test into a passing case for the wrong reason.
    """
    for var in (CONTROL_URL_ENV, WORKER_TOKEN_ENV, PLAN_ID_ENV, WORKER_ID_ENV):
        monkeypatch.delenv(var, raising=False)


def _make_async_worker_recorder(
    return_stats: RemoteWorkerStats | None = None,
) -> tuple[list[dict[str, Any]], Callable[..., Awaitable[RemoteWorkerStats]]]:
    """Build a stub ``_async_worker`` that records its kwargs.

    Returns ``(captured, fake)`` — the test asserts against ``captured``
    after invoking ``run_worker_command``. We return the stats supplied
    by the caller so happy-path tests can pin the stderr summary.
    """
    captured: list[dict[str, Any]] = []
    canned = return_stats if return_stats is not None else RemoteWorkerStats()

    async def _fake(**kwargs: Any) -> RemoteWorkerStats:
        captured.append(kwargs)
        return canned

    return captured, _fake


# ---------------------------------------------------------------------------
# Argparse surface
# ---------------------------------------------------------------------------


def test_build_worker_parser_accepts_no_required_flags() -> None:
    """Argparse surface: no ``required=True`` flags — diagnostics live in run_worker_command.

    The hand-rolled validation in :func:`run_worker_command` is the place
    that names the env var in the diagnostic; argparse's default "the
    following arguments are required:" message would hide the env
    override. Pinning this surface check means a regression that flipped
    ``--token`` to ``required=True`` would surface here rather than as a
    confusing diagnostic in production.
    """
    parser = build_worker_parser()
    args = parser.parse_args([])
    assert args.connect_url is None
    assert args.token is None
    assert args.plan_id is None
    assert args.worker_id is None
    assert args.once is False
    assert args.heartbeat_interval is None
    assert args.max_iterations is None


def test_build_worker_parser_accepts_all_flags() -> None:
    """All optional flags parse without choking — surface check, not behaviour.

    Catches typos in ``add_argument`` (``--max_iterations`` vs
    ``--max-iterations``); argparse normalises dashes to underscores for
    ``dest``, so we assert against the dest names the run handler reads.
    """
    parser = build_worker_parser()
    args = parser.parse_args(
        [
            "--connect",
            "http://control:8000",
            "--token",
            "secret",
            "--plan",
            "P-1",
            "--worker-id",
            "test-worker-x",
            "--once",
            "--heartbeat-interval",
            "0.5",
            "--max-iterations",
            "3",
        ]
    )
    assert args.connect_url == "http://control:8000"
    assert args.token == "secret"
    assert args.plan_id == "P-1"
    assert args.worker_id == "test-worker-x"
    assert args.once is True
    assert args.heartbeat_interval == pytest.approx(0.5)
    assert args.max_iterations == 3


# ---------------------------------------------------------------------------
# Worker-id resolution (mirrors test_cli_run)
# ---------------------------------------------------------------------------


def test_resolve_worker_id_prefers_cli_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI flag wins even when the env var is set — most-explicit wins.

    Pinning the precedence here means a regression that flipped CLI <->
    env priority is caught immediately. Operators rely on the CLI flag
    taking precedence for one-off overrides — same behaviour as
    :func:`whilly.cli.run._resolve_worker_id`.
    """
    monkeypatch.setenv(WORKER_ID_ENV, "from-env")
    assert _resolve_worker_id("from-cli") == "from-cli"


def test_resolve_worker_id_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WORKER_ID_ENV, "env-worker-007")
    assert _resolve_worker_id(None) == "env-worker-007"


def test_resolve_worker_id_generates_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CLI, no env → auto-generated ``<hostname>-<8-hex>``.

    Asserts the *shape*, not exact bytes — uuid4 is non-deterministic.
    The hostname half is delegated to :func:`socket.gethostname` (we
    don't mock it; the real value is fine for shape checking). Same
    expectation as :func:`whilly.cli.run._resolve_worker_id`.
    """
    monkeypatch.delenv(WORKER_ID_ENV, raising=False)
    generated = _resolve_worker_id(None)
    host, _, suffix = generated.rpartition("-")
    assert host, f"generated id has no hostname half: {generated!r}"
    assert len(suffix) == 8, f"suffix length is not 8 hex chars: {suffix!r}"
    assert all(c in "0123456789abcdef" for c in suffix), f"suffix is not lowercase hex: {suffix!r}"


# ---------------------------------------------------------------------------
# Missing-required diagnostics — each input gets its own exit-2 path
# ---------------------------------------------------------------------------


def test_missing_connect_exits_2_with_hint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No ``--connect`` and no ``WHILLY_CONTROL_URL`` → exit 2 + diagnostic.

    The diagnostic must name the env var so a fresh user can fix it
    without grep-ing the source — same UX bar as :mod:`whilly.cli.run`.
    """
    _clear_worker_env(monkeypatch)
    code = run_worker_command(["--token", "X", "--plan", "P-1"])
    assert code == EXIT_ENVIRONMENT_ERROR
    captured = capsys.readouterr()
    assert CONTROL_URL_ENV in captured.err
    assert "--connect" in captured.err


def test_missing_token_exits_2_with_hint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-load-bearing: missing ``--token`` exits 2 with a hint that names the env var.

    "Отсутствие токена → exit 2 с подсказкой" is the literal AC for
    TASK-022c — this test is the canonical regression guard for it.
    """
    _clear_worker_env(monkeypatch)
    code = run_worker_command(["--connect", "http://control:8000", "--plan", "P-1"])
    assert code == EXIT_ENVIRONMENT_ERROR
    captured = capsys.readouterr()
    assert WORKER_TOKEN_ENV in captured.err
    assert "--token" in captured.err


def test_missing_plan_exits_2_with_hint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No ``--plan`` and no ``WHILLY_PLAN_ID`` → exit 2 + diagnostic.

    Symmetric with the connect / token branches — the AC doesn't
    mention plan explicitly, but the worker can't claim without one
    (the server's ``POST /tasks/claim`` filter requires plan_id), so
    surfacing the missing input as a diagnostic rather than a runtime
    schema error is the right shape.
    """
    _clear_worker_env(monkeypatch)
    code = run_worker_command(["--connect", "http://control:8000", "--token", "X"])
    assert code == EXIT_ENVIRONMENT_ERROR
    captured = capsys.readouterr()
    assert PLAN_ID_ENV in captured.err
    assert "--plan" in captured.err


# ---------------------------------------------------------------------------
# Env vars resolve when CLI flags are absent
# ---------------------------------------------------------------------------


def test_env_vars_satisfy_required_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three required inputs come from env → run_worker_command reaches _async_worker.

    Pin the precedence chain end-to-end: with no CLI flags but every env
    var set, the run handler must populate the loop's kwargs from the
    env. A regression that ignored env in any one of the three inputs
    would surface as a missing-required diagnostic here.
    """
    monkeypatch.setenv(CONTROL_URL_ENV, "http://env-control:9000")
    monkeypatch.setenv(WORKER_TOKEN_ENV, "env-token")
    monkeypatch.setenv(PLAN_ID_ENV, "env-plan")
    monkeypatch.setenv(WORKER_ID_ENV, "env-worker")

    captured, fake = _make_async_worker_recorder()
    monkeypatch.setattr(cli_worker, "_async_worker", fake)

    code = run_worker_command([])
    assert code == EXIT_OK
    assert len(captured) == 1
    kwargs = captured[0]
    assert kwargs["connect_url"] == "http://env-control:9000"
    assert kwargs["token"] == "env-token"
    assert kwargs["plan_id"] == "env-plan"
    assert kwargs["worker_id"] == "env-worker"


def test_cli_flags_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI flag wins over env for every input — most-explicit wins.

    Mirrors :func:`_resolve_worker_id`'s precedence rule but applied to
    the three connection-level inputs. Without this regression guard, a
    refactor that read env *after* the CLI value could silently mask the
    operator's one-off override (the same foot-gun :mod:`whilly.cli.run`
    avoids).
    """
    monkeypatch.setenv(CONTROL_URL_ENV, "http://env-control:9000")
    monkeypatch.setenv(WORKER_TOKEN_ENV, "env-token")
    monkeypatch.setenv(PLAN_ID_ENV, "env-plan")

    captured, fake = _make_async_worker_recorder()
    monkeypatch.setattr(cli_worker, "_async_worker", fake)

    code = run_worker_command(
        [
            "--connect",
            "http://cli-control:8000",
            "--token",
            "cli-token",
            "--plan",
            "cli-plan",
        ]
    )
    assert code == EXIT_OK
    kwargs = captured[0]
    assert kwargs["connect_url"] == "http://cli-control:8000"
    assert kwargs["token"] == "cli-token"
    assert kwargs["plan_id"] == "cli-plan"


# ---------------------------------------------------------------------------
# --once translates to max_processed=1
# ---------------------------------------------------------------------------


def test_once_flag_sets_max_processed_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--once`` → ``_async_worker(..., max_processed=1)``.

    AC-load-bearing: the ``--once`` flag is the user-visible knob; we
    pin its translation to the loop's ``max_processed`` kwarg so a
    refactor that moved the once-semantics into a different layer
    surfaces here. Without ``--once`` the kwarg is None (uncapped).
    """
    _clear_worker_env(monkeypatch)
    captured, fake = _make_async_worker_recorder()
    monkeypatch.setattr(cli_worker, "_async_worker", fake)

    code = run_worker_command(
        [
            "--connect",
            "http://control:8000",
            "--token",
            "X",
            "--plan",
            "P-1",
            "--once",
        ]
    )
    assert code == EXIT_OK
    assert captured[0]["max_processed"] == 1


def test_no_once_flag_leaves_max_processed_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``--once`` the loop runs uncapped — ``max_processed`` is None.

    Symmetric counterpart to the once test — without this, a refactor
    that defaulted ``max_processed`` to 1 would silently turn every
    invocation into a one-shot run.
    """
    _clear_worker_env(monkeypatch)
    captured, fake = _make_async_worker_recorder()
    monkeypatch.setattr(cli_worker, "_async_worker", fake)

    code = run_worker_command(
        [
            "--connect",
            "http://control:8000",
            "--token",
            "X",
            "--plan",
            "P-1",
        ]
    )
    assert code == EXIT_OK
    assert captured[0]["max_processed"] is None


# ---------------------------------------------------------------------------
# Defaults on optional flags
# ---------------------------------------------------------------------------


def test_default_heartbeat_interval_is_loop_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``--heartbeat-interval`` → loop's :data:`DEFAULT_HEARTBEAT_INTERVAL`.

    Pin the default so a refactor that changed the loop constant
    silently shifts the worker cadence. The CLI must defer to the loop's
    own default rather than declaring its own.
    """
    _clear_worker_env(monkeypatch)
    captured, fake = _make_async_worker_recorder()
    monkeypatch.setattr(cli_worker, "_async_worker", fake)

    code = run_worker_command(["--connect", "u", "--token", "t", "--plan", "p"])
    assert code == EXIT_OK
    assert captured[0]["heartbeat_interval"] == pytest.approx(DEFAULT_HEARTBEAT_INTERVAL)


def test_max_iterations_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``--max-iterations`` → uncapped loop.

    Production default. Tests pass a value to make the loop terminable.
    """
    _clear_worker_env(monkeypatch)
    captured, fake = _make_async_worker_recorder()
    monkeypatch.setattr(cli_worker, "_async_worker", fake)

    code = run_worker_command(["--connect", "u", "--token", "t", "--plan", "p"])
    assert code == EXIT_OK
    assert captured[0]["max_iterations"] is None


# ---------------------------------------------------------------------------
# Stats summary on stderr
# ---------------------------------------------------------------------------


def test_stats_summary_goes_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Happy-path summary: ``_async_worker`` returns stats → stderr summary line.

    Pins the contract that the operator-visible summary lands on stderr,
    not stdout — same convention as :mod:`whilly.cli.run`. A future test
    might want stdout for structured JSON output (à la
    :mod:`whilly.cli.plan` export); the stderr summary keeps that
    surface free.
    """
    _clear_worker_env(monkeypatch)
    canned = RemoteWorkerStats(
        iterations=4,
        completed=2,
        failed=1,
        idle_polls=1,
        released_on_shutdown=0,
    )
    _, fake = _make_async_worker_recorder(return_stats=canned)
    monkeypatch.setattr(cli_worker, "_async_worker", fake)

    code = run_worker_command(
        [
            "--connect",
            "http://control:8000",
            "--token",
            "X",
            "--plan",
            "P-1",
            "--worker-id",
            "w-test",
        ]
    )
    assert code == EXIT_OK
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "iterations=4" in captured.err
    assert "completed=2" in captured.err
    assert "failed=1" in captured.err
    assert "released_on_shutdown=0" in captured.err
    assert "w-test" in captured.err


# ---------------------------------------------------------------------------
# Runner injection seam
# ---------------------------------------------------------------------------


def test_runner_kwarg_reaches_async_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test-supplied ``runner`` reaches ``_async_worker`` instead of ``run_task``.

    The injection seam is the only way unit tests can stub the agent
    layer; without this, every ``whilly-worker`` test would need a real
    Claude binary. Same contract as :func:`whilly.cli.run.run_run_command`.
    """
    _clear_worker_env(monkeypatch)
    captured, fake = _make_async_worker_recorder()
    monkeypatch.setattr(cli_worker, "_async_worker", fake)

    async def _stub_runner(task: object, prompt: str) -> object:  # pragma: no cover — never invoked
        return None

    code = run_worker_command(
        [
            "--connect",
            "http://control:8000",
            "--token",
            "X",
            "--plan",
            "P-1",
        ],
        runner=_stub_runner,  # type: ignore[arg-type]  # test stub satisfies the structural alias
    )
    assert code == EXIT_OK
    assert captured[0]["runner"] is _stub_runner


def test_install_signal_handlers_kwarg_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    """``install_signal_handlers`` reaches ``_async_worker`` unchanged.

    Tests that drive the CLI from inside pytest-asyncio's loop pass
    ``False`` to skip the asyncio.add_signal_handler call (which raises
    from non-main threads). Pinning the forward here means the test
    harness can rely on the toggle staying wired through.
    """
    _clear_worker_env(monkeypatch)
    captured, fake = _make_async_worker_recorder()
    monkeypatch.setattr(cli_worker, "_async_worker", fake)

    code = run_worker_command(
        [
            "--connect",
            "http://control:8000",
            "--token",
            "X",
            "--plan",
            "P-1",
        ],
        install_signal_handlers=False,
    )
    assert code == EXIT_OK
    assert captured[0]["install_signal_handlers"] is False


# ---------------------------------------------------------------------------
# Console-script wiring
# ---------------------------------------------------------------------------


def test_main_returns_run_worker_command_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main([...])`` returns the int :func:`run_worker_command` produces.

    Tests the console-script glue without poking at ``sys.argv``. The
    pyproject.toml ``[project.scripts]`` entry routes ``whilly-worker``
    to :func:`main`; a regression that returned None or swallowed the
    code would surface here.
    """
    _clear_worker_env(monkeypatch)
    code = main(["--connect", "http://control:8000", "--token", "X"])
    # No --plan and no env var → exit 2 from the validation branch.
    assert code == EXIT_ENVIRONMENT_ERROR


def test_main_reads_sys_argv_when_argv_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling ``main()`` with no argv reads from :data:`sys.argv`.

    Mirrors the entry-point contract Python's setuptools-installed
    ``whilly-worker`` script relies on: ``main()`` is invoked with no
    arguments and must read ``sys.argv[1:]``. Patching the argv list
    rather than spawning a subprocess keeps the test fast.
    """
    _clear_worker_env(monkeypatch)
    monkeypatch.setattr(
        "sys.argv",
        ["whilly-worker", "--connect", "http://control:8000"],
    )
    # Token still missing → exit 2; the assertion is that main() pulled
    # argv from sys.argv (not the connect / token mismatch — the env
    # validation runs in --connect order so we'd see the token diagnostic).
    code = main()
    assert code == EXIT_ENVIRONMENT_ERROR


# ---------------------------------------------------------------------------
# asyncio bridging
# ---------------------------------------------------------------------------


def test_asyncio_run_is_used_for_async_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_worker_command`` keeps a sync surface even though the work is async.

    The call graph is sync → ``asyncio.run`` → ``_async_worker``. Smoke-test
    that ``asyncio.run`` is what bridges them: a regression that swapped
    in ``loop.run_until_complete`` would break callers in environments
    that already own a running loop (Jupyter, FastAPI lifespan).
    """
    _clear_worker_env(monkeypatch)
    _, fake = _make_async_worker_recorder()
    monkeypatch.setattr(cli_worker, "_async_worker", fake)

    seen_calls: list[object] = []
    original_run = asyncio.run

    def _spy_run(coro: object) -> object:
        seen_calls.append(coro)
        return original_run(coro)  # type: ignore[arg-type]

    monkeypatch.setattr(cli_worker.asyncio, "run", _spy_run)

    code = run_worker_command(
        [
            "--connect",
            "http://control:8000",
            "--token",
            "X",
            "--plan",
            "P-1",
        ]
    )
    assert code == EXIT_OK
    assert len(seen_calls) == 1, "asyncio.run was not called exactly once"
