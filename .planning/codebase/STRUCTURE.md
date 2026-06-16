# Codebase Structure

**Analysis Date:** 2026-06-10

## Directory Layout

```
whilly-orchestrator/
├── whilly/                      # Main package (version in __init__.py, sync with pyproject.toml)
│   ├── __init__.py              # Version = "4.7.0"
│   ├── __main__.py              # Entry for `python -m whilly`
│   │
│   ├── cli/                     # Subcommand entry points (user-facing shell commands)
│   │   ├── __init__.py          # main() router (legacy flag shim)
│   │   ├── plan.py              # `whilly plan import|export|show|triz|reset`
│   │   ├── run.py               # `whilly run --plan <plan_id>` (local worker entry)
│   │   ├── init.py              # `whilly init "problem"` (PRD wizard + task generation)
│   │   ├── worker.py            # `whilly worker --bootstrap-token` (remote worker entry)
│   │   ├── server.py            # `whilly server` (FastAPI control plane)
│   │   ├── dashboard.py         # `whilly dashboard --plan <id>` (TUI operator surface)
│   │   ├── jira.py              # `whilly jira import|watch` (Jira integration)
│   │   ├── github_projects.py   # `whilly github-projects` (Project sync)
│   │   ├── qa_release.py        # `whilly qa-release` (QA automation)
│   │   ├── admin.py             # `whilly admin` (system administration)
│   │   ├── skill.py             # `whilly skill` (Serena/GSD integration)
│   │   ├── tui.py               # `whilly tui` (rich-based operator console)
│   │   ├── project_config.py    # `whilly project-config` (profile management)
│   │   ├── project_map.py       # `whilly project-map` (codebase analysis)
│   │   ├── rollback.py          # `whilly rollback` (recovery operations)
│   │   ├── update.py            # `whilly update` (self-update)
│   │   ├── compliance.py        # `whilly compliance` (audit surface)
│   │   ├── feedback.py          # `whilly feedback` (PR feedback harvesting)
│   │   ├── quick_setup.py       # `whilly quick-setup` (onboarding wizard)
│   │   ├── scheduler.py         # `whilly scheduler` (job scheduling)
│   │   ├── worker_launch.py     # Remote worker launcher (system integration)
│   │   └── __main__.py          # `python -m whilly.cli`
│   │
│   ├── core/                    # Pure domain logic (zero external dependencies)
│   │   ├── __init__.py
│   │   ├── models.py            # Task, Plan, TaskId, TaskStatus, Priority, ...
│   │   ├── gates.py             # Decision gate logic (REJECT/ALLOW/SKIP verdicts)
│   │   ├── triz.py              # TRIZ contradiction analyzer
│   │   ├── scheduler.py         # detect_cycles(), dependency readiness logic
│   │   ├── state_machine.py     # Task status transition rules
│   │   ├── task_id.py           # Task ID format validation
│   │   ├── notifications.py     # Notification event models
│   │   ├── prompts.py           # Agent prompt templates
│   │   ├── governance.py        # Policy & rule engine
│   │   ├── agent_runner.py      # Abstract agent execution protocol
│   │   └── __main__.py
│   │
│   ├── adapters/                # I/O boundaries & external system integration
│   │   ├── __init__.py
│   │   │
│   │   ├── db/                  # Postgres persistence layer
│   │   │   ├── __init__.py
│   │   │   ├── pool.py          # asyncpg pool factory
│   │   │   ├── repository.py    # TaskRepository (claim, complete, fail, heartbeat)
│   │   │   ├── schema.sql       # Database schema (tables, indexes, constraints)
│   │   │   └── migrations/      # Alembic migrations (000..020+)
│   │   │
│   │   ├── filesystem/          # File I/O for plans, state, logs
│   │   │   ├── __init__.py
│   │   │   ├── plan_io.py       # parse_plan(), serialize_plan()
│   │   │   ├── state_io.py      # State file (.whilly_state.json)
│   │   │   └── logs.py          # JSONL event writing
│   │   │
│   │   ├── runner/              # Agent execution backends
│   │   │   ├── __init__.py
│   │   │   ├── claude_cli.py    # run_task() — Claude CLI subprocess wrapper
│   │   │   ├── result_parser.py # Parse Claude JSON output → AgentResult
│   │   │   ├── proxy.py         # LLM proxy/interceptor
│   │   │   ├── claude_anonymizer_proxy.py  # Sensitive data scrubbing
│   │   │   ├── anonymizer.py    # Anonymization rules
│   │   │   └── env.py           # Environment variable management
│   │   │
│   │   ├── transport/           # HTTP API & client (shared wire contracts)
│   │   │   ├── __init__.py      # Lazy imports (worker purity enforcement)
│   │   │   ├── schemas.py       # Pydantic models (ClaimRequest, ClaimResponse, ...)
│   │   │   ├── auth.py          # FastAPI bearer auth dependency
│   │   │   ├── server.py        # FastAPI app factory, routes
│   │   │   ├── client.py        # httpx-based RemoteWorkerClient
│   │   │   └── exceptions.py    # HTTPClientError, VersionConflictError, ...
│   │   │
│   │   ├── notifications/       # Integration with notification services
│   │   │   ├── __init__.py
│   │   │   ├── slack.py         # Slack task notifications
│   │   │   ├── email.py         # Email notifications
│   │   │   └── webhook.py       # Generic webhook dispatcher
│   │   │
│   │   └── confluence/          # Confluence documentation integration
│   │       └── ...
│   │
│   ├── api/                     # FastAPI HTTP API (control plane)
│   │   ├── __init__.py
│   │   ├── main.py              # create_app() factory + log_event() helper
│   │   ├── event_flusher.py     # Async event batch flusher (v4.6.1+)
│   │   ├── auth_routes.py       # /auth/* endpoints (login, logout, password change)
│   │   ├── auth_tokens.py       # Bearer token generation & validation
│   │   ├── sessions.py          # Session management (HTTPOnly cookies)
│   │   ├── csrf.py              # CSRF token protection
│   │   ├── oidc_header_auth.py  # OIDC header-based auth (reverse proxy flow)
│   │   ├── must_change_gate.py  # Force password change gate (Finding 6)
│   │   ├── admin_users_routes.py # /admin/users/* (user management)
│   │   ├── users_repo.py        # User persistence & queries
│   │   ├── auth_audit_repo.py   # Login audit trail
│   │   ├── dashboard_token.py   # Dashboard access tokens
│   │   ├── dashboard.py         # Web dashboard (HTMX + Jinja2 templates)
│   │   ├── plans_api.py         # /api/v1/plans/* (plan CRUD)
│   │   ├── tasks_api.py         # /api/v1/tasks/* (task listing)
│   │   ├── tasks_api_crud.py    # Task CREATE/UPDATE/DELETE operations
│   │   ├── sse.py               # Server-sent events (SSE) broker
│   │   ├── sse_endpoint.py      # GET /events/stream endpoint
│   │   ├── metrics.py           # Prometheus /metrics endpoint
│   │   ├── mailer.py            # aiosmtplib SMTP integration (magic links)
│   │   ├── rate_limit.py        # Rate limiting middleware
│   │   ├── route_audit.py       # Per-route audit logging
│   │   ├── static_mount.py      # Static file serving (/static/*)
│   │   ├── totp_routes.py       # /auth/totp/* (RFC 6238 second factor)
│   │   ├── totp_repo.py         # TOTP secret storage & validation
│   │   ├── webauthn_routes.py   # /auth/webauthn/* (passkey/fido2)
│   │   ├── webauthn_repo.py     # Passkey credential storage
│   │   ├── webauthn_challenge_repo.py # Challenge generation & validation
│   │   ├── second_factor.py     # Multi-factor logic (TOTP + WebAuthn)
│   │   ├── prod_mode.py         # Production-mode constraints
│   │   │
│   │   ├── templates/           # Jinja2 HTML templates (HTMX)
│   │   │   ├── base.html
│   │   │   ├── dashboard.html
│   │   │   ├── login.html
│   │   │   ├── tasks.html
│   │   │   ├── workers.html
│   │   │   ├── ...
│   │   │
│   │   └── static/              # Static assets
│   │       ├── htmx.min.js
│   │       ├── style.css
│   │       └── ...
│   │
│   ├── worker/                  # Task executor (local & remote)
│   │   ├── __init__.py
│   │   ├── main.py              # Worker loop supervisor
│   │   ├── local.py             # run_local_worker() — sync Postgres consumer
│   │   ├── remote.py            # RemoteWorker — HTTP long-poll client
│   │   └── funnel.py            # Funnel URL discovery (v4.6.1+)
│   │
│   ├── sources/                 # Task source adapters (GitHub, Jira, etc.)
│   │   ├── __init__.py
│   │   ├── github_issues.py     # fetch_github_issues() → normalize to tasks
│   │   ├── github_issues_and_project.py
│   │   ├── github_pr_feedback.py # Harvest feedback from PR comments
│   │   └── jira.py              # fetch_single_jira_issue() → normalize
│   │
│   ├── sinks/                   # Post-completion action adapters
│   │   ├── __init__.py
│   │   ├── github_pr.py         # open_pr_for_task() — create & push commit
│   │   ├── gitlab_mr.py         # GitLab merge request creation
│   │   └── post_complete_pr_hook.py  # Hook runner & gate (Finding 7)
│   │
│   ├── audit/                   # Immutable event logging
│   │   ├── __init__.py
│   │   ├── event_sink.py        # JsonlEventSink (JSONL file writer)
│   │   └── models.py            # Event model for audit trail
│   │
│   ├── pipeline/                # Post-execution verification & gates
│   │   ├── __init__.py
│   │   ├── verification.py      # resolve_verification_specs() + run_verification_commands()
│   │   └── models.py            # Verification model
│   │
│   ├── ci/                      # CI/CD integration
│   │   ├── __init__.py
│   │   ├── github.py            # GitHub CI polling (check_run, status)
│   │   ├── gitlab.py            # GitLab CI polling
│   │   └── models.py            # CI verification models
│   │
│   ├── project_config/          # Project-aware pipeline stages (profiles)
│   │   ├── __init__.py
│   │   ├── loader.py            # Load project config YAML/JSON
│   │   ├── models.py            # ProjectProfile, PipelineStage, ...
│   │   └── sink_stages.py       # Configured sink stages
│   │
│   ├── workflow/                # GitHub & Jira workflows
│   │   ├── __init__.py
│   │   ├── github.py            # GitHub workflow operations
│   │   ├── pr_iterate.py        # PR iteration loops
│   │   ├── base.py              # Base workflow types
│   │   ├── analyzer.py          # Workflow analysis
│   │   ├── proposer.py          # Change proposal logic
│   │   ├── mapper.py            # Repository mapper
│   │   ├── documentation.py     # Documentation generation
│   │   ├── sync.py              # Workflow synchronization
│   │   └── registry.py          # Workflow registry
│   │
│   ├── quality/                 # Quality gates & checks
│   │   ├── __init__.py
│   │   ├── gate.py              # Quality gate evaluator
│   │   ├── models.py            # Quality models
│   │   └── ...
│   │
│   ├── classifier/              # Task classification (type, domain, ...)
│   │   ├── __init__.py
│   │   └── ...
│   │
│   ├── security/                # Security & hardening features
│   │   ├── __init__.py
│   │   ├── traversal.py         # Path traversal defense (ADR §P1.13)
│   │   ├── models.py            # Security models
│   │   └── ...
│   │
│   ├── repair/                  # Automated recovery & repair
│   │   ├── __init__.py
│   │   └── ...
│   │
│   ├── rollback/                # Rollback & recovery operations
│   │   ├── __init__.py
│   │   └── ...
│   │
│   ├── scheduler/               # Job scheduling (v4.6+)
│   │   ├── __init__.py
│   │   ├── models.py
│   │   ├── runner.py
│   │   └── ...
│   │
│   ├── mcp/                     # MCP client integration
│   │   ├── __init__.py
│   │   ├── client.py
│   │   └── tools.py
│   │
│   ├── forge/                   # Forge/PRD intake integration
│   │   ├── __init__.py
│   │   └── ...
│   │
│   ├── agents/                  # Pluggable agent backends
│   │   ├── __init__.py
│   │   ├── claude.py            # Claude backend
│   │   ├── opencode.py          # OpenCode backend
│   │   └── ...
│   │
│   ├── compliance/              # Compliance & audit features
│   │   ├── __init__.py
│   │   └── ...
│   │
│   ├── qa_release/              # QA release automation
│   │   ├── __init__.py
│   │   └── ...
│   │
│   ├── hierarchy/               # Epic/story hierarchy for GitHub/Jira
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── github.py
│   │   └── jira.py
│   │
│   ├── config.py                # Global WhillyConfig (env var parsing)
│   ├── config_sections.py       # Config sections (auth, db, worker, ...)
│   ├── dashboard.py             # Rich-based TUI operator surface (deprecated)
│   ├── decision_gate.py         # Legacy decision gate (v3 compat)
│   ├── task_manager.py          # Legacy task JSON manager (v3 compat)
│   ├── state_store.py           # State persistence (.whilly_state.json)
│   ├── reporter.py              # Run completion report generation
│   ├── decomposer.py            # Task decomposition (mid-run splitting)
│   ├── log_viewer.py            # `whilly logs` viewer
│   ├── prd_generator.py         # Non-interactive PRD generation
│   ├── prd_wizard.py            # Interactive PRD wizard (Claude CLI)
│   ├── prd_launcher.py          # PRD launcher (plan executor)
│   ├── orchestrator.py          # Batch planning logic
│   ├── tmux_runner.py           # Tmux session management (deprecated)
│   ├── verifier.py              # Task verification (deprecated)
│   ├── agent_runner.py          # Agent execution dispatcher (deprecated)
│   ├── llm_ops.py               # LLM observability exports
│   ├── llm_otel.py              # OpenTelemetry instrumentation
│   ├── triz_analyzer.py         # TRIZ analysis launcher (deprecated)
│   ├── slack_task_notify.py     # Slack notification sender
│   ├── notifications.py         # Notification dispatcher
│   ├── resource_monitor.py      # Resource usage monitoring
│   ├── self_healing.py          # Self-healing recovery
│   ├── recovery.py              # Recovery operations
│   ├── update.py                # Self-update logic
│   ├── workspaces.py            # Workspace/worktree isolation
│   ├── worktree_runner.py       # Git worktree manager (deprecated)
│   ├── github_pr.py             # Legacy GitHub PR handling
│   ├── github_interactive.py    # Interactive GitHub operations
│   ├── github_projects.py       # GitHub Projects API integration
│   ├── github_converter.py      # GitHub format conversion
│   ├── gh_utils.py              # GitHub utilities
│   ├── jira_work.py             # Jira task operations
│   ├── jira_watch.py            # Jira event watching
│   ├── jira_board.py            # Jira board management
│   ├── project_board.py         # Project board abstraction
│   ├── feedback.py              # Feedback collection
│   ├── external_integrations.py # External service integrations
│   ├── history.py               # Execution history tracking
│   ├── operator_views.py        # Operator-facing data views
│   ├── pause_control.py         # Pause/resume control
│   ├── web_status.py            # Web status endpoint
│   ├── secrets.py               # Secret management & rotation
│   ├── doctor.py                # Diagnostic health checker
│   ├── py.typed                 # PEP 561 marker (type checking)
│   └── __pycache__/             # Compiled bytecode
│
├── whilly_worker/               # Remote worker console script (entry point)
│   └── __main__.py
│
├── tests/                       # Test suite
│   ├── __init__.py
│   ├── conftest.py              # Pytest fixtures (db, app, mock runners)
│   ├── fixtures/                # Reusable test data (plans, tasks, responses)
│   │   ├── __init__.py
│   │   ├── plans.py
│   │   ├── responses.py
│   │   └── ...
│   │
│   ├── integration/             # Integration tests (fast, single-process)
│   │   ├── test_plans_crud.py   # Plan import/export/cycle detection
│   │   ├── test_worker_*.py     # Worker loop tests
│   │   ├── test_*_routes.py     # API route tests
│   │   └── ...
│   │
│   ├── unit/                    # (Empty — unit tests live alongside sources)
│   ├── ui/                      # Web UI tests (selenium, playwright)
│   │
│   ├── test_*.py                # Unit tests (co-located with sources)
│   ├── test_auth_matrix.py      # Auth scenarios
│   ├── test_agent_backend_*.py  # Agent backend tests
│   ├── test_decision_gate.py    # Decision gate logic
│   ├── test_github_issues_source.py
│   ├── test_jira_full_cycle.py
│   ├── test_plans_crud.py       # Plan CRUD operations
│   ├── test_github_pr_sink.py   # PR creation sink
│   └── ...
│
├── .planning/                   # (Generated by orchestrator)
│   ├── codebase/                # This directory — analyzed by GSD agents
│   │   ├── ARCHITECTURE.md      # (You are reading this)
│   │   ├── STRUCTURE.md         # Codebase layout & naming
│   │   ├── CONVENTIONS.md       # Coding style & patterns
│   │   ├── TESTING.md           # Test framework & patterns
│   │   ├── STACK.md             # Technology & dependencies
│   │   ├── INTEGRATIONS.md      # External services
│   │   └── CONCERNS.md          # Technical debt & issues
│   ├── reports/                 # Plan execution reports (Markdown)
│   └── tasks.json               # Task list (auto-generated)
│
├── .whilly/                     # User-level state
│   └── config.toml              # Operator-level config
│
├── config/                      # Project configuration
│   ├── project.yaml             # Project profile definition
│   ├── whilly.yaml              # Whilly settings
│   └── ...
│
├── examples/                    # Demo & tutorial plans
│   ├── demo/
│   │   ├── tasks.json
│   │   └── ...
│   └── workshop/
│       └── ...
│
├── docs/                        # User & operator documentation
│   ├── Whilly-Usage.md          # CLI reference & env vars
│   ├── Whilly-Interfaces-and-Tasks.md  # API & module contracts
│   ├── LLM-OPS.md               # Observability & tracing
│   ├── SCHEDULER.md             # Job scheduling docs
│   ├── Project-Description.md   # Architecture overview
│   ├── adr/                     # Architecture Decision Records
│   ├── status/                  # Status dashboards & roadmap
│   ├── target/                  # Target (post-v5) documentation
│   ├── distributed-audit/       # Distributed audit trail docs
│   ├── superpowers/             # Advanced feature docs
│   └── assets/                  # Screenshots, diagrams
│
├── library/                     # Reference & baseline docs
│   ├── deferred-v6-hardening.md # Post-release hardening plans
│   ├── baselines/               # Test data baselines
│   └── ...
│
├── docker/                      # Docker & container configs
│   ├── Dockerfile.control-plane
│   ├── Dockerfile.worker
│   └── ...
│
├── scripts/                     # Automation & deployment
│   ├── funnel/                  # Funnel URL sidecar
│   └── ...
│
├── pyproject.toml               # Python package metadata & deps
├── CHANGELOG.md                 # Release notes
├── README.md                    # Quick start & overview
├── README-RU.md                 # Russian docs
├── .gitignore                   # Git exclusions
├── .importlinter                # Import boundary rules
├── docker-compose.demo.yml      # Demo environment
├── docker-compose.yml           # Production stack
├── Makefile                     # Common commands (lint, test, ...)
└── workshop-demo.sh             # Demo launcher script
```

