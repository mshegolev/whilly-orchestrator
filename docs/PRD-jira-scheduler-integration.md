# PRD: Jira Scheduler & Deep Integration

**Version:** 1.0  
**Date:** 2026-05-12  
**Author:** Mikhail Shchegolev  
**Status:** Draft

---

## 1. Problem Statement

Whilly today has solid single-task intake (`whilly jira intake ABC-123`) and a functional TUI for interactive execution. The gap is *continuous, automated intake* of Jira work at scale:

- An operator must manually trigger intake for each issue. There is no way to say "watch this JQL filter and process matching issues automatically."
- Multiple schedulers cannot run in parallel without risk of double-importing the same issue into different plans.
- Jira issue context (which git repository this work targets, which Confluence space owns the docs) must be re-entered by hand on every intake.
- Documentation-type tasks — ones that should land in Confluence rather than a code repo — follow the same code-oriented flow and require extra operator steps to publish.
- External tool integrations (MCP servers, custom skills) can only be activated globally via env vars; there is no per-issue or per-project routing.

**Root cause:** the existing architecture treats Jira as an on-demand import source (pull-on-request), not as a continuous intake feed. There is no scheduler layer between Jira and the orchestrator.

---

## 2. Goals

| # | Goal |
|---|------|
| G1 | Operators can attach one or more JQL-based scheduler rules to Whilly; matched issues are imported automatically with no manual command. |
| G2 | Deduplication is guaranteed end-to-end: re-running a scheduler, restarting the process, or defining multiple schedulers that overlap will never create duplicate plans. |
| G3 | Interactive TUI for a single Jira issue is enhanced: operators can work interactively *and* hand off to autonomous execution without leaving the TUI. |
| G4 | Git repository context is statically associated with Jira projects or issue labels, so intake no longer requires a `--repo-url` argument. |
| G5 | Documentation tasks are classified automatically and published to Confluence as a first-class flow (no custom scripting). |
| G6 | External tools (MCP servers, Whilly skills) can be configured per-project or per-scheduler rule, not only globally. |

---

## 3. Non-Goals

- **No Jira webhook support in Phase 1.** Polling is sufficient; webhook delivery requires a public endpoint and firewall changes outside Whilly's deployment envelope.
- **No multi-tenant Jira servers.** All schedulers share one `[jira]` config block (one server, one credential). Multi-server support is a future extension.
- **No AI-based JQL suggestion.** Scheduler rules are authored by the operator.
- **No Confluence write API abstraction beyond Markdown → Confluence page.** Complex Confluence macros, attachments, or space hierarchies are out of scope.
- **No autonomous code execution for documentation tasks.** A documentation task creates and publishes the Confluence page; it does not modify code.

---

## 4. User Stories

### Epic A — Interactive TUI for Single-Issue Work

**A1** — As an operator, I can run `whilly jira tui ABC-123` and get a Rich TUI showing the issue summary, classification, and four action options: PRD, Plan, Run, Autonomous. I can navigate with arrow keys and confirm with Enter, without remembering CLI flags.

**A2** — In the TUI, when I select "Autonomous," the issue is imported to DB and a worker is claimed and started immediately. The TUI transitions to a live task-monitoring view (reusing `OperatorSurface.OVERVIEW`) scoped to this plan.

**A3** — In the TUI, when I select "Interactive," I can see task status and the live agent log for each sub-task. I can pause, resume, or cancel individual tasks using the existing hotkey model (`p` = pause, `q` = quit, `l` = logs).

**A4** — The TUI supports a `--repo-url` flag and a `--repo-kind` flag identical to `whilly jira intake` so it can be scripted while remaining interactive when those flags are absent.

### Epic B — JQL Scheduler

**B1** — As an operator, I can define a scheduler rule in `whilly.toml` with a `jql` string, a `poll_interval` (seconds), an optional `repo_target` reference, and an optional `mcp_profile` name. Whilly evaluates the JQL filter on each poll cycle and imports newly matching issues.

**B2** — Multiple scheduler rules with overlapping JQL filters are safe: deduplication is enforced at the `work_intents` table level via the existing `(origin_system, origin_ref)` unique index. A second scheduler that matches the same issue finds an existing `work_intent` row and skips plan creation.

