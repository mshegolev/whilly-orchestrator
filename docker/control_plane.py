"""Production launcher for the Whilly v4 control plane.

Closes the gap between :func:`whilly.adapters.transport.server.create_app`
(which requires an asyncpg pool as its first positional argument) and
``uvicorn --factory`` mode (which calls the factory with no arguments).
The README and ``docs/demo-remote-worker.sh`` example
``uvicorn whilly.adapters.transport.server:create_app --factory`` does
not work because of that mismatch — uvicorn cannot inject the pool.
The integration tests (e.g. ``tests/integration/test_phase5_remote.py``)
side-step it by constructing the pool + app + ``uvicorn.Server`` inside
Python; this launcher is the equivalent for production.

Usage:
    python -m docker.control_plane    # or:
    python /opt/whilly/docker/control_plane.py

Reads from environment:
    WHILLY_DATABASE_URL              required — asyncpg DSN
    WHILLY_WORKER_TOKEN              required — per-worker bearer
    WHILLY_WORKER_BOOTSTRAP_TOKEN    required — cluster-join secret
    WHILLY_HOST                      default 0.0.0.0
    WHILLY_PORT                      default 8000
    WHILLY_LOG_LEVEL                 default info

Pool lifecycle is owned here: we open it before ``create_app``, hand it
to the FastAPI factory, and close it after ``server.serve()`` returns
or raises. ``create_app``'s docstring explicitly notes that it does
*not* own its pool — the caller does — so this matches the contract.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import uvicorn

from whilly.adapters.db import close_pool, create_pool
from whilly.adapters.transport.server import create_app

logger = logging.getLogger("whilly.docker.control_plane")


async def _serve() -> int:
    dsn = os.environ.get("WHILLY_DATABASE_URL")
    if not dsn:
        sys.stderr.write("WHILLY_DATABASE_URL is required\n")
        return 2

    host = os.environ.get("WHILLY_HOST", "0.0.0.0")
    port = int(os.environ.get("WHILLY_PORT", "8000"))
    log_level = os.environ.get("WHILLY_LOG_LEVEL", "info")

    logger.info("Opening asyncpg pool")
    pool = await create_pool(dsn)
    try:
        # Tokens default to env (see whilly.adapters.transport.server.create_app
        # docstring) — passing them explicitly only when tests need to
        # bypass the env. Production reads from env, so we leave them None.
        app = create_app(pool)

        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=log_level,
            # lifespan="on" forces uvicorn to invoke FastAPI's lifespan
            # context manager, which is where create_app stashes pool/repo
            # on app.state and starts the visibility-timeout sweep.
            lifespan="on",
        )
        server = uvicorn.Server(config)
        logger.info("Starting uvicorn on %s:%d", host, port)
        await server.serve()
        return 0
    finally:
        logger.info("Closing asyncpg pool")
        await close_pool(pool)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("WHILLY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    try:
        return asyncio.run(_serve())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
