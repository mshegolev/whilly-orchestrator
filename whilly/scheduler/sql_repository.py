"""SQL-based repository for scheduler rules and poll cycles."""

from __future__ import annotations

import json

from sqlalchemy import select, desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from whilly.adapters.db.models import SchedulerRule as SchedulerRuleModel
from whilly.adapters.db.models import SchedulerPollCycle as SchedulerPollCycleModel
from whilly.scheduler.models import SchedulerRule, SchedulerPollCycle
from whilly.scheduler.repository import SchedulerRepository, SchedulerRepositoryError


class SQLSchedulerRepository(SchedulerRepository):
    """SQL implementation of SchedulerRepository using SQLAlchemy."""

    def __init__(self, session: Session) -> None:
        """Initialize with SQLAlchemy session.

        Args:
            session: SQLAlchemy session for database access
        """
        self.session = session

    async def create_rule(self, rule: SchedulerRule) -> None:
        """Create a new scheduler rule in the database."""
        try:
            db_rule = SchedulerRuleModel(
                id=rule.id,
                name=rule.name,
                description=rule.description,
                enabled=rule.enabled,
                jira_project_key=rule.jira_project_key,
                jql_filter=rule.jql_filter,
                poll_interval_seconds=rule.poll_interval_seconds,
                max_results_per_poll=rule.max_results_per_poll,
                deduplication_fields=json.dumps(rule.deduplication_fields),
                plan_config=rule.plan_config,
                custom_metadata=rule.custom_metadata,
            )
            self.session.add(db_rule)
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise SchedulerRepositoryError(f"Rule {rule.id} already exists") from exc
        except Exception as exc:
            self.session.rollback()
            raise SchedulerRepositoryError(f"Failed to create rule: {exc}") from exc

    async def get_rule(self, rule_id: str) -> SchedulerRule | None:
        """Retrieve a scheduler rule by ID."""
        try:
            stmt = select(SchedulerRuleModel).where(SchedulerRuleModel.id == rule_id)
            db_rule = self.session.execute(stmt).scalar_one_or_none()

            if db_rule is None:
                return None

            return SchedulerRule(
                id=db_rule.id,
                name=db_rule.name,
                description=db_rule.description,
                enabled=db_rule.enabled,
                jira_project_key=db_rule.jira_project_key,
                jql_filter=db_rule.jql_filter,
                poll_interval_seconds=db_rule.poll_interval_seconds,
                max_results_per_poll=db_rule.max_results_per_poll,
                deduplication_fields=json.loads(db_rule.deduplication_fields or "[]"),
                plan_config=db_rule.plan_config,
                custom_metadata=db_rule.custom_metadata,
            )
        except Exception as exc:
            raise SchedulerRepositoryError(f"Failed to get rule: {exc}") from exc

    async def list_rules(self, enabled_only: bool = True) -> list[SchedulerRule]:
        """List all scheduler rules."""
        try:
            if enabled_only:
                stmt = select(SchedulerRuleModel).where(SchedulerRuleModel.enabled.is_(True))
            else:
                stmt = select(SchedulerRuleModel)

            db_rules = self.session.execute(stmt).scalars().all()

            return [
                SchedulerRule(
                    id=db_rule.id,
                    name=db_rule.name,
                    description=db_rule.description,
                    enabled=db_rule.enabled,
                    jira_project_key=db_rule.jira_project_key,
                    jql_filter=db_rule.jql_filter,
                    poll_interval_seconds=db_rule.poll_interval_seconds,
                    max_results_per_poll=db_rule.max_results_per_poll,
                    deduplication_fields=json.loads(db_rule.deduplication_fields or "[]"),
                    plan_config=db_rule.plan_config,
                    custom_metadata=db_rule.custom_metadata,
                )
                for db_rule in db_rules
            ]
        except Exception as exc:
            raise SchedulerRepositoryError(f"Failed to list rules: {exc}") from exc

    async def update_rule(self, rule: SchedulerRule) -> None:
        """Update an existing scheduler rule."""
        try:
            db_rule = self.session.query(SchedulerRuleModel).filter_by(id=rule.id).first()
            if db_rule is None:
                raise SchedulerRepositoryError(f"Rule {rule.id} not found")

            db_rule.name = rule.name
            db_rule.description = rule.description
            db_rule.enabled = rule.enabled
            db_rule.jira_project_key = rule.jira_project_key
            db_rule.jql_filter = rule.jql_filter
            db_rule.poll_interval_seconds = rule.poll_interval_seconds
            db_rule.max_results_per_poll = rule.max_results_per_poll
            db_rule.deduplication_fields = json.dumps(rule.deduplication_fields)
            db_rule.plan_config = rule.plan_config
            db_rule.custom_metadata = rule.custom_metadata

            self.session.commit()
        except SchedulerRepositoryError:
            raise
        except Exception as exc:
            self.session.rollback()
            raise SchedulerRepositoryError(f"Failed to update rule: {exc}") from exc

    async def delete_rule(self, rule_id: str) -> None:
        """Delete a scheduler rule."""
        try:
            db_rule = self.session.query(SchedulerRuleModel).filter_by(id=rule_id).first()
            if db_rule is None:
                raise SchedulerRepositoryError(f"Rule {rule_id} not found")

            self.session.delete(db_rule)
            self.session.commit()
        except SchedulerRepositoryError:
            raise
        except Exception as exc:
            self.session.rollback()
            raise SchedulerRepositoryError(f"Failed to delete rule: {exc}") from exc

    async def record_poll_cycle(self, cycle: SchedulerPollCycle) -> int:
        """Record a completed poll cycle."""
        try:
            db_cycle = SchedulerPollCycleModel(
                rule_id=cycle.rule_id,
                poll_status=cycle.poll_status,
                total_issues_found=cycle.total_issues_found,
                new_issues_created=cycle.new_issues_created,
                duplicate_issues_skipped=cycle.duplicate_issues_skipped,
                error_message=cycle.error_message,
                jql_results=json.dumps(cycle.jql_results or []),
                deduplicated_issues=json.dumps(cycle.deduplicated_issues or []),
                created_at=cycle.created_at,
                completed_at=cycle.completed_at,
            )
            self.session.add(db_cycle)
            self.session.commit()

            return db_cycle.id
        except Exception as exc:
            self.session.rollback()
            raise SchedulerRepositoryError(f"Failed to record poll cycle: {exc}") from exc

    async def get_poll_cycle(self, cycle_id: int) -> SchedulerPollCycle | None:
        """Retrieve a poll cycle by ID."""
        try:
            db_cycle = self.session.query(SchedulerPollCycleModel).filter_by(id=cycle_id).first()

            if db_cycle is None:
                return None

            return SchedulerPollCycle(
                id=db_cycle.id,
                rule_id=db_cycle.rule_id,
                poll_status=db_cycle.poll_status,
                total_issues_found=db_cycle.total_issues_found,
                new_issues_created=db_cycle.new_issues_created,
                duplicate_issues_skipped=db_cycle.duplicate_issues_skipped,
                error_message=db_cycle.error_message,
                jql_results=json.loads(db_cycle.jql_results or "[]"),
                deduplicated_issues=json.loads(db_cycle.deduplicated_issues or "[]"),
                created_at=db_cycle.created_at,
                completed_at=db_cycle.completed_at,
            )
        except Exception as exc:
            raise SchedulerRepositoryError(f"Failed to get poll cycle: {exc}") from exc

    async def list_poll_cycles(
        self,
        rule_id: str | None = None,
        limit: int = 100,
    ) -> list[SchedulerPollCycle]:
        """List recent poll cycles."""
        try:
            query = self.session.query(SchedulerPollCycleModel)

            if rule_id:
                query = query.filter_by(rule_id=rule_id)

            db_cycles = query.order_by(desc(SchedulerPollCycleModel.created_at)).limit(limit).all()

            return [
                SchedulerPollCycle(
                    id=db_cycle.id,
                    rule_id=db_cycle.rule_id,
                    poll_status=db_cycle.poll_status,
                    total_issues_found=db_cycle.total_issues_found,
                    new_issues_created=db_cycle.new_issues_created,
                    duplicate_issues_skipped=db_cycle.duplicate_issues_skipped,
                    error_message=db_cycle.error_message,
                    jql_results=json.loads(db_cycle.jql_results or "[]"),
                    deduplicated_issues=json.loads(db_cycle.deduplicated_issues or "[]"),
                    created_at=db_cycle.created_at,
                    completed_at=db_cycle.completed_at,
                )
                for db_cycle in db_cycles
            ]
        except Exception as exc:
            raise SchedulerRepositoryError(f"Failed to list poll cycles: {exc}") from exc

    async def get_last_successful_poll(self, rule_id: str) -> SchedulerPollCycle | None:
        """Get the most recent successful poll for a rule."""
        try:
            db_cycle = (
                self.session.query(SchedulerPollCycleModel)
                .filter_by(rule_id=rule_id, poll_status="completed")
                .order_by(desc(SchedulerPollCycleModel.created_at))
                .first()
            )

            if db_cycle is None:
                return None

            return SchedulerPollCycle(
                id=db_cycle.id,
                rule_id=db_cycle.rule_id,
                poll_status=db_cycle.poll_status,
                total_issues_found=db_cycle.total_issues_found,
                new_issues_created=db_cycle.new_issues_created,
                duplicate_issues_skipped=db_cycle.duplicate_issues_skipped,
                error_message=db_cycle.error_message,
                jql_results=json.loads(db_cycle.jql_results or "[]"),
                deduplicated_issues=json.loads(db_cycle.deduplicated_issues or "[]"),
                created_at=db_cycle.created_at,
                completed_at=db_cycle.completed_at,
            )
        except Exception as exc:
            raise SchedulerRepositoryError(f"Failed to get last successful poll: {exc}") from exc
