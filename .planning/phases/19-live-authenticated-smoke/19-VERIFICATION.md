---
phase: 19-live-authenticated-smoke
verified: 2026-06-12T01:30:00Z
status: human_needed
score: 9/9 must-haves verified
overrides_applied: 0
re_verification: false
human_verification:
  - test: "Run `whilly jira smoke --issue REAL-KEY` against a real Jira project with valid JIRA_SERVER_URL/JIRA_USERNAME/JIRA_API_TOKEN set"
    expected: "Exits 0 with PASS summary showing 6 checks; a JSON report appears under whilly_logs/smoke/jira-smoke-{timestamp}.json containing no token values or DSN strings; all six checks (auth, issue_fetch, comments, changelog, remote_links, classify) are listed"
    why_human: "Requires real credentials and a real Jira project; cannot be exercised without live network access"
  - test: "Run `whilly gitlab smoke --repo-url https://YOUR_GITLAB/group/repo` against a real GitLab repository with valid GITLAB_URL and GITLAB_TOKEN set"
    expected: "Exits 0 with PASS summary showing 3 checks (auth, project_access, repo_hint); a JSON report appears under whilly_logs/smoke/gitlab-smoke-{timestamp}.json containing no token values; report shows measured durations"
    why_human: "Requires real credentials and a real GitLab repository; cannot be exercised without live network access"
  - test: "Run `whilly jira smoke --issue REAL-KEY` with an intentionally wrong JIRA_API_TOKEN"
    expected: "Exits 1; output includes an actionable hint naming JIRA_SERVER_URL and JIRA_API_TOKEN; the word 'Traceback' does not appear anywhere in stdout or stderr"
    why_human: "Requires real Jira server to trigger an authenticated HTTP failure"
  - test: "Run `whilly gitlab smoke --repo-url URL` with an intentionally wrong GITLAB_TOKEN"
    expected: "Exits 1; output names GITLAB_TOKEN in the hint; no 'Traceback' in output; report file is written and contains no token value"
    why_human: "Requires real GitLab server to trigger an authenticated HTTP failure"
---

# Phase 19: live-authenticated-smoke Verification Report

**Phase Goal:** Jira and GitLab integrations are validated on a real operator machine with real credentials, and every smoke run leaves persisted audit evidence for review.
**Verified:** 2026-06-12T01:30:00Z
**Status:** human_needed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Smoke run writes a persisted JSON report file an operator can read after the run | VERIFIED | `write_smoke_report()` in `whilly/cli/smoke.py:159-180` creates `mkdir(parents=True, exist_ok=True)` then `write_text`; 13 unit tests covering write behavior; smoke.py:175 confirmed |
| 2 | Report file never contains tokens, passwords, or DSN strings | VERIFIED | `_redact_url()` strips `user:pass@` at both `_gitlab_get` raise sites and at `_resolve_gitlab_config_state` config source; unit tests `test_smoke_report_payload_contains_no_tokens_or_dsn`, `test_jira_smoke_report_contains_no_token_or_dsn`, `test_gitlab_smoke_report_contains_no_secrets` and their failure-path variants all pass |
| 3 | Report directory honors WHILLY_LOG_DIR | VERIFIED | `_smoke_report_dir()` calls `_log_dir() / "smoke"` from `whilly.llm_ops`; `_log_dir()` reads `WHILLY_LOG_DIR` from `os.environ`; behavioral spot-check confirmed: setting `os.environ['WHILLY_LOG_DIR']` redirects the directory |
| 4 | Per-check results accumulate so one failed check does not stop later checks | VERIFIED | `SmokeReport.add_check` appends unconditionally; jira wraps each field check independently; gitlab wraps each of its 3 checks in `try/except`; accumulation test confirmed 3 checks recorded when check 2 failed |
| 5 | `whilly jira smoke --issue KEY` exercises auth/issue/comments/changelog/links/classify | VERIFIED | `_run_jira_smoke()` in `whilly/cli/jira.py:715-901` implements 6 named checks (auth, issue_fetch, comments, changelog, remote_links, classify); help output shows `--issue` flag; 12 unit tests pass |
| 6 | Missing config returns exit 2 before any HTTP call (no traceback) | VERIFIED | `_ensure_jira_config` gate runs before `snapshot_collector`; `environ={}` test asserts rc==2 and collector never called; both jira and gitlab spot-checks confirmed (exit 2, no traceback, no collector invocation) |
| 7 | `whilly gitlab smoke --repo-url URL` does token-auth ping plus link-refresh/repo-hint | VERIFIED | `_run_gitlab_smoke()` in `whilly/cli/gitlab.py:248-417` implements 3 checks (auth via `/api/v4/user`, project_access via `/api/v4/projects/{path}`, repo_hint via `_matches_repo`); help output shows `--repo-url`; 21 unit tests pass |
| 8 | `whilly gitlab` is registered in CLI dispatch and listed in --help | VERIFIED | `whilly/cli/__init__.py:451-454` lazy-dispatch branch; `_HELP_TEXT` line 131: `gitlab Smoke-test live GitLab integration (auth, repo-hint).`; `whilly --help` output confirmed contains "gitlab" |
| 9 | An operator can follow documented setup to run jira and gitlab smoke and find the report file | VERIFIED | `docs/Whilly-Usage.md` line 627: `## Live smoke` section with required env vars, commands, exit-code table, and `whilly_logs/smoke/` report path documented; 7 docs regression tests pass (test_docs_live_smoke.py + test_ui_parity_docs.py) |

