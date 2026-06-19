---
phase: 31-cluster-parallel-run-reporting
verified: 2026-06-19T00:00:00Z
status: passed
score: 8/8 must-haves verified
overrides_applied: 0
---

# Phase 31: Cluster-Parallel Run & Reporting Verification Report

**Phase Goal:** A single run fans out the detection engine across all 32 specs in the proven 6-cluster pattern — bounded, resilient, self-describing — and emits both a machine-readable findings artifact and a human summary.
**Verified:** 2026-06-19
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | CLUSTERS partitions all 32 live `openspec/specs` slugs into the 6 named clusters; test proves exhaustive AND disjoint vs the live filesystem (RUN-01) | VERIFIED | Live check: `set(flat)==live`, `len(flat)==len(set(flat))==32`, in-live-not-partition and in-partition-not-live both empty. Cluster keys exactly the 6 named. Tests `test_clusters_partition_is_exhaustive_vs_live_slugs`, `..._is_disjoint_and_thirty_two`, `..._no_unknown_slugs_on_disk` (`scripts/semantic_drift_check.py:71-142`, tests:515-533). |
| 2 | One `--all` invocation reviews every one of the 32 specs via a bounded `ThreadPoolExecutor` (default `--max-workers 6`), reusing `review_spec` with an injected reviewer offline (RUN-01) | VERIFIED | `run_fleet` uses `concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)` submitting one `review_spec` unit per slug (`:473-532`). Spot-check: clean fleet over all 32 slugs → `reviewed==32`. `main` `--all` builds slug list from union of CLUSTERS (`:729`). Tests `test_run_fleet_reviews_every_spec_once`, `..._honors_max_workers_bound`. |
| 3 | Findings sorted deterministically by (slug, then severity); fixed responses → byte-stable ordering (RUN-01) | VERIFIED | `findings.sort(key=lambda f: (f.get("slug",""), _severity_index(f.get("severity",""))))` (`:531`); `_severity_index` ranks by SEVERITIES order. Test `test_run_fleet_findings_sorted_by_slug_then_severity` asserts `[("cap-a","HIGH"),("cap-a","LOW"),("cap-b","MEDIUM")]` stable across two runs. |
| 4 | Each per-spec review runs in try/except: an exception records `{slug, cluster, error}` and the run CONTINUES; surfaced in artifact + summary (RUN-02) | VERIFIED | `future.result()` wrapped in `try/except Exception` → `errors.append({"slug","cluster","error"})` + `continue` (`:520-524`). Spot-check: reviewer raising on `auth-security` → run completed 31/32, error `{'slug':'auth-security','cluster':'platform','error':'boom on auth'}` in artifact, summary shows `Errors: 1` + platform `error:1`. Test `test_run_fleet_resilient_to_reviewer_exception`. |
| 5 | Run is self-describing: artifact run block records model (arg>WHILLY_MODEL>default), git commit+dirty (injectable seam), injectable timestamp, tool version — reproducible without live git in tests (RUN-03) | VERIFIED | `collect_run_metadata` resolves model by precedence, injectable `git_info`/`now` seams, `_default_git_info` degrades to None on failure (`:535-591`). Spot-check artifact `run`: model+commit+dirty+timestamp+tool_version(31.1.0) all present. Tests `test_collect_run_metadata_uses_injected_git_and_time_seams`, `..._model_resolution_precedence`, `..._default_git_seam_degrades_gracefully`. |
| 6 | Run writes a JSON artifact via `--output` with shape `{run, coverage:{reviewed,total:32}, clusters:{<c>:{high,medium,low,clean,error}}, findings, errors}` (REPORT-01) | VERIFIED | `build_artifact` produces exactly those keys; all 6 clusters always present with the 5 buckets (`:599-658`). `main --all` writes via `json.dump(artifact, f)` to `--output` (`:740-741`). Spot-check: artifact top keys `{clusters,coverage,errors,findings,run}`, `coverage={'reviewed':31,'total':32}`, file parseable. Test `test_build_artifact_has_locked_shape`, `test_main_all_writes_artifact_and_returns_zero`. |
| 7 | Pure formatter builds human summary with per-cluster H/M/L+clean table, coverage reviewed/32, confirmed-vs-clean split — from a results dict without running the fleet (REPORT-01, REPORT-02) | VERIFIED | `format_summary` is pure (no I/O/subprocess), renders cluster table + `Coverage: reviewed N/32` + `Confirmed findings` / `Clean specs` split (`:661-699`). Spot-check rendered table observed directly. Test `test_format_summary_is_pure_and_renders_table` builds from hand-made dict. |
| 8 | `--all`/`--slug` mutually exclusive, exactly one required; `--all` exits 0 (gating is Phase 32) | VERIFIED | `parser.add_mutually_exclusive_group(required=True)` with `--slug`/`--all` (`:717-719`); `--all` path returns 0 (`:743`). Spot-check `rc==0`. Tests `test_main_all_and_slug_mutually_exclusive`, `test_main_neither_all_nor_slug_errors`, `test_main_all_writes_artifact_and_returns_zero`. |

