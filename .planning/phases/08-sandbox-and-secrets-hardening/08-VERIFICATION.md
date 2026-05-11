---
phase: 08-sandbox-and-secrets-hardening
verified: 2026-05-08T14:47:44Z
status: passed
score: 13/13 must-haves verified
re_verification:
  previous_status: gaps_found
  previous_score: 12/13
  gaps_closed:
    - "Phase 8 touched files pass the repository formatting gate."
  gaps_remaining: []
  regressions: []
---

# Phase 8: Sandbox and Secrets Hardening Verification Report

**Phase Goal:** Implement `a3-a4-sandbox-and-secrets-lint` from `docs/CODEX-MISSION.md` without overclaiming full VM isolation.
**Verified:** 2026-05-08T14:47:44Z
**Status:** passed
**Re-verification:** Yes - after 08-04 Ruff formatting gap closure

## Goal Achievement

Phase 8 now meets the full verification contract. The prior security behavior remains present and wired: shared secret linting covers task/config/prompt/external-feedback surfaces, runner subprocess environments are allowlist-based, guard failures emit audit-safe reasons, verification output is redacted before audit persistence, and docs/compliance keep full per-task VM/container isolation as future work.

The previous gap was formatting-only. Plan 08-04 reformatted `tests/unit/test_project_config.py` and `whilly/sources/github_issues.py`; the repository-wide Ruff format gate now passes and focused sanity tests still pass.

### Observable Truths

| # | Truth | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Secret-like text in task fields, external feedback, runner prompt text, and config-like mappings can be detected through one shared contract. | VERIFIED | `whilly/security/secret_lint.py` defines shared patterns, `scan_text()`, `scan_mapping()`, `first_secret_finding()`, and `SecretFinding.event_payload()`; sanitizer, GitHub issues, config validation, worker task scanning, and verification redaction import the shared contract. |
| 2 | Persisted project configs reject plaintext token-like config values and allow `env:`, `keyring:`, and `file:` references. | VERIFIED | `whilly/project_config/loader.py` scans flattened config values before dataclass construction; tests cover rejection and allowed references. |
| 3 | Sanitizer and GitHub issue warning code no longer maintain separate secret regex registries. | VERIFIED | `prompt_sanitizer.py` imports `contains_secret`/`redact_secrets`; `github_issues.py` imports shared `SECRET_PATTERNS`/`scan_text`. |
| 4 | Secret findings expose stable pattern ids and redacted excerpts without persisting raw token values. | VERIFIED | `SecretFinding.event_payload()` emits stable fields and redacted excerpts; secret-lint tests assert raw fake secrets are absent. |
| 5 | Agent subprocesses receive only explicit base runner env names plus selected provider credentials. | VERIFIED | `build_runner_env()` uses `BASE_RUNNER_ENV_ALLOWLIST`, explicit `required_env`, and backend/model credential inference. |
| 6 | Operational secrets are not forwarded to coding-agent child processes. | VERIFIED | Runner env and backend subprocess tests plant worker/admin/database/GitHub/Slack secrets and assert exclusion unless explicitly required. |
| 7 | Claude proxy behavior still works without copying the full parent environment. | VERIFIED | `build_subprocess_env()` starts from `build_runner_env()` and only layers proxy variables when active. |
| 8 | Secret-lint blocked work fails before local and remote runner invocation. | VERIFIED | Local and remote workers call `scan_task_secret_surface()` after prompt construction and before shell scanning/runner execution, then continue without invoking the runner. |
| 9 | Blocked secret guard paths emit deterministic audit payloads. | VERIFIED | Local/remote tests assert `event_type`, `pattern_id`, `field_path`, `task_id`, `plan_id`, and `redacted_excerpt` payload shape. |
| 10 | Remote fail requests preserve `secret_lint_blocked` as a security prelude event. | VERIFIED | Transport server includes `SECRET_LINT_BLOCKED_EVENT_TYPE` in `security_prelude_events`; integration tests cover persistence. |
| 11 | Verification stdout/stderr and event command values are redacted before audit persistence. | VERIFIED | `pipeline/verification.py` redacts command/stdout/stderr before result event persistence; tests cover redaction. |
| 12 | Compliance and docs report improved guards while keeping full sandbox/VM isolation partial/future. | VERIFIED | Compliance keeps `Sandbox/VM isolation` as `PARTIAL`; mission/current-vs-target docs state full per-task VM/container isolation remains future work. |
| 13 | Phase 8 touched files pass the repository formatting gate. | VERIFIED | `./.venv/bin/python -m ruff format --check whilly/ tests/` reports `426 files already formatted`; focused 08-04 tests report `55 passed`. |