**Score:** 9/9 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `whilly/cli/smoke.py` | SmokeReport accumulator, write_smoke_report(), _redact_url(), shared exit codes | VERIFIED | 193 lines; exports `SmokeReport`, `write_smoke_report`, `_redact_url`, `EXIT_OK=0`, `EXIT_CHECK_FAILED=1`, `EXIT_CONFIG_MISSING=2`; imports `_log_dir` from `whilly.llm_ops` |
| `tests/unit/cli/test_smoke.py` | Unit tests for report write, redaction, accumulation | VERIFIED | 216 lines, 13 tests collected and passing |
| `tests/unit/cli/__init__.py` | Package init for test package | VERIFIED | File exists (empty, as intended) |
| `whilly/cli/jira.py` | smoke subparser + `_run_jira_smoke` dispatch | VERIFIED | `_run_jira_smoke` at line 715; smoke subparser at line 296; dispatch branch at line 421 |
| `tests/unit/cli/test_jira_smoke.py` | Unit tests for pass/fail/missing-config/classify-readonly/no-secret-leak | VERIFIED | 565 lines, 12 tests collected and passing |
| `whilly/cli/gitlab.py` | `build_gitlab_parser`, `run_gitlab_command`, `_run_gitlab_smoke`, `_gitlab_get`, `_resolve_gitlab_config_state` | VERIFIED | 463 lines; all 5 required functions present; injectable `gitlab_getter` and `environ` params confirmed |
| `whilly/cli/__init__.py` | gitlab lazy-dispatch branch + `_HELP_TEXT` entry | VERIFIED | Dispatch at lines 451-454; `_HELP_TEXT` entry at line 131 |
| `tests/unit/cli/test_gitlab_smoke.py` | Unit tests for auth-pass/missing-config/repo-hint/no-secrets | VERIFIED | 522 lines, 21 tests collected and passing |
| `docs/Whilly-Usage.md` | Live smoke section with setup, commands, exit codes, report path | VERIFIED | `## Live smoke` heading at line 627; both commands, exit codes 0/1/2, and `whilly_logs/smoke/` all documented |
| `tests/unit/test_docs_live_smoke.py` | Docs regression test pinning the Live smoke section anchors | VERIFIED | 51 lines, 5 tests pass; guards `## Live smoke` heading, both commands, report path, and absence of prohibited `q/d/l/t/h` string |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `whilly/cli/smoke.py` | `whilly_logs/smoke/` | `_smoke_report_dir()` calls `_log_dir() / "smoke"` with `mkdir(parents=True, exist_ok=True)` | WIRED | Confirmed at smoke.py:35-37, 175 |
| `whilly/cli/smoke.py` | redacted report payload | `_redact_url` strips `user:pass@` authority before write | WIRED | Confirmed at smoke.py:45-77; both gitlab and jira call `_redact_url` before composing payload |
| `whilly/cli/jira.py:_run_jira_smoke` | `_ensure_jira_config` | config gate runs before `snapshot_collector` | WIRED | `_ensure_jira_config` at jira.py:750-762, called before `snapshot_collector` at line 774 |
| `whilly/cli/jira.py:_run_jira_smoke` | `whilly/cli/smoke.py` | SmokeReport accumulation + `write_smoke_report` | WIRED | `SmokeReport` imported and used; `write_smoke_report(_smoke_report_dir(), "jira", payload)` at jira.py:884 |
| `whilly/cli/jira.py:_run_jira_smoke` | `classify_jira_work` | pure read-only classify against fetched snapshot | WIRED | `snapshot.classification` field access at jira.py:841-847; no extra Jira call |
| `whilly/cli/__init__.py` | `whilly/cli/gitlab.py` | `if cmd == 'gitlab': from whilly.cli.gitlab import run_gitlab_command` | WIRED | __init__.py:451-454 confirmed |
| `whilly/cli/gitlab.py:_run_gitlab_smoke` | `whilly/cli/smoke.py` | SmokeReport accumulation + `write_smoke_report` + `_redact_url` | WIRED | All 3 imports at gitlab.py:33-41; `write_smoke_report(_smoke_report_dir(), "gitlab", payload)` at gitlab.py:392 |
| `whilly/cli/gitlab.py:_gitlab_get` | GitLab `/api/v4` endpoint | `urllib` GET with `Bearer` token; `HTTPError`/`URLError` → `RuntimeError` | WIRED | Confirmed at gitlab.py:143-173; error messages use `safe_url = _redact_url(url)` |
| `docs/Whilly-Usage.md Live smoke section` | `whilly jira smoke` / `whilly gitlab smoke` | documented commands + env vars + report path | WIRED | Both commands appear in docs; env var tables present; exit-code table at line 705-711 |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|-------------------|--------|
| `whilly/cli/smoke.py:write_smoke_report` | `payload` dict | Caller composes payload from `SmokeReport.to_payload()` | Yes — checks list from network calls, timestamps from `datetime.now(timezone.utc)` | FLOWING |
| `whilly/cli/jira.py:_run_jira_smoke` | `snapshot` | `snapshot_collector(args.issue, timeout=args.timeout)` | Yes — real collector calls Jira REST API; unit tests inject a mock returning a real `JiraWorkSnapshot` | FLOWING |
| `whilly/cli/gitlab.py:_run_gitlab_smoke` | `user_data`, `project_data` | `gitlab_getter(f"{api_base}/user", ...)` and `gitlab_getter(f"{api_base}/projects/{repo_path_encoded}", ...)` | Yes — real getter calls GitLab REST API; unit tests inject a mock returning real-shaped dicts | FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| SmokeReport accumulation never stops on first failure | `python -c "r=SmokeReport('test'); r.add_check('a',True); r.add_check('b',False); r.add_check('c',True); assert len(r.checks)==3"` | 3 checks recorded | PASS |
| `_redact_url` strips credentials | `assert '@' not in _redact_url('https://u:p@h/x')` | No `@` in output | PASS |
| `write_smoke_report` creates file and no secrets | `write_smoke_report(Path(tmpdir), 'jira', payload)` | File created, no 'password'/'token' in content | PASS |
| `whilly gitlab smoke` exit 2 on missing config, getter not called | `run_gitlab_command([...], gitlab_getter=bad_getter, environ={})` | rc=2, getter never called | PASS |
| `whilly jira smoke` exit 2 on missing config, collector not called | `run_jira_command([...], snapshot_collector=bad_collector, config_reader=lambda:{}, environ={})` | rc=2, collector never called | PASS |
| `whilly gitlab smoke --help` exits 0 showing `--repo-url` | `.venv/bin/python -m whilly gitlab smoke --help` | Help text shown, exit 0, `--repo-url` present | PASS |
| `whilly jira smoke --help` exits 0 showing `--issue` | `.venv/bin/python -m whilly jira smoke --help` | Help text shown, exit 0, `--issue` present | PASS |
| `whilly --help` lists `gitlab` | `.venv/bin/python -m whilly --help \| grep gitlab` | Output: `gitlab Smoke-test live GitLab integration (auth, repo-hint).` | PASS |

