---
phase: 19-live-authenticated-smoke
plan: "03"
subsystem: cli
tags: [smoke, gitlab, report, redaction, cli, json, pytest, security]

requires:
  - phase: 19-01
    provides: "SmokeReport, write_smoke_report, _redact_url, EXIT_OK/EXIT_CHECK_FAILED/EXIT_CONFIG_MISSING"

provides:
  - "build_gitlab_parser() — argparse parser for whilly gitlab (smoke subcommand)"
  - "run_gitlab_command(argv, *, gitlab_getter=None, environ=None) — injectable entry point"
  - "_run_gitlab_smoke() — 3 read-only checks: auth, project_access, repo_hint"
  - "_gitlab_get(url, *, token, timeout) — urllib Bearer-auth client, HTTPError/URLError -> RuntimeError"
  - "_resolve_gitlab_config_state(env, host) — token precedence GITLAB_TOKEN -> GITLAB_API_TOKEN -> WHILLY_GITLAB_API_TOKEN -> glab"
  - "_resolve_project_path(repo_url) — traversal-safe URL-encoded path for /api/v4/projects/{path}"
  - "whilly/cli/__init__.py: lazy dispatch + _HELP_TEXT entry for 'gitlab'"
  - "7 unit tests: auth-pass, missing-config, repo-hint, raising-getter, no-secrets, token-precedence x2"

affects:
  - "whilly/cli/__init__.py (dispatch table + help)"

tech-stack:
  added: []
  patterns:
    - "_gitlab_get mirrors _jira_get: urllib Request, Bearer header, JSON decode, HTTPError/URLError -> RuntimeError"
    - "Token precedence: GITLAB_TOKEN -> GITLAB_API_TOKEN -> WHILLY_GITLAB_API_TOKEN -> glab subprocess fallback"
    - "_resolve_project_path: urlsplit + strip .git + filter '..' parts + urllib.parse.quote(safe='') (T-19-06)"
    - "Per-check accumulation: each check wrapped in try/except, failure recorded + next check runs (SmokeReport contract)"
    - "Report payload: target.host=_redact_url(url), target.repo_path, booleans, counts — never token"
    - "Lazy dispatch in __init__.py: if cmd == 'gitlab': from whilly.cli.gitlab import run_gitlab_command"

key-files:
  created:
    - whilly/cli/gitlab.py
    - tests/unit/cli/test_gitlab_smoke.py
  modified:
    - whilly/cli/__init__.py

key-decisions:
  - "_resolve_gitlab_config_state exposes GITLAB_TOKEN as the highest-priority env var (above GITLAB_API_TOKEN used by gitlab_mr.py) per CONTEXT.md token precedence"
  - "_resolve_project_path strips '..' components before URL-encoding to satisfy T-19-06 traversal threat mitigation"
  - "gitlab_getter injection point in run_gitlab_command enables full unit testing without real network"
  - "Report payload adds 'target' dict with redacted host + repo_path; token never serialised (T-19-08)"
  - "_gitlab_get converts HTTPError/URLError to RuntimeError before they escape the function (T-19-09)"

metrics:
  duration: "~23 min"
  completed: "2026-06-12"
  tasks: 3
  files_modified: 3
---

# Phase 19 Plan 03: GitLab Smoke Command Summary

**Injectable urllib-based `whilly gitlab smoke` with 3 read-only checks (auth, project_access, repo_hint), traversal-safe path encoding, token-precedence resolution, and 7 unit tests asserting no-secret-leak and no raw traceback**

## Performance

- **Duration:** ~23 min
- **Started:** 2026-06-11T22:23:33Z
- **Completed:** 2026-06-12T22:47:00Z
- **Tasks:** 3
- **Files modified:** 3

## Accomplishments

- Created `whilly/cli/gitlab.py` with `build_gitlab_parser()`, `run_gitlab_command()`, `_run_gitlab_smoke()`, `_gitlab_get()`, `_resolve_gitlab_config_state()`, and `_resolve_project_path()` — modeled on `qa_release.py` + `rollback.py` error-surfacing patterns
- Implemented `_gitlab_get()` mirroring `_jira_get`: `urllib.request.Request` with `Authorization: Bearer {token}` + `Accept: application/json`, `urlopen` with timeout, JSON decode, `HTTPError`/`URLError` -> `RuntimeError` conversion (T-19-09)
- Token precedence: `GITLAB_TOKEN` -> `GITLAB_API_TOKEN` -> `WHILLY_GITLAB_API_TOKEN` -> `glab config get token` CLI fallback (per CONTEXT.md locked decision)
- `_resolve_project_path()` strips scheme, host, trailing `.git`, and `..` components before `urllib.parse.quote(safe='')` — satisfies T-19-06 traversal threat
- Three read-only smoke checks: `auth` (`/api/v4/user`), `project_access` (`/api/v4/projects/{encoded}`), `repo_hint` (path_with_namespace + http_url_to_repo match) — each wrapped in `try/except` so a failing check never stops subsequent checks
- Report payload uses `_redact_url(url)` host, repo path, booleans, counts; token never serialised (T-19-08)
- Registered `whilly gitlab` in `whilly/cli/__init__.py`: lazy dispatch branch + `_HELP_TEXT` entry + module docstring entry
- Created `tests/unit/cli/test_gitlab_smoke.py` with 7 tests: auth-pass, missing-config (getter never called), repo-hint report contents, raising-getter (exit 1, no Traceback), no-secrets, GITLAB_TOKEN precedence, GITLAB_API_TOKEN fallback