**B3** — When an issue that was already imported has its description or links changed (detected via `context_hashes.combined_hash` in `jira_work_sessions`), the scheduler raises a `CONTENT_CHANGED` event and optionally triggers a replan via `whilly plan replan <plan_id>`.

**B4** — The scheduler emits structured log lines and appends `scheduler.poll_cycle` events to `whilly_logs/whilly_events.jsonl` so ops tooling can track scheduler health without querying Postgres.

**B5** — Scheduler rules can be enabled/disabled at runtime via a new `whilly scheduler` CLI subcommand: `whilly scheduler list`, `whilly scheduler enable <name>`, `whilly scheduler disable <name>`, `whilly scheduler status`.

**B6** — Each scheduler rule has an independent `max_inflight` cap (default: `WHILLY_MAX_PARALLEL`). When the cap is reached, new matching issues are queued as `work_intents` with status `queued_for_plan` rather than immediately promoted to plans.

### Epic C — Git Repository Configuration

**C1** — As an operator, I can define a `[project_map]` section in `whilly.toml` that maps Jira project keys (e.g. `ABC`) or label patterns (e.g. `label:service-payments`) to a `repo_target_id`. When a scheduler imports an issue matching a rule, the repo target is resolved automatically without `--repo-url`.

**C2** — The `whilly jira intake` command consults `[project_map]` before prompting for a repo URL; if a match is found, the prompt is skipped.

**C3** — Project map entries support a `default_branch` override and a `verify_command` list (forwarded to `whilly run --verify-command`). This replaces per-invocation flags.

**C4** — A new `whilly project-map show ABC-123` command prints the resolved repo target for an issue key so operators can audit the mapping without running a full import.

### Epic D — Documentation Task Auto-Publishing

**D1** — The `classify_jira_work` classifier is extended with a `documentation` kind. Issues classified as `documentation` (triggers: issue type "Documentation", labels `docs`/`documentation`, keyword signals `"document"`, `"confluence"`, `"wiki"`, `"write up"`) follow a separate `documentation_publish` flow.

**D2** — When a documentation task is classified, Whilly generates a Confluence page draft using the issue description as the source. The draft is written to `out/confluence-<KEY>.md` locally and, if `[confluence]` is configured in `whilly.toml`, published to the target Confluence space via the Confluence REST API.

**D3** — The Confluence publish result (page URL, version, space key) is written back to the Jira issue as a comment (`/whilly-published: <url>`) so the Jira team can navigate directly.

**D4** — If `[confluence]` is not configured, the documentation flow writes the draft to disk and prints a warning with setup instructions, but does not fail.

**D5** — Acceptance criteria for documentation tasks include: Confluence page was created, page URL was recorded in `jira_work_sessions.raw_snapshot['confluence_page_url']`, Jira comment was posted.

### Epic E — External Tool Integration per Rule

**E1** — As an operator, I can define a `[mcp_profile.<name>]` section in `whilly.toml` listing MCP server definitions (name, command/URL, environment overrides). A scheduler rule or a `[project_map]` entry can reference a profile by name.

**E2** — When a task is dispatched for a plan that carries an MCP profile, the agent prompt is enriched with a `## Available Tools` section listing the MCP servers in the profile. The existing `build_task_prompt` and `build_sequential_prompt` builders are extended to accept an optional `mcp_profile: list[McpServerDef]` argument.

**E3** — MCP profiles are merged, not replaced: a task inherits the global tool set plus any profile-specific servers. Conflicts (same server name in both) resolve in favor of the profile-specific definition.

**E4** — The `whilly skill` subcommand is added: `whilly skill list` prints all available skills discovered from the skills directory (`~/.claude/skills/`) and configured MCP servers, with their trigger patterns. This gives operators a reference view without needing to read config files.

---

## 5. Success Metrics

| Metric | Baseline (today) | Target (end of Phase 2) |
|---|---|---|
| Issues imported without manual CLI command per week | 0 | ≥ 50 (scheduler-driven) |
| Duplicate plan rows created by scheduler over 30-day run | — | 0 |
| Operator steps to intake a new Jira issue (with project map configured) | 4 CLI commands | 0 (fully automated via scheduler) or 1 (`whilly jira tui`) |
| Documentation tasks published to Confluence automatically | 0 | 100% of issues classified `documentation` with Confluence configured |
| Scheduler poll-cycle errors visible in structured logs | 0 (no scheduler) | 100% of errors surfaced within 1 poll cycle |

