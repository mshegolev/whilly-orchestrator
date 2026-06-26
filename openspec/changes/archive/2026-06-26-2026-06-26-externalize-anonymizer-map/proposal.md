# Proposal: Externalize anonymizer redaction map — remove hardcoded company name

## Why

`whilly/adapters/runner/anonymizer.py` hard-coded a specific company name
(a hardcoded company name) as the default `company_mappings` value
directly in source.  This violates the project rule that no company or user
data appears in tracked files; the real values must live in local, gitignored
configuration only.

## What Changes

- **ADDED** `agent-dispatch` → "Anonymizer map loaded from environment":
  the redaction map SHALL be loaded from the `WHILLY_ANONYMIZER_MAP`
  environment variable (a JSON object string) at construction time, defaulting
  to an empty dict when the variable is unset, empty, or contains invalid JSON.
  No company name is hardcoded in source.

## Non-goals

- Changing the anonymization algorithm or proxy architecture.
- Documenting any specific company name in specs or tests.

## Impact

- Specs: `agent-dispatch` (one requirement added).
- Code: `whilly/adapters/runner/anonymizer.py` — `_load_company_mappings()`
  helper added; `company_mappings` default_factory changed.
- Tests: `tests/test_claude_anonymizer.py` — real-company variants replaced
  with fictional `globex`/`GLOBEX`/`Globex`; two new env-loading tests added.
- Docs: `docs/anonymizer-usage.md`, `docs/PRD-post-auth-hardening.md`.