### Probe Execution

Step 7c: SKIPPED — no `probe-*.sh` scripts defined for phase 19 and none referenced in any plan or summary.

### Requirements Coverage

| Requirement | Source Plan(s) | Description | Status | Evidence |
|-------------|---------------|-------------|--------|---------|
| LIVE-01 | 19-02, 19-03, 19-04 | Operator can run authenticated Jira smoke (classify, history, comments, links) with documented setup | SATISFIED | `_run_jira_smoke` implements 6 checks; `## Live smoke` docs section documents setup; 12 unit tests pass |
| LIVE-02 | 19-03, 19-04 | Operator can run authenticated GitLab smoke (link refresh, repo hints) against real repository | SATISFIED | `_run_gitlab_smoke` implements auth + project_access + repo_hint checks; docs document `GITLAB_URL`/`GITLAB_TOKEN` setup; 21 unit tests pass |
| LIVE-03 | 19-01, 19-02, 19-03, 19-04 | Smoke runs produce persisted audit evidence/reports an operator can review | SATISFIED | `write_smoke_report` called on every run (pass or fail) in both commands; report location documented; redaction verified in tests |

All 3 requirement IDs declared across plans are accounted for. No orphaned requirements found.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| No TBD/FIXME/XXX markers found in any phase 19 file | — | — | — | — |

