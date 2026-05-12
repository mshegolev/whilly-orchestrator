"""Database repository for scheduler rules and poll cycles."""

from __future__ import annotations


from whilly.scheduler.models import SchedulerPollCycle, SchedulerRule


class SchedulerRepositoryError(RuntimeError):
    """Raised when repository operations fail."""


class SchedulerRepository:
    """Interface for persisting scheduler data to database.

    This is a placeholder for SQL-based persistence.
    Future implementation will use asyncpg or SQLAlchemy ORM.
    """

    async def create_rule(self, rule: SchedulerRule) -> None:
        """Create a new scheduler rule in the database.

        Args:
            rule: SchedulerRule to persist

        Raises:
            SchedulerRepositoryError: if creation fails
        """
        raise NotImplementedError("Database implementation pending")

    async def get_rule(self, rule_id: str) -> SchedulerRule | None:
        """Retrieve a scheduler rule by ID.

        Args:
            rule_id: Unique rule identifier

        Returns:
            SchedulerRule or None if not found
        """
        raise NotImplementedError("Database implementation pending")

    async def list_rules(self, enabled_only: bool = True) -> list[SchedulerRule]:
        """List all scheduler rules.

        Args:
            enabled_only: Only return enabled rules

        Returns:
            List of SchedulerRule objects
        """
        raise NotImplementedError("Database implementation pending")

    async def update_rule(self, rule: SchedulerRule) -> None:
        """Update an existing scheduler rule.

        Args:
            rule: SchedulerRule with updated values

        Raises:
            SchedulerRepositoryError: if update fails
        """
        raise NotImplementedError("Database implementation pending")

    async def delete_rule(self, rule_id: str) -> None:
        """Delete a scheduler rule.

        Args:
            rule_id: Unique rule identifier

        Raises:
            SchedulerRepositoryError: if deletion fails
        """
        raise NotImplementedError("Database implementation pending")

    async def record_poll_cycle(self, cycle: SchedulerPollCycle) -> int:
        """Record a completed poll cycle.

        Args:
            cycle: SchedulerPollCycle to persist

        Returns:
            Assigned ID for the poll cycle

        Raises:
            SchedulerRepositoryError: if recording fails
        """
        raise NotImplementedError("Database implementation pending")

    async def get_poll_cycle(self, cycle_id: int) -> SchedulerPollCycle | None:
        """Retrieve a poll cycle by ID.

        Args:
            cycle_id: Unique cycle identifier

        Returns:
            SchedulerPollCycle or None if not found
        """
        raise NotImplementedError("Database implementation pending")

    async def list_poll_cycles(
        self,
        rule_id: str | None = None,
        limit: int = 100,
    ) -> list[SchedulerPollCycle]:
        """List recent poll cycles.

        Args:
            rule_id: Filter by rule ID (optional)
            limit: Maximum results to return

        Returns:
            List of SchedulerPollCycle objects, ordered by created_at DESC
        """
        raise NotImplementedError("Database implementation pending")

    async def get_last_successful_poll(self, rule_id: str) -> SchedulerPollCycle | None:
        """Get the most recent successful poll for a rule.

        Args:
            rule_id: Rule identifier

        Returns:
            SchedulerPollCycle or None if never polled
        """
        raise NotImplementedError("Database implementation pending")


class InMemorySchedulerRepository(SchedulerRepository):
    """In-memory implementation for testing and development."""

    def __init__(self) -> None:
        """Initialize in-memory storage."""
        self._rules: dict[str, SchedulerRule] = {}
        self._cycles: dict[int, SchedulerPollCycle] = {}
        self._next_cycle_id = 1

    async def create_rule(self, rule: SchedulerRule) -> None:
        """Create a new scheduler rule."""
        if rule.id in self._rules:
            raise SchedulerRepositoryError(f"Rule {rule.id} already exists")
        self._rules[rule.id] = rule

    async def get_rule(self, rule_id: str) -> SchedulerRule | None:
        """Retrieve a scheduler rule by ID."""
        return self._rules.get(rule_id)

    async def list_rules(self, enabled_only: bool = True) -> list[SchedulerRule]:
        """List all scheduler rules."""
        rules = list(self._rules.values())
        if enabled_only:
            rules = [r for r in rules if r.enabled]
        return rules

    async def update_rule(self, rule: SchedulerRule) -> None:
        """Update an existing scheduler rule."""
        if rule.id not in self._rules:
            raise SchedulerRepositoryError(f"Rule {rule.id} not found")
        self._rules[rule.id] = rule

    async def delete_rule(self, rule_id: str) -> None:
        """Delete a scheduler rule."""
        if rule_id not in self._rules:
            raise SchedulerRepositoryError(f"Rule {rule_id} not found")
        del self._rules[rule_id]

    async def record_poll_cycle(self, cycle: SchedulerPollCycle) -> int:
        """Record a completed poll cycle."""
        cycle_id = self._next_cycle_id
        self._next_cycle_id += 1
        cycle.id = cycle_id
        self._cycles[cycle_id] = cycle
        return cycle_id

    async def get_poll_cycle(self, cycle_id: int) -> SchedulerPollCycle | None:
        """Retrieve a poll cycle by ID."""
        return self._cycles.get(cycle_id)

    async def list_poll_cycles(
        self,
        rule_id: str | None = None,
        limit: int = 100,
    ) -> list[SchedulerPollCycle]:
        """List recent poll cycles."""
        cycles = list(self._cycles.values())
        if rule_id:
            cycles = [c for c in cycles if c.rule_id == rule_id]
        cycles.sort(key=lambda c: c.created_at or "", reverse=True)
        return cycles[:limit]

    async def get_last_successful_poll(self, rule_id: str) -> SchedulerPollCycle | None:
        """Get the most recent successful poll for a rule."""
        cycles = [c for c in self._cycles.values() if c.rule_id == rule_id and c.poll_status == "completed"]
        if not cycles:
            return None
        cycles.sort(key=lambda c: c.created_at or "", reverse=True)
        return cycles[0]
