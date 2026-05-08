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
from whilly.ci.github import GitHubCIPollAdapter
from whilly.core.models import Plan, Task, TaskStatus, VerificationCommand
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
            "--verify-command",
            "unit=pytest -q tests/unit",
            "--optional-verify-command",
            "lint=ruff check whilly tests",
            "--verify-timeout",
            "12.5",
        ]
    )
    assert args.plan_id == "P-1"
    assert args.max_iterations == 5
    assert args.idle_wait == pytest.approx(0.1)
    assert args.heartbeat_interval == pytest.approx(0.5)
    assert args.worker_id == "test-worker-x"
    assert args.verify_commands == ["unit=pytest -q tests/unit"]
    assert args.optional_verify_commands == ["lint=ruff check whilly tests"]
    assert args.verify_timeout == pytest.approx(12.5)


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


def test_run_run_command_forwards_verification_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verification flags are parsed at the CLI boundary and forwarded to the async composer."""
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://user@127.0.0.1/whilly")
    seen: dict[str, object] = {}

    async def _fake_async_run(**kwargs: object) -> WorkerStats:
        seen.update(kwargs)
        return WorkerStats()

    monkeypatch.setattr(cli_run, "_async_run", _fake_async_run)

    code = run_run_command(
        [
            "--plan",
            "P-VERIFY",
            "--verify-command",
            "unit=pytest -q tests/unit",
            "--optional-verify-command",
            "lint=ruff check whilly tests",
            "--verify-timeout",
            "10",
        ]
    )

    assert code == EXIT_OK
    assert seen["verify_commands"] == ["unit=pytest -q tests/unit"]
    assert seen["optional_verify_commands"] == ["lint=ruff check whilly tests"]
    assert seen["verify_timeout"] == pytest.approx(10.0)


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


# ─── verification runner composition ─────────────────────────────────────


class _FakeConnection:
    async def execute(self, *_args: object) -> None:
        return None


class _FakeAcquire:
    async def __aenter__(self) -> _FakeConnection:
        return _FakeConnection()

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakePool:
    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire()


async def _capture_async_run_verification_specs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile_commands: tuple[VerificationCommand, ...] = (),
    verify_commands: tuple[str, ...] = (),
    optional_verify_commands: tuple[str, ...] = (),
) -> tuple[object, ...] | None:
    task = Task(id="T-VERIFY", status=TaskStatus.PENDING)
    plan = Plan(
        id="P-VERIFY",
        name="Verification plan",
        tasks=(task,),
        verification_commands=profile_commands,
    )
    captured: dict[str, object] = {}

    async def _fake_create_pool(_dsn: str) -> _FakePool:
        return _FakePool()

    async def _fake_close_pool(_pool: object) -> None:
        captured["closed"] = True

    async def _fake_select_plan_with_tasks(_conn: object, _plan_id: str) -> tuple[Plan, tuple[Task, ...]]:
        return plan, (task,)

    async def _fake_run_verification_commands(commands: object, **_kwargs: object) -> object:
        captured["commands"] = tuple(commands)  # type: ignore[arg-type]
        return object()

    async def _fake_run_worker(
        _repo: object,
        _workspace_runner: object,
        worker_plan: Plan,
        _worker_id: str,
        *,
        verification_runner: object | None,
        **_kwargs: object,
    ) -> WorkerStats:
        captured["plan"] = worker_plan
        captured["verification_runner_present"] = verification_runner is not None
        if verification_runner is not None:
            await verification_runner(task)  # type: ignore[misc]
        return WorkerStats()

    async def _stub_runner(_task: object, _prompt: str) -> object:
        return object()

    monkeypatch.setattr(cli_run, "create_pool", _fake_create_pool)
    monkeypatch.setattr(cli_run, "close_pool", _fake_close_pool)
    monkeypatch.setattr(cli_run, "_select_plan_with_tasks", _fake_select_plan_with_tasks)
    monkeypatch.setattr(cli_run, "run_verification_commands", _fake_run_verification_commands)
    monkeypatch.setattr(cli_run, "run_worker", _fake_run_worker)
    monkeypatch.setattr(cli_run, "TaskRepository", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli_run, "RepoTargetWorkspaceResolver", lambda _repo: object())
    monkeypatch.setattr(cli_run, "is_auto_open_pr_enabled", lambda: False)

    await cli_run._async_run(
        dsn="postgresql://user@127.0.0.1/whilly",
        plan_id=plan.id,
        worker_id="w-verify",
        runner=_stub_runner,
        max_iterations=1,
        idle_wait=0,
        heartbeat_interval=1,
        install_signal_handlers=False,
        verify_commands=verify_commands,
        optional_verify_commands=optional_verify_commands,
        verify_timeout=5,
    )

    assert captured["closed"] is True
    assert captured["plan"] == plan
    assert captured["verification_runner_present"] == bool(
        profile_commands or verify_commands or optional_verify_commands
    )
    return captured.get("commands")  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_async_run_creates_verification_runner_for_profile_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = await _capture_async_run_verification_specs(
        monkeypatch,
        profile_commands=(
            VerificationCommand(
                name="profile-unit",
                command="pytest -q tests/unit",
                required=True,
            ),
        ),
    )

    assert commands is not None
    assert [(command.name, command.source, command.required) for command in commands] == [
        ("profile-unit", "profile", True),
    ]


@pytest.mark.asyncio
async def test_async_run_orders_profile_required_cli_optional_cli_specs(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = await _capture_async_run_verification_specs(
        monkeypatch,
        profile_commands=(
            VerificationCommand(
                name="profile-unit",
                command="pytest -q tests/unit",
                required=True,
            ),
        ),
        verify_commands=("cli-required=pytest -q",),
        optional_verify_commands=("cli-optional=ruff check whilly",),
    )

    assert commands is not None
    assert [command.source for command in commands] == ["profile", "cli", "cli"]
    assert [command.name for command in commands] == ["profile-unit", "cli-required", "cli-optional"]
    assert [command.required for command in commands] == [True, True, False]


@pytest.mark.asyncio
async def test_async_run_preserves_cli_only_verification_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = await _capture_async_run_verification_specs(
        monkeypatch,
        verify_commands=("unit=pytest -q tests/unit",),
        optional_verify_commands=("lint=ruff check whilly",),
    )

    assert commands is not None
    assert [(command.name, command.command, command.source, command.required) for command in commands] == [
        ("unit", "pytest -q tests/unit", "cli", True),
        ("lint", "ruff check whilly", "cli", False),
    ]


@pytest.mark.asyncio
async def test_async_run_passes_ci_poll_runner_for_ci_source(monkeypatch: pytest.MonkeyPatch) -> None:
    task = Task(id="T-CI", status=TaskStatus.PENDING)
    plan = Plan(
        id="P-CI",
        name="CI plan",
        tasks=(task,),
        verification_commands=(
            VerificationCommand(
                name="ci-status",
                command="ci://github/acme/widgets#pr-42",
                required=True,
                source="ci",
            ),
        ),
    )
    captured: dict[str, object] = {}

    async def _fake_create_pool(_dsn: str) -> _FakePool:
        return _FakePool()

    async def _fake_close_pool(_pool: object) -> None:
        return None

    async def _fake_select_plan_with_tasks(_conn: object, _plan_id: str) -> tuple[Plan, tuple[Task, ...]]:
        return plan, (task,)

    async def _fake_run_verification_commands(commands: object, **kwargs: object) -> object:
        captured["commands"] = tuple(commands)  # type: ignore[arg-type]
        captured["ci_poll_runner"] = kwargs.get("ci_poll_runner")
        return object()

    async def _fake_run_worker(
        _repo: object,
        _workspace_runner: object,
        worker_plan: Plan,
        _worker_id: str,
        *,
        verification_runner: object | None,
        **_kwargs: object,
    ) -> WorkerStats:
        captured["plan"] = worker_plan
        assert verification_runner is not None
        await verification_runner(task)  # type: ignore[misc]
        return WorkerStats()

    async def _stub_runner(_task: object, _prompt: str) -> object:
        return object()

    monkeypatch.setattr(cli_run, "create_pool", _fake_create_pool)
    monkeypatch.setattr(cli_run, "close_pool", _fake_close_pool)
    monkeypatch.setattr(cli_run, "_select_plan_with_tasks", _fake_select_plan_with_tasks)
    monkeypatch.setattr(cli_run, "run_verification_commands", _fake_run_verification_commands)
    monkeypatch.setattr(cli_run, "run_worker", _fake_run_worker)
    monkeypatch.setattr(cli_run, "TaskRepository", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli_run, "RepoTargetWorkspaceResolver", lambda _repo: object())
    monkeypatch.setattr(cli_run, "is_auto_open_pr_enabled", lambda: False)

    await cli_run._async_run(
        dsn="postgresql://user@127.0.0.1/whilly",
        plan_id=plan.id,
        worker_id="w-ci",
        runner=_stub_runner,
        max_iterations=1,
        idle_wait=0,
        heartbeat_interval=1,
        install_signal_handlers=False,
        verify_timeout=5,
    )

    assert [command.source for command in captured["commands"]] == ["ci"]  # type: ignore[index]
    assert isinstance(captured["ci_poll_runner"], GitHubCIPollAdapter)
