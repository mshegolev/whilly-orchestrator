# External Integrations

**Analysis Date:** 2026-06-10

## APIs & External Services

**GitHub:**
- GitHub Issues source adapter - Fetches open issues via `gh` CLI; stored in `whilly/sources/github_issues.py`
  - Source: `gh issue list --label <label> --json title,body,number,state` (default label: `whilly:ready`)
  - Supports: Priority labels (e.g., `priority:critical`), issue state sync, and idempotent re-fetch
- GitHub Projects v2 integration - Syncs Project items to issues via `whilly/sources/github_issues_and_project.py`
  - CLI: `whilly github-projects sync-todo <url> --repo owner/repo`
  - Sync endpoints: `GET /projects/items`, status field updates via CLI
- GitHub PR sink - Opens pull requests on task completion via `whilly/sinks/github_pr.py`
  - Subprocess: `git push` + `gh pr create --draft` (if configured)
  - Stores PR result (URL, branch, SHA) in task completion event
- GitHub Hierarchy adapter - Maps GitHub issue refs to plan structure
  - File: `whilly/hierarchy/github.py`
- CI polling adapter - Long-polls GitHub Actions status for verification gates
  - File: `whilly/ci/github.py`
- Auth: GitHub CLI (`gh`) handles token via `~/.config/gh/hosts.yml`; token NOT read by Whilly directly

**Jira:**
- Jira source adapter - Fetches single issues via Atlassian REST API v3
  - File: `whilly/sources/jira.py`
  - Auth: Basic (base64 `username:api_token`); no requests library (uses stdlib `urllib.request`)
  - Config: `server_url`, `username`, `token` from env vars (`JIRA_SERVER_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN`) or `[jira]` TOML section
  - Security: SSL verify optional (`JIRA_VERIFY_SSL=false`), custom CA file support
- Jira work snapshot collection - Captures Jira query state for schedule triggers
  - File: `whilly/jira_work.py`, `whilly/jira_watch.py`
  - JQL-based queries for issue snapshots
- Jira CLI TUI - Interactive issue browser
  - File: `whilly/cli/jira_tui.py`

**Slack:**
- Run-completed notifications - Posts to `chat.postMessage` on task/plan completion
  - Adapter: `whilly/adapters/notifications/slack.py`
  - Transport: stdlib `urllib.request` (no `requests` library)
  - Auth: Bearer token via `SLACK_ACCESS_TOKEN` env var
  - Config: Channel ID (`SLACK_CHANNEL`), API base URL, timeout, message template
  - Failure policy: Silent (logs WARNING, does not block orchestrator exit)
- Task lifecycle notifications - Notifies task start/completion to Slack
  - File: `whilly/slack_task_notify.py`
  - Auth: Same bearer token

**Claude API:**
- Claude Code CLI subprocess backend - Wraps `claude` binary with controlled tool permissions
  - File: `whilly/agents/claude.py`
  - Default: Tool-deny mode (no Write, Edit, Bash unless `WHILLY_AGENT_ALLOW_SHELL=1`)
  - Safe mode: `WHILLY_CLAUDE_SAFE=1` → `--permission-mode acceptEdits`
  - Env injection: `CLAUDE_BIN` (binary path), `WHILLY_CLAUDE_PROXY_URL` (HTTPS proxy override)
  - Model: Configurable via `WHILLY_MODEL` (default: `claude-opus-4-6[1m]`)
  - Output: Stream JSON parsing for live delta consumption
- Claude Handoff backend - Deferred agent backend (TASK-101 planned)
  - File: `whilly/agents/claude_handoff.py` (stub)

**OpenCode (pluggable alternative):**
- OpenCode CLI backend - Optional alternative to Claude
  - File: `whilly/agents/opencode.py`
  - Activation: `WHILLY_AGENT_BACKEND=opencode`
  - Config: `OPENCODE_BIN` (binary path), `OPENCODE_SAFE` (safe mode), `OPENCODE_SERVER_URL` (optional remote server)

## Data Storage

**Databases:**
- PostgreSQL 15+ - Single source of truth for all operational and audit data
  - Connection: `WHILLY_DATABASE_URL` env var (asyncpg DSN, e.g., `postgresql://user:pass@host/dbname`)
  - Client: asyncpg (async) in control plane; raw SQL via Alembic in migrations
  - Schema: Managed by Alembic; current head is **migration 020_users** (as of 2026-06)
  - Key tables:
    - `plans` - Plan metadata (id, name, budget_usd, spent_usd, github_issue_ref, prd_file, archived_at)
    - `tasks` - Task queue (id, plan_id, status, claimed_by, priority, dependencies, key_files, acceptance_criteria, test_steps)
    - `workers` - Worker registry (worker_id, hostname, last_heartbeat, token_hash, registered_at, owner_email)
    - `events` - Append-only audit log (type, plan_id, task_id, worker_id, detail, created_at)
    - `sessions` - User sessions with magic-link auth
    - `users` - User accounts with email + hashed passwords
    - `pull_requests` - PR records linked to completed tasks
    - Auth extras: `user_totp_secrets` (TOTP), `webauthn_credentials` (passkeys)
  - Transaction semantics: Row-level locks on claim/complete; `FOR UPDATE OF t SKIP LOCKED` for budget-safe concurrent claims

