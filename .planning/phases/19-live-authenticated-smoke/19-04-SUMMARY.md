---
phase: 19-live-authenticated-smoke
plan: "04"
subsystem: docs
tags: [docs, smoke, jira, gitlab, regression-test, security]

requires:
  - phase: 19-live-authenticated-smoke
    plan: "02"
    provides: "whilly jira smoke --issue KEY with exit codes 0/1/2"
  - phase: 19-live-authenticated-smoke
    plan: "03"
    provides: "whilly gitlab smoke --repo-url URL with exit codes 0/1/2"

provides:
  - "## Live smoke section in docs/Whilly-Usage.md — setup, commands, exit codes, report path"
  - "tests/unit/test_docs_live_smoke.py — 5 regression tests pinning the section and its required strings"

affects:
  - "LIVE-01 — documented operator setup path for whilly jira smoke"
  - "LIVE-02 — documented operator setup path for whilly gitlab smoke"
  - "LIVE-03 — documented report location whilly_logs/smoke/{jira|gitlab}-smoke-{timestamp}.json"

tech-stack:
  added: []
  patterns:
    - "_read helper + presence assertion style from test_ui_parity_docs.py mirrored in test_docs_live_smoke.py"
    - "Bash block placeholders use $VAR_NAME convention (not <angle-brackets>) to satisfy test_docs_bash_blocks_parse.py"

key-files:
  created:
    - tests/unit/test_docs_live_smoke.py
  modified:
    - docs/Whilly-Usage.md

key-decisions:
  - "Bash code blocks use $JIRA_API_TOKEN_VALUE / $GITLAB_TOKEN_VALUE placeholders (env-var reference style) to satisfy test_docs_bash_blocks_parse.py bash -n validation — angle-bracket placeholders fail bash syntax check"
  - "Live smoke section placed before Troubleshooting (natural operator flow: setup → run → debug)"
  - "exit_codes_documented test scopes its assertion to the Live smoke section only (substring from ## Live smoke to next ## heading)"

metrics:
  duration: "~24 min"
  completed: "2026-06-12"
  tasks: 2
  files_modified: 2

requirements-completed: [LIVE-01, LIVE-02, LIVE-03]
---

# Phase 19 Plan 04: Live Smoke Documentation Summary

**`## Live smoke` section added to `docs/Whilly-Usage.md` with Jira/GitLab setup, env vars, exit-code table, and report location; a 5-test regression file locks the section anchors and guards the prohibited-hotkey constraint**

## Performance

- **Duration:** ~24 min
- **Completed:** 2026-06-12
- **Tasks:** 2
- **Files modified:** 2 (1 created, 1 modified)

## Accomplishments

- Added `## Live smoke` section to `docs/Whilly-Usage.md` (91 net lines):
  - Purpose paragraph: read-only validation, safe against production
  - Jira smoke: env-var table (`JIRA_SERVER_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN`), command `whilly jira smoke --issue PROJECT-123`, optional-flags table (`--timeout`, `--persist`, `--json`)
  - GitLab smoke: env-var table (`GITLAB_URL`, `GITLAB_TOKEN`), token resolution order, command `whilly gitlab smoke --repo-url URL`
  - Exit-codes table: `0` all pass, `1` check failed, `2` config missing
  - Report location: `whilly_logs/smoke/jira-smoke-{timestamp}.json` / `gitlab-smoke-{timestamp}.json`
  - Note on `--persist` gate and `WHILLY_DATABASE_URL` requirement
- Created `tests/unit/test_docs_live_smoke.py` with 5 tests (all pass):
  1. `test_live_smoke_section_present` — `## Live smoke` heading exists
  2. `test_live_smoke_commands_documented` — both `whilly jira smoke` and `whilly gitlab smoke` named
  3. `test_live_smoke_report_path_documented` — `whilly_logs/smoke/` named
  4. `test_live_smoke_exit_codes_documented` — exit codes 0/1/2 in section
  5. `test_live_smoke_no_prohibited_hotkey_string` — `q/d/l/t/h` absent (Pitfall 7 guard)

## Task Commits

1. **Task 1: docs/Whilly-Usage.md Live smoke section** — `bdd24fc` (docs)
2. **Rule 1 fix: replace angle-bracket placeholders** — `5b7a5eb` (fix)
3. **Task 2: tests/unit/test_docs_live_smoke.py** — `7faff6d` (test)

## Files Created/Modified

- `docs/Whilly-Usage.md` — `## Live smoke` section added (line 627); bash blocks use `$VAR` style
- `tests/unit/test_docs_live_smoke.py` — 5-test regression file (51 lines, new file)

## Decisions Made

- Bash code blocks use `$JIRA_API_TOKEN_VALUE` / `$GITLAB_TOKEN_VALUE` placeholders because `test_docs_bash_blocks_parse.py` runs `bash -n` on all fenced bash blocks — angle-bracket `<token>` is parsed as redirection and fails syntax check
- Section placed before "Troubleshooting" (natural operator flow: setup the smoke command before you need to debug it)
- `test_live_smoke_exit_codes_documented` scopes its assertions to the Live smoke section text only (from `## Live smoke` to next `##` heading) to avoid false positives from exit-code references elsewhere in the doc

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Angle-bracket placeholders fail bash -n validation**
- **Found during:** Task 1 (running `tests/unit` suite after Task 1 commit)
- **Issue:** The bash fenced blocks used `export JIRA_API_TOKEN=<token>` and `export GITLAB_TOKEN=<token>`. The existing test `test_docs_bash_blocks_parse.py` runs `bash -n` on every fenced bash block in docs — the `<` is interpreted as stdin redirection, causing syntax error exit 2 on both blocks
- **Fix:** Replaced `<token>` with `$JIRA_API_TOKEN_VALUE` and `$GITLAB_TOKEN_VALUE` following the env-var-reference convention documented in `test_docs_bash_blocks_parse.py`'s docstring
- **Files modified:** `docs/Whilly-Usage.md`
- **Commit:** `5b7a5eb`

## Threat Surface Scan

T-19-10 mitigation verified: both bash example blocks use env-var references (`$JIRA_API_TOKEN_VALUE`, `$GITLAB_TOKEN_VALUE`); no literal token values appear anywhere in the new section. The report-redaction note (`Tokens/secrets are never written`) is explicitly stated in the "Report location" paragraph.

## Self-Check: PASSED

- `docs/Whilly-Usage.md` contains `## Live smoke`: FOUND (line 627)
- `tests/unit/test_docs_live_smoke.py` exists: FOUND
- Commit `bdd24fc` exists: FOUND
- Commit `5b7a5eb` exists: FOUND
- Commit `7faff6d` exists: FOUND
- `pytest tests/unit/test_docs_live_smoke.py tests/unit/test_ui_parity_docs.py -q`: 7 passed
- `pytest tests/unit -q`: 2425 passed, 2 skipped
- `ruff check tests/unit/test_docs_live_smoke.py`: clean

---
*Phase: 19-live-authenticated-smoke*
*Completed: 2026-06-12*