## Directory Purposes

**`whilly/`:**
- Purpose: Main Python package. Exports `whilly` console script and `whilly-worker` remote entry point
- Contains: Core logic, adapters, CLI, API, worker loops
- Key files: `__init__.py` (version), `__main__.py` (entry point)

**`whilly/cli/`:**
- Purpose: User-facing shell commands (subcommands routed by `main()`)
- Contains: `plan`, `run`, `init`, `worker`, `server`, `dashboard`, `jira`, etc.
- Key files: `__init__.py` (router + legacy shim), `plan.py`, `run.py`

**`whilly/core/`:**
- Purpose: Pure domain logic with zero external dependencies
- Contains: Task/Plan models, decision gate, TRIZ analyzer, scheduler (cycle detection)
- Key files: `models.py`, `gates.py`, `triz.py`, `scheduler.py`

**`whilly/adapters/`:**
- Purpose: I/O boundaries and external system integration
- Contains: Database (asyncpg), filesystem (plans), HTTP transport (FastAPI + httpx), runners (Claude CLI), notifications
- Key subdirectories: `db/`, `filesystem/`, `transport/`, `runner/`, `notifications/`

**`whilly/adapters/db/`:**
- Purpose: Postgres persistence
- Contains: Pool factory, TaskRepository (query/claim/complete/fail), schema.sql, Alembic migrations
- Key files: `pool.py`, `repository.py`, `schema.sql`, `migrations/`

