"""Persistent state for crash recovery (F3).

Saves orchestrator state to a JSON file after each iteration so that
Ralph can resume from where it left off after an unexpected crash
(OOM, SIGKILL, etc.).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger("ralph")


class StateStore:
    """Persistent state for crash recovery."""

    def __init__(self, state_file: str = ".ralph_state.json"):
        self.state_file = Path(state_file)

    def save(
        self,
        plan_file: str,
        iteration: int,
        cost_usd: float,
        active_agents: list[dict],
        task_status: dict[str, str],
    ) -> None:
        """Atomic write state to file (write to .tmp, then rename)."""
        state = {
            "plan_file": plan_file,
            "iteration": iteration,
            "cost_usd": cost_usd,
            "active_agents": active_agents,
            "task_status": task_status,
            "saved_at": time.time(),
        }
        content = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        dir_path = self.state_file.parent or Path(".")
        fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp", prefix=".ralph_state_")
        closed = False
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            closed = True
            os.replace(tmp_path, self.state_file)
        except BaseException:
            if not closed:
                os.close(fd)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def load(self) -> dict | None:
        """Load state from file. Returns None if no state or stale (>24h)."""
        if not self.state_file.is_file():
            return None
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt state file %s, ignoring", self.state_file)
            return None
        saved_at = data.get("saved_at", 0)
        if time.time() - saved_at > 86400:  # 24 hours
            log.info("State file is stale (>24h), ignoring")
            return None
        return data

    def clear(self) -> None:
        """Remove state file after successful completion."""
        try:
            self.state_file.unlink(missing_ok=True)
        except OSError:
            pass

    def discover_tmux_sessions(self) -> list[dict]:
        """Find ralph-* tmux sessions that are still running."""
        try:
            r = subprocess.run(
                ["tmux", "ls", "-F", "#{session_name}:#{session_activity}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        if r.returncode != 0:
            return []
        sessions = []
        for line in r.stdout.strip().splitlines():
            if not line.startswith("ralph-"):
                continue
            parts = line.split(":", 1)
            session_name = parts[0]
            activity = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            task_id = session_name.removeprefix("ralph-")
            sessions.append(
                {
                    "session_name": session_name,
                    "task_id": task_id,
                    "last_activity": activity,
                }
            )
        return sessions

    def cleanup_stale_sessions(self, active_task_ids: set[str]) -> int:
        """Kill tmux sessions for tasks no longer in plan. Returns count killed."""
        sessions = self.discover_tmux_sessions()
        killed = 0
        for s in sessions:
            if s["task_id"] not in active_task_ids:
                try:
                    subprocess.run(
                        ["tmux", "kill-session", "-t", s["session_name"]],
                        capture_output=True,
                        timeout=5,
                    )
                    killed += 1
                    log.info("Killed stale tmux session %s", s["session_name"])
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass
        return killed
