# Phase 12: Governance and semantic-memory decision - Research

**Researched:** 2026-05-08
**Domain:** Whilly governance policy, compliance reporting, semantic-memory scope, and docs alignment
**Confidence:** HIGH

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

## Implementation Decisions

### Governance Policy
- Governance risk policy must be deterministic and inspectable, not LLM-scored.
- The minimum risk domains are migrations, authentication/authorization, infrastructure, dependency changes, release actions, and external PR behavior.
- Governance output should identify why a task or plan is high risk and what operator approval or documentation boundary applies.
- Governance must preserve the current control-plane framing: it can recommend or require gates, but it must not claim autonomous production release.

### Semantic Memory
- Semantic memory must remain out of current-capability claims unless it is deterministic, evidence-backed, and wired into task planning or completion.
- If deterministic semantic memory is not implemented in this phase, docs and compliance should explicitly defer it and say deterministic events, task history, PR evidence, and verification logs remain authoritative.
- Semantic recall must never override deterministic audit evidence.
- Future semantic-memory target wording belongs in target/future scope, not implemented capability rows.

### Docs And Compliance Alignment
- `docs/Current-vs-Target.md`, README/docs boundary wording, and compliance report capability rows must describe the same current-vs-target status after Phases 8-11.
- Phase 11 shipped explicit configured CI polling and bounded repair; docs should no longer list those as only future target capabilities.
- Full sandbox or VM isolation, semantic long-term memory, fully autonomous production release, default auto-merge, and continuous PR review repair remain non-goals unless code evidence says otherwise.
- Compliance report tests should guard against positive current-capability claims that contradict docs.

### Claude's Discretion
- Exact module names and risk score shape are at Claude's discretion if they remain pure, typed, and testable.
- Governance can be surfaced through compliance reporting first if that is the smallest coherent runtime/code path.
- Semantic memory may be deferred instead of implemented if implementing it would require a new storage/retrieval subsystem beyond the milestone scope.

### Deferred Ideas (OUT OF SCOPE)

## Deferred Ideas

- A real semantic-memory retrieval subsystem is deferred unless planning finds an existing deterministic event-backed implementation path small enough for this phase.
- Continuous PR review repair loops and autonomous production release remain future capabilities.
- Full per-task VM/container isolation remains future hardening scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| DOC-04 | Current docs and compliance wording stay synchronized as hardening phases ship. | Use compliance wording as a tested contract and update `docs/Current-vs-Target.md`, README/docs boundary wording, and target compliance guidance together. |
| GOV-01 | Governance policy scores risk for migrations, auth, infra, dependencies, release actions, and external PR behavior. | Add a deterministic pure policy module with stable categories, risk scores, reasons, approval boundaries, and unit tests for every required domain. |
| GOV-02 | Semantic memory is either implemented deterministically from event/task history or explicitly deferred from current scope. | Defer semantic memory for this phase unless an existing runtime path is found; compliance/docs should state deterministic events, task history, PR evidence, and verification logs remain authoritative. |
</phase_requirements>

## Summary

Phase 12 should be a small deterministic governance and documentation-alignment slice, not a new governance platform or memory subsystem. The codebase already has the right local patterns: pure value-object policy modules (`whilly.core.gates`, `whilly.repair.policy`), evidence-scanned compliance rows (`whilly.compliance`), and wording-focused compliance tests (`tests/unit/test_compliance_report.py`). Governance scoring should follow those patterns: pure, typed, deterministic, stable labels, no LLM calls, no I/O, and tests that cover every required risk domain.

Semantic memory should be explicitly deferred. Current repository evidence only shows a hardcoded compliance failure for `Semantic memory`; no deterministic semantic-memory runtime module is wired into task planning or completion. Implementing real semantic memory would require storage, retrieval, evidence precedence, secret handling, and worker/planner integration. That is outside the phase boundary unless the planner discovers an existing deterministic event-backed path. The safer Phase 12 outcome is to mark semantic memory as future scope while making deterministic events, task history, PR evidence, and verification logs authoritative.