---

## 6. Technical Scope

### 6.1 New Modules

| Module | Path | Responsibility |
|---|---|---|
| `JqlScheduler` | `whilly/scheduler/jql_scheduler.py` | Async poll loop for one JQL rule; drives `SchedulerEngine` |
| `SchedulerEngine` | `whilly/scheduler/engine.py` | Orchestrates multiple `JqlScheduler` instances; manages lifecycle |
| `SchedulerRule` | `whilly/scheduler/models.py` | Dataclass: `name`, `jql`, `poll_interval`, `max_inflight`, `repo_target_id`, `mcp_profile_name`, `replan_on_content_change` |
| `SchedulerRepository` | `whilly/adapters/db/scheduler_repository.py` | DB operations for scheduler state: `upsert_scheduler_rule`, `get_active_rules`, `record_poll_cycle` |
| `ConfluencePublisher` | `whilly/adapters/confluence/publisher.py` | Thin REST client: `create_page`, `update_page`, `get_page_by_title` |
| `ProjectMapResolver` | `whilly/project_config/project_map.py` | Resolves Jira project key → `RepoTarget` + `verify_commands` via config |
| `McpProfileRegistry` | `whilly/project_config/mcp_profiles.py` | Loads and validates `[mcp_profile.*]` sections; merges profiles |
| `DocumentationFlow` | `whilly/workflow/documentation.py` | Orchestrates documentation-kind tasks: generate draft, publish, comment back |
| `JiraTuiCommand` | `whilly/cli/jira_tui.py` | TUI entry point for single-issue interactive intake |

### 6.2 Modified Modules

| Module | Change |
|---|---|
| `whilly/jira_work.py` | Add `documentation` to `WORK_KINDS`; add `_DOCUMENTATION_KEYWORDS` and `_DOCUMENTATION_TYPES` scorer; update `_recommended_flow` to return `"documentation_publish"` |
| `whilly/cli/jira.py` | Add `tui` subcommand to `build_jira_parser`; route to `JiraTuiCommand` |
| `whilly/cli/__main__.py` | Register `whilly scheduler` and `whilly skill` top-level subcommands |
| `whilly/core/prompts.py` | Add optional `mcp_profile: list[dict]` parameter to `build_task_prompt`; append `## Available Tools` section when non-empty |
| `whilly/project_config/models.py` | Add `ProjectMapEntry`, `McpServerDef`, `McpProfile` dataclasses |
| `whilly/project_config/loader.py` | Load `[project_map]` and `[mcp_profile.*]` from `whilly.toml` |
| `whilly/adapters/db/schema.sql` | Add `scheduler_rules` and `scheduler_poll_cycles` tables (see §6.3) |
| `whilly/adapters/db/repository.py` | Extend `TaskRepository` with `upsert_work_intent_from_jira` (idempotent plan-creation path) |
| `whilly/adapters/transport/server.py` | Add `/scheduler/rules` (GET, POST, PATCH) and `/scheduler/status` (GET) endpoints |

### 6.3 New Database Tables

```sql
-- Scheduler rule definitions (persisted for runtime enable/disable and audit)
CREATE TABLE scheduler_rules (
    name              TEXT PRIMARY KEY,
    jql               TEXT NOT NULL,
    poll_interval_sec INTEGER NOT NULL DEFAULT 300,
    max_inflight      INTEGER NOT NULL DEFAULT 3,
    repo_target_id    TEXT REFERENCES repo_targets (id) ON DELETE SET NULL,
    mcp_profile_name  TEXT,
    replan_on_change  BOOLEAN NOT NULL DEFAULT false,
    enabled           BOOLEAN NOT NULL DEFAULT true,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Per-poll-cycle audit log (one row per JQL execution)
CREATE TABLE scheduler_poll_cycles (
    id              BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    rule_name       TEXT NOT NULL REFERENCES scheduler_rules (name) ON DELETE CASCADE,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    issues_seen     INTEGER NOT NULL DEFAULT 0,
    issues_new      INTEGER NOT NULL DEFAULT 0,
    issues_skipped  INTEGER NOT NULL DEFAULT 0,  -- already imported
    issues_changed  INTEGER NOT NULL DEFAULT 0,  -- content_hash diff
    error_message   TEXT,
    CONSTRAINT ck_poll_cycle_counts_non_negative
        CHECK (issues_seen >= 0 AND issues_new >= 0 AND issues_skipped >= 0 AND issues_changed >= 0)
);

CREATE INDEX ix_scheduler_poll_cycles_rule_started
    ON scheduler_poll_cycles (rule_name, started_at);
```

