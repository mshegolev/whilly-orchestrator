# External Integrations

Whilly может автоматически закрывать задачи во внешних системах после успешного выполнения:

- **GitHub Issues** 🔗
- **Jira Tasks** 🎫  
- **Linear Issues** (планируется)

## 🚀 Quick Start

### GitHub Issues (автоматически)

1. Авторизуйся в GitHub CLI:
```bash
gh auth login
```

2. Включи автоматическое закрытие:
```bash
export WHILLY_GITHUB_AUTO_CLOSE=true
export WHILLY_GITHUB_ADD_COMMENTS=true
```

3. Создавай задачи через `--from-github` - Issues будут автоматически закрываться!

### Jira Tasks

1. Настрой переменные окружения:
```bash
export WHILLY_JIRA_ENABLED=true
export WHILLY_JIRA_SERVER_URL="https://company.atlassian.net"
export WHILLY_JIRA_USERNAME="your.email@company.com"
export JIRA_API_TOKEN="your_api_token"
```

2. Добавь Jira ключи в описания задач:
```json
{
  "description": "Fix user authentication ABC-123",
  "jira_key": "ABC-123"
}
```

## 📋 Конфигурация

### Переменные окружения

```bash
# Общие настройки
WHILLY_CLOSE_EXTERNAL_TASKS=true    # Включить/выключить все интеграции

# GitHub
WHILLY_GITHUB_AUTO_CLOSE=true       # Закрывать Issues автоматически
WHILLY_GITHUB_ADD_COMMENTS=true     # Добавлять комментарии о завершении

# Jira  
WHILLY_JIRA_ENABLED=false           # Включить Jira интеграцию
WHILLY_JIRA_SERVER_URL=""           # URL Jira сервера
WHILLY_JIRA_USERNAME=""             # Jira username
WHILLY_JIRA_AUTO_CLOSE=true         # Закрывать задачи автоматически
WHILLY_JIRA_ADD_COMMENTS=true       # Добавлять комментарии
WHILLY_JIRA_TRANSITION_TO="Done"    # Статус для закрытия

# Безопасность (токены всегда через env)
JIRA_API_TOKEN=""                   # Jira API токен
```

### Конфигурационный файл

Скопируй `config/integrations.example.json` → `config/integrations.json`:

```json
{
  "enabled": true,
  "github": {
    "enabled": true,
    "auto_close": true,
    "add_comments": true
  },
  "jira": {
    "enabled": true,
    "server_url": "https://company.atlassian.net",
    "username": "user@company.com",
    "auto_close": true,
    "add_comments": true,
    "transition_to": "Done"
  }
}
```

## 🔧 Как это работает

### GitHub Issues

1. **Извлечение из Issues** - `--from-github` сохраняет номер Issue и URL
2. **Выполнение задачи** - Whilly делает коммиты
3. **Автоматическое закрытие**:
   - Добавляет комментарий с информацией о завершении
   - Закрывает Issue с reason "completed"

### Jira Tasks

1. **Обнаружение Jira ключей**:
   - Из отдельного поля `jira_key`
   - Из описания задачи (паттерн `ABC-123`)
2. **Завершение**:
   - Добавляет комментарий о завершении
   - Переводит в статус "Done" (настраивается)

### Пример коментария

```
🤖 **Whilly Task Completed**

- **Task ID**: `gh-1-add-contributing-badge`
- **Status**: ✅ Completed successfully
- **Commit**: f04ebb1a

*Automated by [Whilly Orchestrator](https://github.com/mshegolev/whilly-orchestrator)*
```

## 🎯 Интеграция с воркшопом

```bash
# 1. Извлечь GitHub Issues
python3 -m whilly --from-github workshop,whilly:ready

# 2. Запустить с автозакрытием
WHILLY_GITHUB_AUTO_CLOSE=true python3 -m whilly tasks-from-github.json

# 3. Issues автоматически закроются после выполнения! 🎉
```

## 🛠️ Расширение

Для добавления новых систем (Linear, Asana, etc):

1. Наследуй `ExternalIntegration` 
2. Реализуй `close_task()` и `add_comment()`
3. Добавь в `ExternalIntegrationManager._setup_integrations()`
4. Обновить `github_converter.py` для извлечения ссылок

## 🚨 Troubleshooting

### GitHub
```bash
# Проверь авторизацию
gh auth status

# Очисти проблемный токен
unset GITHUB_TOKEN
gh auth refresh
```

### Jira
```bash
# Проверь подключение
curl -u "username:$JIRA_API_TOKEN" \
  "$JIRA_SERVER_URL/rest/api/2/myself"

# Проверь доступные переходы
curl -u "username:$JIRA_API_TOKEN" \
  "$JIRA_SERVER_URL/rest/api/2/issue/ABC-123/transitions"
```

### Логи
```bash
# Включи отладку
export WHILLY_LOG_LEVEL=DEBUG

# Проверь логи
tail -f whilly.log | grep -i "external\|integration\|close"
```

---

**Результат**: Полностью автоматический цикл от GitHub Issue → выполнение → закрытие задачи! 🔄✨