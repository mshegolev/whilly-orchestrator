---
phase: 18-migration-chain-validation
reviewed: 2026-06-11T09:11:55Z
depth: standard
files_reviewed: 4
files_reviewed_list:
  - tests/integration/test_alembic_full_chain.py
  - .gitignore
  - Makefile
  - .github/workflows/ci.yml
findings:
  critical: 1
  warning: 7
  info: 3
  total: 11
status: issues_found
---

# Phase 18: Code Review Report

**Reviewed:** 2026-06-11T09:11:55Z
**Depth:** standard
**Files Reviewed:** 4
**Status:** issues_found

## Summary

Reviewed the Phase 18 deliverables: the extended 28-revision Alembic chain test, the
gitignore entry for the evidence file, the `migrate-chain` Makefile target, and the
`migration-chain` CI job. Verified against the repo: `EXPECTED_CHAIN` matches the 28
files actually on disk in `whilly/adapters/db/migrations/versions/`, the new 017/019a
assertions match what those migrations actually create (`scheduler_rules` /
`scheduler_poll_cycles` tables; `plans.archived_at` + `plans.last_event_at` columns),
and all conftest imports (`_build_alembic_config`, `_retry_colima_flake`,
`DOCKER_REQUIRED`, etc.) resolve.

The core mechanics work, but the **evidence artifact — the headline deliverable of
this phase — fabricates its pass flags**: `downgrade_ok: true` is a hardcoded
constant written by a test that never runs a downgrade. Beyond that, the validation
gate has several silent-pass / silent-skip failure modes (the on-disk chain guard
cannot detect new unlisted migrations; the CI job goes green if Docker is
unavailable; the artifact upload ignores a missing evidence file), and the file's
docstrings were left describing the old 007/016-era chain.

## Critical Issues

### CR-01: Evidence file hardcodes `upgrade_ok` / `downgrade_ok` / `idempotent_ok` as constants — `downgrade_ok: true` is written by a test that never downgrades

**File:** `tests/integration/test_alembic_full_chain.py:486-494`
**Issue:** The machine-readable evidence (the MIG-01/MIG-02 deliverable, uploaded as a
30-day CI artifact) is written at the end of `test_full_chain_then_re_upgrade_idempotent`
with all three pass flags as literal `True`. That test performs `upgrade head` twice and
**never executes a downgrade** — yet it asserts `"downgrade_ok": True`. The downgrade is
exercised only by the separate `test_full_chain_upgrade_then_full_downgrade`, whose
outcome is not consulted. Concrete failure scenario: a migration's `downgrade()` breaks →
the downgrade test fails, the idempotent test still passes, the evidence file is written
claiming `downgrade_ok: true`, and `if: always()` in ci.yml uploads it. Anyone consuming
the artifact (the stated purpose of machine-readable evidence) gets a fabricated signal
that directly contradicts the run. The same applies under test selection (`pytest -k
idempotent` writes full-chain evidence with zero downgrade coverage) and under xdist,
where the two tests run on independent workers with no ordering/outcome coupling.
**Fix:** Derive flags from actual outcomes instead of constants. Simplest robust shape —
record per-test results in a session-scoped state and write the evidence from a hook or
fixture finalizer that sees real outcomes, e.g.:

```python
# conftest-level or module-level accumulator
_RESULTS: dict[str, bool] = {}

def test_full_chain_upgrade_then_full_downgrade(empty_postgres_dsn: str) -> None:
    ...
    assert post_downgrade_tables == set()
    _RESULTS["upgrade_ok"] = True
    _RESULTS["downgrade_ok"] = True

def test_full_chain_then_re_upgrade_idempotent(empty_postgres_dsn: str) -> None:
    ...
    assert second_version == EXPECTED_CHAIN[-1]
    _RESULTS["idempotent_ok"] = True

@pytest.fixture(scope="session", autouse=True)
def _write_evidence() -> Iterator[None]:
    yield
    evidence = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "head_revision": EXPECTED_CHAIN[-1],
        "migration_count": len(EXPECTED_CHAIN),
        "upgrade_ok": _RESULTS.get("upgrade_ok", False),
        "downgrade_ok": _RESULTS.get("downgrade_ok", False),
        "idempotent_ok": _RESULTS.get("idempotent_ok", False),
    }
    (Path(__file__).resolve().parents[2] / "migration-chain-evidence.json").write_text(
        json.dumps(evidence, indent=2)
    )
```

(Defaulting to `False` makes a skipped/failed test produce honest evidence. Under xdist
the session fixture runs per worker; for the dedicated `make migrate-chain` target —
which is single-process — this is exact.)

## Warnings

### WR-01: On-disk chain guard cannot detect a new unlisted migration — the exact failure mode this phase was created to fix

