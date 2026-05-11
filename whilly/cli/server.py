"""``whilly server`` subcommand for the FastAPI control plane."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from typing import Final

import uvicorn

from whilly.adapters.db import close_pool, create_pool
from whilly.adapters.transport.server import create_app

DATABASE_URL_ENV: Final[str] = "WHILLY_DATABASE_URL"
DEFAULT_HOST: Final[str] = "127.0.0.1"
DEFAULT_PORT: Final[int] = 8000
DEFAULT_LOG_LEVEL: Final[str] = "info"
EXIT_OK: Final[int] = 0
EXIT_ENVIRONMENT_ERROR: Final[int] = 2


def build_server_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whilly server",
        description="Run the Whilly FastAPI control plane and web dashboard.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Bind host. Default: {DEFAULT_HOST}.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Bind port. Default: {DEFAULT_PORT}.")
    parser.add_argument(
        "--log-level", default=DEFAULT_LOG_LEVEL, help=f"Uvicorn log level. Default: {DEFAULT_LOG_LEVEL}."
    )
    parser.add_argument("--no-access-log", action="store_true", help="Disable Uvicorn access logs.")
    return parser


def run_server_command(argv: Sequence[str]) -> int:
    parser = build_server_parser()
    args = parser.parse_args(list(argv))

    dsn = os.environ.get(DATABASE_URL_ENV)
    if not dsn:
        print(f"whilly server: {DATABASE_URL_ENV} is not set.", file=sys.stderr)
        return EXIT_ENVIRONMENT_ERROR

    try:
        asyncio.run(
            _async_run_server(
                dsn=dsn,
                host=args.host,
                port=args.port,
                log_level=args.log_level,
                access_log=not args.no_access_log,
            )
        )
    except KeyboardInterrupt:
        return EXIT_OK
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"whilly server: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ENVIRONMENT_ERROR
    return EXIT_OK


async def _async_run_server(
    *,
    dsn: str,
    host: str,
    port: int,
    log_level: str,
    access_log: bool,
) -> None:
    pool = await create_pool(dsn)
    try:
        app = create_app(pool, dsn=dsn)
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=log_level,
            lifespan="on",
            access_log=access_log,
        )
        server = uvicorn.Server(config)
        await server.serve()
    finally:
        await close_pool(pool)


__all__ = [
    "DATABASE_URL_ENV",
    "DEFAULT_HOST",
    "DEFAULT_LOG_LEVEL",
    "DEFAULT_PORT",
    "EXIT_ENVIRONMENT_ERROR",
    "EXIT_OK",
    "build_server_parser",
    "run_server_command",
]
