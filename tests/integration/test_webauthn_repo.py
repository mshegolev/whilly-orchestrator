"""Integration coverage for ``whilly.api.webauthn_repo`` (migration 026 / E15).

Real testcontainers Postgres (``db_pool`` fixture applies ``alembic upgrade
head``, so the ``webauthn_credentials`` table from migration 026 exists). The
repo stores opaque bytes and a counter, so it does NOT need the optional
``webauthn`` package — only Docker.

Pins:
* insert → fetch round-trip (bytes + transports survive).
* ``credential_id`` UNIQUE rejects a duplicate enrolment.
* FK to ``users(username)`` rejects an orphan credential, and cascades on
  user deletion.
* ``bump_sign_count`` advances the counter and stamps ``last_used_at`` (the
  data side of the cloned-credential check).
"""

from __future__ import annotations

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.api import users_repo, webauthn_repo

pytestmark = DOCKER_REQUIRED

_USERNAME = "wauser"
_CRED_ID = b"\x01\x02\x03credential-id"
_PUBKEY = b"\xa5\x01\x02cose-public-key-bytes"


@pytest.fixture
async def _seeded_user(db_pool: asyncpg.Pool):
    """Create (and afterwards remove) a user the credentials can bind to."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM webauthn_credentials WHERE username = $1", _USERNAME)
        await conn.execute("DELETE FROM users WHERE username = $1", _USERNAME)
    await users_repo.create_user(
        db_pool, username=_USERNAME, initial_password="correct horse battery", email=None, role="admin"
    )
    yield _USERNAME
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM webauthn_credentials WHERE username = $1", _USERNAME)
        await conn.execute("DELETE FROM users WHERE username = $1", _USERNAME)


@pytest.mark.asyncio
async def test_insert_then_fetch_roundtrip(db_pool: asyncpg.Pool, _seeded_user: str) -> None:
    await webauthn_repo.insert_credential(
        db_pool,
        username=_USERNAME,
        credential_id=_CRED_ID,
        public_key=_PUBKEY,
        sign_count=0,
        transports=["usb", "internal"],
    )
    creds = await webauthn_repo.get_credentials_by_username(db_pool, username=_USERNAME)
    assert len(creds) == 1
    cred = creds[0]
    assert cred.credential_id == _CRED_ID
    assert cred.public_key == _PUBKEY
    assert cred.sign_count == 0
    assert cred.transports == ["usb", "internal"]
    assert cred.last_used_at is None

    by_id = await webauthn_repo.get_credential_by_id(db_pool, credential_id=_CRED_ID)
    assert by_id is not None
    assert by_id.username == _USERNAME


@pytest.mark.asyncio
async def test_duplicate_credential_id_rejected(db_pool: asyncpg.Pool, _seeded_user: str) -> None:
    await webauthn_repo.insert_credential(db_pool, username=_USERNAME, credential_id=_CRED_ID, public_key=_PUBKEY)
    with pytest.raises(asyncpg.UniqueViolationError):
        await webauthn_repo.insert_credential(
            db_pool, username=_USERNAME, credential_id=_CRED_ID, public_key=b"different"
        )


@pytest.mark.asyncio
async def test_orphan_credential_rejected_by_fk(db_pool: asyncpg.Pool) -> None:
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await webauthn_repo.insert_credential(
            db_pool, username="nobody-here", credential_id=b"orphan", public_key=_PUBKEY
        )


@pytest.mark.asyncio
async def test_bump_sign_count_advances_and_stamps(db_pool: asyncpg.Pool, _seeded_user: str) -> None:
    await webauthn_repo.insert_credential(
        db_pool, username=_USERNAME, credential_id=_CRED_ID, public_key=_PUBKEY, sign_count=0
    )
    await webauthn_repo.bump_sign_count(db_pool, credential_id=_CRED_ID, new_sign_count=7)
    cred = (await webauthn_repo.get_credentials_by_username(db_pool, username=_USERNAME))[0]
    assert cred.sign_count == 7
    assert cred.last_used_at is not None

    with pytest.raises(LookupError):
        await webauthn_repo.bump_sign_count(db_pool, credential_id=b"does-not-exist", new_sign_count=1)


@pytest.mark.asyncio
async def test_fk_cascade_on_user_delete(db_pool: asyncpg.Pool, _seeded_user: str) -> None:
    await webauthn_repo.insert_credential(db_pool, username=_USERNAME, credential_id=_CRED_ID, public_key=_PUBKEY)
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE username = $1", _USERNAME)
    assert await webauthn_repo.get_credential_by_id(db_pool, credential_id=_CRED_ID) is None


@pytest.mark.asyncio
async def test_delete_credentials_for_user(db_pool: asyncpg.Pool, _seeded_user: str) -> None:
    await webauthn_repo.insert_credential(db_pool, username=_USERNAME, credential_id=_CRED_ID, public_key=_PUBKEY)
    deleted = await webauthn_repo.delete_credentials_for_user(db_pool, username=_USERNAME)
    assert deleted == 1
    assert await webauthn_repo.get_credentials_by_username(db_pool, username=_USERNAME) == []
