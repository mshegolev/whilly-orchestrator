"""Tests for ralph.state_store — F3 Crash Resume with Persistent State."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from ralph.state_store import StateStore


@pytest.fixture
def tmp_state_file(tmp_path):
    """Provide a temporary state file path."""
    return str(tmp_path / ".ralph_state.json")


@pytest.fixture
def store(tmp_state_file):
    """Provide a StateStore with a temporary file."""
    return StateStore(tmp_state_file)


class TestSaveAndLoad:
    def test_save_and_load(self, store):
        """State saved to file can be loaded back."""
        store.save(
            plan_file="tasks.json",
            iteration=3,
            cost_usd=1.25,
            active_agents=[{"task_id": "T1", "status": "running"}],
            task_status={"T1": "in_progress", "T2": "done", "T3": "pending"},
        )

        loaded = store.load()
        assert loaded is not None
        assert loaded["plan_file"] == "tasks.json"
        assert loaded["iteration"] == 3
        assert loaded["cost_usd"] == 1.25
        assert loaded["active_agents"] == [{"task_id": "T1", "status": "running"}]
        assert loaded["task_status"] == {"T1": "in_progress", "T2": "done", "T3": "pending"}
        assert "saved_at" in loaded

    def test_save_overwrites_previous(self, store):
        """Second save replaces first."""
        store.save("a.json", 1, 0.5, [], {"T1": "pending"})
        store.save("a.json", 5, 2.0, [], {"T1": "done"})
        loaded = store.load()
        assert loaded["iteration"] == 5
        assert loaded["cost_usd"] == 2.0


class TestAtomicWrite:
    def test_atomic_write_uses_tmp_and_rename(self, tmp_state_file):
        """Save writes to .tmp then renames — state file never partially written."""
        store = StateStore(tmp_state_file)
        store.save("plan.json", 1, 0.1, [], {"T1": "pending"})

        # File exists and is valid JSON
        assert Path(tmp_state_file).is_file()
        data = json.loads(Path(tmp_state_file).read_text())
        assert data["iteration"] == 1

        # No leftover .tmp files
        parent = Path(tmp_state_file).parent
        tmp_files = list(parent.glob(".ralph_state_*.tmp"))
        assert len(tmp_files) == 0

    def test_concurrent_save_no_corruption(self, store):
        """Multiple rapid saves don't corrupt the file."""
        for i in range(10):
            store.save("plan.json", i, float(i), [], {"T1": "pending"})

        loaded = store.load()
        assert loaded is not None
        assert loaded["iteration"] == 9


class TestClear:
    def test_clear_removes_file(self, store, tmp_state_file):
        """clear() removes the state file."""
        store.save("plan.json", 1, 0.1, [], {})
        assert Path(tmp_state_file).is_file()

        store.clear()
        assert not Path(tmp_state_file).is_file()

    def test_clear_no_error_when_missing(self, store):
        """clear() does not raise when file does not exist."""
        store.clear()  # should not raise


class TestLoadEdgeCases:
    def test_load_returns_none_when_no_file(self, store):
        """load() returns None if state file does not exist."""
        assert store.load() is None

    def test_load_returns_none_when_stale(self, store, tmp_state_file):
        """load() returns None if state is older than 24h."""
        store.save("plan.json", 1, 0.1, [], {})

        # Patch saved_at to be 25h ago
        data = json.loads(Path(tmp_state_file).read_text())
        data["saved_at"] = time.time() - 90000  # 25 hours
        Path(tmp_state_file).write_text(json.dumps(data))

        assert store.load() is None

    def test_load_returns_none_on_corrupt_json(self, tmp_state_file):
        """load() returns None for corrupt JSON."""
        Path(tmp_state_file).write_text("{broken json!!!")
        store = StateStore(tmp_state_file)
        assert store.load() is None


class TestDiscoverTmuxSessions:
    @patch("ralph.state_store.subprocess.run")
    def test_discover_finds_ralph_sessions(self, mock_run):
        """discover_tmux_sessions returns ralph-* sessions."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="ralph-TASK-001:1711929600\nralph-TASK-002:1711929700\nother-session:1711929800\n",
        )
        store = StateStore(".ralph_state.json")
        sessions = store.discover_tmux_sessions()

        assert len(sessions) == 2
        assert sessions[0]["task_id"] == "TASK-001"
        assert sessions[0]["session_name"] == "ralph-TASK-001"
        assert sessions[1]["task_id"] == "TASK-002"

    @patch("ralph.state_store.subprocess.run")
    def test_discover_returns_empty_when_no_tmux(self, mock_run):
        """discover_tmux_sessions returns [] when tmux not available."""
        mock_run.side_effect = FileNotFoundError("tmux not found")
        store = StateStore(".ralph_state.json")
        assert store.discover_tmux_sessions() == []

    @patch("ralph.state_store.subprocess.run")
    def test_discover_returns_empty_on_error(self, mock_run):
        """discover_tmux_sessions returns [] when tmux ls fails."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        store = StateStore(".ralph_state.json")
        assert store.discover_tmux_sessions() == []


class TestCleanupStaleSessions:
    @patch("ralph.state_store.subprocess.run")
    def test_cleanup_kills_orphaned_sessions(self, mock_run):
        """cleanup_stale_sessions kills sessions for tasks not in active set."""
        # First call: discover sessions; second+: kill
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="ralph-TASK-001:1711929600\nralph-TASK-002:1711929700\nralph-TASK-003:1711929800\n",
        )
        store = StateStore(".ralph_state.json")
        killed = store.cleanup_stale_sessions({"TASK-001"})

        # Should have killed TASK-002 and TASK-003 (not in active set)
        assert killed == 2
        # Verify kill-session was called for orphaned sessions
        kill_calls = [c for c in mock_run.call_args_list if "kill-session" in str(c)]
        assert len(kill_calls) == 2
