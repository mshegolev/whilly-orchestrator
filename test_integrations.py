#!/usr/bin/env python3
"""Тест интеграций Whilly с внешними системами."""

import sys

# Добавляем путь к модулям Whilly
sys.path.insert(0, "/opt/develop/whilly-orchestrator")

from whilly.external_integrations import create_integration_manager, ExternalTaskRef
from whilly.config import WhillyConfig


def test_github_integration():
    """Тест GitHub интеграции."""
    print("🔗 Testing GitHub Integration...")

    # Создаем конфиг
    config = WhillyConfig.from_env()
    integrations_config = config.get_external_integrations_config()

    # Создаем менеджер интеграций
    manager = create_integration_manager(integrations_config)

    # Проверяем доступность GitHub интеграции
    github_integration = manager.integrations.get("github")
    if github_integration:
        available = github_integration.is_available()
        print(f"  GitHub CLI доступен: {'✅' if available else '❌'}")

        if available:
            # Тестовая ссылка на Issue
            test_ref = ExternalTaskRef(
                system="github",
                task_id="1",  # Issue #1
                url="https://github.com/mshegolev/whilly-orchestrator/issues/1",
            )

            print(f"  Тест: добавление комментария к Issue #{test_ref.task_id}")
            comment_success = github_integration.add_comment(
                test_ref, "🧪 **Test Comment from Whilly**\n\nTesting external integrations functionality!"
            )
            print(f"  Добавление комментария: {'✅' if comment_success else '❌'}")

            # НЕ закрываем Issue в тесте, только показываем что можем
            print("  Закрытие Issue: 🚫 пропущено в тесте (чтобы не закрывать реальные Issues)")

    else:
        print("  ❌ GitHub интеграция недоступна")


def test_jira_integration():
    """Тест Jira интеграции."""
    print("\n🎫 Testing Jira Integration...")

    # Создаем конфиг
    config = WhillyConfig.from_env()
    integrations_config = config.get_external_integrations_config()

    # Создаем менеджер интеграций
    manager = create_integration_manager(integrations_config)

    # Проверяем доступность Jira интеграции
    jira_integration = manager.integrations.get("jira")
    if jira_integration:
        available = jira_integration.is_available()
        print(f"  Jira API доступен: {'✅' if available else '❌'}")

        if not available:
            print("  💡 Для активации Jira интеграции настройте:")
            print("     export WHILLY_JIRA_ENABLED=true")
            print("     export WHILLY_JIRA_SERVER_URL='https://company.atlassian.net'")
            print("     export WHILLY_JIRA_USERNAME='user@company.com'")
            print("     export JIRA_API_TOKEN='your_token'")
    else:
        print("  ❌ Jira интеграция недоступна")


def test_task_ref_extraction():
    """Тест извлечения ссылок на внешние задачи."""
    print("\n📋 Testing External Task Reference Extraction...")

    # Создаем конфиг и менеджер
    config = WhillyConfig.from_env()
    integrations_config = config.get_external_integrations_config()
    manager = create_integration_manager(integrations_config)

    # Тестовая задача с GitHub Issue
    test_task_data = {
        "id": "test-task",
        "description": "Test task with external references ABC-123",
        "github_issue": 1,
        "github_url": "https://github.com/mshegolev/whilly-orchestrator/issues/1",
        "jira_key": "ABC-123",
    }

    # Извлекаем ссылки
    refs = manager.extract_external_refs_from_task(test_task_data)

    print(f"  Найдено внешних ссылок: {len(refs)}")
    for ref in refs:
        print(f"    - {ref.system.upper()}: {ref.task_id} ({ref.url})")


def main():
    """Основная функция тестирования."""
    print("🤖 Whilly External Integrations Test")
    print("=" * 50)

    # Показываем текущие настройки
    print("\n⚙️  Current Configuration:")
    config = WhillyConfig.from_env()
    print(f"  CLOSE_EXTERNAL_TASKS: {config.CLOSE_EXTERNAL_TASKS}")
    print(f"  GITHUB_AUTO_CLOSE: {config.GITHUB_AUTO_CLOSE}")
    print(f"  GITHUB_ADD_COMMENTS: {config.GITHUB_ADD_COMMENTS}")
    print(f"  JIRA_ENABLED: {config.JIRA_ENABLED}")

    # Запускаем тесты
    test_github_integration()
    test_jira_integration()
    test_task_ref_extraction()

    print("\n🎉 Test completed!")
    print("\nДля полной интеграции используй:")
    print("  python3 -m whilly --from-github workshop,whilly:ready")


if __name__ == "__main__":
    main()