The existing `work_intents` table gains two new nullable columns (via Alembic migration):

```sql
ALTER TABLE work_intents
    ADD COLUMN scheduler_rule_name TEXT REFERENCES scheduler_rules (name) ON DELETE SET NULL,
    ADD COLUMN queued_at           TIMESTAMPTZ;
```

`status` in `work_intents` is extended to include `'queued_for_plan'` (existing values: `'ready'`).

### 6.4 Config Schema Extensions

The `whilly.toml` format gains three new sections:

```toml
# One or more scheduler rules
[[scheduler]]
name         = "qa-backlog"
jql          = "project = QA AND status = 'Ready for Automation' AND assignee = currentUser()"
poll_interval = 300          # seconds; default 300
max_inflight  = 2            # concurrent plans from this rule; default = WHILLY_MAX_PARALLEL
repo_target   = "gitlab:qa/autotests"  # optional; overrides project_map lookup
mcp_profile   = "qa-tools"   # optional MCP profile name
replan_on_change = false     # re-run when Jira description changes; default false

[[scheduler]]
name         = "docs-watch"
jql          = "project = QA AND issuetype = Documentation AND status = 'In Progress'"
poll_interval = 600
max_inflight  = 1

# Project key → repo target + verify commands
[project_map]
[project_map.QA]
repo_target      = "gitlab:qa/autotests"
default_branch   = "main"
verify_commands  = ["pytest -q tests/smoke"]

[project_map.DEMO]
repo_target      = "gitlab:demo/backend"
default_branch   = "develop"

# Label-based override (evaluated after project-key match, more specific wins)
[project_map."label:service-payments"]
repo_target    = "gitlab:platform/payments"
default_branch = "main"

# MCP profile definitions
[mcp_profile.qa-tools]
[[mcp_profile.qa-tools.servers]]
name    = "allure"
command = ["python", "-m", "whilly_skills.allure_mcp"]
env     = { ALLURE_URL = "http://allure.internal" }

[[mcp_profile.qa-tools.servers]]
name    = "jira-read"
command = ["python", "-m", "whilly_skills.jira_read_mcp"]

# Confluence publishing (new)
[confluence]
server_url   = "https://wiki.company.com"
username     = "bot@company.com"
token        = "env:CONFLUENCE_API_TOKEN"   # supports same secret schemes as [jira]
default_space = "QA"
parent_page_id = "12345"   # optional; new pages are created under this parent
```

### 6.5 Deduplication Contract

The deduplication invariant is: **exactly one `work_intent` row per `(origin_system='jira_issue', origin_ref=<JIRA-KEY>)`.** This is enforced by the existing `ix_work_intents_origin_unique` index.

The scheduler's import path:

1. Issue matches JQL → scheduler calls `upsert_work_intent_from_jira(key, payload, rule_name)`.
2. `upsert_work_intent_from_jira` executes:
   ```sql
   INSERT INTO work_intents (id, origin_system, origin_ref, content_hash, status, ...)
   VALUES ($1, 'jira_issue', $KEY, $hash, 'ready', ...)
   ON CONFLICT (origin_system, origin_ref) DO UPDATE
       SET content_hash = EXCLUDED.content_hash,
           updated_at   = NOW()
   RETURNING (xmax = 0) AS is_insert,
             (content_hash <> EXCLUDED.content_hash) AS content_changed;
   ```
3. If `is_insert = TRUE`: create plan, import tasks. Record `issues_new++` in poll cycle.
4. If `is_insert = FALSE` and `content_changed = TRUE` and `replan_on_change = TRUE`: emit `CONTENT_CHANGED` event, trigger replan. Record `issues_changed++`.
5. If `is_insert = FALSE` and `content_changed = FALSE`: no-op. Record `issues_skipped++`.

