---
phase: 09-profile-native-verification-wiring
verified: 2026-05-08T16:10:34Z
status: passed
score: 12/12 must-haves verified
---

# Phase 9: Profile-native verification wiring Verification Report

**Phase Goal:** Wire `ProjectConfig.verification_commands` into generated plans and local/remote worker execution.
**Verified:** 2026-05-08T16:10:34Z
**Status:** passed
**Re-verification:** No - initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Generated project-config plans carry profile-native verification commands as typed top-level plan metadata. | VERIFIED | `VerificationCommand` and `Plan.verification_commands` exist in `whilly/core/models.py`; `build_plan_from_project_config()` emits `verification_commands` with `source: "profile"` in `whilly/project_config/plan_builder.py`; covered by `tests/unit/test_project_config.py`. |
| 2 | Filesystem plan parse/serialize preserves verification command name, command, required flag, source, and order. | VERIFIED | `whilly/adapters/filesystem/plan_io.py` serializes, parses, validates, and defaults verification commands; covered by `tests/unit/test_plan_io.py` and `tests/integration/test_plan_io.py`. |
| 3 | Plan import/export persists profile verification metadata through Postgres without changing task status semantics. | VERIFIED | `verification_commands JSONB NOT NULL DEFAULT '[]'::jsonb` is present in `whilly/adapters/db/schema.sql`; migration `015_plan_verification_commands.py` adds/drops it; `whilly/cli/plan.py` imports/exports it while preserving existing task status handling; covered by integration tests. |
| 4 | Remote control-plane plan metadata exposes verification commands without sibling task lists. | VERIFIED | `PlanPayload` carries `verification_commands` and omits tasks in `whilly/adapters/transport/schemas.py`; `GET /api/v1/plans/{plan_id}` in `whilly/adapters/transport/server.py` returns decoded commands; covered by transport tests. |
| 5 | Transport client fetches a plan and reconstructs `Plan.verification_commands` without server-only imports. | VERIFIED | `RemoteControlPlaneClient.get_plan()` fetches `/api/v1/plans/{plan_id}` and validates via `PlanPayload` in `whilly/adapters/transport/client.py`; import-linter contracts remain kept; covered by `tests/unit/test_remote_client.py`. |
| 6 | Local worker execution resolves profile commands first, required CLI commands second, and optional CLI commands last. | VERIFIED | `resolve_verification_specs()` implements the ordering in `whilly/pipeline/verification.py`; `whilly/cli/run.py` passes `plan.verification_commands`, `--verify`, and `--verify-optional` into the resolver; covered by `tests/unit/test_cli_run.py`. |
| 7 | Verification result events distinguish command source and redact command/output content. | VERIFIED | `VerificationSpec`, `VerificationResult`, event payloads, and detail redaction include `source` in `whilly/pipeline/verification.py`; covered by `tests/unit/test_verification_runner.py`. |
| 8 | Existing CLI-only verification remains supported when plan metadata is empty. | VERIFIED | CLI-only compatibility paths are covered in `tests/unit/test_cli_run.py` and `tests/unit/test_cli_worker.py`; resolver accepts empty profile command sequences. |
| 9 | Remote worker execution uses the same source-aware command resolution after fetching profile plan metadata. | VERIFIED | `whilly/cli/worker.py` fetches plan metadata through `client.get_plan()` in static mode and per URL-rotation session, then builds a resolver-backed verification runner; covered by remote worker CLI tests. |
| 10 | Required profile verification failures block normal DONE through the existing `verification_failed` path. | VERIFIED | `whilly/worker/local.py` and `whilly/worker/remote.py` record verification events and fail required verification through `verification_failed` instead of completing; covered by local and remote worker tests. |
| 11 | Local and remote failure detail distinguish `source` and redact secrets. | VERIFIED | `_verification_failure_detail()` in both worker implementations includes source-aware redacted details; covered by `tests/unit/test_local_worker.py` and `tests/unit/test_remote_worker.py`. |
| 12 | Compliance reports profile-native verification separately from explicit CLI verification and avoids exhaustive coverage claims. | VERIFIED | `whilly/compliance/__init__.py` defines a separate profile-native verification capability and evidence; `tests/unit/test_compliance_report.py` checks the wording stays distinct and non-exhaustive. |

**Score:** 12/12 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
| --- | --- | --- | --- |
| `whilly/core/models.py` | Typed verification command model and plan field | VERIFIED | `VerificationCommand` and immutable `Plan.verification_commands` are implemented. |
| `whilly/project_config/plan_builder.py` | Project profile commands emitted into generated plans | VERIFIED | Profile commands are emitted as top-level plan metadata with `source: "profile"`. |
| `whilly/adapters/filesystem/plan_io.py` | Plan file parse/serialize support | VERIFIED | Commands round-trip through markdown frontmatter and validation errors are explicit. |
| `whilly/adapters/db/migrations/versions/015_plan_verification_commands.py` | Database migration | VERIFIED | Adds and removes `plans.verification_commands` with JSONB default semantics. |
| `whilly/adapters/db/schema.sql` | Reference schema update | VERIFIED | `plans.verification_commands` is present with a non-null empty-array default. |
| `whilly/cli/plan.py` | Plan import/export persistence | VERIFIED | Imports encode commands and exports decode them into core `Plan` objects. |
| `whilly/adapters/transport/schemas.py` | Transport payload support | VERIFIED | Plan payloads serialize and deserialize command metadata without tasks. |
| `whilly/adapters/transport/server.py` | Remote plan metadata endpoint | VERIFIED | Plan endpoint selects and returns decoded verification commands. |
| `whilly/adapters/transport/client.py` | Remote plan metadata client | VERIFIED | `get_plan()` reconstructs a core `Plan` with verification commands. |
| `whilly/pipeline/verification.py` | Source-aware verification resolution and execution | VERIFIED | Resolver, runner, result events, and redaction are source-aware. |
| `whilly/cli/run.py` | Local worker command composition | VERIFIED | Local execution combines profile and CLI verification commands before worker startup. |
| `whilly/cli/worker.py` | Remote worker command composition | VERIFIED | Remote execution fetches plan metadata and builds the same resolver-backed runner. |
| `whilly/worker/local.py` | Local failure handling | VERIFIED | Required verification failures block completion and include source-aware detail. |
| `whilly/worker/remote.py` | Remote failure handling | VERIFIED | Required verification failures call remote fail with source-aware detail. |
| `whilly/compliance/__init__.py` | Compliance reporting distinction | VERIFIED | Profile-native verification is reported as distinct from ad hoc CLI verification. |
| Focused tests | Behavioral coverage for the phase | VERIFIED | Phase-focused unit suite passed; persistence/migration integration tests passed or skipped by environment. |

