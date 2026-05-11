from __future__ import annotations

from typing import Any

import pytest

import whilly.cli as cli
from whilly.cli import server as server_cli


def test_run_server_command_without_database_url_returns_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(server_cli.DATABASE_URL_ENV, raising=False)

    rc = server_cli.run_server_command([])

    assert rc == server_cli.EXIT_ENVIRONMENT_ERROR
    assert server_cli.DATABASE_URL_ENV in capsys.readouterr().err


def test_run_server_command_opens_pool_builds_app_and_closes_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, Any]] = []
    pool = object()
    app = object()

    async def fake_create_pool(dsn: str) -> object:
        events.append(("create_pool", dsn))
        return pool

    async def fake_close_pool(seen_pool: object) -> None:
        events.append(("close_pool", seen_pool))

    def fake_create_app(seen_pool: object, *, dsn: str) -> object:
        events.append(("create_app", seen_pool, dsn))
        return app

    class FakeConfig:
        def __init__(self, seen_app: object, **kwargs: object) -> None:
            events.append(("config", seen_app, kwargs))

    class FakeServer:
        def __init__(self, config: FakeConfig) -> None:
            events.append(("server", config))

        async def serve(self) -> None:
            events.append(("serve", None))

    monkeypatch.setenv(server_cli.DATABASE_URL_ENV, "postgresql://whilly:whilly@localhost:5432/whilly")
    monkeypatch.setattr(server_cli, "create_pool", fake_create_pool)
    monkeypatch.setattr(server_cli, "close_pool", fake_close_pool)
    monkeypatch.setattr(server_cli, "create_app", fake_create_app)
    monkeypatch.setattr(server_cli.uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(server_cli.uvicorn, "Server", FakeServer)

    rc = server_cli.run_server_command(
        ["--host", "0.0.0.0", "--port", "9001", "--log-level", "debug", "--no-access-log"]
    )

    assert rc == server_cli.EXIT_OK
    assert events[0] == ("create_pool", "postgresql://whilly:whilly@localhost:5432/whilly")
    assert events[1] == ("create_app", pool, "postgresql://whilly:whilly@localhost:5432/whilly")
    assert events[2][0] == "config"
    assert events[2][1] is app
    assert events[2][2] == {
        "host": "0.0.0.0",
        "port": 9001,
        "log_level": "debug",
        "lifespan": "on",
        "access_log": False,
    }
    assert events[-2:] == [("serve", None), ("close_pool", pool)]


def test_dispatcher_routes_server_to_run_server_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run_server_command(argv: list[str]) -> int:
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(server_cli, "run_server_command", fake_run_server_command)

    rc = cli.main(["server", "--port", "9001"])

    assert rc == 0
    assert calls == [["--port", "9001"]]
