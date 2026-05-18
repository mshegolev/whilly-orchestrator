"""Smoke test pinning the F18b worker-tag filter clause in _CLAIM_SQL.

PRD-post-auth-hardening §Epic F, Item 18 (SQL slice). The full claim-time
behaviour cannot be unit-tested without a real Postgres — the WITH-CTE +
FOR UPDATE SKIP LOCKED + JSONB-payload assembly need a live engine —
but the SQL string itself can be statically verified to contain the
required clauses. Integration coverage (worker with tags=['gpu'] claims
required_tags=['gpu'] but not required_tags=['signing']) will land in a
follow-up PR alongside the register-side plumbing that lets a worker
actually advertise tags.

This test fences future SQL refactors: anyone who deletes the tag-filter
clause by accident (e.g., during a CLAIM_SQL rewrite) gets a loud
regression here instead of silent under-routing in production.
"""

from __future__ import annotations

import re

from whilly.adapters.db.repository import _CLAIM_SQL


def test_claim_sql_contains_tag_filter_clause() -> None:
    """The <@ tag-filter clause must be present and reference workers.tags."""
    # Strip comments and collapse whitespace so the matcher tolerates
    # formatting drift (line wrap, spacing).
    sql = re.sub(r"--.*", "", _CLAIM_SQL)
    sql = re.sub(r"\s+", " ", sql)
    # Two halves of the filter:
    # 1. The "empty required_tags == match anything" short-circuit.
    assert "required_tags = '{}'::text[]" in sql, f"missing empty-tags short-circuit in _CLAIM_SQL; got: {sql[:500]}"
    # 2. The containment check against workers.tags. The exact subquery
    #    shape is allowed to evolve, but the operator + the workers.tags
    #    reference must both be present and adjacent.
    assert "required_tags <@" in sql, "missing <@ containment operator in _CLAIM_SQL"
    assert "workers" in sql and "tags" in sql, "missing workers.tags reference"


def test_claim_sql_uses_text_array_type_for_empty_short_circuit() -> None:
    """The empty-check must cast against text[] explicitly so the Postgres
    planner picks the right operator overload. Without the cast, Postgres
    sometimes treats '{}' as a text scalar and the comparison fails at
    parse time.
    """
    assert "'{}'::text[]" in _CLAIM_SQL


def test_claim_sql_tag_filter_appears_inside_picked_cte() -> None:
    """The tag filter must be inside the WITH picked AS (...) sub-select,
    not in the outer UPDATE clause — otherwise SKIP LOCKED won't apply
    the filter atomically with the row lock and a worker could see a
    claimable row that doesn't actually match its tags.
    """
    picked_start = _CLAIM_SQL.find("WITH picked AS")
    picked_end = _CLAIM_SQL.find(")", _CLAIM_SQL.find("FOR UPDATE OF t"))
    assert picked_start != -1 and picked_end != -1
    picked_section = _CLAIM_SQL[picked_start:picked_end]
    assert "required_tags <@" in picked_section
