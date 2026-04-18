# Whilly — Task Orchestrator

Python-based task orchestrator that runs Claude CLI agents to execute tasks from a JSON plan file.

## Quick Start

```bash
# Run with specific plan
./whilly.py .planning/my_tasks.json

# Auto-discover plans
./whilly.py

# Run all discovered plans
./whilly.py --all
```

## Configuration (Environment Variables)

| Variable | Default | Description |
|----------|---------|-------------|
| WHILLY_MAX_ITERATIONS | 0 (unlimited) | Max work iterations per plan |
| WHILLY_MAX_PARALLEL | 3 | Concurrent agents (1=sequential) |
| WHILLY_USE_TMUX | 1 | Use tmux for agent isolation |
| WHILLY_MODEL | claude-opus-4-6[1m] | LLM model |
| WHILLY_LOG_DIR | whilly_logs | Directory for per-task logs |
| WHILLY_ORCHESTRATOR | file | Orchestration: "file" or "llm" |
| WHILLY_VOICE | 1 | macOS voice notifications |
| WHILLY_DECOMPOSE_EVERY | 5 | Re-plan every N completed tasks |

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| q | Graceful shutdown |
| d | Task detail overlay |
| l | Log viewer (last 30 lines) |
| t | All tasks overview |
| h | Help screen |

## Task Plan JSON Format

```json
{
  "project": "My Project",
  "tasks": [
    {
      "id": "TASK-001",
      "phase": "Phase 1",
      "category": "functional",
      "priority": "critical",
      "description": "What to do",
      "status": "pending",
      "dependencies": [],
      "key_files": ["path/to/file.py"],
      "acceptance_criteria": ["AC1"],
      "test_steps": ["step1"]
    }
  ]
}
```

## Architecture

```
whilly.py              Entry point + main loop
whilly/
  config.py           Config from WHILLY_* env vars
  task_manager.py     JSON plan CRUD, dependency resolution
  agent_runner.py     Claude CLI subprocess + JSON parsing
  tmux_runner.py      Tmux session isolation
  orchestrator.py     File-based + LLM task batching
  dashboard.py        Rich Live TUI + keyboard handler
  reporter.py         JSON + Markdown cost reports
  decomposer.py       Task decomposition via LLM
  notifications.py    macOS voice alerts
```

## Tmux Setup

Agents run in isolated tmux sessions when `WHILLY_USE_TMUX=1`:

```bash
# View running agent sessions
tmux ls | grep whilly-

# Attach to a specific agent
tmux attach -t whilly-TASK-001

# Kill all whilly sessions
tmux kill-session -t whilly-TASK-001
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Dashboard doesn't render | Check terminal supports Rich (try `python3 -c "from rich import print; print('[bold]test[/]')"`) |
| Agent auth errors (403) | Check Claude CLI authentication: `claude --version` |
| Tmux not found | Install: `brew install tmux` or set `WHILLY_USE_TMUX=0` |
| Tasks stuck in_progress | Whilly resets stale tasks on startup |
| Too many API errors | Whilly pauses 60s after 5+ consecutive failures |