**File Storage:**
- Local filesystem only - No cloud storage integration
  - Log directory: `whilly_logs/` (configurable via `WHILLY_LOG_DIR`)
  - Artifacts: Per-task prompt/output files, LLM ops session logs
  - Audit: `whilly_logs/whilly_events.jsonl` (fallback when event flusher unavailable or SMTP down)
  - State: `.whilly_state.json` (plan execution checkpoint for `--resume`)

**Caching:**
- None at the application layer - Cache layers deferred to future
- Database indexes on access patterns: `tasks(plan_id, status)`, `events(task_id, created_at)`, `workers(last_heartbeat)`

## Authentication & Identity

**Auth Provider:**
- Custom (in-app)
  - Magic-link flow: User requests link → event logged → SMTP sent (or link in event log on fallback) → time-limited session token issued
  - Implementation: `whilly/api/auth_routes.py`, `whilly/api/mailer.py`
  - Session storage: Postgres `sessions` table with expiry
  - CSRF protection: Token-based, checked in form submissions
  - Rate limiting: Per-user and global thresholds (configurable)

**Identity Verification:**
- Email-based magic links - Primary auth method (PRD-post-auth-hardening §Epic C)
- Per-worker bearer tokens - Worker-to-control-plane authentication
  - Generated: `whilly worker register --bootstrap-token <secret>`
  - Stored: `workers.token_hash` (one-way hash, plaintext never persisted)
  - Auth scheme: `Authorization: Bearer <token>` on every RPC
  - Bootstrap secret: `WHILLY_WORKER_BOOTSTRAP_TOKEN` (required to mint new worker tokens)

**Second-Factor Options:**
- TOTP (Time-based OTP) - RFC 6238 via pyotp 2.9+
  - Gated: `WHILLY_TOTP_ENABLED=1` (default off)
  - Storage: `user_totp_secrets` table
  - Routes loaded conditionally to avoid import when disabled
- WebAuthn/passkeys - FIDO2-compliant credentials
  - Gated: `WHILLY_WEBAUTHN_ENABLED=1` (default off)
  - Library: webauthn 2.0+ (lazy import)
  - Storage: `webauthn_credentials` table

**Secrets Management:**
- OS keyring integration - Credentials stored securely via keyring (macOS Keychain, Linux Secret Service, Windows Credential Manager)
  - File: `whilly/secrets.py`
  - TOML references: `keyring:service/account` syntax
  - CLI: `whilly worker connect` stores per-worker bearer in keyring by default
- Environment variables - Fallback for CI/container deployments
- File-based - Direct file path references in TOML (e.g., for CA certificates)

## Monitoring & Observability

**Error Tracking:**
- Event logging - Append-only Postgres `events` table captures every state transition, error, and decision gate verdict
  - Schema: `type`, `plan_id`, `task_id`, `worker_id`, `detail` (JSON), `created_at`
  - JSON fields include error reason, exit code, TRIZ findings, PR result metadata
- Fallback event log - JSONL file (`whilly_logs/whilly_events.jsonl`) when Postgres unavailable or event flusher backed up
- None - No external error tracking service integration (e.g., Sentry); events are durable in Postgres

**Logs:**
- Structured logging - Python `logging` module with getLogger per module
  - Verbosity: `WHILLY_VERBOSE=1` sets `WHILLY_TRACE_HTTP=1` + `ANTHROPIC_LOG=info`
  - HTTP trace: `WHILLY_TRACE_HTTP=1` enables `ANTHROPIC_LOG=debug` + HTTP body capture
- File storage - Task-level prompts and outputs in `whilly_logs/{task_id}/`
- LLM Ops artifacts - Session dir structure: `{artifact_dir}/{attempt}/prompt.txt`, `raw.log`, `final.json`, `summary.json`
- TTL cleanup - Age-based cleanup of agent logs at run start (`WHILLY_LOG_TTL_DAYS`, default 14; 0 = disabled)

**Metrics:**
- Prometheus exposition - Bearer-gated `GET /metrics` endpoint (fails closed when `WHILLY_METRICS_TOKEN` unset)
  - Custom metrics: `whilly_claims_total`, `whilly_completes_total`, `whilly_fails_total{reason}`, `whilly_workers_online`, `whilly_claims_pending`, `whilly_plan_budget_remaining_usd`, `whilly_claim_long_poll_duration_seconds`
  - Standard metrics: HTTP request count/latency/size via prometheus-fastapi-instrumentator
  - Refresh interval: 15s (configurable)
- Refresh loop - Coroutine in lifespan TaskGroup; catches transient DB disconnects and retains last-known-good gauge values

