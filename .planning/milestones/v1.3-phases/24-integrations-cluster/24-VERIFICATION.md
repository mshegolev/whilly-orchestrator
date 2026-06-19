---
phase: 24-integrations-cluster
verified: 2026-06-16T00:00:00Z
status: passed
score: 6/6 must-haves verified
overrides_applied: 0
---

# Phase 24: Integrations Cluster Verification Report

**Phase Goal:** The 6 external-integration surfaces (INT-01..06) are captured as normative OpenSpec specs reverse-spec'd from real v4.7.0 code, each stating auth expectations and the read-only vs mutating boundary.
**Verified:** 2026-06-16
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | 6 capabilities specced (one spec.md per slug) | ✓ VERIFIED | All 6 files exist: jira-integration (200L), gitlab-integration (66L), github-integration (177L), jira-watcher-daemon (164L), notifications (88L), mcp-integration (70L) |
| 2 | Each spec passes `openspec validate <slug> --strict` (exit 0, "is valid") | ✓ VERIFIED | Ran all 6 with openspec 1.4.1 — every one printed "Specification '<slug>' is valid", exit=0 |
| 3 | jira-watcher-daemon captures lifecycle, pause/readiness gates, fail-closed behavior (Phase 20) | ✓ VERIFIED | 9 reqs: interval (300s default), threading.Event.wait, backoff (5/10/20/40/60), PID lock O_CREAT\|O_EXCL + os.kill(pid,0), EPERM→fail-closed, pause gate (watch.paused), readiness gate (None=not ready, watch.block) — all match jira_watch_loop.py |
| 4 | Each integration spec states auth expectations AND read-only vs mutating boundary | ✓ VERIFIED | Every spec has dedicated auth requirement(s) + an explicit boundary requirement (mcp framed as registration/discovery vs mutating-caller) — see Requirements Coverage |
| 5 | Specs reverse-spec'd from REAL v4 code, not aspirational/docstring prose | ✓ VERIFIED | Spot-checks below confirm observed behavior; critically gitlab spec specs `git push --force` (real code) not the docstring's `--force-with-lease` prose |
| 6 | INT-01..06 marked done in REQUIREMENTS.md with 1:1 spec mapping; coverage matrix updated; no whilly/ Python changes | ✓ VERIFIED | All 6 [x] in REQUIREMENTS.md; git diff scope = only .planning/ + openspec/specs/; coverage matrix maps all 6 slugs (github=32 module refs) |

**Score:** 6/6 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| openspec/specs/jira-integration/spec.md | INT-01 spec | ✓ VERIFIED | 10 reqs, 23 scenarios, strict-valid |
| openspec/specs/gitlab-integration/spec.md | INT-02 spec | ✓ VERIFIED | 4 reqs, 10 scenarios, strict-valid |
| openspec/specs/github-integration/spec.md | INT-03 subsystem spec | ✓ VERIFIED | 7 reqs, 20 scenarios, subsystem altitude (NOT 32 per-module), strict-valid |
| openspec/specs/jira-watcher-daemon/spec.md | INT-04 spec | ✓ VERIFIED | 9 reqs, 19 scenarios, strict-valid |
| openspec/specs/notifications/spec.md | INT-05 spec | ✓ VERIFIED | 6 reqs, 13 scenarios, strict-valid |
| openspec/specs/mcp-integration/spec.md | INT-06 spec | ✓ VERIFIED | 5 reqs, 10 scenarios, strict-valid |

### Groundedness Spot-Checks (spec claim ↔ real v4 code)

