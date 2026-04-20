#!/usr/bin/env python3
"""Интерактивный режим для работы с GitHub в Whilly."""

from __future__ import annotations

import subprocess
from typing import Optional

from whilly.config import WhillyConfig
from whilly.sources.github_issues import fetch_github_issues
from whilly.external_integrations import create_integration_manager


def _run_command(cmd: list[str]) -> tuple[bool, str]:
    """Запускает команду и возвращает (success, output)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode == 0, result.stdout.strip() or result.stderr.strip()
    except Exception as e:
        return False, str(e)


def _check_gh_auth() -> bool:
    """Проверяет авторизацию в GitHub CLI."""
    success, _ = _run_command(["gh", "auth", "status"])
    return success


def _get_user_repos() -> list[str]:
    """Получает список репозиториев пользователя."""
    if not _check_gh_auth():
        return []

    success, output = _run_command(
        ["gh", "repo", "list", "--limit", "20", "--json", "nameWithOwner", "-q", ".[].nameWithOwner"]
    )

    if success:
        return [line.strip() for line in output.splitlines() if line.strip()]
    return []


def _get_repo_issues(repo: str, label: str = "whilly:ready") -> list[dict]:
    """Получает issues репозитория с определенной меткой."""
    if not _check_gh_auth():
        return []

    success, output = _run_command(
        ["gh", "issue", "list", "--repo", repo, "--state", "open", "--label", label, "--json", "number,title,url"]
    )

    if success:
        import json

        try:
            return json.loads(output or "[]")
        except json.JSONDecodeError:
            return []
    return []


def github_interactive_menu() -> Optional[str]:
    """Интерактивное меню для работы с GitHub."""

    print(f"\n{'=' * 60}")
    print("🐙 WHILLY — GitHub Integration")
    print(f"{'=' * 60}")

    # Загружаем конфигурацию
    config = WhillyConfig.from_env()

    # Показываем текущие настройки
    print("\n⚙️  Текущая конфигурация:")
    print(f"   GitHub auto-close: {config.GITHUB_AUTO_CLOSE}")
    print(f"   GitHub comments: {config.GITHUB_ADD_COMMENTS}")
    print(f"   External integrations: {config.CLOSE_EXTERNAL_TASKS}")

    # Проверяем авторизацию
    if not _check_gh_auth():
        print("\n❌ GitHub CLI не авторизован")
        print("Для работы с GitHub необходимо выполнить:")
        print("  gh auth login")
        return None

    print("✅ GitHub CLI авторизован")

    # Проверяем интеграции
    try:
        integration_manager = create_integration_manager(config.get_external_integrations_config())
        github_available = integration_manager.is_integration_available("github")
        if github_available:
            print("✅ GitHub интеграция настроена")
        else:
            print("⚠️  GitHub интеграция недоступна")
    except Exception as e:
        print(f"⚠️  Ошибка проверки интеграций: {e}")

    while True:
        print("\n📋 Выберите действие:")
        print("  1) 📥 Импортировать issues из репозитория")
        print("  2) 🔍 Просмотреть issues с меткой whilly:ready")
        print("  3) 🏷️  Создать метку whilly:ready в репозитории")
        print("  4) 📊 GitHub Projects workflow")

        # Показываем дополнительные опции только если интеграции включены
        if config.CLOSE_EXTERNAL_TASKS:
            print("  5) ⚙️  Настроить автозакрытие issues")
            print("  6) 💬 Настроить комментарии в issues")

        print("  7) ❓ Помощь по настройке")
        print("  q) ⬅️  Вернуться в главное меню")

        try:
            choice = input("\n  Выбор: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None

        if choice == "q":
            return None
        elif choice == "1":
            return _import_issues_workflow(config)
        elif choice == "2":
            _browse_issues_workflow()
        elif choice == "3":
            _create_label_workflow()
        elif choice == "4":
            return _github_projects_workflow()
        elif choice == "5" and config.CLOSE_EXTERNAL_TASKS:
            _configure_auto_close()
        elif choice == "6" and config.CLOSE_EXTERNAL_TASKS:
            _configure_comments()
        elif choice == "7":
            _show_help(config)
        elif choice == "5" and not config.CLOSE_EXTERNAL_TASKS:
            _show_help(config)
        else:
            print("❌ Неверный выбор")


def _import_issues_workflow(config: WhillyConfig) -> Optional[str]:
    """Workflow для импорта issues."""

    print("\n📥 Импорт GitHub Issues")
    print("-" * 30)

    # Получаем репозитории пользователя
    repos = _get_user_repos()

    if not repos:
        print("❌ Не найдены доступные репозитории")
        repo = input("Введите репозиторий вручную (owner/repo): ").strip()
        if not repo or "/" not in repo:
            print("❌ Неверный формат репозитория")
            return None
    else:
        print("\n📁 Ваши репозитории:")
        for i, repo in enumerate(repos[:10], 1):
            print(f"  {i}) {repo}")

        if len(repos) > 10:
            print(f"  ... и еще {len(repos) - 10}")

        print("  0) Ввести вручную")

        try:
            choice = input("\nВыберите репозиторий: ").strip()
            if choice == "0":
                repo = input("Введите репозиторий (owner/repo): ").strip()
            else:
                idx = int(choice) - 1
                if 0 <= idx < len(repos):
                    repo = repos[idx]
                else:
                    print("❌ Неверный выбор")
                    return None
        except (ValueError, EOFError, KeyboardInterrupt):
            return None

    if not repo:
        return None

    # Выбор метки
    label = input("\nМетка для фильтрации (по умолчанию 'whilly:ready'): ").strip() or "whilly:ready"

    # Проверяем наличие issues
    issues = _get_repo_issues(repo, label)

    if not issues:
        print(f"\n⚠️  Issues с меткой '{label}' не найдены в {repo}")
        create_demo = input("Хотите создать demo issue? [y/N]: ").strip().lower()
        if create_demo in ("y", "yes", "да"):
            _create_demo_issue(repo, label)
            issues = _get_repo_issues(repo, label)

    if issues:
        print(f"\n✅ Найдено {len(issues)} issues:")
        for issue in issues[:5]:
            print(f"  #{issue['number']}: {issue['title']}")
        if len(issues) > 5:
            print(f"  ... и еще {len(issues) - 5}")

    # Показываем что произойдет с задачами
    if config.CLOSE_EXTERNAL_TASKS:
        print("\n📋 После выполнения задач:")
        if config.GITHUB_AUTO_CLOSE:
            print("   ✅ Issues будут автоматически закрыты")
        if config.GITHUB_ADD_COMMENTS:
            print("   💬 К Issues будут добавлены комментарии с результатами")
        if not config.GITHUB_AUTO_CLOSE and not config.GITHUB_ADD_COMMENTS:
            print("   📝 Issues останутся без изменений")
    else:
        print("\n📋 Внешние интеграции отключены - Issues не будут изменены")

    # Создаем план
    output_file = f"github-{repo.replace('/', '-')}-tasks.json"

    print(f"\n🚀 Создаем план задач: {output_file}")

    try:
        stats = fetch_github_issues(repo, label=label, out_path=output_file)

        print("✅ Импорт завершен!")
        print(f"   Новых задач: {stats.new}")
        print(f"   Обновлено: {stats.updated}")
        print(f"   Файл плана: {output_file}")

        return output_file

    except Exception as e:
        print(f"❌ Ошибка импорта: {e}")
        return None


def _browse_issues_workflow() -> None:
    """Просмотр issues с меткой whilly:ready."""

    print("\n🔍 Просмотр GitHub Issues")
    print("-" * 25)

    repo = input("Репозиторий (owner/repo): ").strip()
    if not repo or "/" not in repo:
        print("❌ Неверный формат репозитория")
        return

    label = input("Метка (whilly:ready): ").strip() or "whilly:ready"

    issues = _get_repo_issues(repo, label)

    if not issues:
        print(f"\n❌ Issues с меткой '{label}' не найдены")
        return

    print(f"\n✅ Найдено {len(issues)} issues с меткой '{label}':")
    for issue in issues:
        print(f"  #{issue['number']}: {issue['title']}")
        print(f"    🔗 {issue['url']}")
        print()


def _create_label_workflow() -> None:
    """Создание метки whilly:ready."""

    print("\n🏷️  Создание метки whilly:ready")
    print("-" * 30)

    repo = input("Репозиторий (owner/repo): ").strip()
    if not repo or "/" not in repo:
        print("❌ Неверный формат репозитория")
        return

    description = "Tasks ready for Whilly automation"
    color = "0052cc"  # Синий цвет

    cmd = ["gh", "label", "create", "whilly:ready", "--repo", repo, "--description", description, "--color", color]

    success, output = _run_command(cmd)

    if success:
        print(f"✅ Метка 'whilly:ready' создана в {repo}")
    else:
        if "already exists" in output:
            print(f"ℹ️  Метка 'whilly:ready' уже существует в {repo}")
        else:
            print(f"❌ Ошибка создания метки: {output}")


def _create_demo_issue(repo: str, label: str) -> None:
    """Создает demo issue для демонстрации."""

    title = "Demo task for Whilly automation"
    body = """## Description