**`whilly/adapters/transport/`:**
- Purpose: HTTP API contract (shared schemas) and transport implementations
- Contains: Pydantic DTOs, FastAPI auth, server routes, httpx client
- Key files: `schemas.py`, `server.py`, `client.py`, `auth.py`

**`whilly/api/`:**
- Purpose: FastAPI control-plane HTTP endpoints and dashboard
- Contains: Auth routes (OIDC, magic-link, TOTP, WebAuthn), task/plan API, SSE event streaming, web dashboard
- Key files: `main.py`, `auth_routes.py`, `plans_api.py`, `dashboard.py`, `templates/`

**`whilly/worker/`:**
- Purpose: Task execution loops (local and remote)
- Contains: Local worker (sync Postgres consumer), remote worker (HTTP long-poll client), funnel URL discovery
- Key files: `local.py`, `remote.py`, `main.py`

**`whilly/sources/`:**
- Purpose: External task source adapters
- Contains: GitHub Issues, Jira, GitHub Projects, PRD feedback harvesters
- Key files: `github_issues.py`, `jira.py`, `github_pr_feedback.py`

**`whilly/sinks/`:**
- Purpose: Post-completion action adapters
- Contains: GitHub PR creation, GitLab MR creation, Slack notifications
- Key files: `github_pr.py`, `gitlab_mr.py`, `post_complete_pr_hook.py`