## Task Commits

1. **Task 1: Create whilly/cli/gitlab.py** — `4ea7330` (feat)
2. **Task 2: Register in CLI dispatch + _HELP_TEXT** — `321db04` (feat)
3. **Task 3: Unit tests for gitlab smoke** — `8e553be` (test)

## Files Created/Modified

- `whilly/cli/gitlab.py` — new module (426 lines): parser, injectable entry point, 3-check smoke, urllib client, config resolver, project path helper
- `whilly/cli/__init__.py` — added `gitlab` dispatch branch + `_HELP_TEXT` entry + docstring entry (6 lines added)
- `tests/unit/cli/test_gitlab_smoke.py` — 7 unit tests (256 lines)

## Decisions Made

- Token precedence exposes `GITLAB_TOKEN` as highest priority (above the `GITLAB_API_TOKEN` used in `gitlab_mr.py`) because CONTEXT.md names `GITLAB_TOKEN` as the primary env var
- `_resolve_project_path` filters `..` parts before URL-encoding (traversal guard, T-19-06) rather than relying on GitLab's API rejection
- `gitlab_getter` parameter defaults to `_gitlab_get` at call time (not import time), keeping test injection clean and avoiding circular imports
- `--persist` gate checks `WHILLY_DATABASE_URL` in `os.environ` (not the injected `environ`) because the gate semantics match `whilly jira poll` exactly and persist-to-DB is an operator concern, not a unit-test concern

## Deviations from Plan

**1. [Rule 2 - Missing critical functionality] Added `_extract_host_from_url` helper**
- **Found during:** Task 1
- **Issue:** `_resolve_gitlab_config_state` needed to know the GitLab host before calling the `glab` CLI fallback; the plan referenced `_REMOTE_HOST_RE` from `gitlab_mr.py` but didn't name a helper function
- **Fix:** Added `_extract_host_from_url(repo_url)` using the same `_REMOTE_HOST_RE` pattern; called before `_resolve_gitlab_config_state` in `run_gitlab_command` to pass the host
- **Files modified:** `whilly/cli/gitlab.py`
- **Commit:** `4ea7330`

**2. [Rule 2 - Missing critical functionality] Added `_matches_repo` helper**
- **Found during:** Task 1 (repo_hint check implementation)
- **Issue:** The plan described the repo-hint check as verifying `path_with_namespace` / `http_url_to_repo` but didn't specify a separate helper; inlining the logic would have made `_run_gitlab_smoke` harder to test
- **Fix:** Added `_matches_repo(project, repo_path_encoded)` which does case-insensitive comparison of both fields against the decoded requested path
- **Files modified:** `whilly/cli/gitlab.py`
- **Commit:** `4ea7330`

**3. Added 7th test (GITLAB_API_TOKEN fallback) vs plan's "at least 5 tests"**
- **Found during:** Task 3
- **Issue:** Plan required ≥5 tests; adding a dedicated fallback test makes the precedence chain fully covered (both the positive and negative sides of the GITLAB_TOKEN check)
- **Fix:** Added `test_resolve_gitlab_config_state_falls_back_to_gitlab_api_token`
- **Files modified:** `tests/unit/cli/test_gitlab_smoke.py`
- **Commit:** `8e553be`

## Known Stubs

None. All three checks execute against the injected getter; unit tests assert real pass/fail outcomes and report contents.

## Threat Flags

No new network endpoints, auth paths beyond the documented GitLab `/api/v4` surface, or schema changes introduced. All mitigations from the plan threat register are implemented:

| Threat | Mitigation Status |
|--------|-----------------|
| T-19-06 Tampering: `--repo-url` path into API path | `_resolve_project_path` strips `..` + URL-encodes — DONE |
| T-19-07 Tampering: server response as path/filename | Response fields used only for boolean comparison; report filename derived from fixed `kind` + timestamp — DONE |
| T-19-08 InfoDisc: token in report/stdout | `_redact_url` host only; token never serialised; test asserts — DONE |
| T-19-09 InfoDisc: raw urllib exception surfaced | `_gitlab_get` converts to `RuntimeError`; per-check catch converts to hint; test asserts no "Traceback" — DONE |

## Self-Check: PASSED

- `whilly/cli/gitlab.py` exists: FOUND
- `whilly/cli/__init__.py` contains `run_gitlab_command`: FOUND
- `tests/unit/cli/test_gitlab_smoke.py` exists: FOUND
- Commit `4ea7330` exists: FOUND
- Commit `321db04` exists: FOUND
- Commit `8e553be` exists: FOUND
- 7 unit tests pass: CONFIRMED
- Ruff clean all 3 files: CONFIRMED

---
*Phase: 19-live-authenticated-smoke*
*Completed: 2026-06-12*
