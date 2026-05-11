---
title: Agent Local Setup
nav_order: 3
---

# Agent Runbook: Bring Up A Cloned Whilly Repo Locally

Use this runbook when a human says: "The Whilly repo is already cloned. Bring it
up locally in a prod-like Docker setup."

## Operating Rules For The Agent

- Work from the existing cloned checkout. Do not reclone unless the directory is
  missing or broken.
- Do not commit secrets. `.env` and `.env.worker` are local files.
- Do not run destructive cleanup such as `git reset --hard`, `docker compose
  down -v`, or `docker volume rm` unless the human explicitly asks.
- Prefer the published image from the env templates. Build locally only if the
  image pull fails or the human asks for a local build.

## 1. Verify The Checkout

```bash
pwd
git status --short --branch
git pull origin main
```

If the working tree has local edits, do not overwrite them. Report the dirty
files and continue only when the edits do not affect `.env*` or compose files.

## 2. Verify Docker

```bash
docker ps
```

If Docker is unavailable on macOS:

```bash
if command -v colima >/dev/null 2>&1; then
  colima start
  docker context use colima
  export DOCKER_HOST="$(docker context inspect colima --format '{{.Endpoints.docker.Host}}')"
  export TESTCONTAINERS_RYUK_DISABLED=true
else
  open -a Docker
fi
docker ps
```

If `docker compose` is unavailable but `docker-compose` exists, use
`docker-compose` in the commands below.

You do not need localhost.run / LHR credentials for a fully local setup. Those
are only for public tunnel exposure with `--profile funnel`.

## 3. Generate Local Env Files

Preferred path once Whilly is installed (pipx/venv with the console script on
`$PATH`):

```bash
whilly quick-setup --yes
```

If `whilly` is not yet on `$PATH` (fresh source checkout without an editable
install, or a CI sandbox that skipped activation), the same command runs via
the module entry point:

```bash
python3 -m whilly.cli quick-setup --yes
```

The command generates `.env` and `.env.worker`, creates non-demo local secrets,
detects `docker compose` vs `docker-compose`, and prints the exact startup
commands for this machine. It refuses to overwrite existing env files unless
`--force` is passed.

Useful variants (use the `whilly …` form; substitute
`python3 -m whilly.cli …` if the console script isn't installed):

```bash
whilly quick-setup --print-only
whilly quick-setup --yes --docker-provider colima
whilly quick-setup --yes --compose-command docker-compose
```

After generation, edit `.env` only for real Jira credentials:

```env
JIRA_SERVER_URL=https://your-company.atlassian.net
JIRA_USERNAME=you@example.com
JIRA_API_TOKEN=CHANGE_ME_JIRA_API_TOKEN
```

Edit `.env.worker` if the worker must drain a specific plan or reach a
control-plane on another host:

```env
WHILLY_PLAN_ID=jira-abc-123
WHILLY_CONTROL_URL=http://host.docker.internal:8000
```

Manual fallback if the command is unavailable:

```bash
test -f .env || cp .env.example .env
test -f .env.worker || cp .env.worker.example .env.worker
```

Edit `.env` and replace at least:

```env
POSTGRES_PASSWORD=CHANGE_ME
WHILLY_WORKER_BOOTSTRAP_TOKEN=CHANGE_ME_SHARED_BOOTSTRAP_TOKEN
JIRA_SERVER_URL=https://your-company.atlassian.net
JIRA_USERNAME=you@example.com
JIRA_API_TOKEN=CHANGE_ME_JIRA_API_TOKEN
```

Edit `.env.worker` and make sure the bootstrap token matches `.env`:

```env
WHILLY_WORKER_BOOTSTRAP_TOKEN=CHANGE_ME_SHARED_BOOTSTRAP_TOKEN
WHILLY_IMAGE=mshegolev/whilly:4.7.0
WHILLY_CLI=opencode
WHILLY_MODEL=opencode/big-pickle
CLAUDE_BIN=/opt/whilly/docker/cli_adapter.py
WHILLY_USE_CONNECT_FLOW=1
WHILLY_INSECURE=1
```

For same-host Docker Desktop or Colima on macOS, keep:

```env
WHILLY_CONTROL_URL=http://host.docker.internal:8000
```

If that hostname does not resolve under Colima, try:

```env
WHILLY_CONTROL_URL=http://host.lima.internal:8000
```

For Linux, replace it with the host LAN IP or another address reachable from
the worker container:

```env
WHILLY_CONTROL_URL=http://192.168.1.50:8000
```

## 4. Start The Control Plane

Use the compose command printed by `quick-setup`. Examples below use
`docker compose`; replace it with `docker-compose` if that is what the setup
command printed.

```bash
docker compose --env-file .env -f docker-compose.control-plane.yml up -d
docker compose --env-file .env -f docker-compose.control-plane.yml ps
curl -fsS http://127.0.0.1:8000/health
```

Expected health response includes:

```json
{"status":"ok"}
```

If compose uses the standalone binary on this machine:

```bash
docker-compose --env-file .env -f docker-compose.control-plane.yml up -d
```

## 5. Start The Worker

```bash
docker compose --env-file .env.worker -f docker-compose.worker.yml up -d
docker compose --env-file .env.worker -f docker-compose.worker.yml logs -f worker
```

Expected logs include worker registration and the long-poll loop, for example:

```text
registered worker_id=...
```

## 6. Import Or Intake A Jira Task

Use the control-plane container so it talks to the same Postgres instance:

```bash
docker compose --env-file .env -f docker-compose.control-plane.yml exec \
  -e JIRA_SERVER_URL \
  -e JIRA_USERNAME \
  -e JIRA_API_TOKEN \
  control-plane whilly jira intake ABC-123
```

When Whilly writes the plan id, usually `jira-abc-123`, set the worker to drain
that plan:

```bash
perl -0pi -e 's/^WHILLY_PLAN_ID=.*/WHILLY_PLAN_ID=jira-abc-123/m' .env.worker
docker compose --env-file .env.worker -f docker-compose.worker.yml up -d --force-recreate
```

For a save-only first pass instead of starting work:

```bash
docker compose --env-file .env -f docker-compose.control-plane.yml exec \
  -e JIRA_SERVER_URL \
  -e JIRA_USERNAME \
  -e JIRA_API_TOKEN \
  control-plane whilly jira intake ABC-123 --action save
```

## 7. Basic Troubleshooting

```bash
docker compose --env-file .env -f docker-compose.control-plane.yml logs --tail=120
docker compose --env-file .env.worker -f docker-compose.worker.yml logs --tail=120 worker
docker compose --env-file .env -f docker-compose.control-plane.yml ps
docker compose --env-file .env.worker -f docker-compose.worker.yml ps
```

Common fixes:

- `plain HTTP to non-loopback` - keep `WHILLY_INSECURE=1` for local HTTP.
- worker cannot reach control-plane - change `WHILLY_CONTROL_URL` to
  `host.lima.internal` on Colima or to the host LAN IP on Linux.
- Jira auth fails - fix `JIRA_SERVER_URL`, `JIRA_USERNAME`, and
  `JIRA_API_TOKEN` in `.env`.
- Docker unavailable on macOS - run `colima start && docker context use colima`
  or start Docker Desktop.

## 8. What To Report Back

Report:

- current git commit,
- control-plane health result,
- worker registration status,
- Jira plan id if a task was imported,
- any files changed locally, especially `.env` and `.env.worker` values that
  were intentionally edited without revealing secrets.