Docs need synchronization because Phase 8-11 have moved capabilities forward. The current compliance report already reports bounded CI polling/repair and operator-triggered rollback as PASS with scoped wording, but `docs/Current-vs-Target.md` still lists CI polling/bounded repair as target work and treats robust rollback/semantic memory ambiguously. The implementation plan should update docs and compliance together and add tests that prevent future drift.

**Primary recommendation:** Implement deterministic governance scoring in `whilly/core/governance.py`, expose it through the compliance report first, explicitly defer semantic memory in compliance/docs, and update current-vs-target wording under tests.

## Standard Stack

### Core

| Library / Module | Version | Purpose | Why Standard |
|------------------|---------|---------|--------------|
| Python stdlib `dataclasses`, `enum`, `re` | Python >=3.12 from `pyproject.toml` | Frozen value objects, str enums, deterministic keyword/path matching. | Existing pure-domain modules use frozen dataclasses and str enums; no new dependency is needed. |
| `whilly.core` | local package | Pure governance scoring over task/plan metadata. | `whilly.core.gates` already owns deterministic decision-gate policy and is protected by `.importlinter`. |
| `whilly.compliance` | local package | Evidence-based capability matrix, markdown/JSON report, doc mismatch scanning. | Existing compliance rows already distinguish PASS/PARTIAL/FAIL/UNKNOWN from concrete repo evidence. |
| `pytest` | `>=8.0` from `pyproject.toml` | Unit tests for governance, compliance wording, docs mismatch behavior. | Existing unit tests pin deterministic behavior and report wording. |

### Supporting

| Library / Module | Version | Purpose | When to Use |
|------------------|---------|---------|-------------|
| `ruff` | `0.11.5` from `pyproject.toml` | Formatting/lint gate. | Run for any implementation slice touching code/tests. |
| `import-linter` | `>=2.0` from `pyproject.toml` | Core-purity contract. | Run when adding `whilly.core.governance.py` or exporting it from `whilly.core.__init__`. |
| `whilly.pipeline.sinks` | local package | Existing PR sink approval policy and external-action guard wording. | Use as source context for external PR behavior and profile/human approval boundaries. |
| `whilly.project_config` | local package | Current release-policy, sink, verification, and repo metadata surfaces. | Use if governance scoring accepts project-config metadata beyond plain tasks. |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `whilly/core/governance.py` | `whilly/governance/` | A top-level package matches older roadmap notes, but governance scoring is pure domain logic. Put the scorer in `whilly.core`; add a package later only if runtime orchestration grows. |
| Compliance-only string checks | Pure governance module plus compliance row | Compliance-only checks would satisfy report output but would not provide reusable policy behavior or GOV-01 confidence. |
| Semantic-memory implementation | Explicit deferral | Real semantic memory needs storage/retrieval/security/runtime precedence. Deferral satisfies GOV-02 honestly and avoids overclaiming. |

**Installation:**

```bash
pip install -e '.[dev]'
```

**Version verification:** No new runtime package should be added. Versions above were verified from `pyproject.toml` and `Makefile` on 2026-05-08. Registry checks are not needed because the recommended stack is the repository's existing local Python stack.

## Architecture Patterns

### Recommended Project Structure

```text
whilly/
├── core/
│   ├── governance.py          # Pure deterministic risk scoring and typed policy outputs
│   └── __init__.py            # Optional exports for public core surface
├── compliance/
│   └── __init__.py            # Capability row, semantic-memory deferral row, docs mismatch rules
└── project_config/
    └── models.py              # Existing optional metadata source, no broad schema rewrite

tests/
├── unit/
│   ├── core/
│   │   └── test_governance_policy.py
│   ├── test_compliance_report.py
│   └── test_configured_sinks.py
└── integration/
    └── test_phase1_smoke.py   # Core-purity import smoke, update if exporting governance

docs/
├── Current-vs-Target.md
├── CODEX-MISSION.md
├── target/
│   ├── 04_Compliance_Validation_Guide.md
│   └── 06_Autonomous_Developer_Roadmap.md
├── index.md
└── Project-Description.md
```

