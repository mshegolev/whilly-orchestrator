"""Tests for WUI task creation (POST /api/v1/tasks endpoint)."""

from __future__ import annotations


import pytest

from whilly.adapters.db.repository import TaskRepository
from whilly.adapters.transport.schemas import TaskCreateRequest
from whilly.core.models import Priority, Task, TaskStatus


class TestTaskRepositoryInsertTask:
    """Unit tests for TaskRepository.insert_task()."""

    @pytest.mark.asyncio
    async def test_insert_task_simple(self, pool: any) -> None:
        """Insert a minimal task into an existing plan."""
        repo = TaskRepository(pool)

        # Create a plan first
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO plans (id) VALUES ($1)", "test-plan")

        # Create and insert task
        task = Task(
            id="test-task-1",
            status=TaskStatus.PENDING,
            description="Fix database bug",
            priority=Priority.HIGH,
        )

        inserted = await repo.insert_task(task, plan_id="test-plan")

        assert inserted.id == "test-task-1"
        assert inserted.status == TaskStatus.PENDING
        assert inserted.description == "Fix database bug"
        assert inserted.version == 0
        assert inserted.priority == Priority.HIGH

    @pytest.mark.asyncio
    async def test_insert_task_with_dependencies(self, pool: any) -> None:
        """Insert a task with dependencies in the same plan."""
        repo = TaskRepository(pool)

        # Create plan and dependency task
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO plans (id) VALUES ($1)", "test-plan-2")
            await conn.execute(
                """
                INSERT INTO tasks (id, plan_id, status, priority, description, version)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                "task-a",
                "test-plan-2",
                "PENDING",
                "medium",
                "Task A",
                0,
            )

        # Create task with dependency
        task = Task(
            id="task-b",
            status=TaskStatus.PENDING,
            description="Task B (depends on A)",
            dependencies=("task-a",),
        )

        inserted = await repo.insert_task(task, plan_id="test-plan-2")

        assert inserted.id == "task-b"
        assert inserted.dependencies == ("task-a",)

    @pytest.mark.asyncio
    async def test_insert_task_duplicate_id(self, pool: any) -> None:
        """Reject inserting a task with duplicate ID in same plan."""
        repo = TaskRepository(pool)

        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO plans (id) VALUES ($1)", "test-plan-3")
            await conn.execute(
                """
                INSERT INTO tasks (id, plan_id, status, priority, description, version)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                "duplicate-task",
                "test-plan-3",
                "PENDING",
                "medium",
                "Original",
                0,
            )

        task = Task(
            id="duplicate-task",
            status=TaskStatus.PENDING,
            description="Duplicate",
        )

        with pytest.raises(Exception):  # UniqueViolationError or similar
            await repo.insert_task(task, plan_id="test-plan-3")

    @pytest.mark.asyncio
    async def test_insert_task_nonexistent_plan(self, pool: any) -> None:
        """Reject inserting into non-existent plan."""
        repo = TaskRepository(pool)

        task = Task(id="task-x", status=TaskStatus.PENDING)

        with pytest.raises(ValueError, match="does not exist"):
            await repo.insert_task(task, plan_id="nonexistent-plan")

    @pytest.mark.asyncio
    async def test_insert_task_missing_dependency(self, pool: any) -> None:
        """Reject task with missing dependency."""
        repo = TaskRepository(pool)

        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO plans (id) VALUES ($1)", "test-plan-4")

        task = Task(
            id="task-c",
            status=TaskStatus.PENDING,
            dependencies=("missing-task",),
        )

        with pytest.raises(ValueError, match="not found"):
            await repo.insert_task(task, plan_id="test-plan-4")


class TestTaskCreateRequest:
    """Unit tests for TaskCreateRequest schema."""

    def test_task_create_request_minimal(self) -> None:
        """Parse minimal task creation request."""
        req = TaskCreateRequest(id="my-task")

        assert req.id == "my-task"
        assert req.description == ""
        assert req.priority == Priority.MEDIUM
        assert req.dependencies == []
        assert req.key_files == []

    def test_task_create_request_full(self) -> None:
        """Parse full task creation request."""
        req = TaskCreateRequest(
            id="complex-task",
            description="A complex task",
            priority="high",
            dependencies=["task-1", "task-2"],
            key_files=["src/main.py"],
            acceptance_criteria=["Should work"],
            test_steps=["Run tests"],
        )

        assert req.id == "complex-task"
        assert req.priority == Priority.HIGH
        assert req.dependencies == ["task-1", "task-2"]
        assert len(req.acceptance_criteria) == 1

    def test_task_create_request_invalid_priority(self) -> None:
        """Reject invalid priority."""
        with pytest.raises(ValueError):
            TaskCreateRequest(id="task", priority="invalid")

    def test_task_create_request_missing_id(self) -> None:
        """Reject missing task ID."""
        with pytest.raises(ValueError):
            TaskCreateRequest()
