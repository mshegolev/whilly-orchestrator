# Ralph — Task Orchestrator

Python-based task orchestrator that runs Claude CLI agents to execute tasks from a JSON plan file.

## Quick Start

```bash
# Run with specific plan
./ralph.py .planning/my_tasks.json

# Auto-discover plans
./ralph.py

# Run all discovered plans
./ralph.py --all
```

## Configuration (Environment Variables)

| Variable | Default | Description |
|----------|---------|-------------|
| RALPH_MAX_ITERATIONS | 0 (unlimited) | Max work iterations per plan |
| RALPH_MAX_PARALLEL | 3 | Concurrent agents (1=sequential) |
| RALPH_USE_TMUX | 1 | Use tmux for agent isolation |
| RALPH_MODEL | claude-opus-4-6[1m] | LLM model |
| RALPH_LOG_DIR | ralph_logs | Directory for per-task logs |
| RALPH_ORCHESTRATOR | file | Orchestration: "file" or "llm" |
| RALPH_VOICE | 1 | macOS voice notifications |
| RALPH_DECOMPOSE_EVERY | 5 | Re-plan every N completed tasks |

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
ralph.py              Entry point + main loop
ralph/
  config.py           Config from RALPH_* env vars
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

Agents run in isolated tmux sessions when `RALPH_USE_TMUX=1`:

```bash
# View running agent sessions
tmux ls | grep ralph-

# Attach to a specific agent
tmux attach -t ralph-TASK-001

# Kill all ralph sessions
tmux kill-session -t ralph-TASK-001
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Dashboard doesn't render | Check terminal supports Rich (try `python3 -c "from rich import print; print('[bold]test[/]')"`) |
| Agent auth errors (403) | Check Claude CLI authentication: `claude --version` |
| Tmux not found | Install: `brew install tmux` or set `RALPH_USE_TMUX=0` |
| Tasks stuck in_progress | Ralph resets stale tasks on startup |
| Too many API errors | Ralph pauses 60s after 5+ consecutive failures |
