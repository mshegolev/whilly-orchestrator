"""Round-trip all migrations only in the explicitly named disposable test DB."""

import asyncio
from urllib.parse import urlsplit

import asyncpg
import pytest
from alembic import command

from tests.conftest import _build_alembic_config
from tests.integration.test_swarm_runtime import swarm_dsn  # noqa: F401

pytestmark = pytest.mark.integration


def test_schema_roundtrip_disposable_only(swarm_dsn, monkeypatch):  # noqa: F811
    if urlsplit(swarm_dsn).path != "/whilly_swarm_test":
        pytest.skip("requires explicitly named disposable swarm database")
    monkeypatch.setenv("WHILLY_DATABASE_URL", swarm_dsn)
    config = _build_alembic_config(swarm_dsn)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    command.upgrade(config, "head")

    async def inspect():
        conn = await asyncpg.connect(swarm_dsn)
        try:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == "036_learning_memory"
            assert await conn.fetchval("SELECT to_regclass('swarm_learning_revisions')")
            assert await conn.fetchval("SELECT to_regclass('swarm_learning_payloads')")
            assert await conn.fetchval("SELECT to_regclass('swarm_product_features')")
            assert await conn.fetchval("SELECT to_regclass('swarm_publications')")
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM information_schema.columns WHERE table_name='swarm_product_specs' AND column_name='binding'"
                )
                == 1
            )
        finally:
            await conn.close()

    asyncio.run(inspect())