No TODO/HACK/PLACEHOLDER patterns found. No `return null`/empty-return stubs. No hardcoded empty data in rendering paths. No debt markers.

**Debt marker gate:** Clean — no unresolved markers.

### Human Verification Required

### 1. Jira Authenticated Smoke (LIVE-01)

**Test:** Set `JIRA_SERVER_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN` for a real Jira project, then run `whilly jira smoke --issue PROJECT-123`.
**Expected:** Exits 0 with PASS summary; report file written to `whilly_logs/smoke/jira-smoke-{timestamp}.json`; all 6 checks (auth, issue_fetch, comments, changelog, remote_links, classify) shown; no tokens or raw exceptions in output or report.
**Why human:** Requires real credentials against a live Jira instance. The unit test suite covers all code paths via injection, but the actual REST call path with live credentials cannot be verified programmatically.

### 2. GitLab Authenticated Smoke (LIVE-02)

**Test:** Set `GITLAB_URL` and `GITLAB_TOKEN`, then run `whilly gitlab smoke --repo-url https://YOUR_GITLAB/group/repo`.
**Expected:** Exits 0 with PASS; report file written; 3 checks (auth, project_access, repo_hint) shown with measured durations; no token values in output or report.
**Why human:** Requires real credentials against a live GitLab instance.

### 3. Jira Failure Hint (no traceback)

**Test:** Run `whilly jira smoke --issue REAL-KEY` with an intentionally wrong `JIRA_API_TOKEN`.
**Expected:** Exits 1; actionable hint mentions `JIRA_SERVER_URL` / `JIRA_API_TOKEN` / project key; the word "Traceback" does not appear anywhere in stdout or stderr.
**Why human:** Requires a real Jira server to trigger a real authenticated HTTP 401/403 failure.

### 4. GitLab Failure Hint (no traceback, no credential leak)

**Test:** Run `whilly gitlab smoke --repo-url URL` with an intentionally wrong `GITLAB_TOKEN`.
**Expected:** Exits 1; hint names GITLAB_TOKEN; no "Traceback" in output; report file is written and contains no token value.
**Why human:** Requires a real GitLab server to trigger a real HTTP auth failure.

### Gaps Summary

No automated gaps found. All 9 must-have truths are verified in the codebase with passing tests, correct wiring, and substantive implementation. The phase delivered:

- `whilly/cli/smoke.py` — SmokeReport accumulator + write_smoke_report + _redact_url (Plan 01, LIVE-03)
- `whilly/cli/jira.py` — `whilly jira smoke` with 6 read-only checks, config gate, no-traceback error handling (Plan 02, LIVE-01 + LIVE-03)
- `whilly/cli/gitlab.py` — `whilly gitlab smoke` with 3 read-only checks, injectable getter, SSH rejection, CR-01 credential redaction in error messages (Plan 03, LIVE-02 + LIVE-03)
- `docs/Whilly-Usage.md` — `## Live smoke` section with setup, commands, exit codes, report location (Plan 04, LIVE-01 + LIVE-02 + LIVE-03)
- 51 unit tests across 4 test files (all passing), full unit suite green (2446 passed, 2 skipped)
- All 9 post-review findings from REVIEW.md (CR-01..CR-03, WR-01..WR-09, IN-01) are fixed and committed

The only remaining items are the 4 human UAT runs against live infrastructure with real credentials — these are designated manual UAT per the phase environment notes and cannot be verified from code alone.

---

_Verified: 2026-06-12T01:30:00Z_
_Verifier: Claude (gsd-verifier)_