| Spec | Claim | Code Evidence | Status |
|------|-------|---------------|--------|
| jira | JiraAuth.from_config basic vs bearer; RuntimeError names missing fields | whilly/sources/jira.py:74-146 (auth_scheme normalize 216-222; Basic/Bearer 240-244; RuntimeError 134) | ✓ FLOWING |
| jira | set_issue_status sole mutating transition, case-insensitive to.name, soft-fail | whilly/jira_board.py:82-115 (`_post_transition` only POST, reached only via set_issue_status; `.lower()` match; return False on any Exception) | ✓ FLOWING |
| jira-watcher | backoff sequence, event-wait, interval | jira_watch_loop.py:60 `_BACKOFF_SEQUENCE=(5,10,20,40,60)`; :61 `_DEFAULT_INTERVAL=300`; :158 `stop.wait` | ✓ FLOWING |
| jira-watcher | PID lock fail-closed (EPERM=alive, ESRCH=stale) | jira_watch_loop.py:220-236 O_CREAT\|O_EXCL; os.kill(pid,0); PermissionError→refuse; ProcessLookupError→reclaim | ✓ FLOWING |
| jira-watcher | readiness None = not ready, fail-closed | jira_watch_loop.py:342-366 returns None on undeterminable; spec blocks dispatch | ✓ FLOWING |
| github | gh token chain (WHILLY_GH_TOKEN→keyring→[github].token→ambient) | gh_utils.py:33-48 exact match to spec ordering | ✓ FLOWING |
| github | PR push = force-with-lease; gh pr create; failure_mode never raises | sinks/github_pr.py:273 `push --force-with-lease`; :289 failure_mode; existing-PR→ok=True | ✓ FLOWING |
| github | forge intake idempotent, no Claude tokens, label flip last | forge/intake.py:14-24 SELECT-first idempotency; :19 no second `gh issue edit`; :217 unique ref | ✓ FLOWING |
| github | CI poll never raises, explicit results | ci/github.py:30-72 returns unavailable/unauthenticated/timed_out, no raise | ✓ FLOWING |
| github | subsystem altitude, not per-module | 7 reqs covering auth/reads/merge/conversion+intake/mutating/workflow/CI; coverage matrix carries 32 module refs | ✓ FLOWING |
| gitlab | token precedence + glab fallback; host from repo URL | cli/gitlab.py:101-108 `GITLAB_TOKEN→GITLAB_API_TOKEN→WHILLY_GITLAB_API_TOKEN`→glab; sinks/gitlab_mr.py:82-87 same | ✓ FLOWING |
| gitlab | smoke read-only (GET /user + /projects, redacted); open_mr_for_task mutating | cli/gitlab.py:291-348 Bearer GET only + _redact_url; sinks/gitlab_mr.py:240 push --force + glab mr create | ✓ FLOWING |
| **gitlab** | **spec reflects OBSERVED `--force`, NOT docstring `--force-with-lease`** | gitlab_mr.py:240 code uses `push --force`; docstring line 215 says `--force-with-lease`; **spec line 57 correctly states `--force`** | ✓ FLOWING |
| notifications | factory: SlackNotifier only if SLACK_ENABLED+token+channel else NullNotifier | adapters/notifications/factory.py:31-33 exact gate | ✓ FLOWING |
| notifications | mailer SMTP-or-jsonl, never raises | api/mailer.py:105 "Never raises"; :116 except Exception → event_log fallback | ✓ FLOWING |
| notifications | confluence Basic auth + ValueError on missing creds | adapters/confluence/publisher.py:73-89 ValueError; Basic/Bearer by auth_scheme | ✓ FLOWING |
| notifications | voice noop when disabled/absent | notifications.py:13-43 shutil.which gate, return on disabled, except OSError | ✓ FLOWING |
| mcp | register/get/list, singleton, api_key_env names-not-stores creds | registry.py:70-180 register_tool/get_tool/list_tools/list_categories; :168-180 singleton; :47 api_key_env field (no secret stored) | ✓ FLOWING |
| mcp | profile registry singleton | profiles.py:109-121 get_profile_registry singleton | ✓ FLOWING |

No nonexistent, aspirational, or legacy-as-current pins detected. Every spec claim maps to observed v4.7.0 code.

### Requirements Coverage

