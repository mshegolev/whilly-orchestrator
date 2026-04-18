"""Session history for Ralph orchestrator.

Stores session snapshots and supports replay.

Usage:
    from ralph.history import SessionHistory
    hist = SessionHistory()
    hist.save_session(plan="plan.json", done=10, total=10, cost=2.34, elapsed=600)
    sessions = hist.list_sessions()
    session = hist.load_session("2026-04-02_12-30")
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("ralph.history")

_DEFAULT_DIR = ".ralph_sessions"
_RETENTION_DAYS = 30


class SessionHistory:
    """Manages Ralph session history files."""

    def __init__(self, session_dir: str = _DEFAULT_DIR, retention_days: int = _RETENTION_DAYS):
        self._dir = Path(session_dir)
        self._retention_days = retention_days

    def save_session(
        self,
        plan: str,
        done: int,
        total: int,
        failed: int = 0,
        cost_usd: float = 0.0,
        elapsed_sec: float = 0.0,
        tasks: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Path:
        """Save a session snapshot.

        Returns:
            Path to the saved session file.
        """
        self._dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        session_id = now.strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"{session_id}.json"

        data = {
            "session_id": session_id,
            "timestamp": now.isoformat(),
            "plan": plan,
            "done": done,
            "total": total,
            "failed": failed,
            "cost_usd": round(cost_usd, 4),
            "elapsed_sec": round(elapsed_sec, 1),
            "tasks": tasks or [],
            **(extra or {}),
        }

        path = self._dir / filename
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        log.info("Session saved: %s (done=%d/%d, cost=$%.2f)", session_id, done, total, cost_usd)
        self._cleanup_old()
        return path

    def list_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        """List recent sessions (newest first).

        Returns:
            List of session summaries (session_id, plan, done, total, cost_usd, timestamp).
        """
        if not self._dir.exists():
            return []

        files = sorted(self._dir.glob("*.json"), reverse=True)[:limit]
        sessions = []
        for f in files:
            try:
                with open(f) as fh:
                    data = json.load(fh)
                sessions.append({
                    "session_id": data.get("session_id", f.stem),
                    "plan": data.get("plan", ""),
                    "done": data.get("done", 0),
                    "total": data.get("total", 0),
                    "failed": data.get("failed", 0),
                    "cost_usd": data.get("cost_usd", 0),
                    "elapsed_sec": data.get("elapsed_sec", 0),
                    "timestamp": data.get("timestamp", ""),
                })
            except Exception:
                continue

        return sessions

    def load_session(self, session_id: str) -> dict[str, Any] | None:
        """Load full session data by ID.

        Args:
            session_id: Session identifier (e.g., "2026-04-02_12-30-00").

        Returns:
            Session data dict or None if not found.
        """
        path = self._dir / f"{session_id}.json"
        if not path.exists():
            # Try partial match
            matches = list(self._dir.glob(f"{session_id}*.json"))
            if not matches:
                return None
            path = matches[0]

        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None

    def _cleanup_old(self) -> int:
        """Remove sessions older than retention period."""
        if not self._dir.exists():
            return 0

        cutoff = datetime.now(timezone.utc).timestamp() - (self._retention_days * 86400)
        removed = 0
        for f in self._dir.glob("*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1

        if removed:
            log.info("Cleaned up %d old sessions (>%d days)", removed, self._retention_days)
        return removed

    def format_history_table(self, limit: int = 10) -> str:
        """Format sessions as a text table for CLI output."""
        sessions = self.list_sessions(limit)
        if not sessions:
            return "No sessions found."

        lines = [
            f"{'Session ID':<22} {'Plan':<30} {'Done':>5} {'Total':>6} {'Cost':>8} {'Time':>8}",
            "-" * 85,
        ]
        for s in sessions:
            elapsed = s["elapsed_sec"]
            time_str = f"{int(elapsed // 60)}m{int(elapsed % 60)}s"
            lines.append(
                f"{s['session_id']:<22} {s['plan'][:28]:<30} {s['done']:>5} "
                f"{s['total']:>6} ${s['cost_usd']:>6.2f} {time_str:>8}"
            )
        return "\n".join(lines)