**`whilly/pipeline/`:**
- Purpose: Post-execution verification and gates
- Contains: Verification command runner (lint, test, CI polling), gate evaluation
- Key files: `verification.py`

**`whilly/audit/`:**
- Purpose: Immutable append-only event logging
- Contains: JSONL event writer, event models
- Key files: `event_sink.py`

**`whilly/project_config/`:**
- Purpose: Project-aware pipeline profiles and stages
- Contains: Project YAML loader, pipeline stage models, configured sink stages
- Key files: `loader.py`, `models.py`

**`whilly/workflow/`:**
- Purpose: GitHub & Jira workflow operations
- Contains: PR iteration loops, workflow analyzers, proposal generators
- Key files: `github.py`, `pr_iterate.py`

**`tests/`:**
- Purpose: Test suite (pytest-based)
- Contains: Integration tests, unit tests, fixtures, web UI tests
- Key files: `conftest.py` (fixtures), `test_*.py` (tests), `fixtures/` (reusable data)

**`docs/`:**
- Purpose: User and operator documentation
- Contains: Usage guides, API docs, architectural decisions, roadmap
- Key files: `Whilly-Usage.md`, `Whilly-Interfaces-and-Tasks.md`, `adr/`

**`config/`:**
- Purpose: Project-specific configuration
- Contains: Project profile YAML, Whilly settings, environment profiles
- Key files: `project.yaml`

