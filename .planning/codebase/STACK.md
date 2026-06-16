# Technology Stack

**Analysis Date:** 2026-06-10

## Languages

**Primary:**
- Python 3.12+ - Core orchestrator and control plane; entry points in `whilly/cli/__init__.py` and `whilly/cli/worker.py`
- SQL (PostgreSQL dialect) - Alembic migrations in `whilly/adapters/db/migrations/versions/`
- HTML + Jinja2 - Server-rendered dashboard templates in `whilly/api/templates/`
- JavaScript - Minimal client-side (HTMX 1.9.12 + SSE extensions from CDN)

## Runtime

**Environment:**
- Python 3.12 or 3.13 (pyproject.toml requires `>=3.12`)
- CPython (verified on darwin + linux via multi-arch Docker)

**Package Manager:**
- pip + setuptools
- Lockfile: Yes (via `pyproject.toml` with pinned versions, though no `requirements.txt`)
- Installation entry points: `whilly` (main CLI) and `whilly-worker` (remote worker subprocess)

## Frameworks

**Core:**
- FastAPI 0.110+ - Control plane HTTP server; routes defined across `whilly/api/` modules (tasks, workers, auth, metrics)
- SQLAlchemy 2.0+ with asyncio - ORM for Postgres async access via `whilly/adapters/db/`
- Uvicorn 0.27+ with standard extras - ASGI server for FastAPI

**Database:**
- Alembic 1.13+ - Migration tooling; migrations apply via `alembic upgrade head`
- asyncpg 0.29+ - Async PostgreSQL driver; only used in `[server]` extra, not in remote workers

**Async/Concurrency:**
- Built-in asyncio - Event loop for concurrent claims, flushing, metrics refresh
- Greenlet (via SQLAlchemy[asyncio]) - Env driver support for Alembic sync context in async migrations

**LLM Agent Execution:**
- Claude CLI (external subprocess) - Wraps `claude --output-format stream-json` via `whilly/agents/claude.py`; runs agents with tool permissions denied by default since v4.7.0
- OpenCode CLI (optional, pluggable) - Alternative backend via `WHILLY_AGENT_BACKEND=opencode`

**Testing:**
- pytest 8.0+ - Test runner; config in `[tool.pytest.ini_options]`
- pytest-asyncio 0.23+ - Async test support
- pytest-xdist 3.6+ - Parallel test execution
- testcontainers 4.0+ - Ephemeral Postgres containers for integration tests
- freezegun 1.4+ - Time-travel mocking for schedule tests

**Code Quality:**
- ruff 0.11.5 - Linter + formatter (line length 120, target py312)
- mypy 1.11+ - Type checker with strict mode enforced for `whilly/core/`
- import-linter 2.0+ - Dependency graph validation; `.importlinter` contract pins `whilly.core` as zero-dependency pure domain layer

## Key Dependencies

**Critical:**
- Pydantic 2.6+ - Schema validation; used for task/plan/worker DTOs and wire protocol in `whilly/adapters/transport/schemas.py`
- Typer 0.12+ - CLI argument parser for v4 sub-commands (`whilly plan`, `whilly run`, etc.)
- Rich 13.0+ - TUI dashboard rendering and log formatting
- httpx 0.27+ - Async HTTP client for remote workers only (isolated in `[worker]` extra)
- psutil 5.9.0+ - Resource monitoring (CPU, memory, disk) in legacy v3 compat code
- keyring 24.0+ - OS credential storage integration for secrets
- platformdirs 4.0+ - Cross-platform config directory resolution (`~/.config/whilly/` on Linux)

**Infrastructure:**
- Prometheus client 0.20+ - Metrics exposition; exposed at `GET /metrics` (bearer-gated)
- prometheus-fastapi-instrumentator 7.1+ - Auto-instrumentation of HTTP routes
- sse-starlette 2.0+ - Server-sent events for live dashboard stream at `GET /events/stream`
- Jinja2 3.1+ - Template rendering for HTML dashboard

**Optional LLM Ops Export:**
- opentelemetry-api 1.28+ - Tracing instrumentation
- opentelemetry-sdk 1.28+ - SDK for local trace collection
- opentelemetry-exporter-otlp-proto-http 1.28+ - OTLP/HTTP exporter for Phoenix, Langfuse, or generic collectors
- traceloop-sdk 0.41+ - Instrumentation helpers for LLM frameworks

**Optional Second-Factor Auth:**
- pyotp 2.9+ - TOTP RFC 6238 implementation (enabled via `WHILLY_TOTP_ENABLED=1`)
- webauthn 2.0+ - WebAuthn/passkeys ceremonialization (enabled via `WHILLY_WEBAUTHN_ENABLED=1`)

**Optional Email:**
- aiosmtplib 3.0+ - Async SMTP for magic-link delivery in auth routes; falls back to event log when unset (`whilly/api/mailer.py`)

**External Service SDKs:**
- None vendored; integrations use stdlib `urllib.request` (GitHub, Jira) or `subprocess` (GitHub CLI, Git)

## Configuration

**Environment:**
- Dotenv-style `.env` file supported via `WhillyConfig.load_dotenv()` in `whilly/config.py`
- Layered config: defaults (dataclass) < user TOML (`~/.config/whilly/config.toml`) < repo TOML (`./whilly.toml`) < `.env` < shell env < CLI flags
- Secret references in TOML: `env:VAR`, `keyring:service/account`, `file:/path/to/secret`

**Build:**
- `pyproject.toml` - Package metadata, dependencies, and tool configs
- `.importlinter` - Dependency graph contract (core-purity: `whilly.core` has zero external deps)
- `ruff.toml` (inline in `[tool.ruff]`) - Linter config; line-length 120, target py312
- `alembic.ini` + `alembic/` - Migration configuration and revision chain

**Development Tools:**
- git - Version control; migrations and worktree management via `subprocess`
- Bash scripting - Demo/test harnesses (`workshop-demo.sh`, `docs/demo-remote-worker.sh`)
- Docker + docker-compose - Multi-host deployment; images published as `mshegolev/whilly:4.7.0` (multi-arch: linux/amd64 + linux/arm64)

## Platform Requirements

**Development:**
- Python 3.12+ interpreter
- PostgreSQL 15+ (via Docker or local install)
- Claude CLI on `$PATH` (or override with `CLAUDE_BIN`) for agent execution
- Git + GitHub CLI (`gh`) for GitHub source/sink integration
- Optional: Docker for testcontainers and multi-host demo

**Production:**
- PostgreSQL 15+ - Single source of truth for plans, tasks, workers, events
- Uvicorn-compatible ASGI server (shipped in `[server]` extra)
- Claude CLI available on the worker box
- Optional: Postgres NOTIFY listener for event flushing (uses dedicated connection outside asyncpg pool)
- Optional: SMTP relay for magic-link delivery (aiosmtplib, falls back to event log)
- Optional: OpenTelemetry collector or Langfuse/Phoenix for LLM ops export

---

*Stack analysis: 2026-06-10*
