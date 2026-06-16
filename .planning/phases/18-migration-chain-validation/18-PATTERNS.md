# Phase 18: Migration Chain Validation - Pattern Map

**Mapped:** 2026-06-11
**Files analyzed:** 3 new/modified files
**Analogs found:** 3 / 3

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `tests/integration/test_alembic_full_chain.py` | test | batch (migrate + assert) | `tests/integration/test_alembic_full_chain.py` (existing, stale) | exact — update in place |
| `Makefile` | config | request-response (make target) | `Makefile` (existing `test` target) | exact role-match |
| `.github/workflows/ci.yml` | config | CI pipeline | `.github/workflows/ci.yml` (existing `test` job) | exact role-match |

---

## Pattern Assignments

### `tests/integration/test_alembic_full_chain.py` (test, batch)

**Analog:** Same file — extend in place. The existing file at
`tests/integration/test_alembic_full_chain.py` provides all fixtures and
helpers; only `EXPECTED_CHAIN`, head-revision assertions, structural assertions
for revisions 017–028, and the evidence write need updating.

**Imports pattern** (lines 17–36 of existing file):
```python
from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from typing import Any

import asyncpg
import pytest
from alembic import command

from tests.conftest import (
    DOCKER_REQUIRED,
    HAS_TESTCONTAINERS,
    _build_alembic_config,
    _retry_colima_flake,
    docker_available,
    resolve_docker_host,
)
from whilly.adapters.db import MIGRATIONS_DIR

pytestmark = DOCKER_REQUIRED
```

**Additional imports needed for evidence writing** (add to import block):
```python
import datetime
import json
from pathlib import Path
```

**EXPECTED_CHAIN constant** (replace lines 44–61):
```python
EXPECTED_CHAIN: tuple[str, ...] = (
    "001_initial_schema",
    "002_workers_status",
    "003_events_detail",
    "004_per_worker_bearer",
    "005_plan_budget",
    "006_plan_github_ref",
    "007_plan_prd_file",
    "008_workers_owner_email",
    "009_bootstrap_tokens",
    "010_funnel_url",
    "011_events_notify_trigger",
    "012_pull_requests_and_pr_events",
    "013_work_intents_repo_targets",
    "014_control_state",
    "015_plan_verification_commands",
    "016_jira_work_sessions",
    "017_scheduler_rules_and_cycles",
    "018_sessions_and_magic_links",
    "019a_plans_archived_at",          # 'a' suffix is intentional — not a typo
    "020_users",
    "021_users_must_change_password",
    "022_users_failed_login_counters",
    "023_worker_tags",
    "024_user_totp_secrets",
    "025_auth_audit",
    "026_webauthn_credentials",
    "027_webauthn_challenges",
    "028_webauthn_user_handles",
)
```

**File-existence test** (lines 64–69, unchanged — works for any chain length):
```python
def test_expected_chain_files_exist_on_disk() -> None:
    """Every expected migration file ships at the canonical path."""
    versions_dir = MIGRATIONS_DIR / "versions"
    for revision in EXPECTED_CHAIN:
        path = versions_dir / f"{revision}.py"
        assert path.is_file(), f"Missing migration file at {path}"
```

**empty_postgres_dsn fixture** (lines 72–102, unchanged):
```python
@pytest.fixture
def empty_postgres_dsn(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Boot a fresh Postgres at the empty / pre-001 baseline."""
    if not (HAS_TESTCONTAINERS and docker_available()):
        pytest.skip(
            "Docker daemon not reachable; testcontainers cannot boot Postgres"
        )
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    if "DOCKER_HOST" not in os.environ:
        resolved = resolve_docker_host()
        if resolved is not None:
            monkeypatch.setenv("DOCKER_HOST", resolved)
    monkeypatch.setenv("TESTCONTAINERS_RYUK_DISABLED", "true")

    pg = PostgresContainer("postgres:15-alpine")
    started = False
    try:
        _retry_colima_flake(
            pg.start,
            op="PostgresContainer('postgres:15-alpine').start() (test_alembic_full_chain)",
        )
        started = True
        raw = pg.get_connection_url()
        dsn = raw.replace(
            "postgresql+psycopg2://", "postgresql://"
        ).replace("+psycopg2", "")
        monkeypatch.setenv("WHILLY_DATABASE_URL", dsn)
        yield dsn
    finally:
        if started:
            try:
                pg.stop()
            except Exception:  # noqa: BLE001
                pass
```