## Key File Locations

**Entry Points:**
- `whilly/__main__.py`: `python -m whilly` entry point
- `whilly/cli/__init__.py:main()`: Console script `whilly` router
- `whilly_worker/__main__.py`: Console script `whilly-worker` (remote worker)
- `whilly/cli/server.py`: FastAPI control plane

**Configuration:**
- `whilly/config.py`: Global WhillyConfig (env var parser)
- `whilly/config_sections.py`: Config sections (auth, db, worker, API)
- `pyproject.toml`: Package metadata, dependencies
- `.env.example`: Example environment variables

**Core Logic:**
- `whilly/core/models.py`: Task, Plan, TaskId, TaskStatus models
- `whilly/core/gates.py`: Decision gate rules
- `whilly/core/scheduler.py`: Cycle detection, dependency readiness
- `whilly/core/triz.py`: TRIZ contradiction analyzer

**Database:**
- `whilly/adapters/db/pool.py`: asyncpg pool factory
- `whilly/adapters/db/repository.py`: TaskRepository (200KB+ — main DB operations)
- `whilly/adapters/db/schema.sql`: Complete DB schema
- `whilly/adapters/db/migrations/`: Alembic migration history

**API & Routes:**
- `whilly/adapters/transport/server.py`: FastAPI app factory (composition root)
- `whilly/api/main.py`: Public API surface + log_event() helper
- `whilly/api/auth_routes.py`: Authentication endpoints
- `whilly/api/plans_api.py`: Plan CRUD endpoints
- `whilly/api/tasks_api.py`: Task listing endpoint
- `whilly/api/sse_endpoint.py`: Event streaming endpoint

