"""Add users table with bootstrap admin row for the operator login form.

PRD-wui-multi-plan v2 §3 originally listed "multi-tenancy / team accounts"
as a Non-Goal — the magic-link path was the v2 auth. The follow-up here
keeps magic-link working but adds an *additional* username+password path
so an operator can log in with the standard ``admin/admin`` default that
self-hosted dashboards expect (Grafana, Airflow, etc.).

Schema:

* ``username``   — primary key, lowercase letters/digits/dash/underscore,
                   length 1..64. Lookup key on every POST /auth/login.
* ``password_hash`` — PBKDF2-HMAC-SHA256, 32-byte hex (whilly/api/passwords.py).
* ``password_salt`` — 16-byte hex, regenerated on every password change.
* ``email``      — optional contact, indexed for support tooling.
* ``role``       — text, default ``"operator"``. ``"admin"`` unlocks the
                   admin-scoped endpoints (e.g. POST /api/v1/admin/*).
* ``created_at`` / ``last_login_at`` — observability columns.

Bootstrap row: ``admin`` / ``admin`` (role=admin). Operators MUST change
the password before exposing the dashboard beyond loopback — surface a
warning when an admin row matches the bootstrap hash (see follow-up).

Revision ID: 020_users
Revises: 019a_plans_archived_at
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "020_users"
down_revision: str | None = "019a_plans_archived_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


USERS_TABLE: str = "users"
USERS_EMAIL_INDEX: str = "ix_users_email"
USERS_ROLE_INDEX: str = "ix_users_role"

# Default ``admin`` bootstrap: PBKDF2-HMAC-SHA256 with the fixed salt
# below, 200_000 iterations, 32-byte derived key. Matches
# ``whilly.api.passwords.hash_password("admin", salt=bytes.fromhex(SALT))``.
# Hard-coded so a fresh `alembic upgrade head` produces a deterministic
# bootstrap user without needing an out-of-band setup script.
_ADMIN_SALT_HEX: str = "00112233445566778899aabbccddeeff"
_ADMIN_HASH_HEX: str = "4d4c0992c6a5c80417f50fe2f787961bd49b222be4c4664dbdc7544434e40df2"


def upgrade() -> None:
    op.create_table(
        USERS_TABLE,
        sa.Column("username", sa.Text(), primary_key=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("password_salt", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False, server_default=sa.text("'operator'")),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("last_login_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "username ~ '^[a-z0-9][a-z0-9_-]{0,63}$'",
            name="ck_users_username_format",
        ),
        sa.CheckConstraint(
            "role IN ('operator', 'admin', 'readonly')",
            name="ck_users_role_valid",
        ),
    )
    op.create_index(USERS_EMAIL_INDEX, USERS_TABLE, ["email"], postgresql_where=sa.text("email IS NOT NULL"))
    op.create_index(USERS_ROLE_INDEX, USERS_TABLE, ["role"])

    # Bootstrap admin/admin. Hash is pre-computed from
    # whilly.api.passwords.hash_password("admin", salt=bytes.fromhex(...))
    # so this migration stays pure-DDL (no python import dependency).
    op.execute(
        sa.text(
            "INSERT INTO users (username, password_hash, password_salt, email, role) VALUES (:u, :h, :s, :e, :r)"
        ).bindparams(
            u="admin",
            h=_ADMIN_HASH_HEX,
            s=_ADMIN_SALT_HEX,
            e="admin@whilly.local",
            r="admin",
        )
    )


def downgrade() -> None:
    op.drop_index(USERS_ROLE_INDEX, table_name=USERS_TABLE)
    op.drop_index(USERS_EMAIL_INDEX, table_name=USERS_TABLE)
    op.drop_table(USERS_TABLE)
