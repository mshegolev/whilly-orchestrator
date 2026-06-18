# Requirements: Whilly Orchestrator — v1.5 Semantic Drift-Guard

**Defined:** 2026-06-18
**Core Value:** Operators can safely coordinate AI-assisted engineering work with auditable state,
human control, and verification before claiming success.

**Milestone goal:** Add a repeatable, agent-assisted *semantic* spec-fidelity check that catches
when a capability spec's `SHALL`/`MUST` requirements no longer match live code behavior — the drift
class the v1.4 mechanical gate (coverage matrix + `openspec validate --strict`) provably cannot
detect. The deliverable is the detection *mechanism*, additive to v1.4 and validated against a
known-drift fixture.

**Decisions locked (2026-06-18):**
- Additive to v1.4: the mechanical gate stays per-PR; the semantic check is separate.
- LLM/agent-assisted ⇒ non-deterministic and costly ⇒ runs on a scheduled cadence, not every PR.
- Findings must be evidence-backed (`file:line`) and reproducible (records model + reviewed commit).
- Reuses `openspec/COVERAGE-MATRIX.md` for the spec→module review set (no second mapping).

## v1.5 Requirements

### Detection (DETECT)

- [x] **DETECT-01**: Operator can run a semantic spec-fidelity check that reviews each capability
  spec's `SHALL`/`MUST` requirements against its mapped `whilly/` modules and emits per-requirement
  findings.
- [ ] **DETECT-02**: Each finding records severity (HIGH/MEDIUM/LOW), capability slug, requirement
  name, a one-line drift description, and `file:line` code evidence.
- [ ] **DETECT-03**: Each finding is triaged as `code-bug` (code diverged from a correct spec) or
  `spec-overstatement` (spec claims more than the code does), with a short rationale.
- [ ] **DETECT-04**: The checker derives the spec→module review set from
  `openspec/COVERAGE-MATRIX.md`, reusing the existing mapping rather than a hand-maintained second
  source.

### Orchestration (RUN)

- [ ] **RUN-01**: A single check run fans out across capability clusters in parallel and covers all
  32 capability specs.
- [ ] **RUN-02**: A run is bounded and resilient — a failed cluster/spec review degrades to a
  recorded error for that unit rather than aborting the whole run.
- [ ] **RUN-03**: A run is self-describing: it records the model used and the spec/code commit (or
  tree state) it reviewed, so a findings set is reproducible and auditable.

### Reporting (REPORT)

- [ ] **REPORT-01**: A run writes a machine-readable findings artifact (e.g. JSON) and a
  human-readable summary with per-cluster tallies (H/M/L and clean count).
- [ ] **REPORT-02**: The summary reports coverage (specs reviewed / 32) and distinguishes confirmed
  findings from clean specs.

### CI Integration (CI)

- [ ] **CI-01**: The semantic check runs as a scheduled CI job (cron/manual dispatch), separate
  from and not blocking the v1.4 per-PR mechanical gate.
- [ ] **CI-02**: The scheduled job surfaces results (artifact upload + summary) with a configurable
  gating posture (report-only vs fail-on-HIGH).

### Self-Validation (VALID)

- [ ] **VALID-01**: The mechanism is validated against a known-drift fixture (a deliberately drifted
  spec/code pair) proving it detects a HIGH semantic drift and reports a clean spec as clean — so
  the guard is demonstrably trustworthy, not just plausible.

## Future Requirements (deferred)

- Auto-opening `opsx` change proposals or code-fix PRs from confirmed findings — v1.5 detects and
  reports; remediation stays human-driven.
- Per-PR (not scheduled) semantic checking scoped to the diff's touched capabilities — possible
  once cost/latency are characterized.
- Historical drift trend tracking / dashboards.

## Out of Scope

- Replacing or weakening the v1.4 mechanical gate — the semantic check is strictly additive.
- Auto-applying fixes or auto-archiving spec deltas — externally visible mutation stays human-gated.
- Re-auditing or rewriting the 32 baseline specs — this milestone builds detection, not content.
- Treating LLM findings as authoritative without evidence — every finding needs `file:line` proof.

## Traceability

Every v1.5 requirement maps to exactly one phase. Coverage: 12/12, no orphans, no duplicates.

| Requirement | Phase | Status |
|-------------|-------|--------|
| DETECT-01 | Phase 30 — Detection Engine Core | Complete |
| DETECT-02 | Phase 30 — Detection Engine Core | Pending |
| DETECT-03 | Phase 30 — Detection Engine Core | Pending |
| DETECT-04 | Phase 30 — Detection Engine Core | Pending |
| RUN-01 | Phase 31 — Cluster-Parallel Run & Reporting | Pending |
| RUN-02 | Phase 31 — Cluster-Parallel Run & Reporting | Pending |
| RUN-03 | Phase 31 — Cluster-Parallel Run & Reporting | Pending |
| REPORT-01 | Phase 31 — Cluster-Parallel Run & Reporting | Pending |
| REPORT-02 | Phase 31 — Cluster-Parallel Run & Reporting | Pending |
| CI-01 | Phase 32 — Scheduled CI Integration | Pending |
| CI-02 | Phase 32 — Scheduled CI Integration | Pending |
| VALID-01 | Phase 33 — Known-Drift Fixture Validation | Pending |
