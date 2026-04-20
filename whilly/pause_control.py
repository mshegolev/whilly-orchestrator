"""Simple pause control for Whilly orchestrator."""

import json
import time
from pathlib import Path
from typing import Optional


class PauseControl:
    """Simple pause/resume mechanism for Whilly."""

    def __init__(self, pause_file: str = ".whilly_pause"):
        self.pause_file = Path(pause_file)

    def pause(self, reason: str = "Manual pause") -> None:
        """Create pause file."""
        pause_state = {
            "paused": True,
            "reason": reason,
            "paused_at": time.time(),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.pause_file.write_text(json.dumps(pause_state, indent=2))
        print(f"⏸️  Paused: {reason}")

    def resume(self) -> None:
        """Remove pause file."""
        if self.pause_file.exists():
            self.pause_file.unlink()
            print("▶️  Resumed")

    def is_paused(self) -> bool:
        """Check if currently paused."""
        return self.pause_file.exists()

    def get_pause_info(self) -> Optional[dict]:
        """Get pause information if paused."""
        if not self.is_paused():
            return None
        try:
            return json.loads(self.pause_file.read_text())
        except (json.JSONDecodeError, OSError):
            return {"paused": True, "reason": "Unknown"}

    def wait_if_paused(self, check_interval: int = 2) -> None:
        """Wait while paused, checking every interval."""
        while self.is_paused():
            pause_info = self.get_pause_info()
            reason = pause_info.get("reason", "Unknown") if pause_info else "Unknown"
            print(f"⏸️  Paused: {reason} (delete .whilly_pause to resume)")
            time.sleep(check_interval)


# Quick CLI commands
def pause_plan(plan_file: str, reason: str = "Manual pause"):
    """Pause a plan execution."""
    pc = PauseControl(f".whilly_pause_{Path(plan_file).stem}")
    pc.pause(reason)


def resume_plan(plan_file: str):
    """Resume a plan execution."""
    pc = PauseControl(f".whilly_pause_{Path(plan_file).stem}")
    pc.resume()
