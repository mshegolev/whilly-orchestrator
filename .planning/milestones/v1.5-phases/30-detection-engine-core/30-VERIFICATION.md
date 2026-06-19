---
phase: 30-detection-engine-core
verified: 2026-06-19T00:00:00Z
status: passed
score: 4/4 must-haves verified
overrides_applied: 0
---

# Phase 30: Detection Engine Core Verification Report

**Phase Goal:** A single capability spec can be reviewed against its mapped `whilly/` code, producing structured per-requirement findings that are severity-rated, triaged, and backed by file:line evidence.
**Verified:** 2026-06-19
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria + PLAN must_haves)

| #   | Truth (Success Criterion / DETECT req) | Status     | Evidence |
| --- | --- | --- | --- |
| 1 (SC1 / DETECT-01) | Operator can run the checker against one slug and get per-requirement findings reviewing spec text vs mapped modules | ✓ VERIFIED | `main(--slug)` → `review_spec` → loads `openspec/specs/<slug>/spec.md` + mapped module sources → `build_review_prompt` → injected reviewer → `parse_findings`. Ran `main(['--slug','orchestration-loop'], reviewer=...)`: prints findings JSON, rc=0. `scripts/semantic_drift_check.py:263-400` |
| 2 (SC2 / DETECT-02) | Each finding carries severity (HIGH/MEDIUM/LOW), slug, requirement name, one-line drift, and file:line evidence | ✓ VERIFIED | `FINDING_KEYS` = (severity, slug, requirement, drift, evidence, triage, rationale); `validate_finding` requires exactly these keys, `severity in SEVERITIES`, and an evidence string containing `:`. Tests `test_validate_finding_*`, `test_parse_findings_drops_invalid_entries` pass. `:43-53, :237-255` |
| 3 (SC3 / DETECT-03) | Each finding labeled `code-bug` or `spec-overstatement` with a short rationale | ✓ VERIFIED | `TRIAGE_VALUES = ("code-bug","spec-overstatement")`; validator enforces `triage in TRIAGE_VALUES`; prompt names both values with definitions and requires a `rationale` key. Tests `test_validate_finding_rejects_bad_triage`, `test_build_review_prompt_names_enums_and_file_line` pass. `:53, :160-162, :250` |
| 4 (SC4 / DETECT-04) | Module review set derived live from `openspec/COVERAGE-MATRIX.md`, not a second mapping (changing matrix changes set) | ✓ VERIFIED | `resolve_modules_for_slug` parses the matrix at call time via `_parse_matrix_rows`; injectable `matrix_path`. Ran against real matrix: `orchestration-loop` → 9 modules (exactly the 9 `\| orchestration-loop \|` rows), exact-match (`'orchestration'` → `[]`). Temp-matrix test `test_resolve_modules_for_slug_is_live` proves matrix changes change the set. No second hand-maintained mapping exists. `:63-104` |

**Score:** 4/4 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
| --- | --- | --- | --- |
| `scripts/semantic_drift_check.py` | resolve/prompt/parse/validate + review_spec + claude_reviewer + main | ✓ VERIFIED | 405 lines (min 80/60 met). All 7 functions present and substantive. Lives under `scripts/` not `whilly/` per CONTEXT (no opsx/coverage obligation). `ruff check` clean. |
| `tests/test_semantic_drift_check.py` | unit tests for all functions, fake reviewer, live-skip | ✓ VERIFIED | 485 lines, 31 tests. Loads module by file path. No subprocess/httpx/network at import. `fake_reviewer`, `test_resolve*`, `test_*prompt*`, live-CLI skipif present. |

### Key Link Verification

