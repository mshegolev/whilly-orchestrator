"""GitHub Issues to Whilly tasks converter.

Автоматически извлекает Issues с тегами workshop/whilly:ready и конвертирует
в формат tasks.json для выполнения Whilly оркестратором.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("whilly.github_converter")


@dataclass
class GitHubIssue:
    """Структура GitHub Issue."""
    number: int
    title: str
    body: str | None
    state: str
    labels: list[str]
    created_at: str
    updated_at: str
    url: str

    @classmethod
    def from_gh_json(cls, issue_data: dict[str, Any]) -> GitHubIssue:
        """Создает GitHubIssue из JSON ответа GitHub CLI."""
        return cls(
            number=issue_data["number"],
            title=issue_data["title"],
            body=issue_data.get("body", ""),
            state=issue_data["state"],
            labels=[label["name"] for label in issue_data.get("labels", [])],
            created_at=issue_data["createdAt"],
            updated_at=issue_data["updatedAt"],
            url=issue_data["url"]
        )


@dataclass
class WhillyTask:
    """Структура задачи Whilly - соответствует Task из task_manager."""
    id: str
    description: str
    phase: str = "implementation"
    category: str = "feature"
    status: str = "pending"
    priority: str = "medium"
    dependencies: list[str] | None = None
    key_files: list[str] | None = None
    acceptance_criteria: list[str] | None = None
    test_steps: list[str] | None = None
    prd_requirement: str = ""
    github_issue: int | None = None
    github_url: str | None = None
    jira_key: str | None = None
    jira_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Конвертирует в dict для JSON."""
        result = asdict(self)
        # Убираем None значения
        return {k: v for k, v in result.items() if v is not None}


def _extract_priority(labels: list[str]) -> str:
    """Извлекает приоритет из лейблов GitHub."""
    priority_map = {
        "priority:critical": "critical",
        "priority:high": "high",
        "priority:medium": "medium",
        "priority:low": "low"
    }

    for label in labels:
        if label in priority_map:
            return priority_map[label]

    return "medium"  # default


def _extract_key_files(title: str, body: str) -> list[str]:
    """Пытается извлечь key_files из заголовка и описания issue."""
    key_files = []

    # Простая эвристика на основе заголовка
    title_lower = title.lower()

    if "readme" in title_lower:
        key_files.append("README.md")
    if "contributing" in title_lower:
        key_files.append("CONTRIBUTING.md")
    if "pyproject" in title_lower or "setup.py" in title_lower:
        key_files.append("pyproject.toml")
    if "test" in title_lower:
        key_files.extend(["tests/", "pytest.ini"])
    if "ruff" in title_lower:
        key_files.append("pyproject.toml")
    if "script" in title_lower:
        key_files.append("scripts/")

    return key_files or ["README.md"]  # fallback


def _determine_category(title: str, labels: list[str]) -> str:
    """Определяет категорию задачи на основе заголовка и лейблов."""
    title_lower = title.lower()

    # Проверяем лейблы
    if "bug" in labels or "bugfix" in labels:
        return "bugfix"
    if "documentation" in labels or "docs" in labels:
        return "documentation"
    if "test" in labels or "testing" in labels:
        return "testing"

    # Проверяем заголовок
    if any(word in title_lower for word in ["fix", "bug", "error", "issue"]):
        return "bugfix"
    if any(word in title_lower for word in ["add", "create", "implement", "new"]):
        return "feature"
    if any(word in title_lower for word in ["update", "improve", "enhance"]):
        return "enhancement"
    if any(word in title_lower for word in ["refactor", "cleanup", "reorganize"]):
        return "refactor"
    if any(word in title_lower for word in ["test", "spec"]):
        return "testing"
    if any(word in title_lower for word in ["doc", "readme", "contributing"]):
        return "documentation"

    return "feature"  # default


def _determine_phase(category: str, title: str) -> str:
    """Определяет фазу задачи на основе категории и заголовка."""
    title_lower = title.lower()

    # Специальные фазы для определенных типов
    if category == "documentation":
        return "documentation"
    if category == "testing":
        return "testing"
    if "setup" in title_lower or "init" in title_lower:
        return "setup"
    if "config" in title_lower or "setting" in title_lower:
        return "configuration"

    return "implementation"  # default


def _generate_task_id(issue_number: int, title: str) -> str:
    """Генерирует ID задачи из номера issue и заголовка."""
    # Убираем [workshop] prefix и берем первые слова
    clean_title = title.replace("[workshop]", "").strip()
    words = clean_title.lower().split()[:3]
    slug = "-".join(word.strip(".,!?()[]") for word in words if word.isalnum() or word.strip(".,!?()[]"))
    return f"gh-{issue_number}-{slug}"[:50]  # Ограничиваем длину


