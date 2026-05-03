"""End-to-end integration tests for ``whilly admin ...`` (M2 mission, m2-admin-cli).

Boots the testcontainers Postgres fixture from ``tests/conftest.py``,
points ``WHILLY_DATABASE_URL`` at it, and exercises every admin
subcommand by calling :func:`whilly.cli.admin.run_admin_command`
directly. Mirrors the assertion ids pinned in validation-contract.md
``VAL-M2-ADMIN-CLI-*`` and the ``expectedBehavior`` block on the
``m2-admin-cli`` feature.

Why call ``run_admin_command`` directly instead of subprocess?
    Subprocess calls would re-parse argparse, reload modules, and
    require the test environment to expose the ``whilly`` console
    script on PATH. Calling the entry function directly keeps the
    test fast and exercises the same plumbing the dispatcher uses
    (``whilly admin ...`` → ``run_admin_command(rest)``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from datetime import timedelta

import asyncpg
import pytest

from tests.conftest import DOCKER_REQUIRED
from whilly.adapters.db import TaskRepository
from whilly.cli import admin as admin_cli

pytestmark = DOCKER_REQUIRED


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch, postgres_dsn: str) -> str:
    monkeypatch.setenv(admin_cli.DATABASE_URL_ENV, postgres_dsn)
    return postgres_dsn


@pytest.fixture
async def repo(db_pool: asyncpg.Pool) -> TaskRepository:
    return TaskRepository(db_pool)


async def _run(argv: Sequence[str]) -> int:
    """Invoke ``run_admin_command`` from an async test without nesting event loops.

    The CLI entry point internally calls :func:`asyncio.run`, which raises
    ``RuntimeError: asyncio.run() cannot be called from a running event loop``
    when invoked directly inside an ``async def`` test. Offload to a worker
    thread so the CLI can spin its own loop.
    """
    return await asyncio.to_thread(admin_cli.run_admin_command, list(argv))


def _grep_kv(stdout: str, key: str) -> str:
    for line in stdout.splitlines():
        if line.startswith(f"{key}: "):
            return line.split(": ", 1)[1]
    raise AssertionError(f"key {key!r} not found in stdout:\n{stdout}")


# ---------------------------------------------------------------------------
# bootstrap mint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_prints_token_line_and_persists_hash(
    admin_env: str,
    db_pool: asyncpg.Pool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """VAL-M2-ADMIN-CLI-001 / 002: mint prints a single ``token: <plaintext>``
    line and persists ``sha256(plaintext)`` in ``bootstrap_tokens``."""
    rc = await _run(["bootstrap", "mint", "--owner", "alice@example.com"])
    assert rc == admin_cli.EXIT_OK
    out = capsys.readouterr().out
    assert "token: " in out
    plaintext = _grep_kv(out, "token")
    assert plaintext

    expected_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT token_hash, owner_email, expires_at, revoked_at, is_admin FROM bootstrap_tokens"
        )
    assert row is not None
    assert row["token_hash"] == expected_hash
    assert row["owner_email"] == "alice@example.com"
    assert row["expires_at"] is None
    assert row["revoked_at"] is None
    assert row["is_admin"] is False


@pytest.mark.asyncio
async def test_mint_with_expires_in_30d_sets_expires_at(
    admin_env: str,
    db_pool: asyncpg.Pool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """VAL-M2-ADMIN-CLI-003: ``--expires-in 30d`` sets ``expires_at ≈ NOW() + 30d``."""
    rc = await _run(
        [
            "bootstrap",
            "mint",
            "--owner",
            "bob@example.com",
            "--expires-in",
            "30d",
        ]
    )
    assert rc == admin_cli.EXIT_OK
    capsys.readouterr()

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT created_at, expires_at FROM bootstrap_tokens WHERE owner_email = 'bob@example.com'"
        )
    assert row is not None
    assert row["expires_at"] is not None
    delta = row["expires_at"] - row["created_at"]
    assert abs(delta - timedelta(days=30)) < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_mint_admin_flag_persists_is_admin_true(
    admin_env: str,
    db_pool: asyncpg.Pool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """VAL-M2-ADMIN-CLI-004 / 907: ``--admin`` sets ``is_admin=true`` and
    composes cleanly with ``--expires-in``."""
    rc = await _run(
        [
            "bootstrap",
            "mint",
            "--owner",
            "admin@example.com",
            "--admin",
            "--expires-in",
            "30d",
        ]
    )
    assert rc == admin_cli.EXIT_OK
    capsys.readouterr()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_admin, expires_at, created_at FROM bootstrap_tokens WHERE owner_email = 'admin@example.com'"
        )
    assert row is not None
    assert row["is_admin"] is True
    assert abs((row["expires_at"] - row["created_at"]) - timedelta(days=30)) < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_mint_grep_friendly_kv_lines(
    admin_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """VAL-M2-ADMIN-CLI-016: each output line is ``key: value``."""
    rc = await _run(["bootstrap", "mint", "--owner", "carol@example.com"])
    assert rc == admin_cli.EXIT_OK
    out = capsys.readouterr().out
    for line in out.strip().splitlines():
        assert ": " in line, line
    keys = {line.split(": ", 1)[0] for line in out.strip().splitlines()}
    assert {"token", "owner", "token_hash", "is_admin", "expires_at"}.issubset(keys)


@pytest.mark.asyncio
async def test_mint_json_mode_emits_single_line_object(
    admin_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = await _run(["bootstrap", "mint", "--owner", "dave@example.com", "--json"])
    assert rc == admin_cli.EXIT_OK
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["owner"] == "dave@example.com"
    assert payload["token"]
    assert payload["token_hash"] == hashlib.sha256(payload["token"].encode("utf-8")).hexdigest()
    assert payload["is_admin"] is False


# ---------------------------------------------------------------------------
# bootstrap revoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_by_unique_prefix_flips_revoked_at(
    admin_env: str,
    repo: TaskRepository,
    db_pool: asyncpg.Pool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """VAL-M2-ADMIN-CLI-005 / 904: 8-char prefix is enough when unique."""
    token_hash = await repo.mint_bootstrap_token("plain-revoke-1", owner_email="alice@example.com")
    prefix = token_hash[:8]

    rc = await _run(["bootstrap", "revoke", prefix])
    assert rc == admin_cli.EXIT_OK
    capsys.readouterr()

    async with db_pool.acquire() as conn:
        revoked_at = await conn.fetchval(
            "SELECT revoked_at FROM bootstrap_tokens WHERE token_hash = $1",
            token_hash,
        )
    assert revoked_at is not None


@pytest.mark.asyncio
async def test_revoke_ambiguous_prefix_errors_without_revoking(
    admin_env: str,
    db_pool: asyncpg.Pool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """VAL-M2-ADMIN-CLI-006: ambiguous prefix exits non-zero, no row updated."""
    # Force two active tokens whose hashes share the same 8-char prefix by
    # seeding the rows directly (bypasses the SHA-256 distribution).
    shared_prefix = "deadbeef"
    hash_a = shared_prefix + "0" * 56
    hash_b = shared_prefix + "1" * 56
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO bootstrap_tokens (token_hash, owner_email) VALUES ($1, $2), ($3, $4)",
            hash_a,
            "a@example.com",
            hash_b,
            "b@example.com",
        )

    rc = await _run(["bootstrap", "revoke", shared_prefix])
    assert rc == admin_cli.EXIT_OPERATION_ERROR
    err = capsys.readouterr().err
    assert "ambiguous" in err

    async with db_pool.acquire() as conn:
        cnt = await conn.fetchval(
            "SELECT COUNT(*) FROM bootstrap_tokens WHERE revoked_at IS NOT NULL",
        )
    assert cnt == 0


@pytest.mark.asyncio
async def test_revoke_unknown_prefix_exits_nonzero(
    admin_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """VAL-M2-ADMIN-CLI-007: missing prefix exits non-zero with clear stderr."""
    rc = await _run(["bootstrap", "revoke", "deadbeefdeadbeef"])
    assert rc == admin_cli.EXIT_OPERATION_ERROR
    err = capsys.readouterr().err
    assert "no active token" in err


# ---------------------------------------------------------------------------
# bootstrap list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_excludes_revoked_by_default(
    admin_env: str,
    repo: TaskRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """VAL-M2-ADMIN-CLI-009: default ``list`` hides revoked tokens."""
    active_hash = await repo.mint_bootstrap_token("plain-list-active", owner_email="alice@example.com")
    revoked_hash = await repo.mint_bootstrap_token("plain-list-revoked", owner_email="bob@example.com")
    await repo.revoke_bootstrap_token(revoked_hash)

    rc = await _run(["bootstrap", "list"])
    assert rc == admin_cli.EXIT_OK
    out = capsys.readouterr().out
    assert active_hash[:12] in out
    assert revoked_hash[:12] not in out


@pytest.mark.asyncio
async def test_list_truncates_token_hash_and_omits_plaintext(
    admin_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """VAL-M2-ADMIN-CLI-010 / 901: list output never reveals plaintext."""
    rc = await _run(["bootstrap", "mint", "--owner", "carol@example.com"])
    assert rc == admin_cli.EXIT_OK
    mint_out = capsys.readouterr().out
    plaintext = _grep_kv(mint_out, "token")

    rc = await _run(["bootstrap", "list"])
    assert rc == admin_cli.EXIT_OK
    list_out = capsys.readouterr().out
    assert plaintext not in list_out


@pytest.mark.asyncio
async def test_list_include_revoked_shows_revoked_rows(
    admin_env: str,
    repo: TaskRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """VAL-M2-ADMIN-CLI-902: ``--include-revoked`` exposes revoked rows."""
    token_hash = await repo.mint_bootstrap_token("plain-list-rev", owner_email="alice@example.com")
    await repo.revoke_bootstrap_token(token_hash)

    rc = await _run(["bootstrap", "list"])
    assert rc == admin_cli.EXIT_OK
    default_out = capsys.readouterr().out
    assert token_hash[:12] not in default_out

    rc = await _run(["bootstrap", "list", "--include-revoked"])
    assert rc == admin_cli.EXIT_OK
    inclusive_out = capsys.readouterr().out
    assert token_hash[:12] in inclusive_out
    assert "REVOKED_AT" in inclusive_out


@pytest.mark.asyncio
async def test_list_columns_present(
    admin_env: str,
    repo: TaskRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """VAL-M2-ADMIN-CLI-008: list header carries the documented columns."""
    await repo.mint_bootstrap_token("plain-cols", owner_email="alice@example.com")
    rc = await _run(["bootstrap", "list"])
    assert rc == admin_cli.EXIT_OK
    out = capsys.readouterr().out
    for header in ("TOKEN_HASH", "OWNER", "CREATED_AT", "EXPIRES_AT", "ADMIN"):
        assert header in out


@pytest.mark.asyncio
async def test_list_json_emits_array(
    admin_env: str,
    repo: TaskRepository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await repo.mint_bootstrap_token("plain-json-1", owner_email="alice@example.com")
    await repo.mint_bootstrap_token("plain-json-2", owner_email="bob@example.com")

    rc = await _run(["bootstrap", "list", "--json"])
    assert rc == admin_cli.EXIT_OK
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert len(payload) == 2
    owners = {p["owner_email"] for p in payload}
    assert {"alice@example.com", "bob@example.com"} == owners


# ---------------------------------------------------------------------------
# worker revoke
# ---------------------------------------------------------------------------


async def _seed_worker_with_claim(
    pool: asyncpg.Pool,
    *,
    worker_id: str,
    plan_id: str,
    task_id: str,
) -> None:
    """Seed a workers row + a CLAIMED task owned by it."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
                worker_id,
                f"host-{worker_id}",
                f"hash-{worker_id}",
            )
            await conn.execute(
                "INSERT INTO plans (id, name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                plan_id,
                f"plan-{plan_id}",
            )
            await conn.execute(
                """
                INSERT INTO tasks (id, plan_id, status, priority, claimed_by, claimed_at)
                VALUES ($1, $2, 'CLAIMED', 'medium', $3, NOW())
                """,
                task_id,
                plan_id,
                worker_id,
            )