| Requirement | Source Plan | Slug | Auth req present | Boundary req present | strict-valid | Status |
|-------------|-------------|------|------------------|----------------------|--------------|--------|
| INT-01 | 24-01 | jira-integration | ✓ (Layered credential resolution; Basic vs bearer; TLS; Unconfigured-auth-raises; CLI credential gate) | ✓ (Read-only fetch; Single mutating board-sync; Read-only vs mutating boundary) | ✓ | ✓ SATISFIED |
| INT-02 | 24-03 | gitlab-integration | ✓ (token resolution + host derivation; redaction) | ✓ (Smoke strictly read-only; MR sink only mutating path) | ✓ | ✓ SATISFIED |
| INT-03 | 24-02 | github-integration | ✓ (Centralised gh CLI auth resolution) | ✓ (Read-only state reads; Mutating boundary confined to PR/project/issue writes) | ✓ | ✓ SATISFIED |
| INT-04 | 24-01 | jira-watcher-daemon | ✓ (CLI credential gate before the loop) | ✓ (Pause gate suppresses dispatch only; Readiness gate; Default-off gated dispatch — read-only polling vs gated mutation) | ✓ | ✓ SATISFIED |
| INT-05 | 24-03 | notifications | ✓ (Factory gates on full config; per-channel SMTP/confluence creds) | ✓ (best-effort dispatch; Outbound dispatch never gates orchestration) | ✓ | ✓ SATISFIED |
| INT-06 | 24-03 | mcp-integration | ✓ (Credentials are named, not stored — api_key_env) | ✓ (Registry is a discovery surface, not a mutating caller) | ✓ | ✓ SATISFIED |

No orphaned requirements — REQUIREMENTS.md Phase 24 maps exactly INT-01..06, all claimed by plans.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| (none) | — | No TBD/FIXME/XXX/TODO/HACK/PLACEHOLDER/aspirational markers in any of the 6 specs | — | clean |
| (none) | — | No `## ADDED/MODIFIED/REMOVED Requirements` delta headers | — | clean (specs are baselines, not deltas) |
| (none) | — | No fenced code blocks inside requirement bodies; max body paragraph 493 chars (<500) | — | clean (AUTHORING-conformant) |

### Scope Discipline

- git diff (be4fb22^..f9d2866) touched ONLY `.planning/*` and `openspec/specs/*` — zero `whilly/` Python changes. ✓
- Documentation-only phase as scoped. ✓

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| All 6 specs pass strict validation | `openspec validate <slug> --strict` ×6 | 6× "is valid", exit 0 | ✓ PASS |
| No whilly Python touched | `git diff --name-only be4fb22^..f9d2866 \| grep ^whilly/` | empty | ✓ PASS |
| No delta headers | `grep ADDED/MODIFIED/REMOVED Requirements` | none | ✓ PASS |

### Human Verification Required

None. All success criteria are programmatically verifiable: spec existence, strict validation exit codes, source-code grounding via grep against real modules, requirement/scenario structure, REQUIREMENTS.md checkboxes, and git diff scope are all observable.

### Gaps Summary

No gaps. All 6 integration specs (INT-01..06) exist, pass `openspec validate --strict` (exit 0), are reverse-spec'd from the real v4.7.0 modules (every spot-check claim traced to source), each carries explicit auth-expectations and read-only-vs-mutating boundary requirements, github-integration is held at subsystem-contract altitude (7 reqs, not 32 per-module), no whilly/ Python was modified, no delta headers were introduced, and INT-01..06 are marked done in REQUIREMENTS.md with 1:1 slug mapping plus coverage-matrix accounting. The single discriminating groundedness trap — gitlab's `git push --force` (code) vs `--force-with-lease` (docstring prose) — was specced to the observed code, and github correctly specs its distinct `--force-with-lease` primitive. Phase goal achieved.

---

_Verified: 2026-06-16_
_Verifier: Claude (gsd-verifier)_
