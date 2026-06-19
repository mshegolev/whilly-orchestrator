---
phase: 33-known-drift-fixture-validation
verified: 2026-06-19T05:05:00Z
status: passed
score: 6/6 must-haves verified
overrides_applied: 0
---

# Phase 33: Known-Drift Fixture Validation Verification Report

**Phase Goal:** The guard is demonstrably trustworthy — proven against a deliberately drifted spec/code pair to detect a real HIGH drift while reporting an undrifted spec as clean (VALID-01).
**Verified:** 2026-06-19T05:05:00Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
| --- | ----- | ------ | -------- |
| 1 | Drifted spec/code fixture exists, self-contained (own spec.md + module + matrix), not depending on real openspec tree | ✓ VERIFIED | `drifted/{spec.md,module.py,matrix.md}` present. Spec is `# Capability: drifted`, matrix maps `module.py -> drifted` (own snippet). README L28-30 states it NEVER points at real `openspec/specs` / `COVERAGE-MATRIX.md`. Tests pass `specs_root=_FIXTURES`, `matrix_path=drifted/matrix.md` — fixture tree only. |
| 2 | Drifted spec's SHALL plainly contradicts its module | ✓ VERIFIED | `drifted/spec.md:9-11` — "`summarize` SHALL return a JSON object … SHALL NOT return a bare string." `drifted/module.py:17` — `return f"{field}={value}"  # VIOLATION: returns a bare string`. Contradiction is plain on the face of the code, on its own labeled line. |
| 3 | Clean control fixture exists whose spec exactly matches its module | ✓ VERIFIED | `clean/spec.md:9-10` — same SHALL (return a JSON object/dict). `clean/module.py:11-12` — `def summarize(...) -> dict: return {field: value}`. Spec and code agree. Matrix maps `module.py -> clean`. |
| 4 | Deterministic offline plumbing test runs both fixtures through real review_spec with scripted reviewer; asserts count_high==1 drifted / ==0 clean; CI-green offline | ✓ VERIFIED | `test_plumbing_detects_high_on_drifted_fixture` asserts `len==1`, `severity==SEVERITIES[0]`, `count_high==1`. `test_plumbing_reports_clean_on_control_fixture` asserts `findings==[]`, `count_high==0`. Ran `pytest -k "not live"` → **2 passed, 1 deselected** on python3 3.10, fully offline. Prompt-capture assertions (L93-94) prove the real review_spec pipeline (spec read + matrix resolve + module read) ran, not a shortcut. |
| 5 | Live canary is skipif-guarded and asserts ONLY severity-level outcomes (no wording/count assertions) | ✓ VERIFIED | `@pytest.mark.skipif(shutil.which("claude") is None, ...)` (L110). Real `sdc.claude_reviewer` (L122, L129). Asserts ONLY `count_high(drifted) >= 1` (L135) and `count_high(clean) == 0` (L136) — no assertion on wording, drift/rationale text, requirement string, or exact count. |
| 6 | Zero whilly/ and zero scripts/ production change across phase commits | ✓ VERIFIED | 3 phase commits (341db98 fixtures, 20c8a7d test, b7dc53d README) touch ONLY `tests/fixtures/semantic_drift/*` and `tests/test_semantic_drift_fixture_validation.py`. `git diff-tree` across all 3 commits → NO `whilly/` or `scripts/` files. No whilly import anywhere under fixtures. |

**Score:** 6/6 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
| -------- | -------- | ------ | ------- |
| `tests/fixtures/semantic_drift/drifted/spec.md` | Drifted spec with SHALL the module violates | ✓ VERIFIED | Contains SHALL (L9), concrete JSON-object claim. |
| `tests/fixtures/semantic_drift/drifted/module.py` | Module violating the SHALL | ✓ VERIFIED | Parseable; `return f"{field}={value}"` (bare string) at L17. |
| `tests/fixtures/semantic_drift/drifted/matrix.md` | Self-contained matrix snippet | ✓ VERIFIED | `| Module | Capability | Notes |` header + body row `module.py | drifted`, blank-line terminated. |
| `tests/fixtures/semantic_drift/clean/spec.md` | Clean spec matching module | ✓ VERIFIED | Contains SHALL (L9), same JSON-object claim. |
| `tests/fixtures/semantic_drift/clean/module.py` | Module satisfying clean spec | ✓ VERIFIED | Parseable; `return {field: value}` (dict) at L12. |
| `tests/fixtures/semantic_drift/clean/matrix.md` | Self-contained matrix snippet | ✓ VERIFIED | Header + body row `module.py | clean`, blank-line terminated. |
| `tests/fixtures/semantic_drift/README.md` | Documents contradiction + expected verdict | ✓ VERIFIED | Quotes the SHALL (L36-38), names violating line (L45), expected-verdict table (L66-69), reproduction steps + review_spec params. |
| `tests/test_semantic_drift_fixture_validation.py` | Plumbing + live canary | ✓ VERIFIED | `count_high()`, scripted-reviewer factory, 2 plumbing tests, 1 skipif-guarded live canary; `shutil.which` present. |