### Pattern 1: Pure Deterministic Policy Module

**What:** Create a small pure module that accepts task/plan-like input and returns immutable risk evidence: category, level/score, reason, matched signal, and approval/documentation boundary.

**When to use:** Use for GOV-01 classification before any runtime or compliance report surface. It should be callable from tests, compliance, future plan generation, and future review gates without importing adapters.

**Example:**

```python
# Source pattern: whilly/core/gates.py and tests/unit/core/test_gates.py
from dataclasses import dataclass
from enum import Enum


class GovernanceRiskLevel(str, Enum):
    LOW = "LOW"
    HIGH = "HIGH"


@dataclass(frozen=True, slots=True)
class GovernanceRiskFinding:
    category: str
    level: GovernanceRiskLevel
    score: int
    reason: str
    approval_boundary: str


@dataclass(frozen=True, slots=True)
class GovernanceAssessment:
    level: GovernanceRiskLevel
    score: int
    findings: tuple[GovernanceRiskFinding, ...] = ()
```

**Implementation guidance:**

- Keep required category labels stable: `migration`, `auth`, `infrastructure`, `dependencies`, `release`, `external_pr`.
- Score deterministically from task text, `key_files`, `prd_requirement`, project-config sinks, release policy, and verification/sink metadata.
- Treat any required category hit as high-risk unless a more nuanced score table is explicitly documented and tested.
- Include operator guidance in each finding, for example `requires_operator_approval`, `requires_doc_boundary`, or `external_mutation_must_be_opt_in`.
- Do not call LLMs, read files, query git, query GitHub, or inspect the working tree from the scorer.

### Pattern 2: Compliance as Evidence Aggregator, Not Policy Owner

**What:** Add a compliance capability row for governance policy and revise semantic memory wording. Compliance should consume or scan for the pure policy module and tests, then report scoped evidence.

**When to use:** Use compliance as the first visible runtime/code path because Phase 12 success criteria require compliance output to match docs. Do not bury policy rules inside report string assembly.

**Example:**

```python
# Source pattern: whilly/compliance/__init__.py capability rows
_cap(
    "Governance risk policy",
    _governance_policy_status(files),
    _governance_policy_evidence(files),
    _governance_policy_gap(files),
    "Keep high-risk governance gates deterministic, inspectable, and human-approved.",
)
```

**Semantic-memory row recommendation:**

- Keep the row named `Semantic memory` only if the wording is clear that this is a scope decision, not an implementation claim.
- Use `CapabilityStatus.PARTIAL` for explicit future deferral, not `FAIL`, when docs and compliance agree that semantic memory is out of current scope.
- Evidence should say semantic memory is explicitly deferred and deterministic audit evidence remains authoritative.
- Gap should say no semantic-memory runtime/retrieval subsystem is implemented.
- Recommended action should say keep semantic recall out of current-capability claims until it is deterministic, evidence-backed, and wired into planning/completion.

### Pattern 3: Docs And Compliance Wording Move Together

**What:** Use one shared vocabulary across `docs/Current-vs-Target.md`, README/docs boundary wording, target compliance guide, and compliance rows.

**When to use:** Use whenever a hardening phase changes capability status. Phase 12 should update stale Phase 8-11 wording and then lock it with tests.

**Current required sync points:**

- `docs/Current-vs-Target.md`: move profile-native verification, operator-triggered rollback, configured CI polling, and bounded repair out of target-only wording where compliance already has PASS evidence.
- `README.md`, `README-RU.md`, `docs/index.md`, `docs/Project-Description.md`: keep control-plane framing; update boundary wording from "reliable git rollback" to "autonomous rollback/recovery" or equivalent scoped wording so Phase 10 operator-triggered rollback is not contradicted.
- `docs/target/04_Compliance_Validation_Guide.md`: update expected current states for Phase 8-11 and semantic-memory deferral.
- `docs/target/06_Autonomous_Developer_Roadmap.md`: keep governance and semantic memory as target/future architecture, but align Phase 12 policy wording with current deterministic policy.
- `docs/CODEX-MISSION.md`: likely no large rewrite; verify it still preserves v6 hardening boundaries and does not contradict current Phase 8-11 status.