**Score:** 13/13 must-haves verified

### Required Artifacts

| Artifact | Expected | Status | Details |
| --- | --- | --- | --- |
| `whilly/security/secret_lint.py` | Shared secret registry, scan helpers, redaction helpers, audit-safe finding values. | VERIFIED | Defines required constants, dataclasses, pattern ids, and helper functions. |
| `whilly/security/prompt_sanitizer.py` | Sanitizer backed by shared secret redaction. | VERIFIED | Uses `contains_secret` and `redact_secrets`; private registry remains removed. |
| `whilly/sources/github_issues.py` | GitHub issue source warning detection backed by shared secret scanning and Ruff-formatted. | VERIFIED | Uses shared scanner/pattern ids and now includes the Ruff-required blank line before `@dataclass`. |
| `whilly/project_config/loader.py` | Project-config validation hook for config-value secret linting. | VERIFIED | Calls `scan_mapping()` through `_project_config_secret_finding()` before config construction. |
| `tests/unit/test_project_config.py` | Project-config secret-lint tests, Ruff-formatted. | VERIFIED | The long `test_project_config_cli_validate_reports_secret_lint_blocked` signature is Ruff-formatted and the focused test file passes. |
| `whilly/adapters/runner/env.py` | Pure runner env allowlist and provider credential inference. | VERIFIED | Copies only allowlisted names plus explicit/model-inferred credentials. |
| `whilly/adapters/runner/proxy.py` | Proxy env layering over scrubbed runner env. | VERIFIED | `build_subprocess_env()` starts from `build_runner_env()`. |
| Claude/OpenCode/PRD/handoff subprocess files | Coding-agent subprocesses pass explicit scrubbed env mappings. | VERIFIED | Claude, OpenCode, handoff, and PRD subprocess calls pass `env=...`. |
| `whilly/core/agent_runner.py` | Task secret-surface scanner consumed by workers. | VERIFIED | `scan_task_secret_surface()` scans task fields and rendered runner prompt. |
| `whilly/worker/local.py`, `whilly/worker/remote.py` | Local/remote pre-run secret guard failure paths. | VERIFIED | Both fail with `SECRET_LINT_FAIL_REASON` before shell scan and runner calls. |
| `whilly/adapters/transport/server.py` | Remote security prelude allowlist for secret guard failures. | VERIFIED | Accepts `secret_lint_blocked` detail as a prelude event. |
| `whilly/pipeline/verification.py` | Redaction at verification audit boundaries. | VERIFIED | Redacts command/stdout/stderr before result event persistence. |
| `whilly/compliance/__init__.py`, `docs/CODEX-MISSION.md`, `docs/Current-vs-Target.md` | Residual-risk evidence without VM isolation overclaim. | VERIFIED | Guards are documented; full per-task VM/container isolation remains partial/future. |

### Key Link Verification

