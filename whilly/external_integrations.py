"""External integrations for Whilly - GitHub Issues, Jira tasks, etc.

Автоматическое закрытие задач во внешних системах после успешного выполнения.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("whilly.external_integrations")


@dataclass
class ExternalTaskRef:
    """Ссылка на внешнюю задачу."""
    system: str  # "github" | "jira" | "linear" | etc
    task_id: str  # Issue number, ticket key, etc
    url: str
    project: str | None = None  # For Jira, GitHub repo, etc


class ExternalIntegration(ABC):
    """Базовый класс для интеграций с внешними системами."""

    @abstractmethod
    def close_task(self, task_ref: ExternalTaskRef, whilly_task_id: str, commit_sha: str | None = None) -> bool:
        """Закрывает задачу во внешней системе."""
        pass

    @abstractmethod
    def add_comment(self, task_ref: ExternalTaskRef, comment: str) -> bool:
        """Добавляет комментарий к задаче."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Проверяет доступность интеграции (токены, настройки)."""
        pass


class GitHubIntegration(ExternalIntegration):
    """Интеграция с GitHub Issues."""

    def __init__(self, auto_close: bool = True, add_comments: bool = True):
        self.auto_close = auto_close
        self.add_comments = add_comments

    def is_available(self) -> bool:
        """Проверяет что GitHub CLI доступен и авторизован."""
        try:
            # Убираем проблемный GITHUB_TOKEN
            env = os.environ.copy()
            env.pop("GITHUB_TOKEN", None)

            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                env=env
            )
            return result.returncode == 0
        except FileNotFoundError:
            log.warning("GitHub CLI not found in PATH")
            return False

    def close_task(self, task_ref: ExternalTaskRef, whilly_task_id: str, commit_sha: str | None = None) -> bool:
        """Закрывает GitHub Issue."""
        if not self.auto_close:
            log.debug("Auto-close disabled for GitHub Issues")
            return False

        try:
            # Добавляем комментарий с результатом
            if self.add_comments:
                comment = self._build_completion_comment(whilly_task_id, commit_sha)
                self.add_comment(task_ref, comment)

            # Закрываем issue
            env = os.environ.copy()
            env.pop("GITHUB_TOKEN", None)

            result = subprocess.run(
                ["gh", "issue", "close", task_ref.task_id, "--reason", "completed"],
                capture_output=True,
                text=True,
                env=env
            )

            if result.returncode == 0:
                log.info("✅ Closed GitHub Issue #%s", task_ref.task_id)
                return True
            else:
                log.error("Failed to close GitHub Issue #%s: %s", task_ref.task_id, result.stderr)
                return False

        except Exception as e:
            log.error("Error closing GitHub Issue #%s: %s", task_ref.task_id, e)
            return False

    def add_comment(self, task_ref: ExternalTaskRef, comment: str) -> bool:
        """Добавляет комментарий к GitHub Issue."""
        if not self.add_comments:
            return False

        try:
            env = os.environ.copy()
            env.pop("GITHUB_TOKEN", None)

            result = subprocess.run(
                ["gh", "issue", "comment", task_ref.task_id, "--body", comment],
                capture_output=True,
                text=True,
                env=env
            )

            if result.returncode == 0:
                log.debug("Added comment to GitHub Issue #%s", task_ref.task_id)
                return True
            else:
                log.warning("Failed to comment on GitHub Issue #%s: %s", task_ref.task_id, result.stderr)
                return False

        except Exception as e:
            log.error("Error commenting on GitHub Issue #%s: %s", task_ref.task_id, e)
            return False

    def _build_completion_comment(self, whilly_task_id: str, commit_sha: str | None) -> str:
        """Создает комментарий о завершении задачи."""
        comment = f"🤖 **Whilly Task Completed**\n\n"
        comment += f"- **Task ID**: `{whilly_task_id}`\n"
        comment += f"- **Status**: ✅ Completed successfully\n"

        if commit_sha:
            comment += f"- **Commit**: {commit_sha[:8]}\n"

        comment += f"\n*Automated by [Whilly Orchestrator](https://github.com/mshegolev/whilly-orchestrator)*"
        return comment