This is a single atomic upsert. Multiple schedulers executing concurrently on the same issue will serialize on Postgres row-lock; the second writer's ON CONFLICT branch fires and returns `is_insert = FALSE`.

### 6.6 Documentation Flow Detail

```
Jira issue classified as `documentation`
    │
    ▼
DocumentationFlow.run(issue_key, plan_path)
    ├── Read issue description (already in plan JSON)
    ├── Generate Markdown draft via LLM (uses existing claude_cli runner)
    │       Prompt: "Convert this Jira description to a Confluence page in Markdown..."
    ├── Write draft to out/confluence-<KEY>.md
    ├── If [confluence] configured:
    │       ├── ConfluencePublisher.create_page(space, title, body_markdown)
    │       │       Uses Confluence Storage Format or Markdown macro
    │       ├── Record page_url in jira_work_sessions.raw_snapshot['confluence_page_url']
    │       └── Post Jira comment: "/whilly-published: <page_url>"
    └── Emit events row: event_type='CONFLUENCE_PUBLISHED', payload={url, space, page_id}
```

`ConfluencePublisher` makes two REST calls: `GET /rest/api/content?title=<title>&spaceKey=<space>` (idempotency check) then `POST /rest/api/content` or `PUT /rest/api/content/<id>` (create/update). The same `urllib.request` + no-external-deps approach used by `sources/jira.py` is used here.

### 6.7 TUI Interactive Flow

`JiraTuiCommand` wraps the existing Rich TUI (`whilly/cli/tui.py`) with a pre-flight intake screen:

```
Screen 1 — Issue Summary
  ┌─────────────────────────────────────────────────────┐
  │ Jira: ABC-123                                        │
  │ "Automate regression suite for payments API"         │
  │ Type: Task  Priority: High  Classification: feature  │
  │ Flow: feature_prd  Confidence: high                  │
  │ Repo: gitlab:qa/autotests (from project map)         │
  └─────────────────────────────────────────────────────┘
  [1] PRD/context   [2] Plan preflight   [3] Run autonomous   [4] Interactive   [Q] Quit

Screen 2 (on [4]) — Live TUI (existing OperatorSurface.OVERVIEW filtered to this plan)
  (reuses whilly/cli/tui.py render pipeline; adds ESC → back to Screen 1)
```

Screen 1 is rendered by a new `render_intake_summary(snapshot, project_map_result)` function (pure Rich). It calls `collect_jira_work_snapshot` (existing) and `ProjectMapResolver.resolve(key)` (new). Both are async; the screen shows a spinner while they run.

---

## 7. Dependencies

| Dependency | Type | Notes |
|---|---|---|
| `asyncpg` ≥ 0.29 | Runtime (already required) | Scheduler poll loop runs in async context |
| `rich` ≥ 13 | Runtime (already required) | TUI screens |
| `httpx` or `urllib.request` | Runtime | Confluence REST client; stdlib preferred (consistent with Jira source) |
| Postgres 14+ | Infrastructure | `scheduler_rules`, `scheduler_poll_cycles` tables |
| Alembic | Build / deploy | Two new migration files |
| `whilly.sources.jira` | Internal | Scheduler reuses `fetch_single_jira_issue` + `JiraAuth` |
| `whilly.jira_watch` | Internal | Scheduler uses `collect_jira_work_snapshot` for change detection |
| `whilly.adapters.db.repository` | Internal | `upsert_work_intent_from_jira` |
| Jira REST API | External | Search endpoint: `GET /rest/api/3/search?jql=<JQL>&fields=...` |
| Confluence REST API | External (optional) | `GET/POST/PUT /rest/api/content`; only needed for documentation flow |

---

## 8. Milestones and Phases

### Phase 1 — Interactive TUI for Single Issue (2 weeks)

**Goal:** Complete Epic A. An operator can run `whilly jira tui ABC-123` and get the full intake-through-execution TUI experience.

**Tasks:**

