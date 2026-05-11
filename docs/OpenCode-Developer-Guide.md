---
title: OpenCode Developer Guide
nav_order: 9
---

# OpenCode Developer Guide

This is the developer runbook for running Whilly with OpenCode instead of the
demo stub or Claude Code.

## Quick Start

For the local Docker demo, use `workshop-demo.sh` and select the OpenCode CLI:

```bash
WHILLY_MODEL=opencode/big-pickle \
bash workshop-demo.sh --cli opencode --workers 1 --keep-running
```

Then open the dashboard:

```bash
open http://127.0.0.1:8000/
```

`opencode/big-pickle` is the zero-key OpenCode Zen model. It does not require
`opencode auth login` or provider API keys.

## Required Variables

Whilly still calls the runner through the historical `CLAUDE_BIN` subprocess
contract. For OpenCode, `CLAUDE_BIN` must point to Whilly's adapter:

| Variable | Value | Purpose |
|---|---|---|
| `CLAUDE_BIN` | `/opt/whilly/docker/cli_adapter.py` | Runs Whilly's adapter instead of the fake demo Claude stub. |
| `WHILLY_CLI` | `opencode` | Selects the OpenCode adapter route. |
| `WHILLY_MODEL` | `opencode/big-pickle` | Selects the OpenCode model. |

When using `workshop-demo.sh --cli opencode`, the script sets `CLAUDE_BIN` and
`WHILLY_CLI` for you. Set `WHILLY_MODEL` explicitly when you want a pinned
model.

## Where To Set The Model

For one command:

```bash
WHILLY_MODEL=opencode/big-pickle bash workshop-demo.sh --cli opencode
```

For repeated local Docker runs, put the values in a repo-root `.env` file. The
file is gitignored and must not contain committed secrets:

```bash
CLAUDE_BIN=/opt/whilly/docker/cli_adapter.py
WHILLY_CLI=opencode
WHILLY_MODEL=opencode/big-pickle
```

The compose defaults live in `docker-compose.demo.yml` under
`services.worker.environment`. The helper script mapping lives in
`workshop-demo.sh` in `configure_cli_backend()`.

Important: direct `docker-compose.demo.yml up` uses the fake demo runner unless
`CLAUDE_BIN` is overridden. To run real OpenCode through compose directly, set
all three variables above.

## Direct Compose Run

After building the image:

```bash
docker-compose -f docker-compose.demo.yml build worker
docker-compose -f docker-compose.demo.yml up -d postgres control-plane
docker-compose -f docker-compose.demo.yml run --rm --no-deps \
  control-plane whilly plan import examples/demo/tasks.json
```

Run one local worker iteration through OpenCode:

```bash
docker-compose -f docker-compose.demo.yml run --rm --no-deps \
  -e CLAUDE_BIN=/opt/whilly/docker/cli_adapter.py \
  -e WHILLY_CLI=opencode \
  -e WHILLY_MODEL=opencode/big-pickle \
  -e WHILLY_SLACK_ENABLED=0 \
  -e WHILLY_DATABASE_URL=postgresql://whilly:whilly@postgres:5432/whilly \
  worker whilly run --plan demo --max-iterations 1 --worker-id opencode-dev
```

## Using Another Provider Through OpenCode

OpenCode model ids use `provider/model` form. Set `WHILLY_MODEL` and the
matching credential:

```bash
# Groq
WHILLY_MODEL=groq/openai/gpt-oss-120b
GROQ_API_KEY=gsk_...

# Anthropic through OpenCode
WHILLY_MODEL=anthropic/claude-opus-4-6
ANTHROPIC_API_KEY=sk-ant-...

# OpenAI through OpenCode
WHILLY_MODEL=openai/gpt-4o-mini
OPENAI_API_KEY=sk-...
```

Use `.env` or your shell environment for these values. Do not commit real
provider tokens.

## Smoke Check

To verify only the OpenCode binary in the image:

```bash
docker-compose -f docker-compose.demo.yml run --rm --no-deps \
  -e WHILLY_CLI=opencode \
  -e WHILLY_MODEL=opencode/big-pickle \
  worker opencode run --model opencode/big-pickle "Reply with exactly: OK"
```

To verify the Whilly adapter path:

```bash
docker-compose -f docker-compose.demo.yml run --rm --no-deps \
  -e WHILLY_CLI=opencode \
  -e WHILLY_MODEL=opencode/big-pickle \
  worker /opt/whilly/docker/cli_adapter.py \
    --output-format json \
    --model opencode/big-pickle \
    -p "Reply with exactly: OK"
```

The adapter smoke should return a JSON envelope whose `result` contains `OK`
and `<promise>COMPLETE</promise>`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `WHILLY_CLI env var is required` | Set `WHILLY_CLI=opencode`; the adapter needs it to choose the native CLI. |
| Worker uses the fake demo runner | Set `CLAUDE_BIN=/opt/whilly/docker/cli_adapter.py`; compose defaults to the stub for demo safety. |
| `GROQ_API_KEY is required` | You selected `WHILLY_MODEL=groq/...`; either set `GROQ_API_KEY` or switch back to `WHILLY_MODEL=opencode/big-pickle`. |
| Dashboard restarts with missing static files | Rebuild the image from a revision that includes `api/static/*` in `pyproject.toml` package data. |
