# Whilly Forward Process — Delta-Only Spec Updates

This document defines the **forward delta-only process** for changing Whilly after
the v1.3 reverse-spec baseline. The baseline froze 32 capability specs under
`openspec/specs/` (one per subsystem behavior cluster; see `TAXONOMY.md`). From
here on, the specs are the living description of WHAT Whilly does, and they are
kept honest by routing every behavior change through an `opsx` change proposal.

This is a plain Markdown process doc — it is **not** a capability spec. It does
not live under `openspec/specs/` and is not subject to an `openspec validate`
gate. The capability specs it governs are.

---

## The two systems: OpenSpec = WHAT, GSD = HOW

- **OpenSpec** is the *living WHAT*. `openspec/specs/<slug>/spec.md` describes the
  guaranteed behavior of each subsystem in RFC 2119 normative language
  (`SHALL` / `MUST`). When code behavior and the spec disagree, one of them is a
  bug; the spec is the contract.
- **GSD** (the `.planning/` get-shit-done workflow) is the *HOW*: phases, plans,
  tasks, execution and verification. GSD drives the work; OpenSpec records the
  resulting guarantees.

These do not compete. A GSD plan that changes behavior MUST carry an OpenSpec
delta with it.

---

## Core rule

> **Any change to `whilly/` runtime behavior REQUIRES an `opsx` change proposal
> that updates the relevant `openspec/specs/<slug>/spec.md`, authored as a delta
> spec, and that proposal MUST be applied and archived as part of landing the
> change.**

A change "to behavior" means any change to a guarantee a capability spec makes:
new behavior, changed behavior, or removed behavior. The 32 capability specs are
otherwise **frozen** — they only move via the lifecycle below.

What does **not** require a delta:

- Pure documentation changes (READMEs, this doc, `.planning/` artifacts).
- Tests that pin existing, already-specified behavior.
- Refactors with no observable behavior change.

When in doubt, ask: *does this change a `SHALL` / `MUST` line in any
`openspec/specs/<slug>/spec.md`?* If yes, you need a delta.

---

## Lifecycle: propose → apply → archive

The `opsx` workflow (the `/opsx:*` skills over OpenSpec 1.4.1) has three real
stages. Use only these — do not invent commands or flags.

1. **Propose.** Author a change proposal under `openspec/changes/<name>/`. The
   behavior delta is a **delta spec** at
   `openspec/changes/<name>/specs/<capability>/spec.md` describing the additions,
   modifications, or removals relative to the frozen baseline spec. Manage
   proposals with `openspec change`, and gate the delta with
   `openspec validate <slug> --strict` (the same validator that gates the
   baseline; see `AUTHORING.md` for HOW to write the delta body).
2. **Apply.** Implement the code change so it matches the proposed delta. The
   proposal and the `whilly/` change land together — the delta is the
   acceptance criterion for the code.
3. **Archive.** Fold the applied change back into the baseline with
   `openspec archive`, which updates the relevant `openspec/specs/<slug>/spec.md`
   so the baseline once again reflects current behavior. After archiving, the
   capability spec is the new frozen truth.

A behavior change is not complete until its proposal has been applied and
archived into `openspec/specs/`.

---

## Where things live

| Path | Role |
|------|------|
| `openspec/specs/<slug>/spec.md` | Baseline WHAT — the 32 frozen capability specs. |
| `openspec/changes/<name>/specs/<capability>/spec.md` | In-flight delta specs (proposals not yet archived). |
| `openspec/TAXONOMY.md` | The 32 capability slugs and what each covers. |
| `openspec/COVERAGE-MATRIX.md` | Map from every `whilly/` module to its capability slug (zero silent gaps). |
| `openspec/AUTHORING.md` | HOW to write a spec / delta body (required sections, normative language, validator rules). |
| `openspec/project.md` | Tech stack, load-bearing contracts, domain glossary (`opsx`, `delta spec`). |

---

## See also

- `openspec/AUTHORING.md` — authoring rules for the delta body.
- `openspec/project.md` — `opsx` and `delta spec` glossary entries.
- `CLAUDE.md` / `AGENTS.md` — the require-a-delta rule contributors and agents
  read first.
