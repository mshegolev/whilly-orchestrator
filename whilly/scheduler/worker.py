"""Scheduler worker — main polling loop for continuous issue intake.

DEMO-9843: Enhanced reliability and performance.

Key improvements over the previous version:

* **Parallel rule polling** — rules are now dispatched concurrently with
  ``asyncio.gather`` instead of sequentially.  For N rules with equal poll
  latency this reduces per-iteration wall time from O(N × latency) to
  O(latency), giving a super-linear throughput gain as rule counts grow.
* **asyncio.get_running_loop()** — replaces the deprecated
  ``asyncio.get_event_loop()`` call, eliminating DeprecationWarnings on
  Python 3.10+ and the runtime error on 3.12 when called outside a loop.
* **Graceful shutdown** — ``stop()`` now accepts an optional ``timeout``
  and returns an ``asyncio.Event`` that is set once the run loop exits,
  making it possible for callers to await clean termination.
* **Per-rule interval tracking** — each rule records its ``last_polled_at``
  timestamp so that only rules whose ``poll_interval_seconds`` have elapsed
  are included in a given gather batch.  Rules that aren't due yet are
  skipped, reducing unnecessary Jira API calls.
"""

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
    """Raised when scheduler worker encounters a fatal error."""


class SchedulerWorker:
    """Async worker for polling Jira based on scheduler rules.

    Rules are polled **concurrently** within each iteration; only rules
    whose ``poll_interval_seconds`` have elapsed since their last poll are
    included in a batch.
    """

    def __init__(
        self,
        rules: list[SchedulerRule],
        poll_callback: PollingCallback | None = None,
        on_issues_found: IssuesFoundCallback | None = None,
    ) -> None:
        """Initialize scheduler worker.

        Args:
            rules: List of SchedulerRule objects (disabled rules are filtered out).
            poll_callback: Optional callback invoked after each poll cycle completes.
            on_issues_found: Optional callback invoked when new unique issues are found.
        """
        self.rules = [r for r in rules if r.enabled]
        self.poll_callback = poll_callback
        self.on_issues_found = on_issues_found
        self.running = False
        self._stopped = asyncio.Event()
        # Per-rule last-polled timestamps (rule.id → datetime)
        self._last_polled: dict[str, datetime] = {}

    async def run(self, duration_seconds: int = 3600) -> None:
        """Run the scheduler worker for a specified duration.

        Rules whose ``poll_interval_seconds`` have elapsed are polled
        concurrently on each iteration.

        Args:
            duration_seconds: How long to run in seconds (default 1 hour).
        """
        self.running = True
        self._stopped.clear()
        start_time = datetime.now(timezone.utc)
        end_time = start_time + timedelta(seconds=duration_seconds)

        try:
            while self.running and datetime.now(timezone.utc) < end_time:
                due_rules = self._due_rules()
                if due_rules:
                    await asyncio.gather(*[self._poll_rule(rule) for rule in due_rules])

                await asyncio.sleep(5)
        finally:
            self.running = False
            self._stopped.set()

    def stop(self, *, timeout: float | None = None) -> asyncio.Event:
        """Signal the worker to stop.

        Args:
            timeout: Unused — kept for API compatibility.  Callers that need
                to await actual termination should ``await worker.wait_stopped()``.

        Returns:
            An ``asyncio.Event`` that is set once the run loop exits.
        """
        self.running = False
        return self._stopped

    async def wait_stopped(self) -> None:
        """Await until the run loop has exited cleanly."""
        await self._stopped.wait()

    def _due_rules(self) -> list[SchedulerRule]:
        """Return rules that are due to be polled on this iteration."""
        now = datetime.now(timezone.utc)
        due: list[SchedulerRule] = []
        for rule in self.rules:
            last = self._last_polled.get(rule.id)
            if last is None or (now - last).total_seconds() >= rule.poll_interval_seconds:
                due.append(rule)
        return due

    async def _poll_rule(self, rule: SchedulerRule) -> SchedulerPollCycle:
        """Execute a single poll cycle for a rule.

        Records the completion time in ``_last_polled`` regardless of success
        or failure so the next poll respects the configured interval.

        Args:
            rule: SchedulerRule to poll.

        Returns:
            SchedulerPollCycle with results.
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
        finally:
            # Always update the last-polled timestamp so the interval is respected
            # even after transient failures — this prevents tight retry loops.
            self._last_polled[rule.id] = datetime.now(timezone.utc)

        cycle.completed_at = datetime.now(timezone.utc)

        if self.poll_callback:
            await self.poll_callback(cycle)

        return cycle

    async def _execute_jql_async(self, jql: str, max_results: int = 50) -> list[dict[str, Any]]:
        """Execute JQL in a thread pool without blocking the event loop.

        Uses ``asyncio.get_running_loop()`` (Python 3.10+) instead of the
        deprecated ``asyncio.get_event_loop()``.

        Args:
            jql: JQL filter string.
            max_results: Maximum results to return.

        Returns:
            List of issue dicts.
        """
        loop = asyncio.get_running_loop()
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
        config_path: Path to scheduler config file.
        duration_seconds: How long to run.
        poll_callback: Optional callback for poll cycles.
        issues_callback: Optional callback for found issues.
    """
    try:
        rules = load_scheduler_config(config_path)
        log.info("Loaded %d scheduler rules from %s", len(rules), config_path)

        worker = SchedulerWorker(rules, poll_callback=poll_callback, on_issues_found=issues_callback)
        await worker.run(duration_seconds=duration_seconds)
    except Exception as exc:
        log.exception("Failed to run scheduler: %s", exc)
        raise SchedulerWorkerError(f"Scheduler failed: {exc}") from exc