| From | To | Via | Status | Details |
| --- | --- | --- | --- | --- |
| `semantic_drift_check.py` | `openspec/COVERAGE-MATRIX.md` | matrix table parse (ports audit-coverage-matrix logic) | ✓ WIRED | `_parse_matrix_rows` regex-locates `\| Module \| Capability \| Notes \|` table, reads file at call time. Confirmed live: 9 modules for orchestration-loop. |
| `build_review_prompt` | prompt contract | embeds spec + sources, file:line + code-bug\|spec-overstatement | ✓ WIRED | Prompt string contains `spec-overstatement`, `file:line`, all 3 severities, 7 keys, `[]` clean-spec rule. Pure function (no `open()`/subprocess) — determinism test passes. |
| `review_spec` | resolve + build_prompt + parse_findings | full pipeline | ✓ WIRED | Source reads spec.md + modules, calls all three; `test_review_spec_returns_findings_for_valid_reviewer` asserts prompt contains slug + mapped path. |
| `claude_reviewer` | Claude CLI | subprocess `-p --output-format json`, CLAUDE_BIN | ✓ WIRED | argv built with `--output-format json`, `-p`, honors `CLAUDE_BIN`; envelope `{result:...}` unwrap + raw fallback. Tests `test_claude_reviewer_*` pass. |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
| --- | --- | --- | --- | --- |
| `resolve_modules_for_slug` | matrix rows | live read of `openspec/COVERAGE-MATRIX.md` | Yes — 9 real `whilly/` paths returned for orchestration-loop | ✓ FLOWING |
| `review_spec` | findings | injected reviewer (default = live Claude CLI) | Yes — pipeline returns parsed list; live CLI path exists and is exercised when `claude` present | ✓ FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| --- | --- | --- | --- |
| Offline test suite | `pytest tests/test_semantic_drift_check.py -q` (live deselected) | 29 passed, 1 skipped (optional json_repair), 1 deselected | ✓ PASS |
| Live matrix resolution | `resolve_modules_for_slug('orchestration-loop')` | 9 modules, exact-match, unknown→[] | ✓ PASS |
| CLI clean spec | `main(['--slug','orchestration-loop'], reviewer=lambda:'[]')` | prints `[]`, rc=0 | ✓ PASS |
| CLI with finding | `main(..., reviewer=lambda: <valid finding>)` | prints finding JSON, rc=0 | ✓ PASS |
| Lint | `ruff check scripts/... tests/...` | All checks passed | ✓ PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
| --- | --- | --- | --- | --- |
| DETECT-01 | 30-02 | Operator runs single-spec check → per-requirement findings | ✓ SATISFIED | `main`/`review_spec` pipeline + CLI spot-check rc=0 |
| DETECT-02 | 30-01 | Finding records severity/slug/requirement/drift/file:line | ✓ SATISFIED | `FINDING_KEYS` + `validate_finding` + prompt schema |
| DETECT-03 | 30-01 | Triage code-bug vs spec-overstatement + rationale | ✓ SATISFIED | `TRIAGE_VALUES` enforced + rationale key |
| DETECT-04 | 30-01 | Review set derived live from COVERAGE-MATRIX.md (no second mapping) | ✓ SATISFIED | `_parse_matrix_rows` live read; injectable path; no duplicate mapping |

Note: `.planning/REQUIREMENTS.md` and `.planning/ROADMAP.md` tracking tables still mark DETECT-02/03/04 as "Pending"/unchecked. This is stale tracking metadata, not a code gap — the shipped implementation and passing tests satisfy all four. Recommend updating those checkboxes, but it does not block the phase goal.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| --- | --- | --- | --- | --- |
| (none) | — | No TODO/FIXME/XXX/TBD/HACK/PLACEHOLDER in delivered files | — | — |

### Human Verification Required

None. All four success criteria are programmatically verifiable and verified. The live Claude-CLI reviewer is dependency-injected and the live path is covered by a skip-guarded test; the pipeline's correctness is fully exercised offline with a fake reviewer, so no human UAT is required for the phase goal.

### Gaps Summary

No gaps. The shipped `scripts/semantic_drift_check.py` delivers the single-spec semantic drift engine: live matrix-driven module resolution (DETECT-04), a pure prompt builder encoding the full 7-key schema/enums/file:line/triage contract (DETECT-02/03), robust never-raising findings parse + per-finding validation, and an end-to-end `--slug` CLI with a dependency-injected default Claude reviewer that always exits 0 (DETECT-01, report-only as designed). TDD discipline is evidenced (RED→GREEN commits per plan), scope is correctly confined to `scripts/`+`tests/` with zero `whilly/` change (honoring the CONTEXT exemption decision), and lint is clean. The only follow-up is cosmetic: update the REQUIREMENTS/ROADMAP tracking checkboxes for DETECT-02/03/04 to Complete.

---

_Verified: 2026-06-19_
_Verifier: Claude (gsd-verifier)_
