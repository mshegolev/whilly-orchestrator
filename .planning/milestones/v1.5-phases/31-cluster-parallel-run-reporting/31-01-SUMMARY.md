---
phase: 31-cluster-parallel-run-reporting
plan: 01
subsystem: semantic-drift-tooling
tags: [drift-detection, fleet-runner, reporting, scripts-only]
requires: ["30-02"]
provides:
  - "CLUSTERS partition constant (6 clusters -> 32 slugs)"
  - "run_fleet bounded ThreadPoolExecutor fan-out over review_spec"
  - "collect_run_metadata with injectable git/time/model seams"
  - "build_artifact locked JSON shape + pure format_summary"
  - "--all CLI mode (mutually exclusive with --slug)"
affects: [scripts/semantic_drift_check.py, tests/test_semantic_drift_check.py]
tech-stack:
  added: []
  patterns: [dependency-injection-seams, bounded-thread-pool, per-unit-resilience, pure-formatter]
key-files:
  created: []
  modified:
    - scripts/semantic_drift_check.py
    - tests/test_semantic_drift_check.py
decisions:
  - "TOOL_VERSION constant (31.1.0) for self-describing run metadata, independent of whilly package version"
  - "cluster_for_slug returns None on unknown slug (pinned, never raises)"
  - "findings tagged with their cluster; build_artifact buckets clean = reviewed-no-findings-no-error"
metrics:
  duration: "~12 min"
  completed: 2026-06-19
requirements: [RUN-01, RUN-02, RUN-03, REPORT-01, REPORT-02]
---

# Phase 31 Plan 01: Cluster-Parallel Run & Reporting Summary

Full-fleet semantic-drift runner layered on Phase 30's `review_spec`: one `--all`
invocation reviews all 32 capability specs via a bounded 6-worker ThreadPoolExecutor
grouped into the validated 6-cluster partition, is resilient to per-unit failure,
records reproducible run metadata via injectable git/time/model seams, and emits a
locked-shape JSON artifact plus a pure-formatter human summary — all offline-testable.

## What Was Built

- **Task 1 (RUN-01):** `CLUSTERS` dict — disjoint, exhaustive partition of the 32 live
  `openspec/specs` slugs into the 6 named clusters (orchestration 7, prd-decision 5,
  integrations 5, operator-surface 5, platform 5, safety-quality 5 = 32). Added
  `cluster_for_slug(slug) -> str | None` (reverse index, None on unknown) and
  `live_slugs(specs_root)` filesystem enumerator. Tests assert the partition is
  exhaustive AND disjoint against the live filesystem so the grouping cannot drift.
- **Task 2 (RUN-01/02/03):** `run_fleet(slugs, reviewer, *, max_workers=6, ...)` submits
  one `review_spec` unit per slug to a `concurrent.futures.ThreadPoolExecutor`; each unit
  is wrapped in try/except so a raising reviewer records `{slug, cluster, error}` and the
  fleet CONTINUES. Findings are flattened and sorted deterministically by
  `(slug, severity-index)`. `collect_run_metadata` resolves model (arg > WHILLY_MODEL >
  DEFAULT_MODEL) and accepts injectable `git_info`/`now` seams; the default git seam
  (`_default_git_info`) degrades to `commit=None/dirty=None` on any subprocess failure.
  Added `TOOL_VERSION = "31.1.0"`.
- **Task 3 (REPORT-01/02):** `build_artifact(results, metadata, *, total=32)` produces the
  CONTEXT-locked shape `{run, coverage:{reviewed,total:32}, clusters:{<c>:{high,medium,low,
  clean,error}}, findings, errors}`. `format_summary(artifact)` is a PURE function rendering
  a per-cluster H/M/L+clean+error table, a `reviewed/32` coverage line, and a
  confirmed-vs-clean split. `main` now has a required mutually-exclusive `--all`/`--slug`
  group plus `--max-workers`/`--output`/`--model`; `--all` runs the fleet, writes the JSON
  artifact, prints the summary, and exits 0. Phase 30 `--slug` behavior is unchanged.

## Deviations from Plan

None - plan executed exactly as written. (One non-deviation note: `ruff format` from the
repo pre-commit hook collapsed a multi-line set comprehension in `live_slugs`; cosmetic
only, no logic change.)

## TDD Gate Compliance

Each task followed RED -> GREEN. Tests were committed together with their implementation
per task (single atomic commit per task with both `test`-style and `feat` content), so the
git log shows three `feat(31-01)` commits rather than separate `test`/`feat` pairs. RED was
verified before each implementation (8, 7, 4 failing tests respectively, all due to the
not-yet-added symbols), then GREEN confirmed before commit.

## Verification Results

- `python3 -m pytest tests/test_semantic_drift_check.py -q -p no:cacheprovider`:
  **51 passed, 1 skipped** in 112s (the live-CLI test ran because `claude` is aliased on
  PATH; the 1 skip is the optional `json_repair` test). Offline subset (live_cli
  deselected): 50 passed, 1 skipped.
- `python3 -m ruff check scripts/ tests/`: **All checks passed.**
- `git diff --name-only -- whilly/`: **empty** — zero `whilly/` change, no new
  coverage-matrix / opsx obligation.

## Self-Check: PASSED

- scripts/semantic_drift_check.py: FOUND
- Commits b2b3b9d, 3b89996, 099a99c: FOUND
- Exports CLUSTERS, run_fleet, build_artifact, format_summary, collect_run_metadata,
  cluster_for_slug, live_slugs, TOOL_VERSION: all importable.