**Score:** 8/8 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `scripts/semantic_drift_check.py` | CLUSTERS, run_fleet, per-unit resilience, collect_run_metadata w/ git+time seams, build_artifact, format_summary, --all CLI | VERIFIED | All exports importable; substantive (758 lines, real logic, no stubs). Wired into `main` `--all` path. |
| `tests/test_semantic_drift_check.py` | Fleet/partition/resilience/metadata/artifact/summary/CLI tests | VERIFIED | 45 offline tests pass, 1 skipped (optional json_repair), 6 deselected (live). Each Phase 31 truth has dedicated assertions. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `run_fleet` | `review_spec` | per-slug unit submitted to ThreadPoolExecutor with injected reviewer | WIRED | `pool.submit(_unit, slug)` where `_unit` calls `review_spec(slug, reviewer, ...)` (`:506-516`). |
| `CLUSTERS` | `openspec/specs/*` | exhaustive+disjoint partition test vs live filesystem | WIRED | Live check confirms `set(flat)==live` (32==32), tests assert against real `openspec/specs`. |
| `main` | `build_artifact` / `format_summary` | `--all` writes JSON to `--output` + prints summary | WIRED | `build_artifact(results, metadata)` → `json.dump(...)` → `print(format_summary(artifact))` (`:739-742`). |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|--------------|--------|--------------------|--------|
| `--all` artifact | findings/errors/coverage/clusters | `run_fleet` over live CLUSTERS slugs via `review_spec` (injected reviewer in tests, `claude_reviewer` default) | Yes — spot-check: real finding flowed orchestration-loop→HIGH bucket; clean specs counted; error bucketed to platform | FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Offline test subset | `python3 -m pytest ... -k "not live"` | 45 passed, 1 skipped, 6 deselected | PASS |
| Live partition exhaustive+disjoint | python one-liner vs `openspec/specs` | 32==32, set equal, disjoint True | PASS |
| `--all` end-to-end (finding + raising reviewer) | injected fake reviewer via `main(['--all',...])` | rc=0, artifact locked shape, 31/32 reviewed, error recorded, summary printed | PASS |
| Full 32-spec clean fleet | `run_fleet(all 32, '[]')` | reviewed==32, errors==0, summary 32/32 clean | PASS |
| ruff lint | `python3 -m ruff check scripts/... tests/...` | All checks passed | PASS |
| Zero whilly/ change | `git diff --name-only b2b3b9d^ 099a99c -- whilly/` | empty (only scripts/ + tests/ touched) | PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| RUN-01 | 31-01 | Fan out across clusters in parallel, cover all 32 | SATISFIED | Truths 1-3; ThreadPoolExecutor over 32-slug partition, deterministic sort |
| RUN-02 | 31-01 | Bounded + resilient; failed unit degrades to recorded error | SATISFIED | Truth 4; try/except per unit, error spot-checked surfaced in artifact + summary |
| RUN-03 | 31-01 | Self-describing: model + commit/tree state | SATISFIED | Truth 5; collect_run_metadata with injectable git/time/model seams |
| REPORT-01 | 31-01 | Machine-readable JSON artifact + human summary | SATISFIED | Truths 6-7; build_artifact locked shape + format_summary table |
| REPORT-02 | 31-01 | Coverage reviewed/32 + confirmed-vs-clean split | SATISFIED | Truth 7; pure formatter renders coverage line + confirmed/clean split |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| (none) | — | No TBD/FIXME/XXX; no stub returns; empty-list/dict initializers are accumulators populated by real logic | — | Clean |

### Human Verification Required

None. The phase is `scripts/`-only deterministic tooling fully exercised offline via injected reviewers; no visual/UX/real-time/external-service surface and no live-Claude dependency in the verified paths.

### Gaps Summary

No gaps. All 5 ROADMAP success criteria (RUN-01..03, REPORT-01..02) and all 8 PLAN must-have truths are independently verified against the shipped code — not merely against SUMMARY claims. The CLUSTERS partition is provably exhaustive + disjoint against the live 32-spec filesystem; `run_fleet` fans across all 32 via a bounded ThreadPoolExecutor reusing `review_spec`; a raising reviewer is isolated into a recorded per-unit error while the run continues (confirmed end-to-end: 31/32 reviewed with the error surfaced in both artifact and summary); run metadata is self-describing via injectable seams; the locked-shape JSON artifact and the pure per-cluster summary (coverage/32 + confirmed-vs-clean) are emitted at exit 0. Phase 30 `--slug` behavior is preserved and whilly/ is untouched across the three phase commits.

---

_Verified: 2026-06-19T00:00:00Z_
_Verifier: Claude (gsd-verifier)_