class JiraIntegration(ExternalIntegration):
    """Интеграция с Jira задачами."""

    def __init__(
        self,
        server_url: str | None = None,
        username: str | None = None,
        token: str | None = None,
        auto_close: bool = True,
        add_comments: bool = True,
        transition_to: str = "Done"  # Статус для закрытия
    ):
        self.server_url = server_url or os.getenv("JIRA_SERVER_URL")
        self.username = username or os.getenv("JIRA_USERNAME")
        self.token = token or os.getenv("JIRA_API_TOKEN")
        self.auto_close = auto_close
        self.add_comments = add_comments
        self.transition_to = transition_to

    def is_available(self) -> bool:
        """Проверяет доступность Jira API."""
        return all([self.server_url, self.username, self.token])

    def close_task(self, task_ref: ExternalTaskRef, whilly_task_id: str, commit_sha: str | None = None) -> bool:
        """Закрывает Jira задачу через REST API."""
        if not self.auto_close or not self.is_available():
            return False

        try:
            # Добавляем комментарий
            if self.add_comments:
                comment = self._build_completion_comment(whilly_task_id, commit_sha)
                self.add_comment(task_ref, comment)

            # Переводим в статус "Done"
            success = self._transition_issue(task_ref.task_id, self.transition_to)

            if success:
                log.info("✅ Closed Jira task %s", task_ref.task_id)
                return True
            else:
                log.error("Failed to close Jira task %s", task_ref.task_id)
                return False

        except Exception as e:
            log.error("Error closing Jira task %s: %s", task_ref.task_id, e)
            return False

    def add_comment(self, task_ref: ExternalTaskRef, comment: str) -> bool:
        """Добавляет комментарий к Jira задаче."""
        if not self.add_comments or not self.is_available():
            return False

        try:
            import requests

            url = f"{self.server_url}/rest/api/2/issue/{task_ref.task_id}/comment"
            auth = (self.username, self.token)
            headers = {"Content-Type": "application/json"}
            data = {"body": comment}

            response = requests.post(url, auth=auth, headers=headers, json=data)

            if response.status_code == 201:
                log.debug("Added comment to Jira task %s", task_ref.task_id)
                return True
            else:
                log.warning("Failed to comment on Jira task %s: %d", task_ref.task_id, response.status_code)
                return False

        except ImportError:
            log.error("requests library not available for Jira integration")
            return False
        except Exception as e:
            log.error("Error commenting on Jira task %s: %s", task_ref.task_id, e)
            return False

    def _transition_issue(self, issue_key: str, target_status: str) -> bool:
        """Переводит Jira задачу в указанный статус."""
        try:
            import requests

            # Получаем доступные переходы
            url = f"{self.server_url}/rest/api/2/issue/{issue_key}/transitions"
            auth = (self.username, self.token)

            response = requests.get(url, auth=auth)
            if response.status_code != 200:
                log.error("Failed to get transitions for %s: %d", issue_key, response.status_code)
                return False

            transitions = response.json().get("transitions", [])
            target_transition = None

            for transition in transitions:
                if transition["to"]["name"].lower() == target_status.lower():
                    target_transition = transition["id"]
                    break

            if not target_transition:
                log.warning("No transition to '%s' found for %s", target_status, issue_key)
                return False

            # Выполняем переход
            data = {"transition": {"id": target_transition}}
            response = requests.post(url, auth=auth, json=data)

            return response.status_code == 204

        except ImportError:
            log.error("requests library not available for Jira integration")
            return False
        except Exception as e:
            log.error("Error transitioning Jira task %s: %s", issue_key, e)
            return False

    def _build_completion_comment(self, whilly_task_id: str, commit_sha: str | None) -> str:
        """Создает комментарий о завершении задачи для Jira."""
        comment = f"🤖 *Whilly Task Completed*\n\n"
        comment += f"• *Task ID*: {whilly_task_id}\n"
        comment += f"• *Status*: ✅ Completed successfully\n"

        if commit_sha:
            comment += f"• *Commit*: {commit_sha[:8]}\n"

        comment += f"\n_Automated by Whilly Orchestrator_"
        return comment


class ExternalIntegrationManager:
    """Менеджер для работы с внешними интеграциями."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.integrations: dict[str, ExternalIntegration] = {}
        self._setup_integrations()

    def _setup_integrations(self) -> None:
        """Настраивает доступные интеграции."""
        # GitHub
        github_config = self.config.get("github", {})
        if github_config.get("enabled", True):
            self.integrations["github"] = GitHubIntegration(
                auto_close=github_config.get("auto_close", True),
                add_comments=github_config.get("add_comments", True)
            )

        # Jira
        jira_config = self.config.get("jira", {})
        if jira_config.get("enabled", False):
            self.integrations["jira"] = JiraIntegration(
                server_url=jira_config.get("server_url"),
                username=jira_config.get("username"),
                token=jira_config.get("token"),
                auto_close=jira_config.get("auto_close", True),
                add_comments=jira_config.get("add_comments", True),
                transition_to=jira_config.get("transition_to", "Done")
            )

    def close_external_task(self, task_ref: ExternalTaskRef, whilly_task_id: str, commit_sha: str | None = None) -> bool:
        """Закрывает задачу во внешней системе."""
        integration = self.integrations.get(task_ref.system)
        if not integration:
            log.debug("No integration available for system: %s", task_ref.system)
            return False

        if not integration.is_available():
            log.warning("Integration %s is not available (missing config/tokens)", task_ref.system)
            return False

        return integration.close_task(task_ref, whilly_task_id, commit_sha)

    def extract_external_refs_from_task(self, task_data: dict[str, Any]) -> list[ExternalTaskRef]:
        """Извлекает ссылки на внешние задачи из данных Whilly task."""
        refs = []

        # GitHub Issue
        if "github_issue" in task_data and "github_url" in task_data:
            refs.append(ExternalTaskRef(
                system="github",
                task_id=str(task_data["github_issue"]),
                url=task_data["github_url"]
            ))

        # Jira Task (если есть в description или отдельном поле)
        jira_key = self._extract_jira_key_from_task(task_data)
        if jira_key:
            refs.append(ExternalTaskRef(
                system="jira",
                task_id=jira_key,
                url=f"{self.config.get('jira', {}).get('server_url', '')}/browse/{jira_key}"
            ))

        return refs

    def _extract_jira_key_from_task(self, task_data: dict[str, Any]) -> str | None:
        """Извлекает Jira ключ из описания или отдельного поля задачи."""
        import re

        # Проверяем отдельное поле
        if "jira_key" in task_data:
            return task_data["jira_key"]

        # Ищем в описании (ABC-123, PROJECT-456, etc)
        description = task_data.get("description", "")
        jira_pattern = r"\b[A-Z]{2,}-\d+\b"
        match = re.search(jira_pattern, description)

        return match.group() if match else None


def create_integration_manager(config_dict: dict[str, Any] | None = None) -> ExternalIntegrationManager:
    """Фабрика для создания менеджера интеграций."""
    return ExternalIntegrationManager(config_dict)