**Helper functions** (lines 105–122, unchanged):
```python
def _to_asyncpg_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql://")


async def _fetchval(dsn: str, sql: str, *args: Any) -> Any:
    conn = await asyncpg.connect(_to_asyncpg_dsn(dsn))
    try:
        return await conn.fetchval(sql, *args)
    finally:
        await conn.close()


async def _fetchall(dsn: str, sql: str, *args: Any) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(_to_asyncpg_dsn(dsn))
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()
```

**Core test pattern — upgrade + downgrade** (replaces `test_full_chain_upgrade_then_full_downgrade`):

Head assertion must change from `"016_jira_work_sessions"` to
`EXPECTED_CHAIN[-1]` (never a second literal). Pattern for asserting 017–028
structural deltas follows the same `_fetchval` / `_fetchall` queries against
`information_schema` already used for 006–016:

```python
# Pattern: assert a new table landed (same style as jira_work_tables block)
scheduler_tables = {
    row["table_name"]
    for row in asyncio.run(
        _fetchall(
            empty_postgres_dsn,
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN ('scheduler_rules', 'scheduler_poll_cycles')
            """,
        )
    )
}
assert scheduler_tables == {"scheduler_rules", "scheduler_poll_cycles"}, (
    f"Migration 017 tables missing: {scheduler_tables}"
)

# Pattern: assert a new column landed (same style as github_issue_ref block)
archived_at_count = asyncio.run(
    _fetchval(
        empty_postgres_dsn,
        """
        SELECT count(*)::int FROM information_schema.columns
        WHERE table_name = 'plans'
          AND column_name IN ('archived_at', 'last_event_at')
        """,
    )
)
assert int(archived_at_count) == 2  # 019a added both columns
```

**Post-downgrade table assertion** (the `post_downgrade_tables` set needs
extending for 017–028 tables; all 17–028 migrations have real `drop_table` or
`drop_column` in `downgrade()` — none are stubs):

Confirmed real downgrade implementations:
- 017: `drop_table(scheduler_poll_cycles)`, `drop_table(scheduler_rules)`
- 018: `drop_table(sessions)`, `drop_table(magic_links)`
- 019a: `drop_column(plans, last_event_at)`, `drop_column(plans, archived_at)`
- 020: `drop_table(users)`
- 021–023: `drop_column` on existing tables
- 024: `drop_table(user_totp_secrets)`
- 025: `drop_table(auth_audit)`
- 026: `drop_table(webauthn_credentials)`
- 027: `drop_table(webauthn_challenges)`
- 028: `drop_table(webauthn_user_handles)`

```python
# Post-downgrade: extend existing table list (add 017/018/020/024-028 tables)
post_downgrade_tables = {
    row["table_name"]
    for row in asyncio.run(
        _fetchall(
            empty_postgres_dsn,
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN (
                'workers', 'plans', 'tasks', 'events',
                'bootstrap_tokens', 'funnel_url',
                'control_state', 'jira_work_sessions', 'jira_work_events',
                'scheduler_rules', 'scheduler_poll_cycles',
                'sessions', 'magic_links',
                'users', 'user_totp_secrets', 'auth_audit',
                'webauthn_credentials', 'webauthn_challenges',
                'webauthn_user_handles'
              )
            """,
        )
    )
}
assert post_downgrade_tables == set()
```