**Workers:**
- `whilly/worker/local.py`: Local worker loop (sync Postgres consumer)
- `whilly/worker/remote.py`: Remote worker loop (HTTP long-poll client)
- `whilly/adapters/runner/claude_cli.py`: Claude CLI subprocess wrapper

**Task Sources & Sinks:**
- `whilly/sources/github_issues.py`: GitHub Issues source
- `whilly/sources/jira.py`: Jira source
- `whilly/sinks/github_pr.py`: GitHub PR sink
- `whilly/sinks/post_complete_pr_hook.py`: Post-completion hook runner

**Testing:**
- `tests/conftest.py`: Pytest fixtures (db, app, runners)
- `tests/fixtures/`: Reusable test data (plans, responses)
- `tests/test_*.py`: Unit & integration tests

## Naming Conventions

**Files:**
- `*.py`: Python modules
- `*_routes.py`: FastAPI route handlers (e.g., `auth_routes.py`, `plans_api.py`)
- `*_repo.py` or `*_repository.py`: Database abstraction (e.g., `users_repo.py`)
- `*_sink.py`: Post-completion action (e.g., `github_pr_sink.py`)
- `*_source.py`: Task source adapter (e.g., `github_issues_source.py`)
- `test_*.py`: Test files (pytest discovers these)
- `conftest.py`: Pytest configuration & fixtures