### Key Link Verification

| From | To | Via | Status | Details |
| ---- | -- | --- | ------ | ------- |
| test module | `scripts/semantic_drift_check.py::review_spec` | importlib load-by-path + `review_spec(slug, reviewer, specs_root=, repo_root=, matrix_path=)` | ✓ WIRED | `sdc` loaded via `spec_from_file_location` (L33-35); 3 `review_spec(...)` calls match the engine signature at `scripts/semantic_drift_check.py:350`. |
| test module | `scripts/semantic_drift_check.py::claude_reviewer` | live canary passes the real reviewer | ✓ WIRED | `reviewer=sdc.claude_reviewer` (L122, L129); `claude_reviewer` defined at engine L414. |
| live test | claude CLI presence | skipif guard | ✓ WIRED | `@pytest.mark.skipif(shutil.which("claude") is None, ...)` at L110. |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| -------- | ------- | ------ | ------ |
| Offline plumbing tests classify drifted=detected-HIGH, clean=clean | `python3 -m pytest tests/test_semantic_drift_fixture_validation.py -q -p no:cacheprovider -k "not live"` | 2 passed, 1 deselected | ✓ PASS |
| review_spec genuinely reads module source relative to repo_root | Inspected engine L378-405 (spec read → matrix resolve → per-module read) + test prompt-capture asserts (L93-94) | Pipeline reads spec.md, resolves matrix, reads module.py, builds prompt | ✓ PASS |
| Live canary skips cleanly without claude / no false-positive run | (live deselected; severity-only asserts confirmed by source) | Guarded, severity-only | ? SKIP (routed to human — see below) |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
| ----------- | ----------- | ----------- | ------ | -------- |
| VALID-01 | 33-01-PLAN.md | Mechanism validated against a known-drift fixture: detects a HIGH semantic drift and reports a clean spec as clean | ✓ SATISFIED | Truths 1-6 verified. Deterministic plumbing proves harness classification offline (count_high 1/0); live canary (skipif-guarded) asserts severity-level outcomes against the real model; README documents planted contradiction + expected verdict for reproducibility. |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| ---- | ---- | ------- | -------- | ------ |
| (none) | - | No TBD/FIXME/XXX/TODO/HACK/PLACEHOLDER in any phase file | ℹ️ Info | The `# VIOLATION:` comment in drifted/module.py is intentional fixture documentation of the planted drift, not a debt marker. |

### Human Verification Required

The live acceptance canary (`test_live_real_claude_flags_drift_and_clears_control`) exercises the real Claude CLI subprocess, which is non-deterministic and was deselected in the offline run. The SUMMARY claims it ran (1 passed, 3m43s, >=1 HIGH drifted / 0 HIGH clean), but that is a SUMMARY claim, not codebase-verifiable evidence in this offline verification pass.

#### 1. Live drift-detection canary against the real model

**Test:** With `claude` on PATH and credentials configured, run
`python3 -m pytest tests/test_semantic_drift_fixture_validation.py -k "live" -q`
**Expected:** 1 passed — the real `claude_reviewer` reports `count_high(drifted) >= 1` and `count_high(clean) == 0`.
**Why human:** Invokes a non-deterministic LLM over a network subprocess; cannot be verified deterministically in an offline verification pass and must not be run as a blocking gate. This is the trustworthiness canary VALID-01 ultimately relies on for the live (vs. plumbing) guarantee.

### Gaps Summary

No gaps. All six observable truths are verified against the codebase. The fixtures are genuinely self-contained (own spec/module/matrix, no reference to the real openspec tree, no whilly import), the planted contradiction is plain (spec SHALL return a JSON object vs. module returning a bare string at drifted/module.py:17), and the clean control matches its spec (returns a dict). The deterministic plumbing test runs offline through the real `review_spec` pipeline — confirmed green on python3 3.10 (2 passed) — and its prompt-capture assertions prove the spec-read/matrix-resolve/module-read pipeline actually executed rather than asserting a shortcut. The live canary is correctly skipif-guarded on `shutil.which("claude")` and asserts severity-level outcomes only. Zero whilly/ and zero scripts/ production change across all three phase commits, satisfying the locked final-v1.5-phase scope.

The only item routed to a human is the live model canary, which is inherently non-deterministic and cannot be confirmed in an offline verification — its severity-only assertion shape and skip guard are verified in source; its actual model run is the human-confirmable part.

---

_Verified: 2026-06-19T05:05:00Z_
_Verifier: Claude (gsd-verifier)_