**Idempotency test** — same pattern, update literal `"016_jira_work_sessions"`
to `EXPECTED_CHAIN[-1]` in both assertions (lines 421–426).

**Evidence write pattern** — write after the idempotency test completes
(pytest session teardown or end of `test_full_chain_then_re_upgrade_idempotent`):

```python
# Write machine-readable evidence (pattern: .whilly_state.json at repo root)
evidence = {
    "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    "head_revision": EXPECTED_CHAIN[-1],
    "migration_count": len(EXPECTED_CHAIN),
    "upgrade_ok": True,
    "downgrade_ok": True,
    "idempotent_ok": True,
}
Path("migration-chain-evidence.json").write_text(
    json.dumps(evidence, indent=2)
)
```

---

### `Makefile` (config, request-response)

**Analog:** Existing `test` target (lines 59–60).

**Existing test target pattern** (lines 52–60):
```makefile
# Resource-aware test parallelism cap. Default WHILLY_PYTEST_PARALLEL=4
WHILLY_PYTEST_PARALLEL ?= 4

test: ## Run pytest (parallelism capped via WHILLY_PYTEST_PARALLEL, default 4)
	$(PYTHON) -m pytest -q -n auto --maxprocesses=$(WHILLY_PYTEST_PARALLEL)
```

**New `migrate-chain` target** — add after `test:` block, following the same
`## docstring` convention for `make help` and the same `$(PYTHON) -m pytest`
invocation:
```makefile
migrate-chain: ## Run full Alembic migration chain validation (requires Docker)
	$(PYTHON) -m pytest -q -s \
	    tests/integration/test_alembic_full_chain.py \
	    -v --tb=short
```

Placement: immediately after the `test:` block. The `.PHONY` line at the top
(line 8) must include `migrate-chain`.

---

### `.github/workflows/ci.yml` (config, CI pipeline)

**Analog:** Existing `test` job (lines 190–260) and `arch-guard` job
(lines 118–161).

**Job structure pattern** — all jobs after `lint` follow this skeleton:

```yaml
  <job-name>:
    name: <Human-readable name>
    runs-on: ubuntu-latest
    needs: lint
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.head_ref || github.ref_name }}
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dev deps
        run: pip install -e '.[dev]'
      - name: <primary step>
        run: <command>
```

**`needs: lint` is mandatory** — all downstream jobs (arch-guard, type-check,
test, agent-backends) use `needs: lint` so they pick up the auto-fix commit
from the lint job before running. The `migration-chain` job must follow the
same pattern (see RESEARCH.md Pitfall 4).

**`ref:` checkout pattern** — `${{ github.head_ref || github.ref_name }}` used
in every non-lint job (arch-guard line 129, type-check line 174, test line
203, agent-backends line 272). Copy exactly.

**Artifact upload pattern** (lines 249–260, from `test` job):
```yaml
      - name: Upload coverage artifact (for PR review)
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: coverage-report
          path: |
            .coverage
          if-no-files-found: ignore
          retention-days: 14
```

**New `migration-chain` job** — add at the end of the `jobs:` block, using the
same `needs: lint`, `ref:` checkout, `actions/setup-python@v5`, and
`upload-artifact@v4` patterns:

```yaml
  migration-chain:
    name: Migration chain validation (MIG-01 / MIG-02)
    runs-on: ubuntu-latest
    needs: lint
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.head_ref || github.ref_name }}
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dev deps
        run: pip install -e '.[dev]'
      - name: Run full migration chain validation
        run: make migrate-chain
      - name: Upload migration chain evidence
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: migration-chain-evidence
          path: migration-chain-evidence.json
          if-no-files-found: ignore
          retention-days: 30
```

---

## Shared Patterns