### Key Link Verification

| From | To | Via | Status | Details |
| --- | --- | --- | --- | --- |
| `ProjectConfig.verification_commands` | Generated `Plan.verification_commands` | `build_plan_from_project_config()` | WIRED | Profile-native commands are converted into typed top-level plan metadata. |
| Generated plan metadata | Filesystem plans | `serialize_plan()` / `parse_plan()` | WIRED | Metadata survives plan file round-trip with source and required flags. |
| Filesystem/imported plans | Postgres `plans.verification_commands` | `whilly plan import/export` | WIRED | JSONB persistence and export reconstruction are implemented and tested. |
| Server plan rows | Remote client `Plan` | `/api/v1/plans/{plan_id}` and `PlanPayload` | WIRED | Remote clients can fetch command metadata without receiving task lists. |
| Local CLI execution | Worker verification runner | `resolve_verification_specs()` in `whilly/cli/run.py` | WIRED | Profile commands are prepended ahead of explicit CLI commands. |
| Remote CLI execution | Remote worker verification runner | `client.get_plan()` plus `resolve_verification_specs()` in `whilly/cli/worker.py` | WIRED | Static and URL-rotation remote worker paths fetch plan metadata before task execution. |
| Verification results | Worker failure path and compliance | Source-aware result/detail fields | WIRED | Required failures block DONE and downstream reporting can distinguish profile from CLI verification. |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
| --- | --- | --- | --- | --- |
| VER-01 | `09-01-PLAN.md`, `09-02-PLAN.md`, `09-03-PLAN.md`, `09-04-PLAN.md` | Project-profile verification commands are wired into generated plans and worker execution. | SATISFIED | Verified across plan generation, plan IO, database persistence, transport metadata, local worker execution, remote worker execution, failure blocking, and compliance reporting. |

No additional Phase 9 requirements were found in `.planning/REQUIREMENTS.md` outside the declared plan requirements.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| --- | --- | --- | --- | --- |
| None | - | Goal-blocking TODO, placeholder implementation, unwired stub, or formatting drift | - | Anti-pattern scan only found legitimate JSON helper returns and placeholder DSNs/tokens/comments in tests or CLI help text. Ruff format now passes across `whilly/` and `tests/`. |

### Automated Verification

- `./.venv/bin/python -m pytest -q tests/unit/test_project_config.py tests/unit/test_plan_io.py tests/unit/test_transport_schemas.py tests/unit/test_remote_client.py tests/unit/test_cli_run.py tests/unit/test_verification_runner.py tests/unit/test_cli_worker.py tests/unit/test_local_worker.py tests/unit/test_remote_worker.py tests/unit/test_compliance_report.py --maxfail=1` - 271 passed.
- `./.venv/bin/python -m pytest -q tests/integration/test_plan_io.py tests/integration/test_alembic_015_plan_verification_commands.py tests/integration/test_alembic_full_chain.py tests/integration/test_alembic_013_work_intents.py --maxfail=1` - 3 passed, 15 skipped.
- `./.venv/bin/lint-imports --config .importlinter` - 2 contracts kept, 0 broken.
- `./.venv/bin/python -m ruff format --check whilly/ tests/` - 428 files already formatted after mechanical formatting cleanup.
- `./.venv/bin/python -m pytest -q tests/integration/test_plan_io.py tests/unit/test_transport_schemas.py tests/unit/test_remote_client.py --maxfail=1` - 82 passed, 9 skipped after formatting cleanup.
- `make lint` - not rerun after formatting cleanup; equivalent Ruff format gate passed and previous `ruff check` passed.
- GSD artifact and key-link helper checks could not parse `must_haves` frontmatter from these plans, so artifact and wiring verification was performed manually against the implementation and tests.

### Human Verification Required

None. The phase concerns deterministic code wiring, persistence, and worker failure behavior; the relevant observable outcomes are covered by direct code inspection and automated tests.

### Gaps Summary

No goal-blocking gaps were found. Phase 9 satisfies VER-01: profile-native verification commands flow from project config into generated plans, persist through local and remote plan metadata paths, participate in local and remote worker execution without replacing explicit CLI verification commands, block normal DONE when required commands fail, and remain distinguishable in events, failure detail, and compliance reporting.

Repository formatting hygiene was cleaned up after initial verification. No residual goal-blocking or lint-format gaps remain in Phase 9 evidence.

---

_Verified: 2026-05-08T16:10:34Z_
_Verifier: Claude (gsd-verifier)_