| From | To | Via | Status | Details |
| --- | --- | --- | --- | --- |
| `prompt_sanitizer.py` | `secret_lint.py` | `contains_secret`, `redact_secrets` imports | WIRED | Sanitizer uses shared redaction and secret checks. |
| `github_issues.py` | `secret_lint.py` | `SECRET_PATTERNS`, `scan_text` imports | WIRED | `_detect_secrets()` returns shared pattern ids. |
| `project_config/loader.py` | `secret_lint.py` | `scan_mapping()` | WIRED | `project_config_from_dict()` blocks plaintext secret-like config values. |
| Claude subprocess callers | `runner/proxy.py` | `spawn_env_for_claude(model=...)` | WIRED | Claude CLI, backend, and PRD subprocesses receive scrubbed envs. |
| OpenCode/handoff callers | `runner/env.py` | `build_runner_env()` | WIRED | OpenCode and handoff subprocesses receive scrubbed envs. |
| `worker/local.py` | `core/agent_runner.py` | `scan_task_secret_surface()` | WIRED | Local worker blocks before shell scanning and runner execution. |
| `worker/remote.py` | `core/agent_runner.py` | `scan_task_secret_surface()` | WIRED | Remote worker sends `client.fail(..., reason="secret_lint_blocked")` before runner execution. |
| `adapters/transport/server.py` | `secret_lint.py` | `SECRET_LINT_BLOCKED_EVENT_TYPE` | WIRED | Remote fail detail is persisted as a security prelude event. |
| `pipeline/verification.py` | `secret_lint.py` | `redact_secrets()` | WIRED | Verification result and event details are redacted. |
| `tests/unit/test_project_config.py`, `whilly/sources/github_issues.py` | Ruff format gate | `ruff format --check whilly/ tests/` | WIRED | Repository-wide format gate now passes. |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
| --- | --- | --- | --- | --- |
| SEC-01 | `08-01-PLAN.md`, `08-03-PLAN.md`, `08-04-PLAN.md` | Secret linting covers task descriptions, comments, config values, runner prompts, and external feedback. | SATISFIED | Shared scanner covers config mappings and text; sanitizer/GitHub issue feedback, task fields, runner prompt, and verification/audit text use the shared contract. |
| SEC-02 | `08-02-PLAN.md`, `08-03-PLAN.md`, `08-04-PLAN.md` | Runner environments are scrubbed to an explicit allowlist plus configured required tokens. | SATISFIED | `BASE_RUNNER_ENV_ALLOWLIST`, model-based credential inference, and explicit `env=` wiring cover Claude, OpenCode, PRD, and handoff subprocesses. |
| SEC-03 | `08-03-PLAN.md`, `08-04-PLAN.md` | Command and prompt guard failures emit auditable reasons. | SATISFIED | Prompt, shell, and secret guard paths emit deterministic fail reasons; secret guard payloads include stable ids and redacted excerpts. |

No orphaned Phase 8 requirements were found in `.planning/REQUIREMENTS.md`; SEC-01, SEC-02, and SEC-03 are all declared for Phase 8.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| --- | --- | --- | --- | --- |
| None | - | - | - | No placeholder/TODO/FIXME/`Not implemented`/empty-implementation anti-patterns were found in the Phase 8 implementation scan. |

### Automated Verification

| Check | Result |
| --- | --- |
| Previous verification artifact check | Found prior `status: gaps_found` with one formatting gap. |
| `git diff -- tests/unit/test_project_config.py whilly/sources/github_issues.py` | Only mechanical Ruff formatting changes: split long test signature and added blank line before `@dataclass`. |
| `./.venv/bin/python -m ruff format --check whilly/ tests/` | Passed: `426 files already formatted`. |
| `./.venv/bin/python -m pytest -q tests/unit/test_project_config.py tests/test_github_issues_source.py --maxfail=1` | Passed outside sandbox after pytest-rerunfailures could not bind its local status socket inside sandbox: `55 passed`. |
| `./.venv/bin/python -m ruff check whilly/ tests/` | Passed: `All checks passed!`. |
| `./.venv/bin/lint-imports --config .importlinter` | Passed: 2 contracts kept, 0 broken. |
| `gsd-tools verify artifacts/key-links 08-04-PLAN.md` | Informational only: current tool did not detect nested `must_haves` in this plan frontmatter, so artifact/key-link verification was performed manually. |
| `make test` | Not rerun for this re-verification. Prior verifier recorded unrelated out-of-scope failures; 08-04 changed only formatting and its focused regression tests pass. |

### Human Verification Required

None. This phase is backend/security hardening plus documentation/compliance wording; the remaining gap was an automated formatting gate and is now closed.

### Gaps Summary

No gaps remain. The only previous blocker, the Ruff formatting failure in `tests/unit/test_project_config.py` and `whilly/sources/github_issues.py`, is closed by 08-04 and verified by the exact repository-wide format gate plus focused sanity tests.

---

_Verified: 2026-05-08T14:47:44Z_
_Verifier: Claude (gsd-verifier)_