### Anti-Patterns to Avoid

- **LLM-scored governance:** makes GOV-01 non-deterministic and uninspectable.
- **Compliance-only rules:** report output can pass while no reusable policy exists.
- **Positive semantic-memory claims from target docs:** future roadmap language must not read as current runtime capability.
- **Autonomous release language:** governance can require gates, but must not claim autonomous production release, default auto-merge, or production recovery.
- **Adding task statuses for governance:** Phase 12 does not need new database states; approval boundaries can be report/policy evidence first.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Risk scoring | LLM risk classifier or heuristic hidden in compliance strings | Pure `whilly.core.governance` value-object policy | Determinism, testability, stable labels, and future reuse. |
| Semantic memory | Vector DB, embedding search, summarization cache, or opaque recall | Explicit deferral plus deterministic audit/task/PR/verification evidence wording | Real memory has staleness, secret leakage, precedence, and runtime integration risks. |
| Approval platform | New role-based governance system | Existing human review model, PR sink approval guards, and compliance output | Phase scope is explicit policy and docs, not runtime RBAC or release automation. |
| Docs drift checks | One-off shell grep in implementation only | Extend `tests/unit/test_compliance_report.py` and existing `_doc_mismatches` rules | Keeps DOC-04 enforceable after the phase. |
| CI/repair status | New CI abstraction | Existing Phase 11 `whilly.ci`, `whilly.repair`, verification runner, and compliance row | Already shipped with scoped PASS evidence. |

**Key insight:** The hard part is not matching keywords. The hard part is preserving Whilly's control-plane boundary while making governance and memory status machine-checkable. Reuse the existing compliance/test scaffolding and avoid a new subsystem.

## Common Pitfalls

### Pitfall 1: Governance Rules Only In Compliance Text

**What goes wrong:** The report says governance exists, but no code can use the policy.

**Why it happens:** It is tempting to add a compliance row with file-content checks only.

**How to avoid:** Put policy logic in `whilly/core/governance.py`; compliance should only validate evidence and render it.

**Warning signs:** No `assess_*` function, no category-level tests, or tests assert report strings without direct policy tests.

### Pitfall 2: Semantic Memory Remains An Ambiguous Failure

**What goes wrong:** GOV-02 is nominally deferred, but compliance still reports `Semantic memory | FAIL | Capability is not implemented`.

**Why it happens:** Current `whilly/compliance/__init__.py` hardcodes semantic memory as FAIL.

**How to avoid:** Update the row and tests so explicit future deferral is not a current capability failure.

**Warning signs:** Overall status is FAIL solely because semantic memory is missing, or report wording says only "keep semantic memory out" without saying audit/task/PR/verification evidence remains authoritative.

### Pitfall 3: Docs Contradict Compliance After Phase 11

**What goes wrong:** Compliance says bounded CI polling/repair is PASS, while docs still say CI polling and bounded repair are target work.

**Why it happens:** `docs/Current-vs-Target.md` was not updated after Phase 11.

**How to avoid:** Treat `docs/Current-vs-Target.md` and the compliance matrix as a pair; add unit tests for the exact scoped phrases.

**Warning signs:** Current-vs-target lists CI polling in `Target`, or says PR feedback remains manual one-shot until bounded repair is implemented.

### Pitfall 4: Negative Boundary Text Gets Flagged As Positive Claim

**What goes wrong:** Docs correctly say "does not claim semantic long-term memory", but compliance flags it as a current claim.

**Why it happens:** Claim scanning ignores negation or long boundary lists.