def _extract_acceptance_criteria(body: str) -> list[str]:
    """Извлекает критерии приемки из описания issue."""
    if not body:
        return ["Task completed successfully", "Code passes linting", "Tests pass"]

    # Ищем чекбоксы или списки
    criteria = []
    lines = body.split('\n')

    for line in lines:
        line = line.strip()
        # GitHub checkboxes: - [ ] или - [x]
        if line.startswith('- [ ]') or line.startswith('- [x]'):
            criteria.append(line[5:].strip())
        # Простые списки
        elif line.startswith('- ') and len(line) > 3:
            criteria.append(line[2:].strip())

    return criteria or ["Task completed successfully", "Code passes linting"]


def fetch_github_issues(filter_labels: list[str] | None = None) -> list[GitHubIssue]:
    """Получает Issues из GitHub через gh CLI."""
    cmd = ["gh", "issue", "list", "--json",
           "number,title,body,state,labels,createdAt,updatedAt,url",
           "--limit", "100"]

    if filter_labels:
        for label in filter_labels:
            cmd.extend(["--label", label])

    try:
        # Используем текущее окружение, но без проблемного GITHUB_TOKEN
        import os
        env = os.environ.copy()
        env.pop("GITHUB_TOKEN", None)  # Убираем проблемный токен если есть

        result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=True)
        issues_data = json.loads(result.stdout)

        return [GitHubIssue.from_gh_json(issue) for issue in issues_data]

    except subprocess.CalledProcessError as e:
        log.error("Failed to fetch GitHub issues: %s", e.stderr)
        raise
    except json.JSONDecodeError as e:
        log.error("Failed to parse GitHub issues JSON: %s", e)
        raise


def convert_issues_to_tasks(issues: list[GitHubIssue]) -> list[WhillyTask]:
    """Конвертирует GitHub Issues в задачи Whilly."""
    tasks = []

    for issue in issues:
        # Пропускаем closed issues
        if issue.state != "OPEN":
            continue

        category = _determine_category(issue.title, issue.labels)
        phase = _determine_phase(category, issue.title)

        task = WhillyTask(
            id=_generate_task_id(issue.number, issue.title),
            description=issue.title,
            phase=phase,
            category=category,
            priority=_extract_priority(issue.labels),
            key_files=_extract_key_files(issue.title, issue.body or ""),
            acceptance_criteria=_extract_acceptance_criteria(issue.body or ""),
            test_steps=[
                "Run `make lint` and verify no errors",
                "Run `pytest` and verify all tests pass",
                f"Verify GitHub Issue #{issue.number} requirements are met"
            ],
            prd_requirement=f"GitHub Issue #{issue.number}: {issue.title}",
            github_issue=issue.number,
            github_url=issue.url
        )

        tasks.append(task)
        log.info("Converted Issue #%d: %s → Task ID: %s",
                issue.number, issue.title[:50], task.id)

    return tasks


def create_whilly_plan(tasks: list[WhillyTask], prd_file: Path | None = None) -> dict[str, Any]:
    """Создает план Whilly из списка задач."""
    plan = {
        "project": "workshop-self-writing-orchestrator",
        "prd_file": str(prd_file) if prd_file else "docs/PRD-workshop.md",
        "created_at": datetime.now().isoformat(),
        "source": "github_issues",
        "tasks": [task.to_dict() for task in tasks]
    }

    return plan


def generate_tasks_from_github(
    output_path: Path | str = "tasks-workshop.json",
    filter_labels: list[str] | None = None,
    prd_file: Path | None = None
) -> Path:
    """
    Главная функция: извлекает GitHub Issues и создает tasks.json для Whilly.

    Args:
        output_path: Путь для сохранения tasks.json
        filter_labels: Фильтр по лейблам (по умолчанию: ["workshop", "whilly:ready"])
        prd_file: Путь к PRD файлу

    Returns:
        Path к созданному файлу tasks.json
    """
    output_path = Path(output_path)

    if filter_labels is None:
        filter_labels = ["workshop", "whilly:ready"]

    log.info("Fetching GitHub Issues with labels: %s", filter_labels)
    issues = fetch_github_issues(filter_labels)
    log.info("Found %d issues", len(issues))

    if not issues:
        log.warning("No GitHub Issues found with labels: %s", filter_labels)
        return output_path

    log.info("Converting issues to Whilly tasks...")
    tasks = convert_issues_to_tasks(issues)

    log.info("Creating Whilly plan...")
    plan = create_whilly_plan(tasks, prd_file)

    log.info("Saving plan to %s", output_path)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)

    log.info("✅ Generated %d tasks from %d GitHub Issues", len(tasks), len(issues))
    return output_path


if __name__ == "__main__":
    # Для тестирования
    logging.basicConfig(level=logging.INFO)
    tasks_file = generate_tasks_from_github()
    print(f"Generated: {tasks_file}")