**Distributed Tracing (Optional):**
- OpenTelemetry export - Optional LLM ops instrumentation
  - File: `whilly/llm_otel.py`
  - Exporters: Langfuse, Phoenix, generic OTLP/HTTP collectors
  - Config: `WHILLY_LLM_OPS_EXPORTERS`, `WHILLY_LLM_OPS_OTLP_ENDPOINT`, `WHILLY_LLM_OPS_OTLP_HEADERS`
  - Content capture: `WHILLY_LLM_OPS_CAPTURE_CONTENT=1` (optional; excludes by default for cost)
  - Dependencies: opentelemetry-api, opentelemetry-sdk, opentelemetry-exporter-otlp-proto-http, traceloop-sdk (all optional)

## CI/CD & Deployment

**Hosting:**
- Distributed multi-host - Control plane (Postgres + FastAPI) on one box, remote workers on separate boxes
- Docker images - Published as `mshegolev/whilly:4.7.0` (multi-arch: linux/amd64, linux/arm64)
- Compose files: `docker-compose.demo.yml` (single-host all-in-one), `docker-compose.control-plane.yml` (VPS), `docker-compose.worker.yml` (laptop/worker)
- All-in-one local mode - `whilly run --plan <id>` embeds control plane in worker process

**Public Internet Exposure (v4.5+ M2 via localhost.run sidecar):**
- localhost.run funnel sidecar - Publishes `https://<random>.lhr.life` URL for two-host demos without `--insecure`
  - Funnel URL stored in Postgres `funnel_url` singleton table and exposed at `GET /funnel/url.txt`
  - Workers discover URL via `WHILLY_FUNNEL_URL_SOURCE=postgres|file` and re-register on rotation
  - Replaces removed Tailscale Funnel architecture (2026-05-02 pivot)

**CI Pipeline:**
- GitHub Actions - .github/workflows/ (inferred; tests run on push/PR)
- Test matrix: Python 3.12 + 3.13 (from CI action config)
- Gates: ruff lint + format, mypy strict on `whilly/core/`, import-linter contract, pytest (unit + integration + live-llm conditionally)

## Environment Configuration

**Required env vars (control plane):**
- `WHILLY_DATABASE_URL` - PostgreSQL asyncpg DSN (required for server mode and `whilly run`)
- `WHILLY_WORKER_BOOTSTRAP_TOKEN` - Cluster bootstrap secret for `POST /workers/register`

**Optional but important:**
- `CLAUDE_BIN` - Path to Claude CLI binary (default: `claude` on PATH)
- `WHILLY_MODEL` - Model string (default: `claude-opus-4-6[1m]`)
- `WHILLY_AGENT_BACKEND` - `claude` (default) or `opencode`
- `WHILLY_SMTP_HOST` - SMTP server (empty/unset disables SMTP, uses event log fallback)
- `WHILLY_METRICS_TOKEN` - Bearer token for `/metrics` (fail-closed when unset)

**Integration-specific:**
- GitHub: Uses `gh` CLI auth from `~/.config/gh/hosts.yml`; no env var needed
- Jira: `JIRA_SERVER_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN` or `[jira]` TOML section
- Slack: `SLACK_ACCESS_TOKEN`, `SLACK_CHANNEL`, `SLACK_API_BASE_URL` (optional, default https://slack.com/api)
- LLM Ops: `WHILLY_LLM_OPS_EXPORTERS`, `WHILLY_LLM_OPS_OTLP_ENDPOINT`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `PHOENIX_API_KEY`

**Secrets location:**
- `.env` file - Dotenv-style (kept outside git via `.gitignore`)
- `~/.config/whilly/config.toml` - User-level TOML with secret references (`env:VAR`, `keyring:service/account`)
- `./whilly.toml` - Repo-level TOML (checked in, references secrets indirectly)
- OS keyring - Secure storage for bearer tokens via `whilly worker connect`
- Alembic env.py - Database connection from env only, never hardcoded

## Webhooks & Callbacks

**Incoming:**
- GitHub webhook payloads - Consume via `WHILLY_GITHUB_WEBHOOK_SIGNING_SECRET` (future; currently deferred)
- Jira webhook payloads - Consume status transitions (deferred; currently polling-based only)
- Event stream SSE - `GET /events/stream` pushes task/worker/plan events to connected dashboards in real-time via `pg_notify`

**Outgoing:**
- GitHub PR creation - Pushes commits and opens PR via `gh pr create` subprocess
- Jira task transitions - Auto-close via JIRA REST API (configurable: `JIRA_AUTO_CLOSE=true`, target status configurable)
- Slack notifications - Posts to `chat.postMessage` on run completion
- GitHub issue label transitions - Updates issue labels (e.g., `whilly-pending` → `whilly-in-progress`) via `gh` CLI
- Magic-link emails - SMTP or event log (when `WHILLY_SMTP_HOST` unset)

**Event Stream (Real-Time):**
- PostgreSQL NOTIFY - Trigger on `events` table inserts via `tr_events_notify`
- SSE broker - Fan-out to multiple subscribers with exponential backoff on connection loss
- Last-Event-ID recovery - Up to 1000 cached rows for client reconnect within 60s window
- Slow subscriber timeout - Dropped with WS close-code 1015 if write buffer overflows

---

*Integration audit: 2026-06-10*
