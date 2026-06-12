# Milestones

## v1.2 Adoption & live-ops (Shipped: 2026-06-12)

**Phases completed:** 3 phases, 9 plans, 11 tasks

**Key accomplishments:**

- Full Alembic migration chain (001→028) validated live from empty Docker Postgres with honest
  per-check evidence flags, `make migrate-chain` entry point, and a `migration-chain` CI job —
  first live CI run green with evidence artifact uploaded.
- `whilly jira smoke --issue KEY`: six read-only checks (auth/issue/comments/changelog/links/
  classify) with credential gate, redacted JSON reports, exit codes 0/1/2 — validated LIVE 6/6
  against jira.mts.ru (Server/DC; required JIRA_AUTH_SCHEME=bearer + JIRA_API_VERSION=2, now
  documented).
- New `whilly gitlab` CLI group with `smoke` (auth/project_access/repo_hint via injectable urllib
  client, traversal-safe path encoding, token precedence) — validated LIVE 3/3 against
  gitlab.services.mts.ru; failure paths confirmed leak-free.
- Shared `whilly/cli/smoke.py` foundation: SmokeReport honest accumulation, secret-redacting
  report writer into `whilly_logs/smoke/`.
- `whilly jira watch` daemon: configurable interval, graceful SIGINT/SIGTERM stop, atomic status
  file + `watch-status` reader, PID single-instance guard, 5/10/20/40/60s backoff with audit
  events, fail-closed pause/readiness gates, default-off `--dispatch` through the Phase-17-gated
  path — validated LIVE (2 cycles against real Jira, clean stop).
- Code-review discipline: 7 Critical + 23 Warning findings across the three phases found and
  fixed pre-completion, each with falsification tests (recurring fabricated-evidence bug class
  eliminated three times).

**Live validation:** All three phases validated against real infrastructure (Docker, GitHub
Actions, jira.mts.ru, gitlab.services.mts.ru) — no deferred validation items at close.

---

## v1.1 UI parity completion (Shipped: 2026-05-11)

**Phases completed:** 7 phases, 12 plans, 0 tasks

**Key accomplishments:**

- Shared TUI/WUI operator surface, hotkey, route, and action contracts now drive the active
  dashboard and TUI behavior.

- Stale WUI hotkeys, selectors, and worker routes were brought onto the canonical five-surface
  contract with focused regression coverage.

- Logs/admin/PRD fragments were classified so inactive UI artifacts are not silently treated as
  active operator surfaces.

- Version checks, manual update, and explicit automatic-update policy were added for classic
  operator-controlled package lifecycle behavior.

- `whilly feedback` now creates explicit GitHub bug/idea reports with runtime context and secret
  redaction.

- Jira intake can classify feature/bug/task/devops work, persist issue history, reread comments and
  links, derive repo hints, and gate autonomous execution on code/test readiness.

**Archives:**

- `.planning/milestones/v1.1-ROADMAP.md`
- `.planning/milestones/v1.1-REQUIREMENTS.md`
- `.planning/milestones/v1.1-MILESTONE-AUDIT.md`
- `.planning/milestones/v1.1-RETROSPECTIVE.md`

**Known deferred validation:**

- Live authenticated Jira/GitLab smoke on a real operator machine.
- Full Docker-backed Alembic chain run outside the focused static migration coverage.
- Long-running Jira watcher/daemon wrapper around one-shot `whilly jira poll`.

---

## v1.0 milestone (Shipped: 2026-05-08)

**Phases completed:** 12 phases, 25 plans, 0 tasks

**Key accomplishments:**

- WUI/TUI operator pause, review decisions, refresh behavior, identity controls, table metadata,
  mobile rows, and review actions were aligned for operator workflows.

- Secret linting, runner environment allowlists, guard audits, verification redaction, and
  residual-risk docs closed the scoped `a3-a4` hardening work.

- Project-profile verification commands now flow through generated plans, persistence, local worker
  execution, remote transport, and compliance reporting.

- Rollback points, branch/push preflight, and confirmation-gated restore provide an operator safety
  net for risky repository mutation.

- Explicit configured CI polling and bounded repair attempts create auditable escalation instead of
  unbounded retry or auto-merge behavior.

- Governance policy is deterministic and semantic memory is explicitly deferred from current scope.

**Archives:**

- `.planning/milestones/v1.0-ROADMAP.md`
- `.planning/milestones/v1.0-REQUIREMENTS.md`
- `.planning/milestones/v1.0-MILESTONE-AUDIT.md`

**Known deferred validation:**

- Live authenticated GitHub CI provider smoke against a real PR with checks.
- Browser and assistive-technology QA for mobile WUI/review affordance polish.

---