**How to avoid:** Preserve and extend `_contains_positive_claim` tests in `tests/unit/test_compliance_report.py`.

**Warning signs:** New doc mismatch tests only cover positive claims, not negative boundary sentences.

### Pitfall 5: Risk Domains Miss External PR Behavior

**What goes wrong:** Migrations/auth/dependencies are scored, but externally visible PR behavior is not.

**Why it happens:** PR creation is represented through project-config sink stages and post-complete hook policy, not just task descriptions.

**How to avoid:** Include `external_pr` signals from `github_pr` sink metadata, `WHILLY_AUTO_OPEN_PR` wording, `Configured github_pr sink stage`, and PR-related task text.

**Warning signs:** The required category tests do not mention `CONFIGURED_GITHUB_PR_SINK_REQUIREMENT_PREFIX` or PR sink approval behavior.

### Pitfall 6: Core Purity Regression

**What goes wrong:** `whilly.core.governance` imports filesystem, subprocess, HTTP, DB, or adapter modules.

**Why it happens:** Governance feels connected to git, migrations, and PRs, but this phase only needs deterministic scoring from provided input.

**How to avoid:** Keep the scorer pure and pass in task/plan metadata; run import-linter and core smoke tests.

**Warning signs:** `subprocess`, `Path.read_text`, `httpx`, `asyncpg`, `fastapi`, or `whilly.adapters` in `whilly/core/governance.py`.

## Code Examples

Verified patterns from local repository sources:

### Pure Policy Unit Test Shape

```python
# Source: tests/unit/core/test_gates.py
@pytest.mark.parametrize(
    ("description", "expected_category"),
    [
        ("Add Alembic migration for plans table", "migration"),
        ("Change OAuth authorization checks", "auth"),
        ("Update Docker compose and deployment manifests", "infrastructure"),
        ("Bump FastAPI and httpx dependency versions", "dependencies"),
        ("Prepare production release tag", "release"),
        ("Open external GitHub pull request", "external_pr"),
    ],
)
def test_required_governance_domains_are_high_risk(description: str, expected_category: str) -> None:
    task = _make_task(description=description)

    assessment = assess_governance_risk(task)

    assert assessment.level is GovernanceRiskLevel.HIGH
    assert any(finding.category == expected_category for finding in assessment.findings)
```

### Compliance Row Shape

```python
# Source: whilly/compliance/__init__.py
_cap(
    "Semantic memory",
    CapabilityStatus.PARTIAL,
    "Semantic memory is explicitly deferred from current scope; deterministic events, task history, PR evidence, and verification logs remain authoritative.",
    "No deterministic semantic-memory runtime module is wired into worker task planning or completion.",
    "Keep semantic recall out of current-capability claims until it is deterministic, evidence-backed, and wired.",
)
```

### Docs Mismatch Regression Shape

