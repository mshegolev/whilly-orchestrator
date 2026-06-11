# Phase 18: Migration Chain Validation - Research

**Researched:** 2026-06-11
**Domain:** Alembic migration chain, Docker Postgres, CI integration testing
**Confidence:** HIGH

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
None — all implementation choices are at Claude's discretion (autonomous infrastructure phase).

### Claude's Discretion
All implementation choices. Use ROADMAP phase goal, success criteria, and codebase
conventions to guide decisions.

### Deferred Ideas (OUT OF SCOPE)
None — discuss skipped.

</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| MIG-01 | Full Alembic migration chain runs green from empty Postgres in Docker | Testcontainers pattern already exists in `conftest.py`; full chain is 001→028 (28 revisions, single head). New pytest test in `tests/integration/` extends the existing `test_alembic_full_chain.py` approach. |
| MIG-02 | Chain validation repeatable via scripted/CI entry point, not one-off manual run | `Makefile` has no `migrate-chain` target yet. CI runs only `tests/unit/` — integration tests are explicitly deferred to "release smoke". New `migrate-chain` Makefile target + a new GitHub Actions CI job (`migration-chain`) provide the repeatable entry point. |

</phase_requirements>

## Summary

The codebase already has a rich testcontainers-based integration testing
infrastructure. `tests/conftest.py` provides a session-scoped `postgres_dsn`
fixture that boots `postgres:15-alpine`, applies `alembic upgrade head`, and
handles colima/Rancher/Docker-Desktop variance. There is also a
`tests/integration/test_alembic_full_chain.py` that runs `upgrade head` +
`downgrade base` and a re-upgrade idempotency proof — but it only asserts the
chain through revision `016_jira_work_sessions`. Twelve new revisions (017-028)
have been added since that test was last updated, and the test's
`EXPECTED_CHAIN` constant is stale.