**File:** `tests/integration/test_alembic_full_chain.py:44-84`
**Issue:** The comment above `EXPECTED_CHAIN` claims "If a future migration shifts the
chain this test calls it out loudly rather than silently letting the chain grow without
coverage." It does not. `test_expected_chain_files_exist_on_disk` only checks that each
listed file exists; a newly added `029_*.py` passes it untouched. The only thing that
would fail is the `head_version == EXPECTED_CHAIN[-1]` assertion — which lives in the
Docker-gated tests (`pytestmark = DOCKER_REQUIRED`) that auto-skip on every machine
without Docker. This is precisely how the chain went stale at 016 for 12 migrations
before this phase. The one test that always runs must enforce set equality, not subset.
**Fix:**
```python
def test_expected_chain_files_exist_on_disk() -> None:
    versions_dir = MIGRATIONS_DIR / "versions"
    on_disk = {p.stem for p in versions_dir.glob("*.py") if p.stem != "__init__"}
    assert on_disk == set(EXPECTED_CHAIN), (
        f"Chain drift — extra on disk: {on_disk - set(EXPECTED_CHAIN)}, "
        f"missing on disk: {set(EXPECTED_CHAIN) - on_disk}"
    )
```

### WR-02: Post-downgrade leftover check omits `pull_requests` and all five migration-013 tables

**File:** `tests/integration/test_alembic_full_chain.py:431-464`
**Issue:** The post-`downgrade base` table set checks 19 names, but omits
`pull_requests` (created by 012 — verified: `PULL_REQUESTS_TABLE = "pull_requests"`,
`012_pull_requests_and_pr_events.py:99`) and the 013 tables `work_intents`,
`plan_origins`, `repo_targets`, `plan_repo_targets`, `task_repo_targets` — even though
the 013 tables ARE asserted present after upgrade (lines 297-340). A downgrade that
leaves any of these behind passes step 5. `pull_requests` is in neither list, so it has
zero coverage in either direction.
**Fix:** Stop maintaining a hand-curated `IN (...)` list — after `downgrade base`,
assert that *no* user tables remain at all:
```python
leftover = asyncio.run(
    _fetchall(
        empty_postgres_dsn,
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name <> 'alembic_version'
        """,
    )
)
assert leftover == [], f"Tables left behind after downgrade base: {leftover}"
```
This also makes WR-03's downgrade side self-maintaining for all future migrations.

### WR-03: No upgrade-side structural assertions for migrations 018 and 020–028

**File:** `tests/integration/test_alembic_full_chain.py:391-420`
**Issue:** The phase added structural assertions for 017 and 019a only. Migrations 018
(`sessions`, `magic_links`), 020 (`users`), 021/022 (users columns), 023
(`workers.tags` / `tasks.required_tags` columns), 024 (`user_totp_secrets`), 025
(`auth_audit`), 026–028 (`webauthn_*`) have no upgrade-side existence checks — only the
post-downgrade absence list mentions their tables. A stub/no-op `upgrade()` in any of
them (with a matching no-op downgrade) would sail through with `alembic_version = 028`
and the test would still report the chain valid. These are the auth/security tables; a
silently missing `auth_audit` or `webauthn_credentials` table at "head" is exactly what
this gate exists to catch.
**Fix:** Add one set-equality table check mirroring the existing pattern:
```python
auth_tables = { ... }  # same _fetchall pattern, IN ('sessions','magic_links','users',
                       # 'user_totp_secrets','auth_audit','webauthn_credentials',
                       # 'webauthn_challenges','webauthn_user_handles')
assert auth_tables == {"sessions", "magic_links", "users", "user_totp_secrets",
                       "auth_audit", "webauthn_credentials", "webauthn_challenges",
                       "webauthn_user_handles"}
```
plus a column check for 023 (`workers.tags`, `tasks.required_tags`).

### WR-04: Evidence written to a cwd-relative path — silently lost or scattered depending on invocation directory

**File:** `tests/integration/test_alembic_full_chain.py:494`
**Issue:** `Path("migration-chain-evidence.json").write_text(...)` writes to whatever
directory pytest was launched from. The CI job happens to run `make migrate-chain` from
the repo root, so it works today — but anyone running pytest from a subdirectory writes
the file elsewhere, and the CI upload step then silently uploads nothing (see WR-05).
Side effect in the other direction: a plain full-suite `make test` run also drops this
file into the repo root as a byproduct of an unrelated run (gitignored, but still a
surprising artifact whose flags then go stale).
**Fix:** Anchor to the repo root explicitly:
```python
REPO_ROOT = Path(__file__).resolve().parents[2]
(REPO_ROOT / "migration-chain-evidence.json").write_text(json.dumps(evidence, indent=2))
```

### WR-05: `migration-chain` CI job goes green when Docker is unavailable, and `if-no-files-found: ignore` hides the missing evidence

