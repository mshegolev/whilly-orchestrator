"""Unit tests for the M2 funnel-URL polling + rotation supervisor.

Covers the worker-side complement to ``m2-localhostrun-funnel-sidecar``:

* :class:`StaticUrlSource` is byte-equivalent to v4.4 (no polling).
* :class:`FileUrlSource` reads ``/funnel/url.txt`` (or its override) and
  reports rotations on subsequent polls.
* :func:`make_funnel_url_source` parses the env-var contract: source
  selection, file path, poll cadence, and rejects malformed inputs.
* :func:`run_remote_worker_with_url_rotation` tears down the in-flight
  client when the source publishes a new URL, releases any in-progress
  task via ``client.release``, opens a fresh client against the new
  URL, and resumes the long-poll loop with the *same* ``worker_id`` /
  bearer (no duplicate-worker register on the control plane).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from whilly.adapters.runner.result_parser import AgentResult
from whilly.core.models import Plan, Priority, Task, TaskStatus, WorkerId
from whilly.worker.funnel import (
    DEFAULT_FILE_POLL_SECONDS,
    DEFAULT_POSTGRES_POLL_SECONDS,
    FUNNEL_DATABASE_URL_ENV,
    FUNNEL_URL_FILE_ENV,
    FUNNEL_URL_POLL_SECONDS_ENV,
    FUNNEL_URL_SOURCE_ENV,
    FileUrlSource,
    FunnelUrlSourceError,
    PostgresUrlSource,
    StaticUrlSource,
    make_funnel_url_source,
)
from whilly.worker.remote import (
    RotationStats,
    run_remote_worker_with_url_rotation,
)


WORKER_ID: WorkerId = "worker-rotation-test"
PLAN_ID = "plan-rotation-test"


def _make_plan() -> Plan:
    return Plan(id=PLAN_ID, name="rotation test plan")


def _make_task(task_id: str = "T-1", *, version: int = 1) -> Task:
    return Task(
        id=task_id,
        status=TaskStatus.CLAIMED,
        priority=Priority.MEDIUM,
        description=f"task {task_id}",
        version=version,
    )


# --------------------------------------------------------------------------- #
# StaticUrlSource
# --------------------------------------------------------------------------- #


async def test_static_url_source_returns_seed_url_with_infinite_poll() -> None:
    src = StaticUrlSource("https://control.example/")
    assert await src.fetch() == "https://control.example/"
    assert src.poll_interval == float("inf")
    await src.aclose()


async def test_static_url_source_rejects_empty_seed() -> None:
    with pytest.raises(FunnelUrlSourceError):
        StaticUrlSource("")
    with pytest.raises(FunnelUrlSourceError):
        StaticUrlSource("   ")


# --------------------------------------------------------------------------- #
# FileUrlSource
# --------------------------------------------------------------------------- #


async def test_file_url_source_returns_none_when_missing(tmp_path: Path) -> None:
    src = FileUrlSource(tmp_path / "absent.txt", poll_interval=0.01)
    assert await src.fetch() is None


async def test_file_url_source_reads_published_url(tmp_path: Path) -> None:
    file = tmp_path / "url.txt"
    file.write_text("https://fake-1.lhr.life\n", encoding="utf-8")
    src = FileUrlSource(file, poll_interval=0.01)
    assert await src.fetch() == "https://fake-1.lhr.life"


async def test_file_url_source_treats_blank_file_as_missing(tmp_path: Path) -> None:
    file = tmp_path / "url.txt"
    file.write_text("   \n\n", encoding="utf-8")
    src = FileUrlSource(file, poll_interval=0.01)
    assert await src.fetch() is None


async def test_file_url_source_rejects_non_positive_poll() -> None:
    with pytest.raises(FunnelUrlSourceError):
        FileUrlSource("/tmp/x", poll_interval=0)
    with pytest.raises(FunnelUrlSourceError):
        FileUrlSource("/tmp/x", poll_interval=-1)


# --------------------------------------------------------------------------- #
# make_funnel_url_source — env-var contract
# --------------------------------------------------------------------------- #


def test_factory_default_is_static() -> None:
    src = make_funnel_url_source(control_url="https://h:8000", env={})
    assert isinstance(src, StaticUrlSource)


def test_factory_explicit_static() -> None:
    src = make_funnel_url_source(
        control_url="https://h:8000",
        env={FUNNEL_URL_SOURCE_ENV: "static"},
    )
    assert isinstance(src, StaticUrlSource)


def test_factory_file_default_path_and_cadence() -> None:
    src = make_funnel_url_source(
        control_url="https://h:8000",
        env={FUNNEL_URL_SOURCE_ENV: "file"},
    )
    assert isinstance(src, FileUrlSource)
    assert src.poll_interval == DEFAULT_FILE_POLL_SECONDS


def test_factory_file_overrides() -> None:
    src = make_funnel_url_source(
        control_url="https://h:8000",
        env={
            FUNNEL_URL_SOURCE_ENV: "file",
            FUNNEL_URL_FILE_ENV: "/tmp/whilly/url.txt",
            FUNNEL_URL_POLL_SECONDS_ENV: "1.5",
        },
    )
    assert isinstance(src, FileUrlSource)
    assert src.poll_interval == 1.5


def test_factory_postgres_requires_dsn() -> None:
    with pytest.raises(FunnelUrlSourceError, match="WHILLY_DATABASE_URL"):
        make_funnel_url_source(
            control_url="https://h:8000",
            env={FUNNEL_URL_SOURCE_ENV: "postgres"},
        )


def test_factory_postgres_default_cadence() -> None:
    src = make_funnel_url_source(
        control_url="https://h:8000",
        env={
            FUNNEL_URL_SOURCE_ENV: "postgres",
            FUNNEL_DATABASE_URL_ENV: "postgresql://u:p@h:5432/d",
        },
    )
    assert isinstance(src, PostgresUrlSource)
    assert src.poll_interval == DEFAULT_POSTGRES_POLL_SECONDS


def test_factory_rejects_unknown_source() -> None:
    with pytest.raises(FunnelUrlSourceError, match="not in"):
        make_funnel_url_source(
            control_url="https://h:8000",
            env={FUNNEL_URL_SOURCE_ENV: "redis"},
        )


def test_factory_rejects_non_numeric_poll() -> None:
    with pytest.raises(FunnelUrlSourceError, match="positive"):
        make_funnel_url_source(
            control_url="https://h:8000",
            env={
                FUNNEL_URL_SOURCE_ENV: "file",
                FUNNEL_URL_POLL_SECONDS_ENV: "abc",
            },
        )


def test_factory_rejects_zero_poll() -> None:
    with pytest.raises(FunnelUrlSourceError, match="positive"):
        make_funnel_url_source(
            control_url="https://h:8000",
            env={
                FUNNEL_URL_SOURCE_ENV: "file",
                FUNNEL_URL_POLL_SECONDS_ENV: "0",
            },
        )


# --------------------------------------------------------------------------- #
# Mock URL source for rotation supervisor tests
# --------------------------------------------------------------------------- #


class _ScriptedSource:
    """Replays a list of URLs (or ``None``) on each ``fetch`` call.

    The list is consumed left-to-right; when exhausted, the last value
    (or ``None`` if the list is empty) is returned indefinitely so
    pollers don't crash past the scripted horizon.
    """

    def __init__(self, urls: list[str | None], poll_interval: float = 0.02) -> None:
        self._urls: list[str | None] = list(urls)
        self.poll_interval = poll_interval
        self.fetch_calls: int = 0
        self.closed = False

    async def fetch(self) -> str | None:
        self.fetch_calls += 1
        if self._urls:
            return self._urls.pop(0)
        return None

    async def aclose(self) -> None:
        self.closed = True


class _FakeRemoteClient:
    """In-memory ``RemoteWorkerClient`` stand-in for the rotation tests.

    Tracks per-client claim/release/complete/fail/heartbeat call counts
    keyed by URL the factory was invoked with. The rotation supervisor
    is what we're testing — the inner loop's wire correctness is
    exercised by ``test_remote_worker.py`` already.
    """

    def __init__(self, base_url: str, recorder: "_FactoryRecorder") -> None:
        self.base_url = base_url
        self._recorder = recorder
        self._scripted_claims = list(recorder.claim_script(base_url))
        self.released: list[tuple[str, int, str]] = []

    async def claim(self, worker_id: WorkerId, plan_id: str) -> Task | None:
        if self._scripted_claims:
            return self._scripted_claims.pop(0)
        # Block until a stop arrives — emulate the long-poll empty case.
        await asyncio.sleep(0.05)
        return None

    async def heartbeat(self, worker_id: WorkerId) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(ok=True)

    async def complete(
        self,
        task_id: str,
        worker_id: WorkerId,
        version: int,
        cost_usd: object = None,
    ) -> object:
        self._recorder.complete_calls.append((self.base_url, task_id))
        return None

    async def fail(self, task_id: str, worker_id: WorkerId, version: int, reason: str) -> object:
        self._recorder.fail_calls.append((self.base_url, task_id, reason))
        return None

    async def release(
        self,
        task_id: str,
        worker_id: WorkerId,
        version: int,
        reason: str,
    ) -> object:
        self.released.append((task_id, version, reason))
        self._recorder.release_calls.append((self.base_url, task_id, reason))
        return None


class _FactoryRecorder:
    """Coordinates the per-URL claim script and aggregate call log."""

    def __init__(self) -> None:
        self._claim_scripts: dict[str, list[Task | None]] = {}
        self.opened_urls: list[str] = []
        self.complete_calls: list[tuple[str, str]] = []
        self.fail_calls: list[tuple[str, str, str]] = []
        self.release_calls: list[tuple[str, str, str]] = []

    def script(self, url: str, claims: list[Task | None]) -> None:
        self._claim_scripts[url] = claims

    def claim_script(self, url: str) -> list[Task | None]:
        return self._claim_scripts.get(url, [])

    def factory(self) -> object:
        @contextlib.asynccontextmanager
        async def _make(url: str) -> AsyncIterator[_FakeRemoteClient]:
            self.opened_urls.append(url)
            client = _FakeRemoteClient(url, self)
            try:
                yield client
            finally:
                pass

        return _make


# --------------------------------------------------------------------------- #
# Rotation supervisor — happy path
# --------------------------------------------------------------------------- #


async def test_rotation_supervisor_reconnects_on_url_change() -> None:
    """A scripted URL change causes the worker to release in-flight task and
    reconnect against the new URL with the same worker_id/bearer.

    Wire shape:

    1. Source initially reports ``https://fake-1.lhr.life`` and serves
       one CLAIMED task on the first poll.
    2. Mid-runner, the source advertises ``https://fake-2.lhr.life``.
    3. Worker releases the in-flight task with reason='shutdown',
       closes the old client, and opens a new one against fake-2.
    4. Second session sees no work, the rotation loop exits when the
       outer stop fires.
    """
    recorder = _FactoryRecorder()
    url_a = "https://fake-1.lhr.life"
    url_b = "https://fake-2.lhr.life"

    task = _make_task("T-rotate-1", version=1)
    # Session 1: serve one task; the runner waits long enough for the
    # URL watcher to kick in.
    recorder.script(url_a, [task])
    # Session 2: no work scripted; the supervisor exits via outer stop.
    recorder.script(url_b, [])

    source = _ScriptedSource(
        # First poll (initial snapshot) returns fake-1; subsequent polls
        # advertise fake-2 to trigger the rotation.
        urls=[url_a, url_b, url_b, url_b, url_b],
        poll_interval=0.02,
    )

    # Long-running runner so we observe the rotation tearing it down.
    runner_started = asyncio.Event()
    runner_cancelled = asyncio.Event()

    async def runner(_task: Task, _prompt: str) -> AgentResult:
        runner_started.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            runner_cancelled.set()
            raise
        return AgentResult(output="<promise>COMPLETE</promise>", is_complete=True)

    plan = _make_plan()
    outer_stop = asyncio.Event()

    async def _kill_after_rotation() -> None:
        # Wait until the rotation has happened (second URL opened).
        for _ in range(200):
            if len(recorder.opened_urls) >= 2:
                break
            await asyncio.sleep(0.02)
        # Give the second session a couple of poll cycles, then stop.
        await asyncio.sleep(0.1)
        outer_stop.set()

    killer = asyncio.create_task(_kill_after_rotation())
    try:
        rotation_stats: RotationStats = await asyncio.wait_for(
            run_remote_worker_with_url_rotation(
                recorder.factory(),  # type: ignore[arg-type]
                runner,
                plan,
                WORKER_ID,
                url_a,
                source,  # type: ignore[arg-type]
                heartbeat_interval=0.05,
                install_signal_handlers=False,
                stop=outer_stop,
            ),
            timeout=5.0,
        )
    finally:
        killer.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await killer

    # Two distinct sessions opened: fake-1 then fake-2, in order.
    assert recorder.opened_urls[:2] == [url_a, url_b]
    assert rotation_stats.url_rotations >= 1
    assert rotation_stats.inner_runs >= 2
    # The in-flight task on fake-1 was released cleanly with the
    # canonical 'shutdown' reason — no orphaned CLAIMED row.
    assert any(
        url == url_a and task_id == "T-rotate-1" and reason == "shutdown"
        for url, task_id, reason in recorder.release_calls
    ), recorder.release_calls
    # Runner was started AND cancelled (rotation tore it down).
    assert runner_started.is_set()
    assert runner_cancelled.is_set()


async def test_rotation_supervisor_returns_when_no_rotation_seen() -> None:
    """When the source reports the SAME URL forever and the inner loop
    exits naturally (max_iterations), the supervisor returns without
    reconnecting."""
    recorder = _FactoryRecorder()
    url = "https://stable.lhr.life"
    recorder.script(url, [None])  # 204 from claim, idle poll

    source = _ScriptedSource([url, url, url], poll_interval=0.02)

    async def runner(_task: Task, _prompt: str) -> AgentResult:
        return AgentResult(output="<promise>COMPLETE</promise>", is_complete=True)

    plan = _make_plan()
    rotation_stats = await asyncio.wait_for(
        run_remote_worker_with_url_rotation(
            recorder.factory(),  # type: ignore[arg-type]
            runner,
            plan,
            WORKER_ID,
            url,
            source,  # type: ignore[arg-type]
            heartbeat_interval=0.05,
            install_signal_handlers=False,
            max_iterations=1,
        ),
        timeout=5.0,
    )

    assert recorder.opened_urls == [url]
    assert rotation_stats.url_rotations == 0
    assert rotation_stats.inner_runs == 1


async def test_rotation_supervisor_handles_outer_stop_immediately() -> None:
    """A pre-fired outer stop event makes the supervisor exit without
    even starting the inner loop's work."""
    recorder = _FactoryRecorder()
    url = "https://fake-1.lhr.life"
    recorder.script(url, [None])

    source = _ScriptedSource([url], poll_interval=0.02)

    async def runner(_task: Task, _prompt: str) -> AgentResult:
        return AgentResult(output="<promise>COMPLETE</promise>", is_complete=True)

    outer_stop = asyncio.Event()
    outer_stop.set()  # already fired

    plan = _make_plan()
    rotation_stats = await asyncio.wait_for(
        run_remote_worker_with_url_rotation(
            recorder.factory(),  # type: ignore[arg-type]
            runner,
            plan,
            WORKER_ID,
            url,
            source,  # type: ignore[arg-type]
            heartbeat_interval=0.05,
            install_signal_handlers=False,
            stop=outer_stop,
        ),
        timeout=2.0,
    )

    # Pre-fired stop → the loop exits before opening a session.
    assert rotation_stats.inner_runs == 0
    assert rotation_stats.url_rotations == 0


