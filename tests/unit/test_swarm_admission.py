from __future__ import annotations

import pytest

from whilly.swarm import admission


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *args):
        return False


class _Connection:
    def __init__(self, active=0):
        self.active = active
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))

    def transaction(self):
        return _Acquire(self)

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        if "COUNT" in sql:
            return self.active
        return 42


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


@pytest.mark.asyncio
async def test_capacity_five_fails_closed_without_delegating(monkeypatch):
    connection = _Connection(active=5)

    async def forbidden(*args, **kwargs):
        raise AssertionError("run_engine must not be called")

    monkeypatch.setattr(admission, "run_engine", forbidden)
    with pytest.raises(admission.ModelCapacityExhausted, match="model_capacity_exhausted"):
        await admission.admitted_run_engine(_Pool(connection), "cheap", object(), mode="worker")
    assert not any("INSERT" in sql for sql, _ in connection.calls)


@pytest.mark.asyncio
async def test_delegates_and_releases_durable_row_even_when_engine_fails(monkeypatch):
    connection = _Connection(active=4)
    calls = []

    async def run_engine(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("engine failed")

    monkeypatch.setattr(admission, "run_engine", run_engine)
    with pytest.raises(RuntimeError, match="engine failed"):
        await admission.admitted_run_engine(_Pool(connection), "strong", object(), prompt="x")
    assert calls[0][0][:2] == ("strong", calls[0][0][1])
    assert any("released_at = NOW()" in sql for sql, _ in connection.calls)
    assert any("pg_advisory_xact_lock" in sql for sql, _ in connection.calls)
    assert not any("pg_advisory_unlock" in sql for sql, _ in connection.calls)


@pytest.mark.asyncio
async def test_five_concurrent_calls_enter_and_sixth_is_rejected(monkeypatch):
    import asyncio

    class SharedPool:
        def __init__(self):
            self.active = 0
            self.rows = 0
            self.connections = []

        def acquire(self):
            connection = _Connection()
            connection.pool = self
            self.connections.append(connection)
            return _Acquire(connection)

    pool = SharedPool()

    async def execute(self, sql, *args):
        if "released_at = NOW()" in sql:
            self.pool.active -= 1
        self.calls.append((sql, args))

    async def fetchval(self, sql, *args):
        if "COUNT" in sql:
            return self.pool.active
        self.pool.active += 1
        self.pool.rows += 1
        return self.pool.rows

    _Connection.execute = execute
    _Connection.fetchval = fetchval
    entered = asyncio.Event()
    release = asyncio.Event()

    async def run_engine(*args, **kwargs):
        entered.set()
        await release.wait()
        return "ok"

    monkeypatch.setattr(admission, "run_engine", run_engine)
    calls = [asyncio.create_task(admission.admitted_run_engine(pool, "cheap", object())) for _ in range(5)]
    await asyncio.wait_for(entered.wait(), timeout=1)
    sixth = asyncio.create_task(admission.admitted_run_engine(pool, "cheap", object()))
    with pytest.raises(admission.ModelCapacityExhausted):
        await sixth
    assert not any(task.done() and task.exception() for task in calls)
    release.set()
    assert await asyncio.gather(*calls) == ["ok"] * 5
