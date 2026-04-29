# Continuing Whilly Work on Another Machine

A pragmatic cheat-sheet for picking up in-flight v4.1 plan work from a
secondary machine (laptop / VM / другой комп) without losing context. Read
this **before** you `git pull` and find yourself confused about why
"continue" doesn't continue.

## TL;DR

```bash
# on machine B, fresh clone or after fetching latest
cd whilly-orchestrator
git pull origin main
claude  # or claude code, depending on how you launch it
```

Then **do not** type "continue" — give Claude an unambiguous job:

> Read `.planning/v4-1_tasks.json` and find the next ready (status=pending,
> dependencies all done) task. Show me which one you picked, then start it.

Or — even better — name the task:

> Start TASK-110: write `docs/Whilly-Workstation-Bootstrap.md` per the
> acceptance criteria for that task id in `.planning/v4-1_tasks.json`.

## Why "continue" alone won't work

Claude Code on machine B starts cold. It has access to:

- The repo (commits, PRDs, plan JSON, code) — via `git pull`
- `~/.claude/projects/<project>/memory/MEMORY.md` **on machine B's
  filesystem** — *not* synced from machine A
- `CLAUDE.md` (project-level) — synced via git ✅

It does **not** have access to:

- Any prior conversation's session history
- The reasoning behind decomposition decisions
- The Postgres state (until you set `WHILLY_DATABASE_URL` and reach the DB)
- Auto-memory feedback from machine A's `~/.claude/projects/...`

The repo holds the durable state; the *intent* and *next step* are in your
head, not in the repo.

## The three things to set up on machine B

### 1. Repo + dev install

```bash
git clone git@github.com:mshegolev/whilly-orchestrator.git  # or git pull
cd whilly-orchestrator
git checkout main
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
```

### 2. Postgres reach (only if you'll run `whilly` commands)

If your task is **just docs / code review / writing tests** without running
the orchestrator — skip this. TASK-110 (the workstation bootstrap doc), for
example, needs zero Postgres.

If you need to inspect plan state or run a worker, pick one:

| Mode | Setup | Trade-off |
|---|---|---|
| Local standalone | `./scripts/db-up.sh && alembic upgrade head && whilly plan import .planning/v4-1_tasks.json` | Separate world, no collision risk, but results don't reflect to machine A |
| SSH tunnel to A | `ssh -N -L 5432:127.0.0.1:5432 user@machine-a` then `WHILLY_DATABASE_URL=postgresql://whilly:whilly@127.0.0.1:5432/whilly` | Shared plan, work merges, but A must be reachable |
| Cross-host tunnel via `scripts/whilly-share.sh` | (after TASK-111 lands) `whilly-worker --connect <public-url> --token X` | No SSH access to A required, public exposure caveats apply |

### 3. Claude CLI auth

Anthropic credentials are per-machine. Re-authenticate on B (`claude login`
or whatever your auth flow uses). If B is on a network that can't reach
`api.anthropic.com` directly, follow `docs/Whilly-Claude-Proxy-Guide.md`
(TASK-109) — set `WHILLY_CLAUDE_PROXY_URL` to your tunnel.

## Memory sync (optional but useful)

Auto-memory at `~/.claude/projects/<project-id>/memory/MEMORY.md` does **not**
travel with the repo. If you want it on machine B:

- **Quick**: paste the 2-3 most relevant memory lines into your first message
  to B-Claude.
- **Long-term**: keep `~/.claude/projects/<project>/memory/` in a private git
  repo or sync it via Dropbox / iCloud / `rsync` between machines.

`CLAUDE.md` (the per-project file at the repo root) **is** in git and
transfers automatically. Project-specific guidance lives there, not in
auto-memory.

## What machine-B-Claude should and should not do

| | Allowed | Avoid |
|---|---|---|
| Code / docs / tests | ✅ Commit, push, open PRs | — |
| `whilly plan import` | ⚠ Only if B has access to the canonical Postgres (shared mode); otherwise skip — machine A or CI will sync | Running it against B's local-only Postgres while thinking it updates A's |
| Marking tasks `done` in Postgres | ⚠ Same as above — only in shared mode | Mass status updates that diverge from git's `.planning/v4-1_tasks.json` |
| Memory writes | ✅ Local to B's `~/.claude/projects/...` | Don't expect them to reach A |

**Rule of thumb:** treat git as the single source of truth across machines.
Plan JSON in git → import to Postgres on whichever machine actually runs the
orchestrator. Don't try to keep two Postgres instances in sync manually.

## Picking a ready task on machine B

```bash
# without Postgres — read the JSON directly
python3 -c "
import json
plan = json.load(open('.planning/v4-1_tasks.json'))
done = {t['id'] for t in plan['tasks'] if t.get('status') == 'done'}
ready = [t for t in plan['tasks']
         if t.get('status') == 'pending'
         and all(d in done for d in t.get('dependencies', []))]
for t in ready:
    print(f\"{t['id']:14s} {t['priority']:8s} {t['title'][:70]}\")
"
```

That tells you what's actually claimable without booting Postgres. Hand the
chosen task id to Claude:

> Start TASK-XXX. Read its full entry in `.planning/v4-1_tasks.json` plus
> any PRD it references. Show me your plan before writing code.

## Related references in this repo

- `docs/Whilly-Workstation-Bootstrap.md` — fuller per-machine bootstrap
  runbook (TASK-110, may not exist yet at time of reading)
- `docs/Whilly-Claude-Proxy-Guide.md` — Claude CLI proxy setup (TASK-109)
- `docs/Whilly-v4-Worker-Protocol.md` — control plane / worker HTTP contract
- `docs/demo-remote-worker.sh` — end-to-end remote-worker demo script
- `.planning/v4-1_tasks.json` — canonical task graph for v4.1