**Directories:**
- `whilly/`: Main package (lowercase)
- `tests/`: Test suite (lowercase)
- `docs/`: Documentation
- `config/`: Configuration files
- `examples/`: Demo & tutorial data

**Python Naming (PEP 8):**
- **Modules & files:** `lowercase_with_underscores` (e.g., `claude_cli.py`)
- **Classes:** `PascalCase` (e.g., `TaskRepository`, `ClaimResponse`)
- **Functions:** `lowercase_with_underscores` (e.g., `create_pool()`, `run_task()`)
- **Constants:** `UPPERCASE_WITH_UNDERSCORES` (e.g., `EXIT_OK`, `DEFAULT_HEARTBEAT_INTERVAL`)
- **Private members:** `_lowercase_with_leading_underscore` (e.g., `_hash_token()`)

**Database:**
- **Tables:** `lowercase_plural` (e.g., `tasks`, `workers`, `events`, `plans`)
- **Columns:** `lowercase_with_underscores` (e.g., `task_id`, `created_at`, `claimed_by`)
- **Indexes:** `idx_table_columns` (e.g., `idx_tasks_plan_id_status`)
- **Migrations:** `###_description.py` (e.g., `001_initial_schema.py`, `011_pg_notify.py`)

**API Endpoints:**
- `/api/v1/...`: Versioned JSON API
- `/tasks/...`: Task operations (claim, complete, fail)
- `/workers/...`: Worker registry & heartbeat
- `/auth/...`: Authentication flows
- `/events/stream`: SSE endpoint
- `/health`: Health check
- `/metrics`: Prometheus metrics
- `/:id`: Dashboard (HTMX)