```python
# Source: tests/unit/test_compliance_report.py
def test_doc_mismatch_scan_allows_explicit_semantic_memory_deferral(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (repo / "README.md").write_text(
        "Semantic long-term memory is explicitly deferred from current scope; "
        "deterministic events and verification logs remain authoritative.\n",
        encoding="utf-8",
    )
    for relative in ("Whilly-v4-Architecture.md", "Whilly-Usage.md", "CODEX-MISSION.md"):
        (docs / relative).write_text("Current capability boundaries are documented here.\n", encoding="utf-8")

    report = build_compliance_report(repo_root=repo, doc_root=docs)

    assert not any("claims semantic long-term memory" in item for item in report.doc_mismatches)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Compliance report used static capability rows and semantic memory was a generic FAIL. | Capability rows should distinguish implemented, partial, future, and explicitly deferred scope with concrete evidence. | Phase 12 target, 2026-05-08 | GOV-02 can complete without overclaiming semantic memory. |
| Docs listed CI polling and bounded repair as target work. | Phase 11 compliance evidence reports explicit configured CI polling, bounded repair attempts, and `repair.escalated` with no continuous polling/auto-merge/production recovery claim. | Phase 11 completed 2026-05-08 | DOC-04 requires current-vs-target docs to move those items out of target-only wording. |
| Rollback docs said "reliable git rollback" is not current. | Compliance now reports operator-triggered rollback as PASS with no autonomous recovery. | Phase 10 completed 2026-05-08 | Docs should distinguish operator-triggered rollback from autonomous recovery. |
| Governance lived only in target roadmap concepts. | Phase 12 should add deterministic current policy evidence for high-risk domains. | Phase 12 | Governance becomes inspectable code and compliance evidence without implying autonomous release. |

**Deprecated/outdated:**

- `docs/Current-vs-Target.md` target-only wording for CI polling and bounded repair is outdated after Phase 11.
- Generic "semantic memory missing" as a current compliance failure is outdated if Phase 12 explicitly defers semantic memory.
- Broad "reliable git rollback" non-goal wording is too coarse after Phase 10; the accurate boundary is no autonomous recovery or production rollback.

## Open Questions

1. **Should governance be exposed beyond compliance in Phase 12?**
   - What we know: Context allows governance to surface through compliance first.
   - What's unclear: Whether planners want a CLI or project-config integration now.
   - Recommendation: Do not add a new CLI in this phase. Implement pure policy plus compliance output and leave runtime enforcement for a later governance platform.

2. **Should semantic memory row status be `PARTIAL` or a renamed scoped capability?**
   - What we know: Allowed statuses are PASS/PARTIAL/FAIL/UNKNOWN; semantic memory is not implemented.
   - What's unclear: Whether the planner prefers keeping capability name `Semantic memory` or renaming to `Semantic memory scope`.
   - Recommendation: Keep `Semantic memory` but use `PARTIAL` for explicit future deferral, with evidence/gap wording that says it is not implemented. Do not use PASS for semantic-memory implementation.

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest `>=8.0` from `pyproject.toml` |
| Config file | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| Quick run command | `.venv/bin/python -m pytest -q tests/unit/core/test_governance_policy.py tests/unit/test_compliance_report.py --maxfail=1` |
| Full suite command | `make test` |

### Phase Requirements -> Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|--------------|
| DOC-04 | Current-vs-target docs and compliance report use the same Phase 8-12 wording for sandbox, profile verification, rollback, CI repair, governance, and semantic memory. | unit + report smoke | `.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py --maxfail=1` | yes |
| GOV-01 | Governance scoring identifies migrations, auth/authz, infrastructure, dependency changes, release actions, and external PR behavior with deterministic high-risk reasons and approval boundaries. | unit | `.venv/bin/python -m pytest -q tests/unit/core/test_governance_policy.py --maxfail=1` | no, Wave 0 |
| GOV-01 | New governance module stays pure and importable without I/O/transport dependencies. | integration/purity | `.venv/bin/python -m pytest -q tests/integration/test_phase1_smoke.py::test_whilly_core_is_importable_without_io_dependencies tests/integration/test_phase1_smoke.py::test_whilly_core_subprocess_and_chdir_grep_clean --maxfail=1` | yes |
| GOV-02 | Semantic memory is explicitly deferred from current scope in compliance and docs; deterministic audit/task/PR/verification evidence remains authoritative. | unit + report smoke | `.venv/bin/python -m pytest -q tests/unit/test_compliance_report.py --maxfail=1` | yes |
| DOC-04, GOV-01, GOV-02 | Compliance report renders governance and semantic-memory scope in markdown/JSON without positive overclaims. | CLI smoke | `.venv/bin/python -m whilly compliance report --format markdown --out /private/tmp/phase12-compliance.md` | yes |

### Sampling Rate

- **Per task commit:** `.venv/bin/python -m pytest -q tests/unit/core/test_governance_policy.py tests/unit/test_compliance_report.py --maxfail=1`
- **Per wave merge:** `.venv/bin/python -m pytest -q tests/unit/core/test_governance_policy.py tests/unit/test_compliance_report.py tests/unit/test_configured_sinks.py --maxfail=1`
- **Phase gate:** `.venv/bin/python -m ruff check whilly/ tests/`, `.venv/bin/python -m ruff format --check whilly/ tests/`, `.venv/bin/lint-imports --config .importlinter`, `.venv/bin/python -m pytest -q tests/unit --maxfail=3`, and `make test` when practical.

### Wave 0 Gaps

- [ ] `tests/unit/core/test_governance_policy.py` - covers GOV-01 category scoring, deterministic output, no-I/O behavior, and stable score shape.
- [ ] `whilly/core/governance.py` - pure scorer and typed result objects.
- [ ] `whilly/core/__init__.py` and `tests/integration/test_phase1_smoke.py` - update only if governance is exported/import-smoked as part of core public surface.
- [ ] `tests/unit/test_compliance_report.py` - extend existing tests for governance row, semantic-memory deferral, current-vs-target wording, and no positive current-capability claims.

## Sources

### Primary (HIGH confidence)

- `.planning/phases/12-governance-and-semantic-memory-decision/12-CONTEXT.md` - locked Phase 12 decisions, scope boundaries, docs/compliance constraints.
- `.planning/REQUIREMENTS.md` - DOC-04, GOV-01, GOV-02, future semantic-memory boundary, out-of-scope release/sandbox claims.
- `.planning/STATE.md` - Phase 8-11 decisions, current known compliance concern that semantic memory still causes overall FAIL.
- `.planning/ROADMAP.md` - Phase 12 goal, success criteria, and single plan.
- `whilly/compliance/__init__.py` - current capability matrix, semantic-memory FAIL row, bounded CI repair PASS row, doc mismatch scanner.
- `tests/unit/test_compliance_report.py` - existing compliance wording and overclaim guards.
- `whilly/core/gates.py` and `tests/unit/core/test_gates.py` - pure deterministic policy module and no-I/O/unit-test pattern.
- `whilly/pipeline/sinks.py`, `whilly/project_config/plan_builder.py`, `tests/unit/test_configured_sinks.py` - external PR sink approval policy and configured CI status sink evidence.
- `docs/Current-vs-Target.md`, `README.md`, `README-RU.md`, `docs/index.md`, `docs/Project-Description.md`, `docs/Project-Config.md`, `docs/CODEX-MISSION.md` - user-facing docs and boundary wording.
- `docs/target/04_Compliance_Validation_Guide.md`, `docs/target/05_Technical_Implementation_Brief.md`, `docs/target/06_Autonomous_Developer_Roadmap.md` - target capability matrix, explicit out-of-scope semantic memory, target governance concepts.

### Secondary (MEDIUM confidence)

- `/private/tmp/phase12-current-compliance.md` generated during research with `.venv/bin/python -m whilly compliance report --format markdown --out /private/tmp/phase12-current-compliance.md`; current evidence: overall FAIL, `Bounded CI polling and repair` PASS, `Git rollback` PASS, `Sandbox/VM isolation` PARTIAL, `Semantic memory` FAIL.
- `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md` - prior roadmap note pointing Phase 12 toward governance categories and semantic-memory target decision. Useful historical context, but Phase 12 context and current code override it.

### Tertiary (LOW confidence)

- None. No external web/library research was needed because this phase is internal repository policy and documentation alignment with no new dependencies.

## Metadata

**Confidence breakdown:**

- Standard stack: HIGH - verified from local `pyproject.toml`, `Makefile`, and existing modules; no new packages required.
- Architecture: HIGH - supported by existing pure policy modules, import-linter contracts, compliance implementation, and tests.
- Pitfalls: HIGH - grounded in current stale docs, generated compliance output, and existing overclaim tests.
- Semantic-memory recommendation: HIGH - no current runtime module is wired into task planning or completion; explicit deferral is directly allowed by context.

**Research date:** 2026-05-08
**Valid until:** 2026-06-07, or earlier if Phase 12 implementation changes compliance/doc architecture.
