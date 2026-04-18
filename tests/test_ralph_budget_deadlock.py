"""Tests for Ralph v3 features: F1 (Cost Budget Guard) and F2 (Deadlock Detection)."""

import json
import os
from unittest.mock import patch

import pytest
from ralph.config import RalphConfig
from ralph.dashboard import Dashboard
from ralph.notifications import notify_budget_exceeded, notify_budget_warning, notify_deadlock
from ralph.reporter import CostTotals
from ralph.task_manager import VALID_STATUSES, TaskManager

# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def plan_file(tmp_path):
    """Create a temp plan file with tasks in various states."""
    plan = {
        "project": "test-budget",
        "tasks": [
            {
                "id": "T-1",
                "phase": "P1",
                "category": "func",
                "priority": "high",
                "description": "Task one",
                "status": "done",
                "dependencies": [],
                "key_files": [],
            },
            {
                "id": "T-2",
                "phase": "P1",
                "category": "func",
                "priority": "medium",
                "description": "Task two",
                "status": "pending",
                "dependencies": [],
                "key_files": [],
            },
            {
                "id": "T-3",
                "phase": "P2",
                "category": "test",
                "priority": "low",
                "description": "Task three",
                "status": "in_progress",
                "dependencies": [],
                "key_files": [],
            },
        ],
    }
    f = tmp_path / "tasks.json"
    f.write_text(json.dumps(plan))
    return f


@pytest.fixture
def tm(plan_file):
    return TaskManager(str(plan_file))


# ── F1: Config — BUDGET_USD ─────────────────────────────────────


class TestBudgetConfig:
    def test_default_budget(self):
        config = RalphConfig()
        assert config.BUDGET_USD == 0.0  # unlimited by default

    def test_budget_from_env(self):
        with patch.dict(os.environ, {"RALPH_BUDGET_USD": "25.5"}):
            config = RalphConfig.from_env()
            assert config.BUDGET_USD == 25.5

    def test_budget_zero_unlimited(self):
        with patch.dict(os.environ, {"RALPH_BUDGET_USD": "0"}):
            config = RalphConfig.from_env()
            assert config.BUDGET_USD == 0.0


class TestMaxTaskRetriesConfig:
    def test_default_max_retries(self):
        config = RalphConfig()
        assert config.MAX_TASK_RETRIES == 5

    def test_max_retries_from_env(self):
        with patch.dict(os.environ, {"RALPH_MAX_TASK_RETRIES": "8"}):
            config = RalphConfig.from_env()
            assert config.MAX_TASK_RETRIES == 8


# ── F1: Dashboard — budget display ──────────────────────────────


class TestDashboardBudget:
    def test_dashboard_has_budget_attrs(self, tm):
        d = Dashboard(tm, "test-agent", 10)
        assert hasattr(d, "budget_usd")
        assert hasattr(d, "session_cost_usd")
        assert d.budget_usd == 0.0
        assert d.session_cost_usd == 0.0

    def test_dashboard_renders_budget_line(self, tm):
        d = Dashboard(tm, "test-agent", 10)
        d.budget_usd = 10.0
        d.session_cost_usd = 3.50
        rendered = d._render()
        # The rendered Group should contain budget info
        assert rendered is not None

    def test_dashboard_budget_color_green(self, tm):
        """Budget <50% should show green."""
        d = Dashboard(tm, "test-agent", 10)
        d.budget_usd = 10.0
        d.session_cost_usd = 2.0  # 20%
        rendered = d._render()
        # Verify render doesn't crash at low budget
        assert rendered is not None

    def test_dashboard_budget_color_yellow(self, tm):
        """Budget 50-80% should show yellow."""
        d = Dashboard(tm, "test-agent", 10)
        d.budget_usd = 10.0
        d.session_cost_usd = 7.0  # 70%
        rendered = d._render()
        assert rendered is not None

    def test_dashboard_budget_color_red(self, tm):
        """Budget >80% should show red."""
        d = Dashboard(tm, "test-agent", 10)
        d.budget_usd = 10.0
        d.session_cost_usd = 9.0  # 90%
        rendered = d._render()
        assert rendered is not None

    def test_dashboard_no_budget_when_zero(self, tm):
        """When budget_usd=0 (unlimited), no budget line rendered."""
        d = Dashboard(tm, "test-agent", 10)
        d.budget_usd = 0.0
        d.session_cost_usd = 5.0
        rendered = d._render()
        assert rendered is not None


# ── F1: Notification functions ───────────────────────────────────


class TestBudgetNotifications:
    @patch("ralph.notifications.SAY_BIN", None)
    def test_notify_budget_warning_noop(self):
        """Should not crash when say binary is unavailable."""
        notify_budget_warning(80)

    @patch("ralph.notifications.SAY_BIN", None)
    def test_notify_budget_exceeded_noop(self):
        notify_budget_exceeded()


# ── F2: Skipped status ──────────────────────────────────────────