## Where to Add New Code

**New Feature (Bounded Scope):**
- Implementation: `whilly/core/` if domain logic, else `whilly/adapters/*/` for I/O
- Tests: `tests/test_*.py` (co-located unit tests) or `tests/integration/` (integration tests)
- Example: Adding task status "BLOCKED" → update `TaskStatus` enum in `whilly/core/models.py`, add transition rules in `whilly/core/state_machine.py`, update schema in `whilly/adapters/db/schema.sql` + Alembic migration, test in `tests/test_state_machine.py`

**New CLI Subcommand:**
- Implement in `whilly/cli/` as a new module (e.g., `whilly/cli/newcmd.py`)
- Add handler to router in `whilly/cli/__init__.py`
- Reference in `__all__` export
- Example: `whilly report` → create `whilly/cli/report.py:run_report_command()`, add to main router

**New API Endpoint:**
- Implementation: `whilly/api/` (e.g., `whilly/api/custom_routes.py`)
- Mount routes in `whilly/adapters/transport/server.py:create_app()`
- Wire auth dependency if needed (bearer, OIDC, session)
- Test in `tests/integration/test_custom_routes.py`

**New Database Operation:**
- Add method to `whilly/adapters/db/repository.py:TaskRepository`
- Update schema in `whilly/adapters/db/schema.sql` (or create Alembic migration if adding columns/tables)
- Example: New worker query → add `async def get_workers_by_status()` method + `SELECT * FROM workers WHERE status = $1`

**New Task Source:**
- Create `whilly/sources/newsource.py` with adapter class (e.g., `NewSourceAdapter`)
- Implement `fetch_tasks()` method returning list of `Task` objects
- Register in `whilly/sources/__init__.py`
- Example: `ForgePRDSource` → `whilly/sources/forge.py:fetch_forge_prd()`, export in `__init__.py`

**New Post-Completion Sink:**
- Create `whilly/sinks/newsink.py` with function `send_to_newsink(task, context)`
- Hook into post-completion flow in `whilly/worker/local.py:_run_post_complete_hooks()`
- Test in `tests/integration/test_newsink.py`
- Example: Slack sink → `whilly/sinks/slack.py:post_to_slack()`, called after `task.done` event

**Utilities & Helpers:**
- Shared helpers: `whilly/*/utils.py` (e.g., `whilly/adapters/runner/utils.py`)
- Common constants: top of relevant module or dedicated `constants.py`
- Example: Retry logic → `whilly/adapters/transport/client.py` (built-in); shared parser → `whilly/sources/utils.py`

## Special Directories

**`.planning/`:**
- Purpose: Generated by orchestrator (plan execution reports, analyzed codebase docs)
- Generated: Yes (by GSD agent commands)
- Committed: Yes (reports are artifacts; docs are checked in)

**`.whilly/`:**
- Purpose: User-level state and config
- Generated: Yes (CLI creates on first run)
- Committed: No (.gitignored)

**`.whilly_workspaces/`:**
- Purpose: Transient git worktrees for isolated task execution (opt-in via `WHILLY_USE_WORKSPACE=1`)
- Generated: Yes (on plan run)
- Committed: No (.gitignored)

**`whilly_logs/`:**
- Purpose: Run logs and event trail (JSONL)
- Generated: Yes (every run appends to `whilly_events.jsonl`)
- Committed: No (.gitignored)

**`docs/adr/`:**
- Purpose: Architecture Decision Records (immutable, time-stamped)
- Generated: No (manually authored by maintainers)
- Committed: Yes (source of truth for design decisions)

**`library/baselines/`:**
- Purpose: Test data baselines (golden files for regression tests)
- Generated: No (hand-curated)
- Committed: Yes (guards against unintended output changes)

---

*Structure analysis: 2026-06-10*