The current CI `test` job runs only `tests/unit/` and intentionally skips
`tests/integration/` ("requires Postgres via testcontainers — release-smoke
level, not every PR"). Phase 18 closes both gaps: it updates the full-chain
test to cover all 28 revisions through `028_webauthn_user_handles` (the current
single head), adds a Makefile `migrate-chain` target, and wires a new
`migration-chain` CI job that actually executes the test under GitHub Actions'
Docker environment (Docker is available on ubuntu-latest runners).

The migration chain is strictly linear: one base (`001_initial_schema`), one
head (`028_webauthn_user_handles`). `alembic branches` returns nothing. The
`019a_plans_archived_at` revision has an unusual `a`-suffix but it is fully
in-chain — it revises `018_sessions_and_magic_links` and is the parent of
`020_users`.

**Primary recommendation:** Extend `test_alembic_full_chain.py` to cover all
28 revisions, add a `migrate-chain` Makefile target, and add a
`migration-chain` CI job that runs `pytest tests/integration/test_alembic_full_chain.py`
with Docker available. This delivers MIG-01 (green full-chain run) and MIG-02
(scripted/CI entry point) with minimal code written and zero new infrastructure.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Migration execution (`alembic upgrade head`) | Database / Storage | — | Alembic talks directly to Postgres; no application tier involved |
| Test isolation (ephemeral Postgres) | Test infrastructure | — | `testcontainers` spins a fresh container; no persistent volume used |
| Evidence capture (exit code, revision, count) | CI / Test runner | Shell script | pytest test assertions + stdout from `alembic history` provide machine-readable evidence |
| Repeatable entry point | CI (GitHub Actions job) | Makefile target | Makefile is the local operator entry point; the CI job calls the same target |

## Standard Stack

### Core (already installed — no new packages)

| Library | Version (pyproject.toml / PyPI) | Purpose | Why Standard |
|---------|----------------------------------|---------|--------------|
| `alembic` | `>=1.13` (1.18.4 on PyPI) | Migration execution | Project's migration tooling; `env.py` already written |
| `testcontainers` | `>=4.0` (4.14.2 on PyPI) | Ephemeral Postgres | Session-scoped fixture already in `tests/conftest.py` |
| `asyncpg` | `>=0.29` (0.31.0 on PyPI) | Postgres driver | Project's only Postgres driver; required by `env.py` |
| `pytest` | `>=8.0` (9.0.3 on PyPI) | Test runner | Project standard |
| `pytest-asyncio` | `>=0.23` | Async test support | Already in `[dev]` extras |

[VERIFIED: pip index versions] — all packages confirmed on PyPI at stated versions.

### No new packages required

This phase does not install any new packages. Everything needed is already in
`[dev]` extras (`pip install -e '.[dev]'`).

**Installation:** None needed. `pip install -e '.[dev]'` already covers all
dependencies.

## Package Legitimacy Audit

No new packages are installed by this phase. All packages used (`alembic`,
`testcontainers`, `asyncpg`, `pytest`) are existing project dependencies.

Slopcheck verified existing packages for completeness:

| Package | Registry | slopcheck | Disposition |
|---------|----------|-----------|-------------|
| alembic | PyPI | [OK] | Approved — existing dep |
| testcontainers | PyPI | [OK] | Approved — existing dep |
| asyncpg | PyPI | [OK] | Approved — existing dep |
| pytest | PyPI | [OK] | Approved — existing dep |

[VERIFIED: slopcheck 0.6.1 — scanned 4 packages, 4 OK]

**Packages removed due to slopcheck [SLOP] verdict:** none
**Packages flagged as suspicious [SUS]:** none

## Architecture Patterns

### System Architecture Diagram

```
Operator / CI
    │
    ▼
make migrate-chain  ──────────────────────────────────────────────────────────┐
    │                                                                          │
    ▼                                                                          │
pytest tests/integration/test_alembic_full_chain.py                           │ (equivalent)
    │                                                                          │
    ▼                                                                          │
conftest.postgres_dsn fixture                                                  │
    │                                                                          │
    ├── testcontainers: boot postgres:15-alpine (fresh, ephemeral)             │
    │       │                                                                  │
    │       ▼                                                                  │
    │   container healthy                                                      │
    │       │                                                                  │
    ├── WHILLY_DATABASE_URL set from container port                            │
    │       │                                                                  │
    ├── alembic upgrade head (001→028, single pass)                            │
    │       │                                                                  │
    │   [MIG-01: green = pass]                                                 │
    │       │                                                                  │
    └── yield DSN to test body                                                 │
            │                                                                  │
            ▼                                                                  │
test_full_chain_upgrade_then_full_downgrade                                    │
    │                                                                          │
    ├── assert alembic_version = "028_webauthn_user_handles"                   │
    ├── assert key tables/columns/indexes per revision                         │
    ├── alembic downgrade base                                                  │
    └── assert alembic_version = None (empty)                                  │
                                                                               │
test_full_chain_then_re_upgrade_idempotent                                     │
    │                                                                          │
    ├── upgrade head (first pass)                                              │
    ├── upgrade head (second pass — no-op)                                     │
    └── assert same head revision                                              │
            │                                                                  │
            ▼                                                                  │
Evidence: pytest exit code + captured stdout                                   │
    (migration count, final revision, pass/fail)  ◄─────────────────────────-─┘
```

### Recommended Project Structure

No new directories. New/modified files only:

```
tests/integration/
└── test_alembic_full_chain.py   # UPDATE: extend EXPECTED_CHAIN to 028,
                                  #   assert all 28 revisions on upgrade,
                                  #   fix stale "016" head assertion
Makefile                         # ADD: migrate-chain target
.github/workflows/ci.yml         # ADD: migration-chain job (uses Docker)
```

### Pattern 1: Extend EXPECTED_CHAIN constant

**What:** The existing `EXPECTED_CHAIN` tuple in `test_alembic_full_chain.py`
covers 001–016. Update it to cover 001–028. Update head-revision assertions
from `"016_jira_work_sessions"` to `"028_webauthn_user_handles"`.

**When to use:** Every time a new migration is added, `EXPECTED_CHAIN` must be
updated and the corresponding structural assertion added.

**Example:**

```python
# Source: tests/integration/test_alembic_full_chain.py (existing pattern)
EXPECTED_CHAIN: tuple[str, ...] = (
    "001_initial_schema",
    "002_workers_status",
    # ... (all 28 revisions in order)
    "028_webauthn_user_handles",
)


def test_expected_chain_files_exist_on_disk() -> None:
    versions_dir = MIGRATIONS_DIR / "versions"
    for revision in EXPECTED_CHAIN:
        path = versions_dir / f"{revision}.py"
        assert path.is_file(), f"Missing migration file at {path}"


def test_full_chain_upgrade_then_full_downgrade(empty_postgres_dsn: str) -> None:
    cfg = _build_alembic_config(empty_postgres_dsn)
    _retry_colima_flake(lambda: command.upgrade(cfg, "head"), op="upgrade head")
    head_version = asyncio.run(
        _fetchval(empty_postgres_dsn, "SELECT version_num FROM alembic_version")
    )
    assert head_version == "028_webauthn_user_handles"
    # ... structural assertions for 017-028 deltas ...
    _retry_colima_flake(lambda: command.downgrade(cfg, "base"), op="downgrade base")
    base_version = asyncio.run(
        _fetchval(empty_postgres_dsn, "SELECT version_num FROM alembic_version")
    )
    assert base_version is None
```

### Pattern 2: Makefile migrate-chain target

**What:** A Makefile target that runs the full-chain integration test in one
command. Follows the `make test` convention; gates Docker availability in the
same way the test itself does (skips if Docker unavailable rather than failing).

**Example:**

```makefile
# Added to Makefile beside existing `test` target
migrate-chain: ## Run full Alembic migration chain validation (requires Docker)
	$(PYTHON) -m pytest -q -s \
	    tests/integration/test_alembic_full_chain.py \
	    -v --tb=short
```

### Pattern 3: CI migration-chain job

**What:** A GitHub Actions job that runs `make migrate-chain`. Uses
`ubuntu-latest` (Docker daemon available by default). Runs in parallel with
existing jobs (no `needs:` dependency on `lint` required — it is standalone
infra validation). Evidence is the job exit code plus test stdout captured as
an artifact.

**Example:**

```yaml
# Added to .github/workflows/ci.yml
migration-chain:
  name: Migration chain validation (MIG-01 / MIG-02)
  runs-on: ubuntu-latest
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

### Pattern 4: Evidence file (machine-readable)

**What:** After the chain test passes, write a JSON evidence file containing
exit code, migration count, final revision, and timestamp. This satisfies
success criterion 4 ("chain result recorded as inspectable evidence").

The evidence file can be written by the test itself (using a pytest hook or a
conftest fixture) or by the Makefile target via a shell wrapper. The simplest
approach: a pytest plugin hook in `conftest.py` that writes the file after the
session completes, or a standalone Python snippet called by the Makefile.

```json
{
  "timestamp": "2026-06-11T12:00:00Z",
  "head_revision": "028_webauthn_user_handles",
  "migration_count": 28,
  "upgrade_ok": true,
  "downgrade_ok": true,
  "idempotent_ok": true
}
```

**File location:** `migration-chain-evidence.json` at repo root (`.gitignore`d,
uploaded as CI artifact). Matches the `whilly_logs/` convention of keeping
runtime evidence outside source tree.

### Anti-Patterns to Avoid

- **Re-using the persistent compose volume:** `docker-compose.yml`'s `whilly_pgdata`
  volume is for development, not testing. Never run the chain test against it —
  use `testcontainers` ephemeral containers so the test is idempotent.

- **Hardcoding the head revision string in two places:** `EXPECTED_CHAIN[-1]`
  is the authoritative source of truth. The head-version assertion should
  compare against `EXPECTED_CHAIN[-1]`, not a second literal string, to avoid
  copy-paste drift.

- **Relying on `alembic upgrade head --sql` for evidence:** The `--sql` flag
  emits DDL but does not connect to a DB, so it cannot verify the chain
  executes without error. Use actual DB execution via testcontainers.

- **Running migrations in the existing shared `postgres_dsn` fixture:** The
  session-scoped `postgres_dsn` fixture in `conftest.py` migrates once and
  shares the state across all integration tests. The full-chain test (upgrade +
  downgrade + re-upgrade) must use its own isolated container (`empty_postgres_dsn`
  fixture) to avoid leaving the shared DB in a degraded state.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Ephemeral Postgres provisioning | Custom Docker subprocess wrapper | `testcontainers.postgres.PostgresContainer` | Already in codebase; includes colima/Rancher flake mitigation |
| Migration execution | Direct SQL or subprocess | `alembic.command.upgrade()` / `alembic.command.downgrade()` | Thread-safe, uses the project's `env.py` and DSN resolution |
| Docker availability detection | `shutil.which("docker")` alone | `conftest.docker_available()` + `DOCKER_REQUIRED` marker | Already handles macOS multi-context, colima auto-start, testcontainers absence |
| Colima port-forwarding retries | `time.sleep` loop | `conftest._retry_colima_flake()` | 5-attempt exponential backoff; already handles the known flake |
| DSN format conversion | String replace ad hoc | Existing `_to_asyncpg_dsn()` and `_build_alembic_config()` helpers | Already strip `+psycopg2` suffix; import from `tests.conftest` |

**Key insight:** The full infrastructure for ephemeral Postgres + Alembic +
retry logic is already present in `tests/conftest.py`. The task is entirely
in extending what exists, not building something new.

## Common Pitfalls

### Pitfall 1: Stale `EXPECTED_CHAIN` and `head_version` assertion

**What goes wrong:** `test_full_chain_upgrade_then_full_downgrade` asserts
`head_version == "016_jira_work_sessions"`. After Phase 18, this assertion
must be `"028_webauthn_user_handles"` or the test will always fail. The
`EXPECTED_CHAIN` tuple also needs all 12 missing revisions (017-028) added.

**Why it happens:** The test was written for revision 016 and has not been
updated since 12 revisions were added.

**How to avoid:** Update `EXPECTED_CHAIN` to include all 28 revisions (listed
in order below) and update the head assertion in both
`test_full_chain_upgrade_then_full_downgrade` and
`test_full_chain_then_re_upgrade_idempotent`.

**Complete EXPECTED_CHAIN (001–028):**
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
    "019a_plans_archived_at",          # note: 'a' suffix is intentional
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

**Warning signs:** Test output containing `assert '016_jira_work_sessions' == '028_webauthn_user_handles'`.

### Pitfall 2: `019a` revision ID format in assertions

**What goes wrong:** The revision ID `019a_plans_archived_at` contains an
alphabetic suffix that breaks naive `NNN_slug` parsing. Code that pattern-
matches `\d{3}_\w+` will miss it.

**Why it happens:** A future `019b` slot was reserved; `019a` was used to
avoid a gap in the 019 slot.

**How to avoid:** Use exact string matching for the revision ID, not regex.
The full EXPECTED_CHAIN tuple above contains the exact string.

**Warning signs:** File-existence check fails for `019a_plans_archived_at.py`
if using a `%03d_` format string instead of the exact revision name.

### Pitfall 3: Downgrade coverage — not all migrations have `downgrade()` stubs

**What goes wrong:** Some migrations have `def downgrade() -> None: pass` (a
no-op stub) rather than a real DDL reversal. `alembic downgrade base` will
succeed (no error), but the schema is not actually reversed for those tables.
The test for `downgrade base` should check that whilly tables are gone after
downgrade, not just that `alembic_version` is empty.

**Why it happens:** Auth/security migrations (e.g. 020-028) often ship with
stub downgrade paths because rolling back auth schema is a security-sensitive
decision.

**How to avoid:** The existing test already checks that core tables (`plans`,
`workers`, etc.) are absent after downgrade. Verify that the `post_downgrade_tables`
assertion covers any new tables added by 017-028 (e.g. `scheduler_rules`,
`users`, `auth_audit`, `webauthn_credentials`).

**Warning signs:** `post_downgrade_tables` assertion passes but the DB still
has orphan tables from later migrations that lack proper `downgrade()`.

### Pitfall 4: CI job needs `needs: lint` to get auto-fix commit

**What goes wrong:** The `migration-chain` CI job runs on a stale commit if
the `lint` job auto-fixed and committed code formatting issues.

**Why it happens:** Other jobs in CI (`arch-guard`, `type-check`, `test`)
all have `needs: lint` to pick up the auto-commit.

**How to avoid:** Add `needs: lint` to the `migration-chain` job. Use the same
`ref: ${{ github.head_ref || github.ref_name }}` checkout step.

### Pitfall 5: Docker daemon not available in CI (won't happen on ubuntu-latest)

**What goes wrong:** Integration tests skip silently when Docker is not
available.

**Why it happens:** The `DOCKER_REQUIRED` marker skips rather than fails.

**How to avoid:** On `ubuntu-latest` GitHub Actions runners, Docker is
available by default. The migration-chain CI job will use `ubuntu-latest` so
the test will run, not skip. Confirm the test does not accidentally apply
`DOCKER_REQUIRED` at module level in a way that skips without evidence.

## Code Examples

### Correct `_build_alembic_config` usage (already in conftest)

```python
# Source: tests/conftest.py (verified in codebase)
from alembic import command
from alembic.config import Config
from whilly.adapters.db import MIGRATIONS_DIR


def _build_alembic_config(dsn: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("version_path_separator", "os")
    cfg.set_main_option("sqlalchemy.url", dsn)
    return cfg
```

### Structural assertion for a new revision (017 example)

```python
# Pattern for asserting 017 (scheduler_rules_and_cycles) landed correctly
scheduler_tables = {
    row["table_name"]
    for row in asyncio.run(
        _fetchall(
            empty_postgres_dsn,
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN ('scheduler_rules', 'scheduler_cycles')
            """,
        )
    )
}
assert scheduler_tables == {"scheduler_rules", "scheduler_cycles"}, (
    f"Migration 017 tables missing: {scheduler_tables}"
)
```

### Evidence JSON write (post-test)

```python
# Produce machine-readable evidence after chain test passes
import json
from pathlib import Path

evidence = {
    "timestamp": datetime.utcnow().isoformat() + "Z",
    "head_revision": EXPECTED_CHAIN[-1],
    "migration_count": len(EXPECTED_CHAIN),
    "upgrade_ok": True,
    "downgrade_ok": True,
    "idempotent_ok": True,
}
Path("migration-chain-evidence.json").write_text(json.dumps(evidence, indent=2))
```

## State of the Art

| Old Approach | Current Approach | Impact |
|--------------|------------------|--------|
| Per-migration tests only (test_alembic_004..016) | Full-chain upgrade+downgrade test | Catches broken edges between revisions |
| Manual `alembic upgrade head` to validate | testcontainers ephemeral run | Repeatable, no state left behind |
| Chain test limited to 016 revisions | Needs extension to 028 | Phase 18 closes the gap |
| Integration tests outside CI | New CI `migration-chain` job | MIG-02: scripted entry point |

**Deprecated/outdated:**
- `EXPECTED_CHAIN` ending at `016_jira_work_sessions`: stale, needs update to 028.
- Head assertion `"016_jira_work_sessions"` in test_full_chain: stale, needs update.

## Open Questions

1. **Should downgrade assertions cover all 28 tables or only the core set?**
   - What we know: The existing test checks 9 core tables after downgrade base.
     Revisions 017-028 added: `scheduler_rules`, `scheduler_cycles`,
     `sessions`, `magic_links`, `users`, `user_totp_secrets`, `auth_audit`,
     `webauthn_credentials`, `webauthn_challenges`, `webauthn_user_handles`.
   - What's unclear: Some of these migrations may have stub `downgrade()` methods
     that leave tables in place after `downgrade base` — that would make the test
     fail if the assertion checks for their absence.
   - Recommendation: Check each of the 12 new migration files for `def downgrade()`
     content before writing the post-downgrade assertion. If any have no-op
     downgrades, exclude those tables from the "must be absent" assertion and add
     a code comment explaining the known stub.

2. **Where should the evidence JSON file be written?**
   - What we know: `whilly_logs/` is the standard runtime log directory. The
     CI artifact upload needs a path relative to the workspace root.
   - What's unclear: Whether `whilly_logs/` is `.gitignore`d (it should be).
   - Recommendation: Write to `migration-chain-evidence.json` at repo root
     (follows the `.whilly_state.json` pattern — root-level ephemeral file),
     upload as CI artifact.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Docker CLI | testcontainers / `db-up.sh` | Available locally (daemon not running) | Docker Compose 2.33.0 | — (required; tests skip if absent) |
| Python 3.12 | `pip install -e '.[dev]'` | Available via CI; local is 3.10 | 3.10 local / 3.12 CI | dev env uses system python |
| `testcontainers` | `postgres_dsn` fixture | Installed | 4.14.2 | — |
| `alembic` | migration execution | Installed | 1.18.4 | — |

**Missing dependencies with no fallback:**
- Docker daemon (required for testcontainers). On `ubuntu-latest` CI runners,
  Docker is always available. Locally, the test skips gracefully via
  `DOCKER_REQUIRED` marker.

**Missing dependencies with fallback:**
- None.

**Note:** Docker daemon was not running at research time on the developer
machine (verified: `docker info` returned no server info). Tests will skip
locally until Docker is started. The CI job uses `ubuntu-latest` where Docker
is always available, so the CI entry point (MIG-02) will always execute.

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 8.0+ (9.0.3 on PyPI) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `pytest -q tests/integration/test_alembic_full_chain.py` |
| Full suite command | `make migrate-chain` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| MIG-01 | Full chain 001→028 applies without error from empty Postgres | integration | `pytest -q tests/integration/test_alembic_full_chain.py::test_full_chain_upgrade_then_full_downgrade` | Exists (needs extension) |
| MIG-01 | Downgrade base returns schema to pre-001 state | integration | `pytest -q tests/integration/test_alembic_full_chain.py::test_full_chain_upgrade_then_full_downgrade` | Exists (needs extension) |
| MIG-01 | Re-upgrade from clean container is idempotent | integration | `pytest -q tests/integration/test_alembic_full_chain.py::test_full_chain_then_re_upgrade_idempotent` | Exists (needs extension) |
| MIG-02 | `make migrate-chain` runs without manual steps | smoke | `make migrate-chain` | Wave 0 gap — Makefile target missing |
| MIG-02 | CI job `migration-chain` runs chain test | CI | CI job in `ci.yml` | Wave 0 gap — job missing |

### Sampling Rate

- **Per task commit:** `pytest -q tests/integration/test_alembic_full_chain.py --tb=short`
  (requires Docker; skip silently if absent)
- **Per wave merge:** `make migrate-chain`
- **Phase gate:** `make migrate-chain` green before `/gsd-verify-work`

### Wave 0 Gaps

- [ ] `Makefile` — add `migrate-chain` target (MIG-02 entry point)
- [ ] `.github/workflows/ci.yml` — add `migration-chain` job (MIG-02 CI entry point)
- [ ] `tests/integration/test_alembic_full_chain.py` — extend `EXPECTED_CHAIN`
  and head assertions through revision 028 (MIG-01 coverage gap)
- [ ] `migration-chain-evidence.json` writing — evidence recording (success criterion 4)

## Security Domain

Security enforcement is not the focus of this infrastructure phase. This phase
does not introduce new authentication, secrets, or input validation paths.

The migration chain itself is the subject of security migrations (017-028 added
sessions, auth, WebAuthn), but Phase 18 only validates that those migrations
apply without error — it does not add new security controls.

ASVS applicability: Not applicable to this phase (infrastructure CI only).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Migrations 017-028 all have functional `downgrade()` implementations (or at least non-erroring stubs) | Common Pitfalls #3 | If any `downgrade()` raises an exception, `downgrade base` will fail and the test will fail for reasons unrelated to chain integrity. Investigation needed before writing the downgrade assertion. |
| A2 | GitHub Actions `ubuntu-latest` has Docker daemon available without any setup steps | Architecture Patterns (CI job) | If Docker requires explicit setup, the CI job will fail at testcontainers start. Counter-evidence: many open-source projects use testcontainers on ubuntu-latest without Docker setup steps. |

**Risk mitigation for A1:** Before writing assertions, inspect each of the 12
new migration downgrade methods. This is a read-only codebase task with no
external lookup needed.

## Sources

### Primary (HIGH confidence)

- Codebase — `tests/integration/test_alembic_full_chain.py` (verified in session)
- Codebase — `tests/conftest.py` (verified in session)
- Codebase — `whilly/adapters/db/migrations/env.py` (verified in session)
- Codebase — `alembic.ini` (verified in session)
- Codebase — `docker-compose.yml` (verified in session)
- Codebase — `Makefile` (verified in session)
- Codebase — `.github/workflows/ci.yml` (verified in session)
- Python alembic ScriptDirectory API — `ScriptDirectory.from_config()` + `walk_revisions()` (verified: single head `028_webauthn_user_handles`, 28 total revisions, no branches)

### Secondary (MEDIUM confidence)

- PyPI `pip index versions` — alembic 1.18.4, testcontainers 4.14.2, asyncpg 0.31.0 (verified in session)
- slopcheck 0.6.1 scan — all 4 packages rated [OK]

### Tertiary (LOW confidence)

- GitHub Actions ubuntu-latest Docker availability — [ASSUMED] based on common knowledge of the platform.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — no new packages; all existing verified
- Architecture: HIGH — extends well-understood existing patterns
- Pitfalls: HIGH — discovered from actual code inspection (stale EXPECTED_CHAIN, 019a suffix)
- CI integration: MEDIUM — ubuntu-latest Docker availability is [ASSUMED], not verified via GitHub docs

**Research date:** 2026-06-11
**Valid until:** Stable — migrations do not change without a new phase. EXPECTED_CHAIN and head revision will need updating if any migration is added in a future phase.
