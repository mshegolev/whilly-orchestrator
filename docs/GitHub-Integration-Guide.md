---
title: GitHub Integration
nav_order: 4
---

# GitHub Integration Guide

Whilly предоставляет несколько способов интеграции с GitHub для автоматизации работы с Issues и Projects.

## Быстрый старт

### 1. Настройка GitHub CLI

```bash
# Установка GitHub CLI (если не установлен)
# macOS: brew install gh
# Ubuntu: apt install gh

# Авторизация
gh auth login
```

### 2. Интерактивный режим

```bash
# Запуск Whilly
whilly

# В меню выберите:
# g) 🐙 GitHub интеграция
```

Интерактивный режим предоставляет следующие возможности:

- 📥 **Импорт issues** - создание плана задач из GitHub Issues
- 🔍 **Просмотр issues** - обзор issues с метками whilly
- 🏷️ **Создание меток** - автоматическое создание меток в репозитории
- 📊 **GitHub Projects** - интеграция с GitHub Projects (beta)
- ⚙️ **Настройка интеграций** - конфигурация автозакрытия и комментариев

## Способы получения данных из GitHub

### 1. Через Issues с метками

```bash
# Прямая команда
whilly --source gh:owner/repo                    # метка whilly:ready (по умолчанию)
whilly --source gh:owner/repo:custom-label       # кастомная метка

# Примеры
whilly --source gh:mshegolev/whilly-orchestrator
whilly --source gh:mycompany/myproject:ready-for-automation
```

### 2. Через GitHub Projects (экспериментальная функция)

```bash
# Синхронизация Todo items
whilly --sync-todo 'https://github.com/users/username/projects/4' --repo owner/repo

# Мониторинг проекта
whilly --watch-project 'https://github.com/users/username/projects/4' --repo owner/repo
```

### 3. Программно через Python API

```python
from whilly.sources import fetch_github_issues

# Импорт issues в план
stats = fetch_github_issues(
    "owner/repo", 
    label="whilly:ready", 
    out_path="my-tasks.json"
)

print(f"Импортировано {stats.new} новых задач")
```

## Конфигурация интеграций

Whilly учитывает конфигурацию при работе с GitHub. Основные переменные окружения:

### Общие настройки

```bash
export WHILLY_CLOSE_EXTERNAL_TASKS=1      # Включить внешние интеграции
export WHILLY_MAX_PARALLEL=3              # Параллельные задачи
export WHILLY_BUDGET_USD=10               # Лимит бюджета
export WHILLY_MODEL=claude-opus-4-6[1m]   # Модель ИИ
```

### GitHub-специфичные настройки

```bash
export WHILLY_GITHUB_AUTO_CLOSE=1         # Автозакрытие issues после выполнения
export WHILLY_GITHUB_ADD_COMMENTS=1       # Комментарии с результатами
```

### Ресурсы и мониторинг

```bash
export WHILLY_RESOURCE_CHECK_ENABLED=1    # Мониторинг ресурсов системы
export WHILLY_MAX_CPU_PERCENT=80          # Максимальная загрузка CPU
export WHILLY_MAX_MEMORY_PERCENT=75       # Максимальное использование памяти
```

## Подготовка GitHub Issues

Для оптимальной работы с Whilly создавайте issues с четкой структурой:

### Пример issue

```markdown
## Description
Добавить функцию логирования для модуля аутентификации

## Acceptance Criteria
- [ ] Добавлен logger в auth.py
- [ ] Логируются успешные входы
- [ ] Логируются неудачные попытки входа
- [ ] Настроен уровень логирования

## Test Steps
1. Запустить тесты аутентификации
2. Проверить создание лог-файлов
3. Валидировать формат логов
4. Проверить ротацию логов

## Dependencies
- Зависит от #42 (настройка конфигурации)
```

### Обязательные метки

- `whilly:ready` - задача готова к автоматизации
- `priority:high`, `priority:medium`, `priority:low` - приоритет
- `type:feature`, `type:bug`, `type:refactor` - тип задачи

## Workflow выполнения

1. **Подготовка**: Issues создаются в GitHub с соответствующими метками
2. **Импорт**: Whilly создает план задач из issues
3. **Выполнение**: ИИ-агенты выполняют задачи параллельно
4. **Интеграция**: По завершении:
   - Issues закрываются автоматически (если включено)
   - Добавляются комментарии с результатами (если включено)
   - Создаются PR с изменениями

## Мониторинг и отчеты

Whilly предоставляет детальную отчетность:

- **Интерактивный dashboard** с прогрессом выполнения
- **JSON логи** для автоматической обработки
- **Markdown отчеты** по результатам
- **Интеграция с внешними системами** (Jira, Slack, etc.)

## Безопасность

- GitHub CLI использует собственную авторизацию
- Переменная `GITHUB_TOKEN` автоматически исключается для избежания конфликтов
- Поддерживается корпоративная настройка через `WHILLY_GH_BIN`

## Устранение неполадок

### GitHub CLI не авторизован

```bash
gh auth status
gh auth login
```

### Issues не импортируются

1. Проверьте права доступа к репозиторию
2. Убедитесь что issues имеют корректные метки
3. Проверьте что issues открыты (не закрыты)

### Интеграции не работают

1. Установите `WHILLY_CLOSE_EXTERNAL_TASKS=1`
2. Проверьте права GitHub CLI на запись в репозиторий
3. Проверьте логи выполнения в `whilly_logs/`

## Примеры использования

### Простая автоматизация

```bash
# 1. Создать issues в GitHub с меткой whilly:ready
# 2. Запустить автоматизацию
whilly --source gh:myteam/myproject

# 3. Мониторить выполнение через dashboard
# 4. Проверить результаты в GitHub
```

### Непрерывная интеграция

```bash
# В CI/CD pipeline
export WHILLY_HEADLESS=1
export WHILLY_BUDGET_USD=5
whilly --source gh:myteam/myproject --timeout 1800
```

### Локальная разработка

```bash
# Интерактивный режим для разработчика
export WHILLY_MAX_PARALLEL=1
export WHILLY_BUDGET_USD=2
whilly  # выбрать "g" для GitHub интеграции
```