| Task | Module | Acceptance Criteria |
|---|---|---|
| TASK-SCH-001 | `whilly/cli/jira_tui.py` | `whilly jira tui ABC-123` launches Rich screen showing issue summary + 4 action options. Works in headless fallback (prints JSON). |
| TASK-SCH-002 | `whilly/cli/jira_tui.py` | Action [1] PRD writes context markdown; action [2] Plan runs `plan apply --strict` + `plan triz`. Same logic as `_run_intake_plan_preflight`. |
| TASK-SCH-003 | `whilly/cli/jira_tui.py` | Action [3] autonomous: imports to DB, starts worker, transitions to live OperatorSurface.OVERVIEW filtered by plan_id. |
| TASK-SCH-004 | `whilly/cli/jira_tui.py` | Action [4] interactive: opens existing TUI in plan-scoped mode. Hotkeys p/q/l/t work unchanged. ESC returns to Screen 1. |
| TASK-SCH-005 | `whilly/cli/jira.py` | `build_jira_parser` adds `tui` sub-command routing to `JiraTuiCommand`. Tests in `tests/test_jira_tui.py`. |
| TASK-SCH-006 | `tests/` | Unit tests for intake screen rendering (mocked snapshot + project map). Integration test: `whilly jira tui --repo-kind skip --action run` non-interactively. |

**Out of scope in Phase 1:** scheduler, project map, MCP profiles.

### Phase 2 — Project Map and Repository Configuration (1.5 weeks)

**Goal:** Complete Epic C. `whilly jira intake` and the TUI resolve repo targets automatically from config.

**Tasks:**

| Task | Module | Acceptance Criteria |
|---|---|---|
| TASK-SCH-010 | `whilly/project_config/project_map.py` | `ProjectMapResolver.resolve(key)` returns `RepoTarget | None`. Matches on project key first, then label patterns. |
| TASK-SCH-011 | `whilly/project_config/loader.py` | Loads `[project_map.*]` from `whilly.toml`; validates required fields. Error on unknown keys. |
| TASK-SCH-012 | `whilly/cli/jira.py` | `_resolve_intake_repo_choice` calls `ProjectMapResolver` before prompting. If resolved, skip prompt; print `"repo_target=<id> (from project_map)"`. |
| TASK-SCH-013 | `whilly/cli/jira_tui.py` | Screen 1 shows resolved repo target with source label `(project_map)` or `(manual)`. |
| TASK-SCH-014 | `whilly/cli/__main__.py` | Add `whilly project-map show <KEY>` command. |
| TASK-SCH-015 | `tests/` | Unit tests for project-map resolution (project key match, label match, no match). |

### Phase 3 — JQL Scheduler Core (3 weeks)

**Goal:** Complete Epic B. At least one JQL scheduler rule runs continuously, imports matching issues, and deduplicates correctly.

**Tasks:**

| Task | Module | Acceptance Criteria |
|---|---|---|
| TASK-SCH-020 | `whilly/adapters/db/migrations/` | Migration adds `scheduler_rules`, `scheduler_poll_cycles`; `work_intents` gains `scheduler_rule_name`, `queued_at`. |
| TASK-SCH-021 | `whilly/adapters/db/scheduler_repository.py` | `upsert_work_intent_from_jira`: atomic upsert returning `is_insert` + `content_changed`. Test: 100 concurrent calls for same key → exactly 1 plan created. |
| TASK-SCH-022 | `whilly/scheduler/models.py` | `SchedulerRule` dataclass; `SchedulerRuleConfig.from_toml` parser. |
| TASK-SCH-023 | `whilly/scheduler/jql_scheduler.py` | `JqlScheduler.run_one_cycle()`: calls Jira search API, iterates results, calls `upsert_work_intent_from_jira`, respects `max_inflight`. |
| TASK-SCH-024 | `whilly/scheduler/engine.py` | `SchedulerEngine`: starts multiple `JqlScheduler` as asyncio Tasks with `poll_interval` sleep. Handles shutdown gracefully on SIGINT/SIGTERM. |
| TASK-SCH-025 | `whilly/cli/__main__.py` | `whilly scheduler start` launches `SchedulerEngine`. `whilly scheduler list/enable/disable/status` CRUD via DB + HTTP. |
| TASK-SCH-026 | `whilly/adapters/transport/server.py` | `/scheduler/rules` GET+POST+PATCH, `/scheduler/status` GET endpoints. |
| TASK-SCH-027 | `tests/` | Integration test: two rules with overlapping JQL → zero duplicate plans after 3 poll cycles. Test `replan_on_change` branch. |

### Phase 4 — Documentation Flow and Confluence Publishing (2 weeks)