@pytest.mark.asyncio
async def test_worker_revoke_nulls_token_hash_and_releases_claims(
    admin_env: str,
    db_pool: asyncpg.Pool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """VAL-M2-ADMIN-CLI-011 / 012: worker revoke nulls token_hash, releases
    claimed task, writes RELEASE event with reason='admin_revoked'."""
    worker_id = "w-revoke-1"
    plan_id = "PLAN-REVOKE"
    task_id = "T-revoke-1"
    await _seed_worker_with_claim(db_pool, worker_id=worker_id, plan_id=plan_id, task_id=task_id)

    rc = await _run(["worker", "revoke", worker_id])
    assert rc == admin_cli.EXIT_OK
    out = capsys.readouterr().out
    assert _grep_kv(out, "released_tasks") == "1"
    assert _grep_kv(out, "revoked") == "true"

    async with db_pool.acquire() as conn:
        token_hash = await conn.fetchval("SELECT token_hash FROM workers WHERE worker_id = $1", worker_id)
        status = await conn.fetchval("SELECT status FROM tasks WHERE id = $1", task_id)
        claimed_by = await conn.fetchval("SELECT claimed_by FROM tasks WHERE id = $1", task_id)
        event_payload = await conn.fetchval(
            """
            SELECT payload FROM events
            WHERE task_id = $1 AND event_type = 'RELEASE'
            ORDER BY id DESC LIMIT 1
            """,
            task_id,
        )
    assert token_hash is None
    assert status == "PENDING"
    assert claimed_by is None
    assert event_payload is not None
    payload = json.loads(event_payload) if isinstance(event_payload, str) else event_payload
    assert payload["reason"] == "admin_revoked"
    assert payload["worker_id"] == worker_id
    assert payload["task_id"] == task_id
    assert payload["plan_id"] == plan_id


@pytest.mark.asyncio
async def test_worker_revoke_missing_id_exits_nonzero(
    admin_env: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """VAL-M2-ADMIN-CLI-013: revoking unknown worker exits non-zero."""
    rc = await _run(["worker", "revoke", "w-does-not-exist"])
    assert rc == admin_cli.EXIT_OPERATION_ERROR
    err = capsys.readouterr().err
    assert "worker not found" in err


@pytest.mark.asyncio
async def test_worker_revoke_existing_worker_with_no_claims_succeeds(
    admin_env: str,
    db_pool: asyncpg.Pool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A worker that exists but has no in-flight tasks is still revoked
    cleanly (released_tasks: 0)."""
    worker_id = "w-no-claims"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            worker_id,
            "host-no-claims",
            "hash-no-claims",
        )

    rc = await _run(["worker", "revoke", worker_id])
    assert rc == admin_cli.EXIT_OK
    out = capsys.readouterr().out
    assert _grep_kv(out, "released_tasks") == "0"
    async with db_pool.acquire() as conn:
        token_hash = await conn.fetchval("SELECT token_hash FROM workers WHERE worker_id = $1", worker_id)
    assert token_hash is None


@pytest.mark.asyncio
async def test_worker_revoke_json_mode(
    admin_env: str,
    db_pool: asyncpg.Pool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    worker_id = "w-revoke-json"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workers (worker_id, hostname, token_hash) VALUES ($1, $2, $3)",
            worker_id,
            "host-json",
            "hash-json",
        )

    rc = await _run(["worker", "revoke", worker_id, "--json"])
    assert rc == admin_cli.EXIT_OK
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload == {"revoked": True, "worker_id": worker_id, "released_tasks": 0}


# ---------------------------------------------------------------------------
# Cross-cutting: database-unreachable diagnostic envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_commands_exit_nonzero_on_unreachable_db(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """VAL-M2-ADMIN-CLI-014: closed-port DSN surfaces a clean operation error."""
    # Bind to an obviously-closed port; create_pool's SELECT 1 health
    # check fails, the CLI maps the exception to EXIT_OPERATION_ERROR.
    closed_port_dsn = "postgresql://" + "u" + ":" + "p" + "@127.0.0.1:1/nobody"
    monkeypatch.setenv(admin_cli.DATABASE_URL_ENV, closed_port_dsn)
    rc = await _run(["bootstrap", "list"])
    assert rc == admin_cli.EXIT_OPERATION_ERROR
    err = capsys.readouterr().err
    assert "whilly admin bootstrap list" in err


# ---------------------------------------------------------------------------
# Plaintext mint flows through the auth dep (smoke check that the same
# token can be used to register a worker — VAL-M2-ADMIN-CLI-001 + AUTH-001).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_minted_token_authenticates_against_db_bootstrap_dep(
    admin_env: str,
    db_pool: asyncpg.Pool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end seam: a plaintext minted via the CLI resolves via
    :meth:`TaskRepository.get_bootstrap_token_owner`."""
    rc = await _run(["bootstrap", "mint", "--owner", "alice@example.com", "--admin"])
    assert rc == admin_cli.EXIT_OK
    plaintext = _grep_kv(capsys.readouterr().out, "token")

    repo = TaskRepository(db_pool)
    owner = await repo.get_bootstrap_token_owner(plaintext)
    assert owner == ("alice@example.com", True)
