"""Core task management: dataclasses and JSON plan file operations."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

PRIORITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}

VALID_STATUSES = frozenset({"pending", "in_progress", "done", "failed", "skipped"})


@dataclass
class Task:
    """Single task from the JSON plan."""

    id: str
    phase: str
    category: str
    priority: str
    description: str
    status: str
    dependencies: list[str] = field(default_factory=list)
    key_files: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    test_steps: list[str] = field(default_factory=list)
    prd_requirement: str = ""

    # External integrations (optional)
    github_issue: int | None = None
    github_url: str | None = None
    jira_key: str | None = None
    jira_url: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> Task:
        """Create Task from a JSON dict, ignoring unknown keys."""
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict:
        """Serialize back to a dict suitable for JSON."""
        result = {
            "id": self.id,
            "phase": self.phase,
            "category": self.category,
            "priority": self.priority,
            "description": self.description,
            "status": self.status,
            "dependencies": self.dependencies,
            "key_files": self.key_files,
            "acceptance_criteria": self.acceptance_criteria,
            "test_steps": self.test_steps,
            "prd_requirement": self.prd_requirement,
        }

        # Add optional external integration fields if they exist
        if self.github_issue is not None:
            result["github_issue"] = self.github_issue
        if self.github_url is not None:
            result["github_url"] = self.github_url
        if self.jira_key is not None:
            result["jira_key"] = self.jira_key
        if self.jira_url is not None:
            result["jira_url"] = self.jira_url

        return result


@dataclass
class Plan:
    """Parsed plan metadata."""

    file_path: Path
    project: str
    prd_file: str
    created_at: str
    agent_instructions: dict[str, list[str]]
    tasks: list[Task]

    @classmethod
    def from_raw(cls, file_path: Path, data: dict) -> Plan:
        """Build Plan from raw JSON dict."""
        return cls(
            file_path=file_path,
            project=data.get("project", ""),
            prd_file=data.get("prd_file", ""),
            created_at=data.get("created_at", ""),
            agent_instructions=data.get("agent_instructions", {}),
            tasks=[Task.from_dict(t) for t in data.get("tasks", [])],
        )


class TaskManager:
    """Manages task state in JSON plan files."""

    def __init__(self, plan_path: str | Path):
        self.path = Path(plan_path)
        self._data: dict = {}
        self.tasks: list[Task] = []
        self.reload()

    def reload(self) -> None:
        """Reload tasks from file."""
        self._data = json.loads(self.path.read_text(encoding="utf-8"))
        self.tasks = [Task.from_dict(t) for t in self._data.get("tasks", [])]

    def save(self) -> None:
        """Save tasks back to JSON file (atomic write via temp + rename)."""
        self._data["tasks"] = [t.to_dict() for t in self.tasks]
        content = json.dumps(self._data, ensure_ascii=False, indent=2) + "\n"
        dir_path = self.path.parent
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp", prefix=".whilly_")
        closed = False
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            closed = True
            os.replace(tmp_path, self.path)
        except BaseException:
            if not closed:
                os.close(fd)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def reset_stale_tasks(self) -> int:
        """Reset in_progress -> pending at startup. Returns count."""
        count = 0
        for task in self.tasks:
            if task.status == "in_progress":
                task.status = "pending"
                count += 1
        if count:
            self.save()
        return count

    def get_ready_tasks(self) -> list[Task]:
        """Get unblocked pending tasks sorted by (priority_order, phase)."""
        done_ids = {t.id for t in self.tasks if t.status == "done"}
        ready = [t for t in self.tasks if t.status == "pending" and all(dep in done_ids for dep in t.dependencies)]
        ready.sort(key=lambda t: (PRIORITY_ORDER.get(t.priority, 99), t.phase))
        return ready

    def mark_status(self, task_ids: list[str], status: str) -> None:
        """Update status for given task IDs and save."""
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status {status!r}, must be one of {VALID_STATUSES}")
        id_set = set(task_ids)
        for task in self.tasks:
            if task.id in id_set:
                task.status = status
        self.save()

    @property
    def done_count(self) -> int:
        return sum(1 for t in self.tasks if t.status == "done")

    @property
    def pending_count(self) -> int:
        return sum(1 for t in self.tasks if t.status == "pending")

    @property
    def total_count(self) -> int:
        return len(self.tasks)

    @property
    def project(self) -> str:
        return self._data.get("project", "(unnamed)")

    def has_pending(self) -> bool:
        return any(t.status == "pending" for t in self.tasks)

    def get_task(self, task_id: str) -> Task | None:
        """Find task by ID, or None."""
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None

    def counts_by_status(self) -> dict[str, int]:
        """Return {status: count} dict."""
        return dict(Counter(t.status for t in self.tasks))

    @property
    def plan(self) -> Plan:
        """Return parsed Plan metadata."""
        return Plan.from_raw(self.path, self._data)