**Goal:** Complete Epic D. Issues classified as `documentation` produce Confluence pages automatically.

**Tasks:**

| Task | Module | Acceptance Criteria |
|---|---|---|
| TASK-SCH-030 | `whilly/jira_work.py` | Add `documentation` kind to `WORK_KINDS`, classifier, and recommended flow `documentation_publish`. Tests: 10 documentation-signal cases in `test_jira_work.py`. |
| TASK-SCH-031 | `whilly/adapters/confluence/publisher.py` | `ConfluencePublisher.create_page(space, title, body)` — stdlib-only REST client. `get_page_by_title` idempotency check. |
| TASK-SCH-032 | `whilly/workflow/documentation.py` | `DocumentationFlow.run`: generate draft via LLM, write to disk, call publisher if configured, post Jira comment. |
| TASK-SCH-033 | `whilly/adapters/db/repository.py` | `record_confluence_publish(issue_key, page_url, space_key, page_id)` — writes to `jira_work_sessions.raw_snapshot`. |
| TASK-SCH-034 | `whilly/cli/jira.py` | Scheduler and intake route `documentation` kind to `DocumentationFlow` instead of standard plan-creation path. |
| TASK-SCH-035 | `tests/` | Unit tests for `DocumentationFlow` (mock Confluence, mock Jira comment). Integration test: end-to-end with Confluence mock server. |

### Phase 5 — MCP Profiles and External Tool Routing (1.5 weeks)

**Goal:** Complete Epic E. Operators can attach MCP profiles to scheduler rules; agent prompts include the tool list.

**Tasks:**

| Task | Module | Acceptance Criteria |
|---|---|---|
| TASK-SCH-040 | `whilly/project_config/mcp_profiles.py` | `McpProfileRegistry.load_from_config()` + `McpProfileRegistry.resolve(profile_name, global_profile)` with merge logic. |
| TASK-SCH-041 | `whilly/project_config/loader.py` | Load `[mcp_profile.*]` sections. Validate server `name`, `command`/`url` presence. |
| TASK-SCH-042 | `whilly/core/prompts.py` | `build_task_prompt` accepts optional `mcp_profile: list[McpServerDef]`; appends `## Available Tools` section when non-empty. |
| TASK-SCH-043 | `whilly/scheduler/jql_scheduler.py` | Pass resolved MCP profile from `SchedulerRule` through to plan creation and agent dispatch. |
| TASK-SCH-044 | `whilly/cli/__main__.py` | `whilly skill list` command: discovers skills from `~/.claude/skills/` and configured MCP servers; prints name + trigger patterns. |
| TASK-SCH-045 | `tests/` | Unit test: prompts with and without MCP profile. Integration test: profile from config file passes through to agent prompt. |

---

## 9. Potential Challenges and Mitigations

| Challenge | Risk | Mitigation |
|---|---|---|
| Jira search API rate limits | Medium: a scheduler with short `poll_interval` and large result set hits per-minute limits | Add `poll_interval` floor of 60s; respect `Retry-After` header; exponential backoff in `JqlScheduler.run_one_cycle()` on 429 |
| `jira_work_sessions` content hash drift | Low: schema field `combined_hash` is SHA-256 over summary+description+links; minor whitespace changes trigger spurious `CONTENT_CHANGED` | Normalize whitespace before hashing; add `replan_on_change = false` default; require explicit opt-in |
| Confluence Markdown rendering | Medium: Confluence Storage Format is not standard Markdown; complex tables and code blocks may not render correctly | Phase 4 uses Confluence's `wiki` markup macro as a safe fallback; advanced formatting is a future enhancement |
| Multiple scheduler processes on same DB | Medium: running `whilly scheduler start` twice would double-fire poll cycles | Add `scheduler_rules.locked_by` column (nullable worker_id) with `SELECT FOR UPDATE SKIP LOCKED` on rule claim; Phase 3 mitigates by documenting single-instance deployment |
| `[project_map]` label matching performance | Low: label match scans all label patterns on every intake; number of rules is small in practice | `ProjectMapResolver` builds a compiled regex set at init time; benchmarks show <1ms for 1000 rules |
| Confluence page idempotency on retry | Low: network error mid-publish could cause orphaned partial pages | `ConfluencePublisher.create_page` checks `GET /content?title=<title>` first; only creates if not found; `raw_snapshot['confluence_page_url']` acts as a publish-once guard |

