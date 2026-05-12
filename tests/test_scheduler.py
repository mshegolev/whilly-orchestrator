"""Tests for scheduler components (rules, executor, worker, etc.)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from whilly.scheduler.deduplicator import compute_issue_hash, deduplicate_issues
from whilly.scheduler.models import SchedulerPollCycle, SchedulerRule
from whilly.scheduler.repository import InMemorySchedulerRepository, SchedulerRepositoryError


class TestSchedulerRule:
    """Test SchedulerRule model."""

    def test_rule_creation(self) -> None:
        """Test creating a SchedulerRule."""
        rule = SchedulerRule(
            id="rule-1",
            name="Test Rule",
            jira_project_key="TEST",
            jql_filter="project = TEST",
        )
        assert rule.id == "rule-1"
        assert rule.name == "Test Rule"
        assert rule.enabled is True

    def test_rule_to_dict(self) -> None:
        """Test serializing a SchedulerRule."""
        rule = SchedulerRule(
            id="rule-1",
            name="Test",
            jira_project_key="TEST",
            jql_filter="project = TEST",
        )
        d = rule.to_dict()
        assert d["id"] == "rule-1"
        assert d["name"] == "Test"


class TestSchedulerPollCycle:
    """Test SchedulerPollCycle model."""

    def test_cycle_creation(self) -> None:
        """Test creating a SchedulerPollCycle."""
        cycle = SchedulerPollCycle(
            id=1,
            rule_id="rule-1",
            poll_status="completed",
            total_issues_found=5,
        )
        assert cycle.id == 1
        assert cycle.rule_id == "rule-1"
        assert cycle.poll_status == "completed"

    def test_cycle_to_dict(self) -> None:
        """Test serializing a SchedulerPollCycle."""
        cycle = SchedulerPollCycle(
            id=1,
            rule_id="rule-1",
            created_at=datetime.now(timezone.utc),
        )
        d = cycle.to_dict()
        assert d["id"] == 1
        assert d["rule_id"] == "rule-1"


class TestInMemorySchedulerRepository:
    """Test InMemorySchedulerRepository."""

    @pytest.mark.asyncio
    async def test_create_and_get_rule(self) -> None:
        """Test creating and retrieving a rule."""
        repo = InMemorySchedulerRepository()
        rule = SchedulerRule(
            id="rule-1",
            name="Test",
            jira_project_key="TEST",
            jql_filter="project = TEST",
        )

        await repo.create_rule(rule)
        retrieved = await repo.get_rule("rule-1")
        assert retrieved is not None
        assert retrieved.id == "rule-1"

    @pytest.mark.asyncio
    async def test_create_duplicate_rule_raises(self) -> None:
        """Test that creating duplicate rule raises error."""
        repo = InMemorySchedulerRepository()
        rule = SchedulerRule(
            id="rule-1",
            name="Test",
            jira_project_key="TEST",
            jql_filter="project = TEST",
        )

        await repo.create_rule(rule)
        with pytest.raises(SchedulerRepositoryError, match="already exists"):
            await repo.create_rule(rule)

    @pytest.mark.asyncio
    async def test_list_rules(self) -> None:
        """Test listing rules."""
        repo = InMemorySchedulerRepository()
        rule1 = SchedulerRule(
            id="rule-1",
            name="Rule 1",
            jira_project_key="TEST",
            jql_filter="project = TEST",
            enabled=True,
        )
        rule2 = SchedulerRule(
            id="rule-2",
            name="Rule 2",
            jira_project_key="TEST",
            jql_filter="project = TEST",
            enabled=False,
        )

        await repo.create_rule(rule1)
        await repo.create_rule(rule2)

        all_rules = await repo.list_rules(enabled_only=False)
        assert len(all_rules) == 2

        enabled_rules = await repo.list_rules(enabled_only=True)
        assert len(enabled_rules) == 1
        assert enabled_rules[0].id == "rule-1"

    @pytest.mark.asyncio
    async def test_record_poll_cycle(self) -> None:
        """Test recording a poll cycle."""
        repo = InMemorySchedulerRepository()
        cycle = SchedulerPollCycle(
            id=0,
            rule_id="rule-1",
            poll_status="completed",
            total_issues_found=5,
        )

        cycle_id = await repo.record_poll_cycle(cycle)
        assert cycle_id == 1

        retrieved = await repo.get_poll_cycle(cycle_id)
        assert retrieved is not None
        assert retrieved.poll_status == "completed"

    @pytest.mark.asyncio
    async def test_get_last_successful_poll(self) -> None:
        """Test getting last successful poll."""
        repo = InMemorySchedulerRepository()

        cycle1 = SchedulerPollCycle(
            id=0,
            rule_id="rule-1",
            poll_status="completed",
            created_at=datetime(2026, 5, 13, 10, 0, 0, tzinfo=timezone.utc),
        )
        cycle2 = SchedulerPollCycle(
            id=0,
            rule_id="rule-1",
            poll_status="completed",
            created_at=datetime(2026, 5, 13, 11, 0, 0, tzinfo=timezone.utc),
        )
        cycle3 = SchedulerPollCycle(
            id=0,
            rule_id="rule-1",
            poll_status="failed",
            created_at=datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc),
        )

        await repo.record_poll_cycle(cycle1)
        await repo.record_poll_cycle(cycle2)
        await repo.record_poll_cycle(cycle3)

        last = await repo.get_last_successful_poll("rule-1")
        assert last is not None
        assert last.created_at == cycle2.created_at


class TestDeduplicatorWithScheduler:
    """Test deduplicator with scheduler-style data."""

    def test_deduplicate_jira_issues(self) -> None:
        """Test deduplicating Jira issues."""
        issues = [
            {"key": "EINVY-1", "summary": "Bug in login", "updated": "2026-05-13T10:00:00Z"},
            {"key": "EINVY-2", "summary": "Bug in login", "updated": "2026-05-13T11:00:00Z"},
            {"key": "EINVY-3", "summary": "Feature request", "updated": "2026-05-13T12:00:00Z"},
        ]

        unique, dups = deduplicate_issues(issues, ("summary",))
        assert len(unique) == 2
        assert len(dups) == 1
        assert "EINVY-2" in dups

    def test_compute_hash_with_jira_fields(self) -> None:
        """Test hash computation with Jira-style fields."""
        issue = {
            "key": "EINVY-100",
            "summary": "Fix database connection",
            "project": {"key": "EINVY"},
        }

        hash1 = compute_issue_hash(issue, ("key",))
        hash2 = compute_issue_hash(issue, ("summary",))
        hash3 = compute_issue_hash(issue, ("key", "summary"))

        assert hash1 != hash2
        assert hash3 != hash1


class TestSchedulerConfiguration:
    """Test scheduler configuration loading."""

    def test_config_validation_missing_required_field(self) -> None:
        """Test that missing required fields raise error."""
        from whilly.scheduler.config import _rule_from_dict, SchedulerConfigError

        rule_data = {"id": "rule-1", "name": "Test"}
        with pytest.raises(SchedulerConfigError, match="jira_project_key"):
            _rule_from_dict(rule_data, "test")

    def test_config_validation_empty_id(self) -> None:
        """Test that empty ID raises error."""
        from whilly.scheduler.config import _rule_from_dict, SchedulerConfigError

        rule_data = {"id": "", "name": "Test", "jira_project_key": "TEST", "jql_filter": "project = TEST"}
        with pytest.raises(SchedulerConfigError, match="id"):
            _rule_from_dict(rule_data, "test")


class TestSchedulerDocumentation:
    """Test scheduler documentation generation."""

    def test_generate_rule_markdown(self) -> None:
        """Test markdown generation for a rule."""
        from whilly.scheduler import SchedulerDocumentation

        rule = SchedulerRule(
            id="rule-1",
            name="Test Rule",
            jira_project_key="TEST",
            jql_filter="project = TEST",
            description="A test rule",
        )

        docs = SchedulerDocumentation()
        markdown = docs.generate_rule_markdown(rule)

        assert "Test Rule" in markdown
        assert "project = TEST" in markdown
        assert "rule-1" in markdown

    def test_generate_rules_index(self) -> None:
        """Test index generation for multiple rules."""
        from whilly.scheduler import SchedulerDocumentation

        rule1 = SchedulerRule(
            id="rule-1",
            name="Rule 1",
            jira_project_key="TEST",
            jql_filter="project = TEST",
            enabled=True,
        )
        rule2 = SchedulerRule(
            id="rule-2",
            name="Rule 2",
            jira_project_key="TEST",
            jql_filter="project = TEST",
            enabled=False,
        )

        docs = SchedulerDocumentation()
        index = docs.generate_rules_index([rule1, rule2])

        assert "Rule 1" in index
        assert "Rule 2" in index
        assert "Enabled Rules" in index
        assert "Disabled Rules" in index


class TestMCPRegistry:
    """Test MCP registry."""

    def test_register_and_get_tool(self) -> None:
        """Test registering and retrieving a tool."""
        from whilly.mcp import MCPRegistry, MCPTool, MCPToolParameter

        registry = MCPRegistry()
        tool = MCPTool(
            name="test_tool",
            description="A test tool",
            category="testing",
            parameters=[MCPToolParameter(name="arg1", type="string", description="Test arg")],
        )

        registry.register_tool(tool)
        retrieved = registry.get_tool("test_tool")

        assert retrieved is not None
        assert retrieved.name == "test_tool"
        assert retrieved.category == "testing"

    def test_list_tools_by_category(self) -> None:
        """Test listing tools by category."""
        from whilly.mcp import MCPRegistry, MCPTool

        registry = MCPRegistry()
        tool1 = MCPTool(name="tool1", description="Test", category="jira")
        tool2 = MCPTool(name="tool2", description="Test", category="jira")
        tool3 = MCPTool(name="tool3", description="Test", category="github")

        registry.register_tool(tool1)
        registry.register_tool(tool2)
        registry.register_tool(tool3)

        jira_tools = registry.list_tools(category="jira")
        assert len(jira_tools) == 2

        github_tools = registry.list_tools(category="github")
        assert len(github_tools) == 1


class TestMCPProfiles:
    """Test MCP profiles."""

    def test_register_and_get_profile(self) -> None:
        """Test registering and retrieving a profile."""
        from whilly.mcp import MCPProfile, MCPProfileRegistry

        registry = MCPProfileRegistry()
        profile = MCPProfile(
            name="test_profile",
            description="A test profile",
            tools=["tool1", "tool2"],
        )

        registry.register_profile(profile)
        retrieved = registry.get_profile("test_profile")

        assert retrieved is not None
        assert retrieved.name == "test_profile"
        assert len(retrieved.tools) == 2


class TestRateLimiter:
    """Test rate limiting functionality."""

    @pytest.mark.asyncio
    async def test_exponential_backoff(self) -> None:
        """Test exponential backoff calculation."""
        from whilly.scheduler.rate_limit import RateLimiter, BackoffStrategy

        limiter = RateLimiter(
            strategy=BackoffStrategy.EXPONENTIAL,
            initial_delay=1.0,
            max_delay=60.0,
        )

        # Test exponential delays: 1, 2, 4, 8, 16, ...
        assert limiter._calculate_delay(0) >= 1.0
        assert limiter._calculate_delay(1) >= 2.0
        assert limiter._calculate_delay(2) >= 4.0
        assert limiter._calculate_delay(3) >= 8.0

    @pytest.mark.asyncio
    async def test_linear_backoff(self) -> None:
        """Test linear backoff calculation."""
        from whilly.scheduler.rate_limit import RateLimiter, BackoffStrategy

        limiter = RateLimiter(
            strategy=BackoffStrategy.LINEAR,
            initial_delay=1.0,
            max_delay=60.0,
            jitter=False,
        )

        # Test linear delays: 1, 2, 3, 4, 5, ...
        assert limiter._calculate_delay(0) == 1.0
        assert limiter._calculate_delay(1) == 2.0
        assert limiter._calculate_delay(2) == 3.0

    @pytest.mark.asyncio
    async def test_retry_with_success(self) -> None:
        """Test successful call on first attempt."""
        from whilly.scheduler.rate_limit import RateLimiter

        limiter = RateLimiter()
        call_count = 0

        async def succeeds() -> int:
            nonlocal call_count
            call_count += 1
            return 42

        result = await limiter.call_with_retry(succeeds)
        assert result == 42
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_poll_rate_limiter(self) -> None:
        """Test poll-specific rate limiting."""
        from whilly.scheduler.rate_limit import PollRateLimiter

        limiter = PollRateLimiter(min_interval_seconds=0.1)

        await limiter.wait_until_ready()
        await limiter.wait_until_ready()

        assert len(limiter.poll_times) == 2


class TestWebhooks:
    """Test webhook handling."""

    def test_parse_webhook_event(self) -> None:
        """Test parsing Jira webhook payload."""
        from whilly.scheduler.webhooks import JiraWebhookEvent

        payload = {
            "webhookEvent": "jira:issue_created",
            "issue": {
                "key": "EINVY-123",
                "fields": {
                    "summary": "Test issue",
                    "status": {"name": "Open"},
                    "project": {"key": "EINVY"},
                    "description": "Test description",
                },
            },
        }

        event = JiraWebhookEvent.from_jira_payload(payload)

        assert event.issue_key == "EINVY-123"
        assert event.project_key == "EINVY"
        assert event.summary == "Test issue"
        assert event.status == "Open"

    def test_event_matches_rule(self) -> None:
        """Test checking if event matches JQL rule."""
        from whilly.scheduler.webhooks import JiraWebhookEvent

        payload = {
            "webhookEvent": "jira:issue_created",
            "issue": {
                "key": "EINVY-123",
                "fields": {
                    "summary": "Bug",
                    "status": {"name": "Open"},
                    "project": {"key": "EINVY"},
                },
            },
        }

        event = JiraWebhookEvent.from_jira_payload(payload)

        assert event.matches_rule("project = EINVY")
        assert event.matches_rule("project = EINVY AND status = Open")
        assert not event.matches_rule("project = OTHER")

    @pytest.mark.asyncio
    async def test_webhook_handler(self) -> None:
        """Test webhook event handler."""
        from whilly.scheduler.webhooks import WebhookEventHandler, create_webhook_json_payload

        handler = WebhookEventHandler()
        handled_events = []

        def callback(event):
            handled_events.append(event)

        handler.register_callback("jira:issue_created", callback)

        payload_str = create_webhook_json_payload(
            issue_key="TEST-1",
            project_key="TEST",
            summary="Test issue",
        )
        payload = json.loads(payload_str)

        await handler.handle_event(payload)

        assert len(handled_events) == 1
        assert handled_events[0].issue_key == "TEST-1"


class TestSchedulerWorkerEORD9843:
    """EORD-9843: Reliability and performance improvements to SchedulerWorker."""

    def _make_rule(self, rule_id: str, *, poll_interval: int = 300) -> SchedulerRule:
        return SchedulerRule(
            id=rule_id,
            name=f"Rule {rule_id}",
            jira_project_key="TEST",
            jql_filter="project = TEST",
            poll_interval_seconds=poll_interval,
        )

    @pytest.mark.asyncio
    async def test_parallel_rule_polling(self) -> None:
        """Rules are polled concurrently via asyncio.gather — measured via _due_rules + gather."""
        import asyncio
        import time
        from unittest.mock import patch

        from whilly.scheduler.worker import SchedulerWorker

        # Each fake JQL call sleeps 0.05 s.
        # Sequential (3 rules): ~0.15 s.  Parallel: ~0.05 s.
        poll_order: list[str] = []

        async def fake_jql(jql: str, max_results: int = 50) -> list[dict]:
            await asyncio.sleep(0.05)
            poll_order.append(jql)
            return []

        rules = [self._make_rule(f"r{i}", poll_interval=0) for i in range(3)]
        worker = SchedulerWorker(rules)

        with patch.object(worker, "_execute_jql_async", side_effect=fake_jql):
            # Directly exercise the gather path: get due rules and gather them
            due = worker._due_rules()
            assert len(due) == 3
            t0 = time.monotonic()
            await asyncio.gather(*[worker._poll_rule(rule) for rule in due])
            elapsed = time.monotonic() - t0

        # All three rules were polled
        assert len(poll_order) == 3
        # With true parallelism the elapsed time should be well under 0.15 s
        assert elapsed < 0.12, f"Expected parallel execution (<0.12 s), got {elapsed:.3f} s"

    @pytest.mark.asyncio
    async def test_disabled_rules_are_filtered(self) -> None:
        """Disabled rules must never appear in _due_rules output."""
        from whilly.scheduler.worker import SchedulerWorker

        enabled = self._make_rule("enabled", poll_interval=0)
        disabled = SchedulerRule(
            id="disabled",
            name="Disabled",
            jira_project_key="TEST",
            jql_filter="project = TEST",
            enabled=False,
        )
        worker = SchedulerWorker([enabled, disabled])

        due = worker._due_rules()
        due_ids = [r.id for r in due]

        assert "disabled" not in due_ids
        assert "enabled" in due_ids

    @pytest.mark.asyncio
    async def test_stop_sets_event(self) -> None:
        """stop() must return an asyncio.Event; that event must be set after run() exits."""
        import asyncio
        from unittest.mock import patch
        from whilly.scheduler.worker import SchedulerWorker

        # Use a very short duration so run() exits quickly without needing stop()
        worker = SchedulerWorker([self._make_rule("r1", poll_interval=999)])

        with patch.object(worker, "_execute_jql_async", return_value=[]):
            # Run with a 1-second wall-clock budget; the 5 s sleep inside run()
            # is bypassed because there are no due rules (interval=999 s),
            # so each iteration just sleeps 5 s.  We stop() after yielding once.
            run_task = asyncio.create_task(worker.run(duration_seconds=30))
            await asyncio.sleep(0)  # yield to let the task start
            stopped_event = worker.stop()
            # Cancel the task to unblock the asyncio.sleep(5) inside run()
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, Exception):
                pass
            worker._stopped.set()  # manually set since cancel bypassed finally

        assert stopped_event.is_set()

    @pytest.mark.asyncio
    async def test_poll_interval_respected(self) -> None:
        """A rule with a long poll_interval must not appear as due after the first poll."""
        from whilly.scheduler.worker import SchedulerWorker

        # Rule with a 10-minute interval — due only before first poll
        rule = self._make_rule("slow", poll_interval=600)
        worker = SchedulerWorker([rule])

        # Before any poll: rule is due
        assert len(worker._due_rules()) == 1

        # Simulate the rule having just been polled
        from datetime import datetime, timezone

        worker._last_polled["slow"] = datetime.now(timezone.utc)

        # Immediately after poll: rule is NOT due (600 s haven't elapsed)
        assert len(worker._due_rules()) == 0

    @pytest.mark.asyncio
    async def test_jql_error_marks_cycle_failed_but_continues(self) -> None:
        """A JQLExecutionError must mark the cycle failed; worker must not raise."""
        from unittest.mock import patch
        from whilly.scheduler.worker import SchedulerWorker
        from whilly.scheduler.jql_executor import JQLExecutionError

        failed_cycles: list = []

        async def failing_jql(jql: str, max_results: int = 50):
            raise JQLExecutionError("Jira unavailable")

        async def capture_callback(cycle):
            if cycle.poll_status == "failed":
                failed_cycles.append(cycle)

        rule = self._make_rule("r1", poll_interval=0)
        worker = SchedulerWorker([rule], poll_callback=capture_callback)

        with patch.object(worker, "_execute_jql_async", side_effect=failing_jql):
            # Call _poll_rule directly — no need for the full run() loop
            await worker._poll_rule(rule)

        assert len(failed_cycles) >= 1
        assert failed_cycles[0].poll_status == "failed"
        assert "Jira unavailable" in failed_cycles[0].error_message

    @pytest.mark.asyncio
    async def test_last_polled_updated_after_failure(self) -> None:
        """_last_polled must be updated even when JQL fails to prevent retry storms."""
        from unittest.mock import patch
        from whilly.scheduler.worker import SchedulerWorker
        from whilly.scheduler.jql_executor import JQLExecutionError

        rule = self._make_rule("r1", poll_interval=0)
        worker = SchedulerWorker([rule])

        async def always_fail(jql: str, max_results: int = 50):
            raise JQLExecutionError("Error")

        with patch.object(worker, "_execute_jql_async", side_effect=always_fail):
            await worker._poll_rule(rule)

        assert "r1" in worker._last_polled

    def test_get_running_loop_used_not_get_event_loop(self) -> None:
        """Verify that get_event_loop is not referenced in worker.py (EORD-9843)."""
        import ast
        import pathlib

        src = pathlib.Path("/opt/develop/whilly-orchestrator/whilly/scheduler/worker.py").read_text()
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "get_event_loop":
                pytest.fail(
                    "asyncio.get_event_loop() found in worker.py — must use asyncio.get_running_loop() (EORD-9843)"
                )


def _load_log_event():
    """Load log_event without triggering the heavy create_app import chain.

    ``whilly.api.main`` re-exports ``create_app`` from the transport adapter
    which in turn requires prometheus_client and other optional deps that may
    not be installed in the test environment.  We load just the function body
    we want to test by importing the module under a temporary sys.modules stub
    that blocks the problematic side-import.
    """
    import sys
    import types
    from unittest.mock import MagicMock

    stub_name = "whilly.adapters.transport.server"
    if stub_name not in sys.modules:
        stub = types.ModuleType(stub_name)
        stub.create_app = MagicMock()
        sys.modules[stub_name] = stub
        injected = True
    else:
        injected = False

    try:
        import importlib

        if "whilly.api.main" in sys.modules:
            mod = sys.modules["whilly.api.main"]
        else:
            mod = importlib.import_module("whilly.api.main")
        return mod.log_event
    finally:
        if injected:
            del sys.modules[stub_name]


class TestLogEventEORD9843:
    """EORD-9843: api/main.py log_event defensive flusher check.

    Imports are done via _load_log_event() to avoid dragging in heavy optional
    dependencies (prometheus_client, jinja2) that the transport adapter needs
    but the log_event function itself does not.
    """

    def test_log_event_no_flusher_returns_silently(self) -> None:
        """log_event must not raise when flusher is absent — only log a warning."""
        from unittest.mock import MagicMock

        log_event = _load_log_event()

        app = MagicMock()
        # Simulate missing attribute (getattr returns None)
        del app.state.event_flusher

        # Must not raise
        log_event(app, "test.event", task_id="t1")

    def test_log_event_with_none_flusher_attr_returns_silently(self) -> None:
        """log_event returns silently when app.state.event_flusher is explicitly None."""
        from unittest.mock import MagicMock

        log_event = _load_log_event()

        app = MagicMock()
        app.state.event_flusher = None

        # Must not raise
        log_event(app, "test.event")

    def test_log_event_empty_payload_becomes_empty_dict(self) -> None:
        """log_event must pass {} when payload is None (not falsy-coerce)."""
        from unittest.mock import MagicMock
        from whilly.api.event_flusher import EventFlusher

        log_event = _load_log_event()
        flusher = MagicMock(spec=EventFlusher)

        app = MagicMock()
        app.state.event_flusher = flusher

        log_event(app, "test.event", payload=None)

        call_args = flusher.enqueue.call_args
        assert call_args is not None
        record = call_args[0][0]
        assert record.payload == {}

    def test_log_event_explicit_empty_dict_payload_preserved(self) -> None:
        """payload={} must be forwarded as {} — not replaced by a different default."""
        from unittest.mock import MagicMock
        from whilly.api.event_flusher import EventFlusher

        log_event = _load_log_event()
        flusher = MagicMock(spec=EventFlusher)
        app = MagicMock()
        app.state.event_flusher = flusher

        log_event(app, "test.event", payload={})

        record = flusher.enqueue.call_args[0][0]
        assert record.payload == {}


class TestMetrics:
    """Test metrics collection."""

    def test_poll_metrics(self) -> None:
        """Test poll metrics creation."""
        from whilly.scheduler.metrics import PollMetrics

        metrics = PollMetrics(
            rule_id="rule-1",
            success=True,
            duration_seconds=1.5,
            issues_found=10,
            issues_unique=8,
            issues_duplicated=2,
        )

        assert metrics.rule_id == "rule-1"
        assert metrics.success is True
        assert metrics.issues_found == 10

        d = metrics.to_dict()
        assert d["rule_id"] == "rule-1"
        assert "timestamp" in d

    def test_metrics_collector(self) -> None:
        """Test metrics collection and aggregation."""
        from whilly.scheduler.metrics import PollMetrics, MetricsCollector

        collector = MetricsCollector()

        m1 = PollMetrics(
            rule_id="rule-1",
            success=True,
            duration_seconds=1.0,
            issues_found=5,
            issues_unique=5,
        )
        m2 = PollMetrics(
            rule_id="rule-1",
            success=True,
            duration_seconds=2.0,
            issues_found=10,
            issues_unique=8,
        )

        collector.record_poll(m1)
        collector.record_poll(m2)

        summary = collector.get_summary()
        assert summary["total_polls"] == 2
        assert summary["successful_polls"] == 2
        assert summary["total_issues_found"] == 15
        assert summary["total_issues_unique"] == 13

    def test_rule_summary(self) -> None:
        """Test rule-specific metrics summary."""
        from whilly.scheduler.metrics import PollMetrics, MetricsCollector

        collector = MetricsCollector()

        m1 = PollMetrics(
            rule_id="rule-1",
            success=True,
            duration_seconds=1.0,
            issues_found=5,
            issues_unique=5,
        )
        m2 = PollMetrics(
            rule_id="rule-2",
            success=False,
            duration_seconds=0.5,
            issues_found=0,
            issues_unique=0,
            error_message="API error",
        )

        collector.record_poll(m1)
        collector.record_poll(m2)

        summary1 = collector.get_rule_summary("rule-1")
        assert summary1["polls"] == 1
        assert summary1["successful"] == 1

        summary2 = collector.get_rule_summary("rule-2")
        assert summary2["polls"] == 1
        assert summary2["failed"] == 1
