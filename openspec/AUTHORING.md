# Whilly Spec Authoring Conventions

This document is the canonical authoring guide for capability specs in
`openspec/specs/`. Every spec authored against OpenSpec 1.4.1 MUST follow
the rules below exactly. The validator `openspec validate <slug> --strict`
is the arbiter — authors MUST run it before merging any spec.

> **Before writing a delta:** read `openspec/FORWARD-PROCESS.md`. After the v1.3
> baseline, the 32 capability specs are frozen; any behavior change flows through
> an `opsx` proposal (propose → apply → archive) whose delta spec is authored per
> the rules in this document. This guide is the HOW for the delta body;
> `FORWARD-PROCESS.md` is the WHEN/WHY.

---

## Spec File Location

```
openspec/specs/<capability-slug>/spec.md
```

The slug is the directory name and the spec ID returned by `openspec spec list`.
Slugs MUST use kebab-case (e.g., `task-model-fsm`, `cli-surface`).
One spec file per capability. The file MUST be named `spec.md`.

---

## Required Sections

Every capability spec MUST contain exactly the two `##`-level sections below,
in this order. Additional `##` sections are permitted but not required.

### `## Purpose`

- Content: plain prose (no headings or code blocks) describing what the
  capability covers and why it exists.
- Minimum length: **50 characters** (strict mode enforces this — fewer than 50
  characters produces a WARNING that becomes a failure under `--strict`).
- Two sentences is a safe minimum. Example purpose opening:

  ```
  The <capability-slug> capability governs <what it does and for whom>.
  This capability covers <scope boundary>.
  ```

### `## Requirements`

- MUST contain at least one `### Requirement:` child block.
- Zero requirements produce an ERROR.

---

## Requirement Block Format

Each requirement MUST follow this structure (exactly):

```
### Requirement: <human-readable name>
The system SHALL <normative statement on the FIRST body line>.

#### Scenario: <scenario name>
- **WHEN** <condition or trigger>
- **THEN** <expected system behavior>
- **AND** <additional outcome>  (optional; repeat as needed)
```

### Normative Body Line (SHALL / MUST)

The validator checks the **first non-empty body line** of each requirement
block (the content between `### Requirement:` and the first `#### Scenario:`)
for the literal word `SHALL` or `MUST` (case-sensitive).

Rules:
- The normative keyword MUST appear in the first body line — not only in the
  header text.
- `SHALL` and `MUST` are interchangeable in strength (RFC 2119: both signal an
  unconditional guarantee).
- Do NOT use `should`, `may`, `might`, or `can` in normative statements — these
  words are not testable contracts and will not satisfy the validator.

### Scenario Format Rules

- Scenario headers MUST use **exactly 4 hashtags** (`#### Scenario:`).
- Using 3 hashtags (`### Scenario:`) causes the scenario to be silently
  ignored — the parser does not produce a warning, but the requirement ends up
  with 0 scenarios, which is an ERROR.
- Writing `**WHEN** ...` / `**THEN** ...` bullets WITHOUT a preceding
  `#### Scenario:` header also produces 0 parsed scenarios (silent fail → ERROR).
- Every requirement MUST have at least one `#### Scenario:`.
- `- **WHEN**` and `- **THEN**` are required within each scenario.
- `- **AND**` is optional and may repeat.

---

## Strict Validation Checklist

Run this command before committing or opening an `opsx` proposal:

```bash
openspec validate <slug> --strict --json
```

A spec MUST have 0 errors AND 0 warnings to pass `--strict`. Any warning is
treated as a failure. Check each item in this table:

| Check | Failure Level | Condition |
|-------|---------------|-----------|
| `## Purpose` section present | ERROR | Section entirely missing |
| `## Requirements` section present | ERROR | Section entirely missing |
| At least one `### Requirement:` | ERROR | Section has 0 requirements |
| Body line contains `SHALL` or `MUST` | ERROR | First body line has neither keyword |
| At least one `#### Scenario:` per requirement | ERROR | Requirement has 0 scenarios |
| Delta header in a main spec | ERROR | `## ADDED Requirements` (or MODIFIED / REMOVED / RENAMED) appears in `openspec/specs/*/spec.md` |
| `## Purpose` content >= 50 characters | WARNING (strict failure) | Content is shorter than 50 characters |

Requirement body longer than 500 characters produces an INFO-level note (not a
WARNING) and does NOT fail strict validation. Split long bodies anyway for
readability.

---

## Anti-Patterns