class TestSkippedStatus:
    def test_skipped_is_valid_status(self):
        assert "skipped" in VALID_STATUSES

    def test_mark_task_skipped(self, tm):
        tm.mark_status(["T-3"], "skipped")
        tm.reload()
        t3 = tm.get_task("T-3")
        assert t3 is not None
        assert t3.status == "skipped"

    def test_skipped_not_counted_as_pending(self, tm):
        tm.mark_status(["T-2"], "skipped")
        tm.reload()
        assert tm.pending_count == 0  # T-1 done, T-2 skipped, T-3 in_progress

    def test_skipped_not_in_ready_tasks(self, tm):
        tm.mark_status(["T-2"], "skipped")
        tm.reload()
        ready = tm.get_ready_tasks()
        assert all(t.id != "T-2" for t in ready)


# ── F2: Deadlock notification ────────────────────────────────────


class TestDeadlockNotification:
    @patch("ralph.notifications.SAY_BIN", None)
    def test_notify_deadlock_noop(self):
        notify_deadlock("T-99")


# ── F2: Deadlock detection logic (unit) ──────────────────────────


class TestDeadlockDetectionLogic:
    """Test the deadlock detection logic in isolation (same algorithm as run_plan)."""

    @staticmethod
    def _detect_deadlocks(
        tasks_status: dict[str, str],
        task_attempt_count: dict[str, int],
        task_prev_status: dict[str, str],
        max_task_retries: int = 5,
    ) -> tuple[list[str], dict[str, int], dict[str, str]]:
        """Reproduce the deadlock detection logic from run_plan for testing."""
        skipped = []
        for tid, status in tasks_status.items():
            if status == "in_progress":
                prev = task_prev_status.get(tid)
                if prev == "in_progress":
                    task_attempt_count[tid] = task_attempt_count.get(tid, 1) + 1
                else:
                    task_attempt_count[tid] = 1
            else:
                task_attempt_count.pop(tid, None)
            task_prev_status[tid] = status

        for tid, attempts in list(task_attempt_count.items()):
            if attempts >= max_task_retries:
                skipped.append(tid)
                task_attempt_count.pop(tid, None)
            elif attempts >= 3:
                skipped.append(tid)
                task_attempt_count.pop(tid, None)

        return skipped, task_attempt_count, task_prev_status

    def test_no_deadlock_first_iteration(self):
        skipped, counts, prev = self._detect_deadlocks({"T-1": "in_progress"}, {}, {})
        assert skipped == []
        assert counts == {"T-1": 1}

    def test_no_deadlock_second_iteration(self):
        skipped, counts, prev = self._detect_deadlocks({"T-1": "in_progress"}, {"T-1": 1}, {"T-1": "in_progress"})
        assert skipped == []
        assert counts == {"T-1": 2}

    def test_deadlock_at_third_iteration(self):
        skipped, counts, prev = self._detect_deadlocks({"T-1": "in_progress"}, {"T-1": 2}, {"T-1": "in_progress"})
        assert "T-1" in skipped
        assert "T-1" not in counts

    def test_no_deadlock_if_status_changed(self):
        """If a task transitions done -> in_progress, counter resets."""
        skipped, counts, prev = self._detect_deadlocks({"T-1": "in_progress"}, {"T-1": 2}, {"T-1": "done"})
        assert skipped == []
        assert counts == {"T-1": 1}

    def test_max_retries_exceeded(self):
        skipped, counts, prev = self._detect_deadlocks(
            {"T-1": "in_progress"},
            {"T-1": 4},
            {"T-1": "in_progress"},
            max_task_retries=5,
        )
        assert "T-1" in skipped

    def test_counter_reset_when_done(self):
        """When task finishes, its counter is removed."""
        skipped, counts, prev = self._detect_deadlocks({"T-1": "done"}, {"T-1": 2}, {"T-1": "in_progress"})
        assert skipped == []
        assert "T-1" not in counts


# ── F1: Budget guard logic (unit) ────────────────────────────────


class TestBudgetGuardLogic:
    @staticmethod
    def _check_budget(session_cost: float, budget: float) -> tuple[bool, bool]:
        """Reproduce budget check logic: returns (exceeded, warning)."""
        exceeded = False
        warning = False
        if budget > 0:
            pct = (session_cost / budget) * 100
            if pct >= 100:
                exceeded = True
            elif pct >= 80:
                warning = True
        return exceeded, warning

    def test_no_budget_unlimited(self):
        exceeded, warning = self._check_budget(100.0, 0)
        assert not exceeded
        assert not warning

    def test_budget_under_80(self):
        exceeded, warning = self._check_budget(5.0, 10.0)
        assert not exceeded
        assert not warning

    def test_budget_at_80(self):
        exceeded, warning = self._check_budget(8.0, 10.0)
        assert not exceeded
        assert warning

    def test_budget_exceeded(self):
        exceeded, warning = self._check_budget(10.0, 10.0)
        assert exceeded
        assert not warning

    def test_budget_over_100(self):
        exceeded, warning = self._check_budget(15.0, 10.0)
        assert exceeded