---

## 10. Future Extensions

- **Jira Webhook Source:** replace polling with webhook delivery to a new `POST /webhooks/jira` endpoint; eliminates latency between Jira update and Whilly intake.
- **Multi-Server Jira Support:** `[[jira_server]]` config table mapping project-key prefixes to separate auth blocks.
- **Confluence → Whilly reverse sync:** watch Confluence page edits and create Jira subtasks for follow-up work.
- **Scheduler Web UI:** extend the existing Web UI dashboard (`whilly/api/`) with a scheduler management tab showing rule status, poll history, and matched issues.
- **Natural Language Rule Authoring:** `whilly scheduler add "issues ready for automation in the QA project"` → LLM suggests JQL, operator confirms.
- **Per-Issue Budget Caps:** derive `budget_usd` from Jira story points or priority so high-priority issues get more LLM budget.
- **GitLab MR as Documentation Target:** documentation flow publishes to GitLab Wiki instead of Confluence when `[confluence]` is absent and repo target is a GitLab project.

---

## 11. Appendix A — Existing Whilly Components Referenced

| Component | Location | Role in this PRD |
|---|---|---|
| `TaskRepository` | `whilly/adapters/db/repository.py` | Extended with `upsert_work_intent_from_jira` |
| `work_intents` table | `whilly/adapters/db/schema.sql` | Deduplication anchor; `(origin_system, origin_ref)` unique index |
| `jira_work_sessions` table | `whilly/adapters/db/schema.sql` | Stores content hashes for change detection |
| `classify_jira_work` | `whilly/jira_work.py` | Extended with `documentation` kind |
| `fetch_single_jira_issue` | `whilly/sources/jira.py` | Reused by scheduler for issue payload fetch |
| `collect_jira_work_snapshot` | `whilly/jira_watch.py` | Reused for change detection in scheduler |
| `build_task_prompt` | `whilly/core/prompts.py` | Extended with `mcp_profile` parameter |
| `WhillyConfig.from_env()` | `whilly/config.py` | `[scheduler]`, `[project_map]`, `[mcp_profile.*]`, `[confluence]` sections added via `load_layered()` |
| `OperatorSurface` / `fetch_operator_snapshot` | `whilly/operator_views.py` | Reused in TUI Screen 2 |
| `ProjectMapEntry` / `McpServerDef` | `whilly/project_config/models.py` (new fields) | Config model classes |
| `parse_jira_key` | `whilly/sources/jira.py` | Key normalization in all new entry points |
| `jira_context_hashes` | `whilly/jira_work.py` | Content-hash computation for change detection |

---

## 12. Appendix B — whilly.toml Full Example (Post-PRD)

```toml
[jira]
server_url   = "https://jira.example.com"
username     = "mvschegole"
token        = "env:JIRA_API_TOKEN"
verify_ssl   = false
auth_scheme  = "bearer"

[confluence]
server_url    = "https://wiki.example.com"
username      = "mvschegole"
token         = "env:CONFLUENCE_API_TOKEN"
default_space = "QA"
parent_page_id = "9876543"

[[scheduler]]
name           = "qa-ready-for-auto"
jql            = "project = QA AND status = 'Ready for Automation' ORDER BY priority DESC"
poll_interval  = 300
max_inflight   = 2
mcp_profile    = "qa-tools"
replan_on_change = false

[[scheduler]]
name          = "docs-watch"
jql           = "project = QA AND issuetype = Documentation AND status = 'In Progress'"
poll_interval = 600
max_inflight  = 1

[project_map.QA]
repo_target     = "gitlab:qa/autotests"
default_branch  = "main"
verify_commands = ["python -m pytest -q tests/smoke"]

[project_map.DEMO]
repo_target    = "gitlab:demo/backend"
default_branch = "develop"

[mcp_profile.qa-tools]
[[mcp_profile.qa-tools.servers]]
name    = "allure"
command = ["python", "-m", "whilly_skills.allure_mcp"]
env     = { ALLURE_URL = "http://allure.internal:8080" }

[[mcp_profile.qa-tools.servers]]
name    = "jira-read"
command = ["python", "-m", "whilly_skills.jira_read_mcp"]
```
