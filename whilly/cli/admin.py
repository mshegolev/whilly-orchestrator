"""``whilly admin ...`` operator CLI (M2 mission, m2-admin-cli).

Composition root for operator-facing administration commands. Owns the
argparse tree for the ``admin bootstrap`` and ``admin worker`` namespaces
and the asyncpg-pool plumbing they share.

Subcommand surface
------------------
* ``whilly admin bootstrap mint --owner X [--expires-in 30d] [--admin]``
  Mints a new ``bootstrap_tokens`` row for operator ``X`` and prints the
  plaintext bearer EXACTLY ONCE. The plaintext is never persisted —
  re-running ``list`` after ``mint`` never reveals it (VAL-M2-ADMIN-CLI-901).
* ``whilly admin bootstrap revoke <prefix>``
  Revokes the unique active bootstrap token whose ``token_hash`` starts
  with ``<prefix>`` (minimum 8 hex chars; ambiguous + missing prefixes
  exit non-zero with clear diagnostics — VAL-M2-ADMIN-CLI-005/006/007).
* ``whilly admin bootstrap list [--include-revoked]``
  Lists active bootstrap-token metadata (truncated ``token_hash``,
  owner, lifecycle timestamps, admin bit). Plaintext NEVER appears
  (VAL-M2-ADMIN-CLI-010).
* ``whilly admin worker revoke <worker_id>``
  Sets ``workers.token_hash = NULL`` for the named worker, releases
  every CLAIMED / IN_PROGRESS task it owns back to PENDING, and writes
  one ``RELEASE`` audit event per released task with
  ``payload.reason = 'admin_revoked'`` (VAL-M2-ADMIN-CLI-011/012).

Output conventions
------------------
Default output is line-oriented ``key: value`` pairs (mirrors the
``whilly worker register`` shape — VAL-M2-ADMIN-CLI-016) so operators can
``... | grep '^token:' | cut -d' ' -f2`` from shell pipelines without
needing ``jq``. Pass ``--json`` to switch any subcommand to a single-line
JSON object on stdout for scripted consumers.

Exit codes
----------
Mirrors :mod:`whilly.cli.run` so the v4 CLI surface is consistent:

* ``0`` — operation succeeded.
* ``1`` — operation-level failure: prefix not found / ambiguous, worker
  not found, malformed ``--owner``, DB lookup failed at runtime, or any
  other runtime precondition failed. The CLI prints a clear single-line
  diagnostic to stderr (VAL-M2-ADMIN-CLI-013/014).
* ``2`` — environment / argparse failure: ``WHILLY_DATABASE_URL`` unset,
  required arg missing (argparse default ``SystemExit(2)``).

Why not embed admin commands inside the existing ``whilly plan ...``
dispatcher? The ``plan`` namespace is task-level — ``admin`` is operator-
level (mint a bearer, revoke a worker). Keeping them in separate top-level
verbs makes ``whilly admin --help`` self-documenting for operators who
have never read the source.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Final

from whilly.adapters.db import TaskRepository, close_pool, create_pool
from whilly.adapters.db.repository import BootstrapTokenRecord

__all__ = [
    "DATABASE_URL_ENV",
    "EXIT_ENVIRONMENT_ERROR",
    "EXIT_OK",
    "EXIT_OPERATION_ERROR",
    "build_admin_parser",
    "parse_expires_in",
    "run_admin_command",
]

logger = logging.getLogger(__name__)


# Same env var :mod:`whilly.cli.plan` / :mod:`whilly.cli.run` read. Single
# source of truth for the v4 CLI's Postgres pointer.
DATABASE_URL_ENV: Final[str] = "WHILLY_DATABASE_URL"

EXIT_OK: Final[int] = 0
EXIT_OPERATION_ERROR: Final[int] = 1
EXIT_ENVIRONMENT_ERROR: Final[int] = 2

# Minimum prefix length accepted by ``bootstrap revoke`` — keeps shorter
# operator typos from ambiguously matching half the table. SHA-256 hashes
# are 64 hex chars; 8 hex = 32 bits ≈ 4B distinct prefixes, more than
# enough to be unique inside any single deployment's bootstrap-token set.
_MIN_REVOKE_PREFIX_LEN: Final[int] = 8

# ``--owner`` shape gate (mirrors
# :data:`whilly.adapters.db.repository._OWNER_EMAIL_RE` so the CLI rejects
# malformed values BEFORE opening a pool). Compiled once at import time.
_OWNER_EMAIL_RE: Final[re.Pattern[str]] = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ``--expires-in`` shape: ``<int><unit>`` where unit ∈ {s, m, h, d, w}.
# Bare integers (no unit) are rejected — operators pasting "30" must add
# "d" so the unit is unambiguous in audit trails.
_EXPIRES_IN_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<n>\d+)(?P<unit>[smhdw])$")
_EXPIRES_IN_UNITS: Final[dict[str, timedelta]] = {
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
}

# Truncated hash length used in ``bootstrap list`` output. Wide enough to
# pick a unique row by eye in any reasonably-sized operator set; narrow
# enough that the plaintext bearer is never recoverable from the prefix.
_HASH_DISPLAY_PREFIX_LEN: Final[int] = 12


# ---------------------------------------------------------------------------
# Parsing helpers (pure functions; unit-tested without DB)
# ---------------------------------------------------------------------------


class ExpiresInError(ValueError):
    """Raised when ``--expires-in <value>`` does not match the documented shape."""


def parse_expires_in(value: str) -> timedelta:
    """Parse ``--expires-in`` (e.g. ``30d``, ``2w``, ``12h``) into a :class:`timedelta`.

    Returns the corresponding :class:`timedelta`. Raises
    :class:`ExpiresInError` on malformed input or non-positive integer
    components.
    """
    if not value or not value.strip():
        raise ExpiresInError("--expires-in must not be empty")
    cleaned = value.strip().lower()
    match = _EXPIRES_IN_RE.match(cleaned)
    if match is None:
        raise ExpiresInError(f"--expires-in {value!r} must match <int><unit> where unit is one of s/m/h/d/w (e.g. 30d)")
    n = int(match.group("n"))
    if n <= 0:
        raise ExpiresInError(f"--expires-in {value!r} must be a positive integer count")
    unit = _EXPIRES_IN_UNITS[match.group("unit")]
    return unit * n


# ---------------------------------------------------------------------------
# argparse tree
# ---------------------------------------------------------------------------


def build_admin_parser() -> argparse.ArgumentParser:
    """Build the ``whilly admin ...`` argparse tree.

    Two-level subparser layout:

    ``admin``
      ├── ``bootstrap``
      │     ├── ``mint``
      │     ├── ``revoke``
      │     └── ``list``
      └── ``worker``
            └── ``revoke``

    Every subcommand declares ``--json`` so an operator can opt into
    machine-readable output without a separate flag wrapper.
    """
    parser = argparse.ArgumentParser(
        prog="whilly admin",
        description=(
            "Operator-facing admin commands: mint / revoke / list bootstrap "
            "tokens and revoke worker bearers. Requires WHILLY_DATABASE_URL."
        ),
    )
    sub = parser.add_subparsers(dest="namespace", metavar="<namespace>")
    sub.required = True

    # ── bootstrap ────────────────────────────────────────────────
    bootstrap = sub.add_parser(
        "bootstrap",
        help="Manage per-operator bootstrap tokens (mint / revoke / list).",
        description="Mint, revoke, or list bootstrap tokens stored in the bootstrap_tokens table.",
    )
    bootstrap_sub = bootstrap.add_subparsers(dest="action", metavar="<action>")
    bootstrap_sub.required = True

    mint = bootstrap_sub.add_parser(
        "mint",
        help="Mint a new bootstrap token; prints plaintext exactly once.",
        description=(
            "Mint a new bootstrap token row and print the plaintext bearer to stdout. "
            "The plaintext is shown ONCE — capture it for the operator who will use it."
        ),
    )
    mint.add_argument(
        "--owner",
        dest="owner",
        default=None,
        help=(
            "Operator email this token is bound to (e.g. alice@example.com). Required. "
            "Stored in bootstrap_tokens.owner_email and propagated to workers.owner_email "
            "when the operator registers a worker via this token."
        ),
    )
    mint.add_argument(
        "--expires-in",
        dest="expires_in",
        default=None,
        help=(
            "Optional TTL, e.g. '30d' / '2w' / '12h'. Sets bootstrap_tokens.expires_at "
            "to NOW() + this duration. Omit for a never-expiring token."
        ),
    )
    mint.add_argument(
        "--admin",
        dest="admin",
        action="store_true",
        help=("Mark the new token as admin-scoped (is_admin=true). Required to access /api/v1/admin/* routes."),
    )
    mint.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit a single-line JSON object on stdout instead of key: value lines.",
    )

    revoke = bootstrap_sub.add_parser(
        "revoke",
        help="Revoke a bootstrap token by unique token_hash prefix (min 8 chars).",
        description=(
            "Mark the bootstrap_tokens row matching <prefix> as revoked. The prefix must "
            "uniquely match exactly one ACTIVE token; ambiguous + missing prefixes exit "
            "non-zero. Already-revoked rows are not matched."
        ),
    )
    revoke.add_argument(
        "prefix",
        help=(
            f"Token-hash prefix (minimum {_MIN_REVOKE_PREFIX_LEN} hex chars) of the token to revoke. "
            "Look it up via `whilly admin bootstrap list`."
        ),
    )
    revoke.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit a single-line JSON object on stdout instead of key: value lines.",
    )

    list_cmd = bootstrap_sub.add_parser(
        "list",
        help="List bootstrap tokens (truncated hash, owner, expires_at, is_admin).",
        description=(
            "Print one row per active bootstrap token. token_hash is truncated; plaintext "
            "is NEVER displayed. Pass --include-revoked for forensic audits."
        ),
    )
    list_cmd.add_argument(
        "--include-revoked",
        dest="include_revoked",
        action="store_true",
        help="Include revoked + expired rows in the output (off by default).",
    )
    list_cmd.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit a JSON array on stdout instead of a tabular text listing.",
    )

    # ── worker ────────────────────────────────────────────────────
    worker = sub.add_parser(
        "worker",
        help="Manage worker rows (currently: revoke).",
        description="Operator-side actions on the workers table.",
    )
    worker_sub = worker.add_subparsers(dest="action", metavar="<action>")
    worker_sub.required = True

    worker_revoke = worker_sub.add_parser(
        "revoke",
        help="Revoke a worker's bearer and release any in-flight tasks.",
        description=(
            "Set workers.token_hash = NULL for <worker_id>, release every CLAIMED / "
            "IN_PROGRESS task back to PENDING, and emit one RELEASE event per released "
            "task with payload.reason = 'admin_revoked'."
        ),
    )
    worker_revoke.add_argument(
        "worker_id",
        help="Worker id to revoke (matches workers.worker_id).",
    )
    worker_revoke.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit a single-line JSON object on stdout instead of key: value lines.",
    )

    return parser


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _emit_kv(stream: object, **fields: object) -> None:
    """Write line-oriented ``key: value`` pairs to ``stream`` in argument order.

    Mirrors the ``whilly worker register`` output shape so the same shell
    pipelines work for every M2 admin output.
    """
    out = stream  # type: ignore[assignment]
    for key, value in fields.items():
        out.write(f"{key}: {value}\n")  # type: ignore[attr-defined]
    out.flush()  # type: ignore[attr-defined]


def _format_iso(value: datetime | None) -> str:
    """Render a :class:`datetime` (or None) as an ISO-8601 string."""
    if value is None:
        return "<never>"
    return value.astimezone(timezone.utc).isoformat()


def _record_to_json(record: BootstrapTokenRecord) -> dict[str, object]:
    """Serialise a :class:`BootstrapTokenRecord` for ``--json`` output."""
    return {
        "token_hash": record.token_hash,
        "token_hash_prefix": record.token_hash[:_HASH_DISPLAY_PREFIX_LEN],
        "owner_email": record.owner_email,
        "created_at": _format_iso(record.created_at),
        "expires_at": _format_iso(record.expires_at),
        "revoked_at": _format_iso(record.revoked_at),
        "is_admin": record.is_admin,
    }


def _emit_table(records: Sequence[BootstrapTokenRecord], *, include_revoked: bool) -> None:
    """Write the human-readable ``bootstrap list`` table to stdout."""
    headers = ["TOKEN_HASH", "OWNER", "CREATED_AT", "EXPIRES_AT", "ADMIN"]
    if include_revoked:
        headers.append("REVOKED_AT")

    rows: list[list[str]] = []
    for r in records:
        row = [
            r.token_hash[:_HASH_DISPLAY_PREFIX_LEN],
            r.owner_email,
            _format_iso(r.created_at),
            _format_iso(r.expires_at),
            "yes" if r.is_admin else "no",
        ]
        if include_revoked:
            row.append(_format_iso(r.revoked_at))
        rows.append(row)

    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    sep = "  "
    sys.stdout.write(sep.join(h.ljust(widths[i]) for i, h in enumerate(headers)) + "\n")
    sys.stdout.write(sep.join("-" * widths[i] for i in range(len(headers))) + "\n")
    for row in rows:
        sys.stdout.write(sep.join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Subcommand dispatch
# ---------------------------------------------------------------------------


def _resolve_dsn() -> str | None:
    """Read ``WHILLY_DATABASE_URL`` and return it (or ``None`` if absent)."""
    dsn = os.environ.get(DATABASE_URL_ENV)
    if dsn is None or not dsn.strip():
        return None
    return dsn


def _missing_dsn_diagnostic() -> str:
    return (
        f"whilly admin: {DATABASE_URL_ENV} is not set — point it at the control-plane Postgres DSN "
        "(see scripts/db-up.sh)."
    )


def run_admin_command(argv: Sequence[str]) -> int:
    """Entry point for ``whilly admin ...``; returns the process exit code."""
    parser = build_admin_parser()
    args = parser.parse_args(list(argv))

    if args.namespace == "bootstrap":
        if args.action == "mint":
            return _run_bootstrap_mint(args)
        if args.action == "revoke":
            return _run_bootstrap_revoke(args)
        if args.action == "list":
            return _run_bootstrap_list(args)
    elif args.namespace == "worker":
        if args.action == "revoke":
            return _run_worker_revoke(args)

    parser.error(f"unknown admin command: {args.namespace} {args.action}")
    return EXIT_OPERATION_ERROR  # unreachable — ``parser.error`` raises SystemExit(2).


# ── bootstrap mint ───────────────────────────────────────────────


def _run_bootstrap_mint(args: argparse.Namespace) -> int:
    if not args.owner or not args.owner.strip():
        print(
            "whilly admin bootstrap mint: --owner is required (e.g. --owner alice@example.com).",
            file=sys.stderr,
        )
        return EXIT_OPERATION_ERROR
    owner = args.owner.strip()
    if not _OWNER_EMAIL_RE.match(owner):
        print(
            f"whilly admin bootstrap mint: --owner {args.owner!r} is not a valid email shape "
            "(expected local@domain.tld).",
            file=sys.stderr,
        )
        return EXIT_OPERATION_ERROR

    expires_at: datetime | None = None
    if args.expires_in is not None:
        try:
            delta = parse_expires_in(args.expires_in)
        except ExpiresInError as exc:
            print(f"whilly admin bootstrap mint: {exc}", file=sys.stderr)
            return EXIT_OPERATION_ERROR
        expires_at = datetime.now(timezone.utc) + delta

    dsn = _resolve_dsn()
    if dsn is None:
        print(_missing_dsn_diagnostic(), file=sys.stderr)
        return EXIT_ENVIRONMENT_ERROR

    plaintext = _generate_plaintext()
    try:
        token_hash, created_at_value = asyncio.run(
            _async_mint(
                dsn,
                plaintext=plaintext,
                owner_email=owner,
                expires_at=expires_at,
                is_admin=args.admin,
            )
        )
    except Exception as exc:
        print(f"whilly admin bootstrap mint: {_format_db_error(exc)}", file=sys.stderr)
        return EXIT_OPERATION_ERROR

    if args.json_output:
        sys.stdout.write(
            json.dumps(
                {
                    "token": plaintext,
                    "token_hash": token_hash,
                    "owner": owner,
                    "is_admin": args.admin,
                    "expires_at": _format_iso(expires_at),
                    "created_at": _format_iso(created_at_value),
                }
            )
            + "\n"
        )
        sys.stdout.flush()
    else:
        _emit_kv(
            sys.stdout,
            token=plaintext,
            owner=owner,
            token_hash=token_hash,
            is_admin="true" if args.admin else "false",
            expires_at=_format_iso(expires_at),
        )
    return EXIT_OK


# ── bootstrap revoke ─────────────────────────────────────────────


def _run_bootstrap_revoke(args: argparse.Namespace) -> int:
    raw_prefix = (args.prefix or "").strip().lower()
    if len(raw_prefix) < _MIN_REVOKE_PREFIX_LEN:
        print(
            f"whilly admin bootstrap revoke: prefix {args.prefix!r} too short — "
            f"need at least {_MIN_REVOKE_PREFIX_LEN} hex characters.",
            file=sys.stderr,
        )
        return EXIT_OPERATION_ERROR
    if not all(c in "0123456789abcdef" for c in raw_prefix):
        print(
            f"whilly admin bootstrap revoke: prefix {args.prefix!r} must be hex characters only.",
            file=sys.stderr,
        )
        return EXIT_OPERATION_ERROR

    dsn = _resolve_dsn()
    if dsn is None:
        print(_missing_dsn_diagnostic(), file=sys.stderr)
        return EXIT_ENVIRONMENT_ERROR

    try:
        matches = asyncio.run(_async_find_active_by_prefix(dsn, raw_prefix))
    except Exception as exc:
        print(f"whilly admin bootstrap revoke: {_format_db_error(exc)}", file=sys.stderr)
        return EXIT_OPERATION_ERROR

    if not matches:
        print(
            f"whilly admin bootstrap revoke: no active token matching prefix {raw_prefix!r}.",
            file=sys.stderr,
        )
        return EXIT_OPERATION_ERROR
    if len(matches) > 1:
        print(
            (
                f"whilly admin bootstrap revoke: prefix {raw_prefix!r} is ambiguous — "
                f"matches {len(matches)} active tokens. Use a longer prefix."
            ),
            file=sys.stderr,
        )
        return EXIT_OPERATION_ERROR

    target = matches[0]
    try:
        asyncio.run(_async_revoke_bootstrap(dsn, target.token_hash))
    except Exception as exc:
        print(f"whilly admin bootstrap revoke: {_format_db_error(exc)}", file=sys.stderr)
        return EXIT_OPERATION_ERROR

    if args.json_output:
        sys.stdout.write(
            json.dumps(
                {
                    "revoked": True,
                    "token_hash": target.token_hash,
                    "owner": target.owner_email,
                    "is_admin": target.is_admin,
                }
            )
            + "\n"
        )
        sys.stdout.flush()
    else:
        _emit_kv(
            sys.stdout,
            revoked="true",
            token_hash=target.token_hash,
            owner=target.owner_email,
        )
    return EXIT_OK


# ── bootstrap list ───────────────────────────────────────────────


def _run_bootstrap_list(args: argparse.Namespace) -> int:
    dsn = _resolve_dsn()
    if dsn is None:
        print(_missing_dsn_diagnostic(), file=sys.stderr)
        return EXIT_ENVIRONMENT_ERROR
    try:
        records = asyncio.run(_async_list_bootstrap(dsn, include_revoked=args.include_revoked))
    except Exception as exc:
        print(f"whilly admin bootstrap list: {_format_db_error(exc)}", file=sys.stderr)
        return EXIT_OPERATION_ERROR

    if args.json_output:
        sys.stdout.write(json.dumps([_record_to_json(r) for r in records]) + "\n")
        sys.stdout.flush()
    else:
        if not records:
            sys.stdout.write("(no active bootstrap tokens)\n")
            sys.stdout.flush()
        else:
            _emit_table(records, include_revoked=args.include_revoked)
    return EXIT_OK


# ── worker revoke ────────────────────────────────────────────────


def _run_worker_revoke(args: argparse.Namespace) -> int:
    worker_id = (args.worker_id or "").strip()
    if not worker_id:
        print(
            "whilly admin worker revoke: <worker_id> must be a non-empty string.",
            file=sys.stderr,
        )
        return EXIT_OPERATION_ERROR

    dsn = _resolve_dsn()
    if dsn is None:
        print(_missing_dsn_diagnostic(), file=sys.stderr)
        return EXIT_ENVIRONMENT_ERROR

    try:
        found, released = asyncio.run(_async_revoke_worker(dsn, worker_id))
    except Exception as exc:
        print(f"whilly admin worker revoke: {_format_db_error(exc)}", file=sys.stderr)
        return EXIT_OPERATION_ERROR

    if not found:
        print(
            f"whilly admin worker revoke: worker not found: {worker_id!r}.",
            file=sys.stderr,
        )
        return EXIT_OPERATION_ERROR

    if args.json_output:
        sys.stdout.write(
            json.dumps(
                {
                    "revoked": True,
                    "worker_id": worker_id,
                    "released_tasks": released,
                }
            )
            + "\n"
        )
        sys.stdout.flush()
    else:
        _emit_kv(
            sys.stdout,
            revoked="true",
            worker_id=worker_id,
            released_tasks=str(released),
        )
    return EXIT_OK


# ---------------------------------------------------------------------------
# Async helpers (one short-lived pool per command)
# ---------------------------------------------------------------------------


def _generate_plaintext() -> str:
    """Mint a fresh URL-safe plaintext bearer (32 random bytes ≈ 256 bits)."""
    import secrets

    return secrets.token_urlsafe(32)


async def _async_mint(
    dsn: str,
    *,
    plaintext: str,
    owner_email: str,
    expires_at: datetime | None,
    is_admin: bool,
) -> tuple[str, datetime | None]:
    pool = await create_pool(dsn)
    try:
        repo = TaskRepository(pool)
        token_hash = await repo.mint_bootstrap_token(
            plaintext,
            owner_email,
            expires_at=expires_at,
            is_admin=is_admin,
        )
        async with pool.acquire() as conn:
            created_at_value = await conn.fetchval(
                "SELECT created_at FROM bootstrap_tokens WHERE token_hash = $1",
                token_hash,
            )
        return token_hash, created_at_value
    finally:
        await close_pool(pool)


async def _async_find_active_by_prefix(dsn: str, prefix: str) -> list[BootstrapTokenRecord]:
    pool = await create_pool(dsn)
    try:
        repo = TaskRepository(pool)
        records = await repo.list_bootstrap_tokens(include_revoked=False)
    finally:
        await close_pool(pool)
    return [r for r in records if r.token_hash.startswith(prefix)]


async def _async_revoke_bootstrap(dsn: str, token_hash: str) -> None:
    pool = await create_pool(dsn)
    try:
        repo = TaskRepository(pool)
        await repo.revoke_bootstrap_token(token_hash)
    finally:
        await close_pool(pool)


async def _async_list_bootstrap(dsn: str, *, include_revoked: bool) -> list[BootstrapTokenRecord]:
    pool = await create_pool(dsn)
    try:
        repo = TaskRepository(pool)
        return await repo.list_bootstrap_tokens(include_revoked=include_revoked)
    finally:
        await close_pool(pool)


async def _async_revoke_worker(dsn: str, worker_id: str) -> tuple[bool, int]:
    pool = await create_pool(dsn)
    try:
        repo = TaskRepository(pool)
        return await repo.revoke_worker_bearer(worker_id)
    finally:
        await close_pool(pool)


def _format_db_error(exc: BaseException) -> str:
    """Single-line stderr-friendly representation of a DB / runtime exception."""
    return f"{type(exc).__name__}: {exc}".replace("\n", " ")