This is a demo task to test Whilly automation.

## Acceptance Criteria
- [ ] Task is picked up by Whilly
- [ ] Automation completes successfully
- [ ] Results are documented

## Test Steps
1. Verify Whilly detects this issue
2. Check task execution
3. Validate completion status
"""

    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body, "--label", label]

    success, output = _run_command(cmd)

    if success:
        print(f"✅ Demo issue создан в {repo}")
    else:
        print(f"❌ Ошибка создания demo issue: {output}")


def _github_projects_workflow() -> Optional[str]:
    """GitHub Projects workflow."""

    print("\n📊 GitHub Projects Workflow")
    print("-" * 30)

    print("Этот режим позволяет:")
    print("  • Синхронизировать Todo items из GitHub Projects")
    print("  • Мониторить изменения статусов")
    print("  • Автоматически создавать issues для новых задач")
    print()

    project_url = input("URL проекта: ").strip()
    if not project_url:
        return None

    repo = input("Репозиторий (owner/repo): ").strip()
    if not repo or "/" not in repo:
        print("❌ Неверный формат репозитория")
        return None

    print("\n🔄 Доступные операции:")
    print("  1) Синхронизировать Todo items")
    print("  2) Мониторинг проекта")
    print("  3) Полная конвертация проекта")

    try:
        choice = input("Выбор: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None

    if choice == "1":
        cmd = f"whilly --sync-todo '{project_url}' --repo {repo}"
    elif choice == "2":
        cmd = f"whilly --watch-project '{project_url}' --repo {repo}"
    elif choice == "3":
        cmd = f"whilly --from-project '{project_url}' --repo {repo}"
    else:
        print("❌ Неверный выбор")
        return None

    print("\n🚀 Команда для выполнения:")
    print(f"   {cmd}")
    print("\n(Это расширенная функциональность, требует дополнительной настройки)")

    return None


def _configure_auto_close() -> None:
    """Настройка автозакрытия GitHub Issues."""

    print("\n⚙️  Настройка автозакрытия GitHub Issues")
    print("-" * 35)

    print("Автозакрытие Issues означает что после успешного выполнения задачи")
    print("соответствующий GitHub Issue будет автоматически закрыт.")
    print()
    print("Для включения/отключения используйте переменные окружения:")
    print("  export WHILLY_GITHUB_AUTO_CLOSE=1     # включить")
    print("  export WHILLY_GITHUB_AUTO_CLOSE=0     # отключить")
    print("  export WHILLY_CLOSE_EXTERNAL_TASKS=1  # общее включение интеграций")

    input("\n📖 Нажмите Enter для возврата в меню...")


def _configure_comments() -> None:
    """Настройка комментариев в GitHub Issues."""

    print("\n💬 Настройка комментариев в GitHub Issues")
    print("-" * 35)

    print("Автоматические комментарии добавляются к Issues при выполнении задач.")
    print("Комментарии содержат информацию о результате выполнения и коммитах.")
    print()
    print("Для настройки используйте переменные окружения:")
    print("  export WHILLY_GITHUB_ADD_COMMENTS=1   # включить")
    print("  export WHILLY_GITHUB_ADD_COMMENTS=0   # отключить")

    input("\n📖 Нажмите Enter для возврата в меню...")


def _show_help(config: WhillyConfig) -> None:
    """Показывает помощь по настройке с учетом текущей конфигурации."""

    print("\n❓ Помощь по настройке GitHub интеграции")
    print("=" * 50)

    print("\n📊 Текущая конфигурация:")
    print(f"   GitHub автозакрытие: {config.GITHUB_AUTO_CLOSE}")
    print(f"   GitHub комментарии: {config.GITHUB_ADD_COMMENTS}")
    print(f"   Внешние интеграции: {config.CLOSE_EXTERNAL_TASKS}")
    print(f"   Модель: {config.MODEL}")
    print(f"   Параллельность: {config.MAX_PARALLEL}")
    print(f"   Бюджет: {config.BUDGET_USD} USD")

    print("\n1️⃣  Авторизация GitHub CLI:")
    print("   gh auth login")
    print("   # Выберите GitHub.com, HTTPS, и авторизуйтесь через браузер")

    print("\n2️⃣  Создание меток в репозитории:")
    print("   gh label create whilly:ready --repo owner/repo")
    print("   # Или используйте опцию 3 в этом меню")

    print("\n3️⃣  Подготовка issues:")
    print("   • Создайте issues в GitHub")
    print("   • Добавьте метку 'whilly:ready' к готовым задачам")
    print("   • Используйте разделы в описании:")
    print("     ## Acceptance Criteria")
    print("     ## Test Steps")
    print("     ## Dependencies")

    print("\n4️⃣  Запуск автоматизации:")
    print("   whilly --source gh:owner/repo")
    print("   # Или используйте опцию 1 в этом меню")

    print("\n5️⃣  Настройка переменных окружения:")
    print("   # Основные настройки")
    print("   export WHILLY_MAX_PARALLEL=3")
    print("   export WHILLY_BUDGET_USD=10")
    print("   export WHILLY_MODEL=claude-opus-4-6[1m]")
    print()
    print("   # GitHub интеграции")
    print("   export WHILLY_CLOSE_EXTERNAL_TASKS=1")
    print("   export WHILLY_GITHUB_AUTO_CLOSE=1")
    print("   export WHILLY_GITHUB_ADD_COMMENTS=1")
    print()
    print("   # Ресурсы и мониторинг")
    print("   export WHILLY_RESOURCE_CHECK_ENABLED=1")
    print("   export WHILLY_MAX_CPU_PERCENT=80")
    print("   export WHILLY_MAX_MEMORY_PERCENT=75")

    input("\n📖 Нажмите Enter для возврата в меню...")


if __name__ == "__main__":
    # Для тестирования
    plan_file = github_interactive_menu()
    if plan_file:
        print(f"✅ Создан план: {plan_file}")
        print("🚀 Запустите: whilly для выполнения задач")