# --------------------------------------------------------------------------- #
# PostgresUrlSource — dependency-injection happy path
# --------------------------------------------------------------------------- #


async def test_postgres_url_source_lazy_imports_asyncpg(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Postgres source must import asyncpg lazily (via ``importlib``)
    so the static graph (and therefore ``lint-imports``) stays clean for
    the worker entry closure. We assert the behaviour by monkeypatching
    ``importlib.import_module`` to return a stub asyncpg.
    """
    import importlib

    fetched: list[str] = []

    class _FakeConn:
        async def fetchval(self, sql: str) -> str:
            fetched.append(sql)
            return "https://fake-pg.lhr.life"

    class _FakeAcquireCM:
        async def __aenter__(self) -> _FakeConn:
            return _FakeConn()

        async def __aexit__(self, *_a: object) -> None:
            return None

    class _FakePool:
        def acquire(self) -> _FakeAcquireCM:
            return _FakeAcquireCM()

        async def close(self) -> None:
            return None

    class _FakeAsyncpg:
        @staticmethod
        async def create_pool(dsn: str, *, min_size: int, max_size: int) -> _FakePool:
            return _FakePool()

    real_import = importlib.import_module

    def _patched_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "asyncpg":
            return _FakeAsyncpg
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib, "import_module", _patched_import)

    src = PostgresUrlSource("postgresql://u:p@h:5432/d", poll_interval=0.5)
    assert await src.fetch() == "https://fake-pg.lhr.life"
    assert fetched and fetched[0].startswith("SELECT url FROM funnel_url")
    await src.aclose()


async def test_postgres_url_source_returns_none_on_lookup_failure() -> None:
    """A connect / query failure is treated as transient (returns ``None``)
    so the rotation loop keeps polling rather than crashing."""
    src = PostgresUrlSource(
        # Deliberately point at an unreachable port; create_pool will raise.
        "postgresql://u:p@127.0.0.1:1/db",
        poll_interval=0.1,
    )
    result = await asyncio.wait_for(src.fetch(), timeout=10.0)
    assert result is None
    await src.aclose()
