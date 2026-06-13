---
phase: 21
slug: spec-baseline-taxonomy
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-13
---

# Phase 21 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

This is a documentation/scaffolding phase — there is no application code to unit-test. Validation is
performed by the OpenSpec tool itself (`openspec validate --strict`) plus file/grep smoke checks.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | `openspec validate --strict` (CLI tool, not pytest) |
| **Config file** | `openspec/config.yaml` (schema: spec-driven) |
| **Quick run command** | `openspec validate task-model-fsm --strict` |
| **Full suite command** | `openspec validate --specs --strict --json` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run the relevant quick check (`openspec validate <slug> --strict` or the file/grep smoke for that artifact).
- **After every plan wave:** Run `openspec validate --specs --strict --json`.
- **Before `/gsd-verify-work`:** Full suite must be green (0 errors, 0 warnings under strict).
- **Max feedback latency:** ~5 seconds.

---

## Per-Task Verification Map

| Req ID | Behavior | Test Type | Automated Command | File Exists |
|--------|----------|-----------|-------------------|-------------|
| BASE-04 | `openspec/project.md` exists with stack/conventions/glossary | manual | `ls openspec/project.md && wc -l openspec/project.md` | ❌ W0 |
| BASE-03 | Authoring-conventions doc exists (MUST/SHALL + `#### Scenario:` rules) | manual | `ls openspec/AUTHORING.md` | ❌ W0 |
| BASE-01 | Taxonomy index lists ~32 capabilities with slug + purpose | manual | `ls openspec/TAXONOMY.md && grep -c '^| ' openspec/TAXONOMY.md` | ❌ W0 |
| BASE-01 | Capability stub directories exist under `openspec/specs/` | smoke | `ls openspec/specs/ \| wc -l` | ❌ W0 |
| BASE-02 | Coverage matrix lists every `whilly/` module | smoke | `grep -c 'whilly/' openspec/COVERAGE-MATRIX.md` | ❌ W0 |
| BASE-02 | Coverage matrix has zero silent gaps | smoke | `grep -c 'UNMAPPED' openspec/COVERAGE-MATRIX.md` (must equal explicitly-tracked count) | ❌ W0 |
| SC-5 | Reference exemplar passes strict validation | automated | `openspec validate task-model-fsm --strict --json` | ❌ W0 |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `openspec/specs/task-model-fsm/spec.md` — reference exemplar (covers SC-5; researcher verified this slug passes `--strict`)
- [ ] `openspec/TAXONOMY.md` — capability index (covers BASE-01)
- [ ] `openspec/COVERAGE-MATRIX.md` — module→capability mapping scaffold (covers BASE-02)
- [ ] `openspec/AUTHORING.md` — conventions document (covers BASE-03)
- [ ] `openspec/project.md` — project context (covers BASE-04)
- [ ] Stub directories for all ~32 capability slugs under `openspec/specs/`

*OpenSpec CLI is already installed (1.4.1); no framework install needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Taxonomy is the right ~32 capabilities and matches phases 22-27 clusters | BASE-01 | Judgment call against ROADMAP clusters | Read `openspec/TAXONOMY.md` against ROADMAP.md phases 22-27 |
| project.md glossary is accurate | BASE-04 | Domain accuracy, not mechanically checkable | Read `openspec/project.md` against CLAUDE.md |
| Exemplar spec is normative, not descriptive | SC-5 | "Normative vs descriptive" is a human judgment | Read `openspec/specs/task-model-fsm/spec.md` — every requirement uses MUST/SHALL and is testable |

---

## Validation Sign-Off

- [ ] All tasks have an automated `openspec validate` or file/grep smoke verify, or a Wave 0 dependency
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 5s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
