# PRD: Workshop - Self-Writing Orchestrator

## Problem Statement
Нужно продемонстрировать возможности Whilly через создание самопишущегося оркестратора, который автоматически выполняет задачи разработки из GitHub Issues. Это покажет полный цикл от задачи до готового кода.

## Objectives
- Создать воркшоп-демо самопишущегося оркестратора
- Автоматически конвертировать GitHub Issues в задачи Whilly
- Продемонстрировать возможности автоматизации разработки
- Показать интеграцию Whilly с GitHub ecosystem

## Target Users
- Разработчики, изучающие автоматизацию
- Участники воркшопа по AI-driven development
- Контрибьюторы проекта Whilly

## Requirements

### Functional Requirements
- Автоматическое извлечение задач из GitHub Issues (workshop-теги)
- Конвертация GitHub Issues в формат tasks.json
- Интеграция с GitHub Projects (проект #4 "Whilly vNext")
- Автоматический запуск выполнения задач
- Поддержка приоритетов и зависимостей из GitHub
- Создание PR после выполнения каждой задачи

### Non-Functional Requirements
- Время конвертации Issue → Task < 10 секунд
- Поддержка до 50 одновременных задач
- Логирование всех операций
- Откат при ошибках

### Technical Constraints
- Использование GitHub CLI API
- Совместимость с текущей архитектурой Whilly
- Python 3.10+ requirement

## Success Criteria
- ✅ Успешная конвертация всех workshop Issues в задачи
- ✅ Автоматическое выполнение минимум 3 задач
- ✅ Создание working PR с результатами
- ✅ Демонстрация полного цикла < 15 минут

## Out of Scope
- Полная реализация FR-1 до FR-11 из Project #4 (это для будущих версий)
- Production-ready GitHub integration
- Advanced error handling

## Risks and Assumptions
- **Risk**: GitHub API rate limits
- **Assumption**: Доступ к GitHub CLI и токенам
- **Risk**: Claude API cost на автоматические задачи
- **Assumption**: Стабильная работа tmux/workspace isolation

## Timeline
- **Phase 1** (30 мин): Создание GitHub→Whilly converter
- **Phase 2** (45 мин): Настройка автоматического workflow  
- **Phase 3** (15 мин): Демо и тестирование