The following patterns are FORBIDDEN in `openspec/specs/*/spec.md` files.

### 1. Delta Headers in Main Specs

NEVER write:

```
## ADDED Requirements
## MODIFIED Requirements
## REMOVED Requirements
## RENAMED Requirements
```

in a file under `openspec/specs/*/spec.md`. The parser reports ERROR:
`"Main spec contains delta header..."` and truncates the Requirements section.

Delta headers are legal ONLY in delta spec files under
`openspec/changes/<change-name>/specs/<capability>/spec.md`. They MUST NOT
appear in the baseline capability specs.

### 2. SHALL / MUST Only in the Header

NEVER write:

```
### Requirement: The system SHALL do X
(body line is empty or descriptive only)
```

The validator checks the body, not the header. Always write the normative
statement on the first body line:

```
### Requirement: <descriptive name>
The system SHALL do X when Y.
```

### 3. Bullet-List Scenarios

NEVER write:

```
### Requirement: <name>
The system SHALL do X.

- **WHEN** the user triggers Y
- **THEN** the system responds with Z
```

This produces 0 parsed scenarios (silent fail → ERROR at validation time).
Always use a `#### Scenario:` header first.

### 4. Three-Hashtag Scenario Header

NEVER write:

```
### Scenario: <name>
```

This is not parsed as a scenario. Use exactly 4 hashtags:

```
#### Scenario: <name>
```

### 5. Descriptive Language Instead of Normative

NEVER write:

```
The dashboard shows task status.
This module reads the plan file.
```

ALWAYS write normative contracts:

```
The system SHALL display task status in the TUI dashboard.
The `<component>` MUST read the plan file from the path supplied at startup.
```

Every requirement body line must start with a normative assertion — not a
descriptive observation.

### 6. Zero Scenarios on a Requirement

NEVER leave a `### Requirement:` block with no `#### Scenario:` below it.
Every requirement MUST have at least one scenario. The ERROR message is:
`"Requirement must have at least one scenario"`.

---

## Spec Location for Delta (Change) Specs

When proposing a change to a capability via `opsx`, the delta spec lives at:

```
openspec/changes/<change-name>/specs/<capability-slug>/spec.md
```

Delta specs use `## ADDED Requirements`, `## MODIFIED Requirements`, etc.
They MUST NOT be confused with main capability specs. A delta spec CANNOT
replace a main spec and vice versa.

---

## Validation Commands Reference

```bash
# Validate a single capability spec (strict)
openspec validate <slug> --strict

# Validate all capability specs (strict, JSON output for CI)
openspec validate --specs --strict --json

# Validate in normal mode (errors only, no warnings)
openspec validate <slug>

# Validate all specs, check that the tool runs cleanly
openspec validate --specs --strict --json
```

The `openspec` binary is at:
`~/.reflex/.nvm/versions/node/v20.19.6/bin/openspec`

If `openspec` is not on `PATH`, source nvm or use the full path.

---

## Normative Language Reference (RFC 2119)

| Keyword | Meaning | Use in specs |
|---------|---------|--------------|
| `SHALL` | Unconditional requirement | REQUIRED for normative body lines |
| `MUST` | Same strength as SHALL | REQUIRED for normative body lines |
| `SHALL NOT` | Prohibition | Use for explicit prohibitions |
| `MUST NOT` | Same as SHALL NOT | Use for explicit prohibitions |
| `should` | Recommended | AVOID — not machine-checkable |
| `may` | Optional | AVOID — not machine-checkable |
| `might` | Possibility | AVOID — ambiguous |

Every capability spec requirement body MUST use `SHALL` or `MUST`. Any
requirement that uses only `should` or `may` will fail validation with:
`"Requirement must contain SHALL or MUST keyword"`.

---

## Authoring Checklist (Quick Reference)

Before submitting a spec, verify:

- [ ] File is at `openspec/specs/<slug>/spec.md` (kebab-case slug).
- [ ] `## Purpose` section present with >= 50 characters of content.
- [ ] `## Requirements` section present with >= 1 `### Requirement:` block.
- [ ] Every `### Requirement:` has `SHALL` or `MUST` on the first body line.
- [ ] Every `### Requirement:` has at least one `#### Scenario:` (4 hashtags).
- [ ] Every `#### Scenario:` has `- **WHEN**` and `- **THEN**` bullets.
- [ ] No `## ADDED Requirements` or other delta headers in the file.
- [ ] `openspec validate <slug> --strict` reports 0 errors and 0 warnings.
