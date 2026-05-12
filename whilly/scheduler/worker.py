"""Scheduler worker — main polling loop for continuous issue intake."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from whilly.scheduler.config import load_scheduler_config
from whilly.scheduler.deduplicator import deduplicate_issues
from whilly.scheduler.jql_executor import execute_jql, JQLExecutionError
from whilly.scheduler.models import SchedulerPollCycle, SchedulerRule

log = logging.getLogger(__name__)


class SchedulerWorkerError(RuntimeError):
    """Raised when scheduler worker encounters an error."""


class SchedulerWorker:
    """Async worker for polling Jira based on scheduler rules."""

    def __init__(
        self,
        rules: list[SchedulerRule],
        poll_callback: PollingCallback | None = None,
        on_issues_found: IssuesFoundCallback | None = None,
    ):
        """Initialize scheduler worker.

        Args:
            rules: List of SchedulerRule objects
            poll_callback: Optional callback for poll cycle completion
            on_issues_found: Optional callback when issues are discovered
        """
        self.rules = [r for r in rules if r.enabled]
        self.poll_callback = poll_callback
        self.on_issues_found = on_issues_found
        self.running = False

    async def run(self, duration_seconds: int = 3600) -> None:
        """Run the scheduler worker for a specified duration.

        Args:
            duration_seconds: How long to run (default 1 hour)
        """
        self.running = True
        start_time = datetime.now(timezone.utc)
        end_time = start_time + timedelta(seconds=duration_seconds)

        try:
            while self.running and datetime.now(timezone.utc) < end_time:
                for rule in self.rules:
                    if not self.running:
                        break

                    await self._poll_rule(rule)
                    await asyncio.sleep(1)

                await asyncio.sleep(5)
        finally:
            self.running = False

    def stop(self) -> None:
        """Stop the worker."""
        self.running = False

    async def _poll_rule(self, rule: SchedulerRule) -> SchedulerPollCycle:
        """Execute a single poll cycle for a rule.

        Args:
            rule: SchedulerRule to poll

        Returns:
            SchedulerPollCycle with results
        """
        cycle = SchedulerPollCycle(
            id=0,
            rule_id=rule.id,
            poll_status="running",
            created_at=datetime.now(timezone.utc),
        )

        try:
            log.info("Polling rule %s: %s", rule.id, rule.name)
            issues = await self._execute_jql_async(
                rule.jql_filter,
                max_results=rule.max_results_per_poll,
            )
            cycle.total_issues_found = len(issues)
            cycle.jql_results = issues

            unique_issues, duplicate_keys = deduplicate_issues(
                issues,
                fields_to_hash=rule.deduplication_fields,
            )
            cycle.deduplicated_issues = unique_issues
            cycle.duplicate_issues_skipped = len(duplicate_keys)

            log.info(
                "Rule %s: found %d issues, %d unique, %d duplicates",
                rule.id,
                len(issues),
                len(unique_issues),
                len(duplicate_keys),
            )

            if unique_issues and self.on_issues_found:
                await self.on_issues_found(rule, unique_issues)
                cycle.new_issues_created = len(unique_issues)

            cycle.poll_status = "completed"
        except JQLExecutionError as exc:
            log.error("JQL execution failed for rule %s: %s", rule.id, exc)
            cycle.poll_status = "failed"
            cycle.error_message = str(exc)
        except Exception as exc:
            log.exception("Unexpected error polling rule %s", rule.id)
            cycle.poll_status = "failed"
            cycle.error_message = f"Unexpected error: {exc}"

        cycle.completed_at = datetime.now(timezone.utc)

        if self.poll_callback:
            await self.poll_callback(cycle)

        return cycle

    async def _execute_jql_async(self, jql: str, max_results: int = 50) -> list[dict[str, Any]]:
        """Execute JQL asynchronously.

        Args:
            jql: JQL filter string
            max_results: Maximum results to return

        Returns:
            List of issue dicts
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: execute_jql(jql, max_results=max_results))


PollingCallback = Callable[[SchedulerPollCycle], Any]
IssuesFoundCallback = Callable[[SchedulerRule, list[dict[str, Any]]], Any]


async def run_scheduler_from_config(
    config_path: str,
    duration_seconds: int = 3600,
    poll_callback: PollingCallback | None = None,
    issues_callback: IssuesFoundCallback | None = None,
) -> None:
    """Load scheduler configuration and run the worker.

    Args:
        config_path: Path to scheduler config file
        duration_seconds: How long to run
        poll_callback: Optional callback for poll cycles
        issues_callback: Optional callback for found issues
    """
    try:
        rules = load_scheduler_config(config_path)
        log.info("Loaded %d scheduler rules from %s", len(rules), config_path)

        worker = SchedulerWorker(rules, poll_callback=poll_callback, on_issues_found=issues_callback)
        await worker.run(duration_seconds=duration_seconds)
    except Exception as exc:
        log.exception("Failed to run scheduler: %s", exc)
        raise SchedulerWorkerError(f"Scheduler failed: {exc}") from exc
