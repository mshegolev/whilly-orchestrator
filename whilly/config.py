"""Whilly orchestrator configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields


@dataclass
class WhillyConfig:
    """Configuration loaded from environment variables with WHILLY_ prefix."""

    MAX_ITERATIONS: int = 0
    MAX_PARALLEL: int = 3
    HEARTBEAT_INTERVAL: int = 1
    DECOMPOSE_EVERY: int = 5
    AGENT: str = ""
    USE_TMUX: bool = False
    LOG_DIR: str = "whilly_logs"
    MODEL: str = "claude-opus-4-6[1m]"
    VOICE: bool = True
    ORCHESTRATOR: str = "file"
    RICH_DASHBOARD: bool = True
    BUDGET_USD: float = 0.0  # 0 = unlimited
    MAX_TASK_RETRIES: int = 5
    HEADLESS: bool = False
    TIMEOUT: int = 0
    STATE_FILE: str = ".whilly_state.json"
    WORKTREE: bool = False  # WHILLY_WORKTREE=1 — per-task git worktree (parallel agents)
    USE_WORKSPACE: bool = True  # WHILLY_USE_WORKSPACE=0 — отключить plan-level workspace

    # Agent backend selection (OC-109) — drives whilly.agents.get_backend()
    AGENT_BACKEND: str = "claude"  # "claude" | "opencode"
    OPENCODE_BIN: str = "opencode"  # path to the opencode CLI binary
    OPENCODE_SAFE: bool = False  # OPENCODE_SAFE=1 → safe mode (prompt before tool use)
    OPENCODE_SERVER_URL: str = ""  # optional remote OpenCode server URL (empty = local CLI)

    # External integrations (GitHub Issues, Jira, etc)
    CLOSE_EXTERNAL_TASKS: bool = True  # WHILLY_CLOSE_EXTERNAL_TASKS=0 → disable auto-closing
    GITHUB_AUTO_CLOSE: bool = True  # Auto-close GitHub Issues
    GITHUB_ADD_COMMENTS: bool = True  # Add completion comments to GitHub Issues
    JIRA_ENABLED: bool = False  # Enable Jira integration
    JIRA_SERVER_URL: str = ""  # Jira server URL
    JIRA_USERNAME: str = ""  # Jira username
    JIRA_AUTO_CLOSE: bool = True  # Auto-close Jira tasks
    JIRA_ADD_COMMENTS: bool = True  # Add completion comments to Jira
    JIRA_TRANSITION_TO: str = "Done"  # Target status for closing Jira tasks

    @classmethod
    def from_env(cls) -> WhillyConfig:
        """Load config from WHILLY_* environment variables, falling back to defaults."""
        kwargs: dict = {}
        for f in fields(cls):
            env_key = f"WHILLY_{f.name}"
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

    def get_external_integrations_config(self) -> dict[str, dict]:
        """Returns configuration for external integrations."""
        return {
            "enabled": self.CLOSE_EXTERNAL_TASKS,
            "github": {
                "enabled": True,  # GitHub always available if CLI present
                "auto_close": self.GITHUB_AUTO_CLOSE,
                "add_comments": self.GITHUB_ADD_COMMENTS,
            },
            "jira": {
                "enabled": self.JIRA_ENABLED,
                "server_url": self.JIRA_SERVER_URL or os.getenv("JIRA_SERVER_URL", ""),
                "username": self.JIRA_USERNAME or os.getenv("JIRA_USERNAME", ""),
                "token": os.getenv("JIRA_API_TOKEN", ""),  # Always from env for security
                "auto_close": self.JIRA_AUTO_CLOSE,
                "add_comments": self.JIRA_ADD_COMMENTS,
                "transition_to": self.JIRA_TRANSITION_TO,
            }
        }
