"""Ralph orchestrator configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields


@dataclass
class RalphConfig:
    """Configuration loaded from environment variables with RALPH_ prefix."""

    MAX_ITERATIONS: int = 0
    MAX_PARALLEL: int = 3
    HEARTBEAT_INTERVAL: int = 1
    DECOMPOSE_EVERY: int = 5
    AGENT: str = ""
    USE_TMUX: bool = False
    LOG_DIR: str = "ralph_logs"
    MODEL: str = "claude-opus-4-6[1m]"
    VOICE: bool = True
    ORCHESTRATOR: str = "file"
    RICH_DASHBOARD: bool = True
    BUDGET_USD: float = 0.0  # 0 = unlimited
    MAX_TASK_RETRIES: int = 5
    HEADLESS: bool = False
    TIMEOUT: int = 0
    STATE_FILE: str = ".ralph_state.json"
    WORKTREE: bool = False  # RALPH_WORKTREE=1 — per-task git worktree (parallel agents)
    USE_WORKSPACE: bool = True  # RALPH_USE_WORKSPACE=0 — отключить plan-level workspace

    @classmethod
    def from_env(cls) -> RalphConfig:
        """Load config from RALPH_* environment variables, falling back to defaults."""
        kwargs: dict = {}
        for f in fields(cls):
            env_key = f"RALPH_{f.name}"
            env_val = os.environ.get(env_key)
            if env_val is None:
                continue
            if f.type == "bool":
                kwargs[f.name] = env_val.lower() not in ("0", "false", "no", "off", "")
            elif f.type == "int":
                kwargs[f.name] = int(env_val)
            elif f.type == "float":
                kwargs[f.name] = float(env_val)
            else:
                kwargs[f.name] = env_val
        return cls(**kwargs)
