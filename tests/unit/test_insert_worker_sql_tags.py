"""Smoke test pinning the F18b register-side ``tags`` column in _INSERT_WORKER_SQL.

PRD-post-auth-hardening §Epic F, Item 18 (register-side plumbing). The
full register-then-claim behaviour can only be tested against a real
Postgres because the ``workers.tags`` column is ``text[]`` and the
claim-time filter relies on the ``<@`` containment operator — neither
is available in the SQLite tier used by the unit fixtures. But the SQL
string itself, plus the ``register_worker`` repo method signature, can
be statically verified to keep the wire-to-storage path honest.

This test fences future SQL refactors: anyone who deletes the ``tags``
column from the INSERT by accident (e.g., during a workers-schema
rewrite) gets a loud regression here instead of silent capability
truncation in production. The companion ``test_claim_sql_tag_filter.py``
fences the consumer side (the ``<@`` containment clause in
``_CLAIM_SQL``).
"""

from __future__ import annotations

import inspect
import re

from whilly.adapters.db.repository import TaskRepository, _INSERT_WORKER_SQL


def test_insert_worker_sql_includes_tags_column() -> None:
    """The INSERT must mention ``tags`` in both the column list and the VALUES.

    Permissive on whitespace / comment formatting so the matcher
    tolerates future cosmetic rewrites; strict on column ordering
    because the bind positions are hard-coded in
    :meth:`TaskRepository.register_worker`.
    """
    sql = re.sub(r"--.*", "", _INSERT_WORKER_SQL)
    sql = re.sub(r"\s+", " ", sql).strip()
    assert "INSERT INTO workers" in sql
    # The column list must contain ``tags`` adjacent to the canonical
    # ordering. We don't pin the exact column ordering here because a
    # future ``ALTER TABLE`` reorder is harmless as long as the bind
    # positions follow the column list.
    assert "tags" in sql, f"missing tags column in _INSERT_WORKER_SQL; got: {sql}"
    # The VALUES clause must have exactly five placeholders (worker_id,
    # hostname, token_hash, owner_email, tags). A drift in placeholder
    # count means the Python caller and the SQL disagreed about bind
    # positions — a silent corruption risk.
    placeholders = re.findall(r"\$\d+", sql)
    assert sorted(set(placeholders)) == [
        "$1",
        "$2",
        "$3",
        "$4",
        "$5",
    ], f"expected $1..$5 placeholders, got {placeholders}"


def test_register_worker_accepts_tags_kwarg() -> None:
    """``TaskRepository.register_worker`` exposes a ``tags`` keyword-only param.

    Pinned because the server handler in
    :func:`whilly.adapters.transport.server.register_worker` calls
    ``repo.register_worker(..., tags=payload.tags)`` — a refactor that
    silently dropped the kwarg would break the F18b registration flow
    without the static type-checker catching it on every CI run (some
    callers pass ``payload.tags`` positionally in test fixtures).
    """
    sig = inspect.signature(TaskRepository.register_worker)
    assert "tags" in sig.parameters
    tags_param = sig.parameters["tags"]
    # Keyword-only — positional aliasing across the (``owner_email``,
    # ``tags``) boundary would let a typo silently shift values across
    # bind positions. The ``*,`` in the source forces operators to
    # name the field at the call site.
    assert tags_param.kind == inspect.Parameter.KEYWORD_ONLY
    # Default is ``None`` so legacy callers (M2 bootstrap fixtures)
    # that predate F18b keep working with no behavioural change.
    assert tags_param.default is None