### Docker availability guard
**Source:** `tests/integration/test_alembic_full_chain.py` lines 38, 75–76
**Apply to:** `test_alembic_full_chain.py` (already applied)
```python
pytestmark = DOCKER_REQUIRED  # module-level skip if no Docker

# Inside fixture — belt-and-suspenders:
if not (HAS_TESTCONTAINERS and docker_available()):
    pytest.skip("Docker daemon not reachable; testcontainers cannot boot Postgres")
```

### Alembic config builder
**Source:** `tests/conftest.py` lines 615–627
**Apply to:** `test_alembic_full_chain.py` (imported from conftest, already used)
```python
from tests.conftest import _build_alembic_config

cfg = _build_alembic_config(empty_postgres_dsn)
command.upgrade(cfg, "head")
```

### Colima flake retry
**Source:** `tests/conftest.py` lines 148–195; `test_alembic_full_chain.py` lines 89, 143, 377, 420, 424
**Apply to:** `test_alembic_full_chain.py` (imported from conftest, already used)
```python
_retry_colima_flake(
    lambda: command.upgrade(cfg, "head"), op="upgrade head (chain)"
)
_retry_colima_flake(
    lambda: command.downgrade(cfg, "base"), op="downgrade base (chain)"
)
```

### Head revision reference — use `EXPECTED_CHAIN[-1]`, never a second literal
**Source:** RESEARCH.md Anti-Patterns
**Apply to:** `test_alembic_full_chain.py` — two `assert ... == "016_jira_work_sessions"` stale literals
```python
# Correct pattern (avoids copy-paste drift):
assert head_version == EXPECTED_CHAIN[-1]

# Stale pattern to replace (lines 146, 422, 426):
assert head_version == "016_jira_work_sessions"  # DELETE — replace with above
```

### `make help` docstring convention
**Source:** `Makefile` lines 10–11 (awk-based help scraper)
**Apply to:** `Makefile` new `migrate-chain` target
```makefile
# Rule: any target with ":.*?## " comment is auto-listed by `make help`
migrate-chain: ## Run full Alembic migration chain validation (requires Docker)
```

---

## No Analog Found

None — all three files have close analogs in the codebase.

---

## Key Findings from Codebase Inspection

**Migrations 017–028 all have real `downgrade()` implementations** (no stubs):
- Tables added by 017–028 and their confirmed `drop_table` coverage:
  - `scheduler_rules`, `scheduler_poll_cycles` (017)
  - `sessions`, `magic_links` (018)
  - columns on `plans`: `archived_at`, `last_event_at` (019a)
  - `users` (020), columns on `users`: `must_change_password`, `updated_at` (021),
    `failed_attempts`, `locked_until` (022)
  - columns on `workers`/`tasks`: `tags`, `required_tags` (023)
  - `user_totp_secrets` (024), `auth_audit` (025), `webauthn_credentials` (026),
    `webauthn_challenges` (027), `webauthn_user_handles` (028)
- RESEARCH.md Pitfall 3 concern is resolved: all 12 new migrations have functional
  downgrades. The post-downgrade assertion can safely include ALL 18 new tables.

**019a suffix** — the file is `019a_plans_archived_at.py` with revision ID
`019a_plans_archived_at`. No pattern matching `\d{3}_` — use the exact string
from `EXPECTED_CHAIN`.

**Stale assertions to fix** — `"016_jira_work_sessions"` appears in 3 places:
- `EXPECTED_CHAIN` tuple (line 60) — replace tuple with full 28-entry version
- `test_full_chain_upgrade_then_full_downgrade` head assertion (line 146)
- `test_full_chain_then_re_upgrade_idempotent` first assertion (line 422)
- `test_full_chain_then_re_upgrade_idempotent` second assertion (line 426)

---

## Metadata

**Analog search scope:** `tests/`, `Makefile`, `.github/workflows/ci.yml`
**Files scanned:** 5 (test_alembic_full_chain.py, conftest.py,
test_alembic_013_work_intents.py, Makefile, ci.yml) + 12 migration files
(017–028) for downgrade inspection
**Pattern extraction date:** 2026-06-11
