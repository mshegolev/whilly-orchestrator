"""Integration tests for scheduler components working together."""

from __future__ import annotations

import pytest

from whilly.scheduler import (
    InMemorySchedulerRepository,
    MetricsCollector,
    PollMetrics,
    PollRateLimiter,
    RateLimiter,
    SchedulerPollCycle,
    SchedulerRule,
    WebhookEventHandler,
    deduplicate_issues,
)
from whilly.scheduler.rate_limit import BackoffStrategy


class TestSchedulerIntegration:
    """Integration tests for scheduler components."""

    @pytest.mark.asyncio
    async def test_end_to_end_poll_cycle(self) -> None:
        """Test complete poll cycle with rate limiting and metrics."""
        repo = InMemorySchedulerRepository()
        limiter = PollRateLimiter()
        metrics = MetricsCollector()

        rule = SchedulerRule(
            id="integration-test",
            name="Integration Test Rule",
            jira_project_key="TEST",
            jql_filter="project = TEST AND status = Open",
        )

        await repo.create_rule(rule)

        await limiter.wait_until_ready()

        issues = [
            {"key": "TEST-1", "summary": "Bug 1"},
            {"key": "TEST-2", "summary": "Bug 1"},
            {"key": "TEST-3", "summary": "Bug 2"},
        ]

        unique, dups = deduplicate_issues(issues, ("summary",))

        cycle = SchedulerPollCycle(
            id=0,
            rule_id=rule.id,
            poll_status="completed",
            total_issues_found=len(issues),
            deduplicated_issues=unique,
            duplicate_issues_skipped=len(dups),
        )

        cycle_id = await repo.record_poll_cycle(cycle)

        poll_metric = PollMetrics(
            rule_id=rule.id,
            success=True,
            duration_seconds=1.5,
            issues_found=len(issues),
            issues_unique=len(unique),
            issues_duplicated=len(dups),
        )

        metrics.record_poll(poll_metric)

        assert cycle_id == 1
        assert len(unique) == 2
        assert len(dups) == 1
        assert metrics.get_summary()["total_polls"] == 1

    @pytest.mark.asyncio
    async def test_retry_with_metrics(self) -> None:
        """Test retry logic integration with metrics."""
        limiter = RateLimiter(
            max_retries=2,
            initial_delay=0.01,
            strategy=BackoffStrategy.LINEAR,
            jitter=False,
        )
        metrics = MetricsCollector()

        attempt_count = 0

        async def flaky_operation() -> int:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 2:
                raise ValueError("Temporary failure")
            return attempt_count

        result = await limiter.call_with_retry(flaky_operation)

        assert result == 2
        assert attempt_count == 2

        metric = PollMetrics(
            rule_id="flaky-rule",
            success=True,
            duration_seconds=0.05,
            api_requests=attempt_count,
        )
        metrics.record_poll(metric)

        summary = metrics.get_summary()
        assert summary["total_polls"] == 1

    @pytest.mark.asyncio
    async def test_webhook_with_rule_matching(self) -> None:
        """Test webhook event handling with rule matching."""
        from whilly.scheduler.webhooks import JiraWebhookEvent

        handler = WebhookEventHandler()
        matched_rules = []

        def check_rule(event: JiraWebhookEvent) -> None:
            rule = SchedulerRule(
                id="webhook-rule",
                name="Webhook Rule",
                jira_project_key="WEBHOOK",
                jql_filter="project = WEBHOOK AND status = Open",
            )
            if event.matches_rule(rule.jql_filter):
                matched_rules.append(event.issue_key)

        handler.register_callback("jira:issue_created", check_rule)

        payload = {
            "webhookEvent": "jira:issue_created",
            "issue": {
                "key": "WEBHOOK-100",
                "fields": {
                    "summary": "New issue",
                    "status": {"name": "Open"},
                    "project": {"key": "WEBHOOK"},
                },
            },
        }

        await handler.handle_event(payload)

        assert len(matched_rules) == 1
        assert matched_rules[0] == "WEBHOOK-100"

    @pytest.mark.asyncio
    async def test_multiple_rules_aggregated_metrics(self) -> None:
        """Test metrics aggregation across multiple rules."""
        repo = InMemorySchedulerRepository()
        metrics = MetricsCollector()

        rules = [
            SchedulerRule(
                id=f"rule-{i}",
                name=f"Rule {i}",
                jira_project_key=f"PROJ{i}",
                jql_filter=f"project = PROJ{i}",
            )
            for i in range(3)
        ]

        for rule in rules:
            await repo.create_rule(rule)

        for i, rule in enumerate(rules):
            metric = PollMetrics(
                rule_id=rule.id,
                success=i < 2,
                duration_seconds=float(i + 1),
                issues_found=10 * (i + 1),
                issues_unique=8 * (i + 1),
                issues_duplicated=2 * (i + 1),
            )
            metrics.record_poll(metric)

        summary = metrics.get_summary()
        assert summary["total_polls"] == 3
        assert summary["successful_polls"] == 2
        assert summary["failed_polls"] == 1

        rule_summary_0 = metrics.get_rule_summary("rule-0")
        assert rule_summary_0["polls"] == 1
        assert rule_summary_0["successful"] == 1
        assert rule_summary_0["total_issues"] == 10

        rule_summary_2 = metrics.get_rule_summary("rule-2")
        assert rule_summary_2["polls"] == 1
        assert rule_summary_2["successful"] == 0
        assert rule_summary_2["failed"] == 1
