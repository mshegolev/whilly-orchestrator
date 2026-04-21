---
title: Home
layout: default
nav_order: 1
description: "Whilly Orchestrator — task orchestrator that runs Claude CLI agents on a JSON plan."
permalink: /
---

# Whilly Orchestrator
{: .fs-9 }

Task orchestrator that runs coding-agent CLIs on a JSON plan — loopable, resumable, budget-capped, and happy to hand tasks back to a human when it hits a wall.
{: .fs-5 .fw-300 }

[Getting Started]({{ site.baseurl }}/Getting-Started){: .btn .btn-primary .fs-5 .mb-4 .mb-md-0 .mr-2 }
[View on GitHub](https://github.com/mshegolev/whilly-orchestrator){: .btn .fs-5 .mb-4 .mb-md-0 }

---

## What whilly does

- **Pulls tasks** from a GitHub repo (`--from-github`, `--from-issue`), a Projects v2 board (`--from-project`), or a Jira project (`--from-jira`).
- **Runs agents** — Claude CLI by default, OpenCode, or `claude_handoff` (file-based RPC to you).
- **Tracks lifecycle** on your Projects v2 board *and* your Jira ticket in real time: `Todo → In Progress → In Review → Done`, plus `On Hold` and `Human Loop` for anything that needs a human.
- **Stays safe** — hard budget cap, wall-clock timeout, automatic task resume, idempotent plan files.
- **Works everywhere** — pure Python 3.10+, stdlib-only for Jira, tested on Linux / macOS / Windows.

## One-liner demo

```bash
pipx install whilly-orchestrator
whilly --config path            # where to drop your config
whilly --from-issue you/repo/42 --go
```

That fetches issue 42, generates a one-task plan, spawns an agent, and exits `0` when the agent reports complete.

## Read next

| Page | When to read |
|---|---|
| **[Getting Started]({{ site.baseurl }}/Getting-Started)** | First time here — eight practical walkthroughs |
| **[Full Usage Reference]({{ site.baseurl }}/Whilly-Usage)** | Every CLI flag, env var, and config field |
| **[GitHub Integration Guide]({{ site.baseurl }}/GitHub-Integration-Guide)** | Setting up Projects v2 + board sync |
| **[Interfaces & Tasks]({{ site.baseurl }}/Whilly-Interfaces-and-Tasks)** | Module contracts + the JSON plan schema |
| **[Architecture Decisions]({{ site.baseurl }}/workshop/adr/)** | Why things are the way they are (if published) |

## Under the hood

```
CLI  ──▶  TaskManager  ──▶  AgentBackend  ──▶  Claude / OpenCode / claude_handoff
                ↓
   on_status_change hook  ──▶  Projects v2 (GraphQL)  +  Jira (REST)
                ↓
         Dashboard, Reporter, Budget + Resource guards
```

Full module map lives in [`Whilly-Interfaces-and-Tasks`]({{ site.baseurl }}/Whilly-Interfaces-and-Tasks).

## Current status

- **643 tests**, cross-OS CI (Linux / macOS / Windows) on every PR.
- Layered config — `whilly.toml` + OS keyring, migrates from legacy `.env` with one command.
- [Latest release](https://github.com/mshegolev/whilly-orchestrator/releases/latest) · [Open issues](https://github.com/mshegolev/whilly-orchestrator/issues) · [Changelog](https://github.com/mshegolev/whilly-orchestrator/commits/main)