**File:** `.github/workflows/ci.yml:312-334`
**Issue:** Two stacked silent-pass modes in the new gate. (1) The whole test module is
gated by `pytestmark = DOCKER_REQUIRED` — if Docker/testcontainers is unreachable on the
runner (self-hosted runner, future image change), all tests *skip*, pytest exits 0,
`make migrate-chain` succeeds, and the "Migration chain validation" job shows green
having validated nothing. (2) The artifact step uses `if-no-files-found: ignore`, so the
absence of `migration-chain-evidence.json` (skipped tests, WR-04 path drift, CR-01
restructure bugs) never surfaces. A validation gate that can pass without running is not
a gate.
**Fix:** Add a hard post-condition after the test run, and make missing evidence loud:
```yaml
      - name: Run full migration chain validation
        run: make migrate-chain
      - name: Assert evidence was produced (tests actually ran, not skipped)
        run: test -f migration-chain-evidence.json
      - name: Upload migration chain evidence
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: migration-chain-evidence
          path: migration-chain-evidence.json
          if-no-files-found: error
          retention-days: 30
```

### WR-06: `datetime.datetime.utcnow()` is deprecated (Python 3.12)

**File:** `tests/integration/test_alembic_full_chain.py:487`
**Issue:** `datetime.datetime.utcnow()` emits `DeprecationWarning` on the exact
interpreter CI pins (3.12) and is slated for removal; it also returns a naive datetime,
which is why the manual `+ "Z"` suffix is needed. Already noted as known in the phase
context — confirming it must be fixed, not just observed.
**Fix:**
```python
"timestamp": datetime.datetime.now(datetime.timezone.utc)
    .isoformat()
    .replace("+00:00", "Z"),
```

### WR-07: Module and test docstrings still describe the 007/006/016-era chain — contradicting the code this phase changed

**File:** `tests/integration/test_alembic_full_chain.py:1-15, 140-154, 467-474`
**Issue:** The phase rewrote the assertions to `EXPECTED_CHAIN[-1]` (= 028) but left the
prose describing the old world: the module docstring says the test pins
"``001 → 002 → 003 → 004 → 005 → 006 → 007``"; the upgrade/downgrade test docstring
(step 2) says "alembic_version reports ``006_plan_github_ref``" and (step 5) "the
migration-006 column"; the idempotency docstring says "re-running ``upgrade head``
against an already-006 database". Every one of these now actively misstates what the
code asserts — the next maintainer debugging a chain failure reads documentation that
points at the wrong revision. Not a style nit: it is the same documentation drift that
let this file rot at 016.
**Fix:** Update the three docstrings to reference `EXPECTED_CHAIN` / "head (currently
028_webauthn_user_handles)" instead of hardcoded revision names, so they cannot go stale
the same way again.

## Info

### IN-01: `migrate-chain` Makefile target passes contradictory verbosity flags `-q`, `-s`, and `-v`

**File:** `Makefile:62-65`
**Issue:** `pytest -q -s ... -v --tb=short` combines `-q` (verbosity −1) and `-v`
(verbosity +1), which cancel to default verbosity, plus `-s` (capture off). The flag
soup obscures intent.
**Fix:** Pick one: `$(PYTHON) -m pytest -v --tb=short tests/integration/test_alembic_full_chain.py` (verbose, matching the CI-log readability goal; drop `-q` and `-s`).

### IN-02: Pre-existing — `!tasks.json` negation in .gitignore carries a trailing comment that is parsed as part of the pattern

**File:** `.gitignore:56`
**Issue:** Pre-existing (not introduced by this phase, but in the reviewed file).
`!tasks.json  # Keep example tasks.json files` — in gitignore, `#` only starts a comment
at the beginning of a line, so the literal pattern here is `!tasks.json  # Keep example
tasks.json files`, which matches nothing; the intended negation is inert. (It also
happens to be unnecessary: `tasks-*.json` never matched `tasks.json` to begin with.)
**Fix:** Move the comment to its own line:
```gitignore
# Keep example tasks.json files
!tasks.json
```

### IN-03: Misleading assertion message — prints the *present* set under the label "missing"

**File:** `tests/integration/test_alembic_full_chain.py:405-407`
**Issue:** `f"Migration 017 tables missing: {scheduler_tables}"` interpolates the set of
tables that *were found*, labelled "missing". On failure the message reads e.g.
`Migration 017 tables missing: {'scheduler_rules'}` when `scheduler_rules` is the one
table that exists.
**Fix:** `f"Migration 017 tables missing: {{'scheduler_rules', 'scheduler_poll_cycles'}} - found {scheduler_tables}"` or compute the actual difference.

---

_Reviewed: 2026-06-11T09:11:55Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
