import logging
from collections import defaultdict
from pathlib import Path

log = logging.getLogger("ralph")


def plan_batches(ready_tasks: list, max_parallel: int) -> list[list]:
    """Group ready tasks into parallel batches based on key_files overlap.

    Tasks that share key_files cannot run in parallel.
    Returns list of batches, each batch is a list of Task objects.
    """
    if max_parallel <= 1:
        return [[t] for t in ready_tasks]

    batches = []
    remaining = list(ready_tasks)

    while remaining:
        batch = []
        batch_files: set[str] = set()
        next_remaining = []

        for task in remaining:
            task_files = set(task.key_files)
            if not task_files or not (batch_files & task_files):
                batch.append(task)
                batch_files |= task_files
                if len(batch) >= max_parallel:
                    idx = remaining.index(task)
                    next_remaining.extend(remaining[idx + 1 :])
                    break
            else:
                next_remaining.append(task)

        if batch:
            batches.append(batch)
        remaining = next_remaining

    return batches


def build_orchestrator_prompt(ready_tasks: list, max_parallel: int) -> str:
    """Build prompt for LLM-based task orchestration."""
    task_lines = "\n".join(f"  {t.id}: {t.description[:80]} (key_files: {t.key_files})" for t in ready_tasks)
    return (
        f"Ты — архитектор-оркестратор. Определи, какие из готовых задач можно выполнять ПАРАЛЛЕЛЬНО.\n\n"
        f"Готовые задачи:\n{task_lines}\n\n"
        f"Максимум параллельных агентов: {max_parallel}\n\n"
        "Правила:\n"
        "- Задачи с общими key_files НЕЛЬЗЯ параллелить\n"
        "- При сомнениях — НЕ параллелить\n\n"
        'Ответь ТОЛЬКО валидным JSON — массив массивов task ID:\n'
        '[["TASK-001", "TASK-003"], ["TASK-002"]]\n\n'
        "ВАЖНО: верни ТОЛЬКО JSON, без пояснений."
    )


def plan_batches_llm(
    ready_tasks: list,
    max_parallel: int,
    tasks_file: str,
    agent_model: str,
) -> list[list]:
    """LLM-based orchestration with fallback to file-based.

    Uses json-repair for robust parsing. Falls back to plan_batches() on any error.
    """
    if len(ready_tasks) <= 1:
        return [[t] for t in ready_tasks]

    prompt = build_orchestrator_prompt(ready_tasks, max_parallel)

    try:
        from ralph.agent_runner import run_agent

        result = run_agent(prompt, model=agent_model, timeout=120)

        if result.exit_code != 0:
            log.warning("LLM orchestrator failed (exit=%d), falling back to file-based", result.exit_code)
            return plan_batches(ready_tasks, max_parallel)

        raw = result.result_text.strip()

        # Try json-repair for robust parsing
        try:
            import json_repair

            batches_raw = json_repair.loads(raw)
        except Exception:
            import json
            import re

            # Strip markdown fences
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            batches_raw = json.loads(raw)

        if not isinstance(batches_raw, list):
            raise ValueError(f"Expected list, got {type(batches_raw)}")

        # Validate task IDs
        valid_ids = {t.id for t in ready_tasks}
        task_map = {t.id: t for t in ready_tasks}
        batches = []
        for batch_raw in batches_raw:
            if not isinstance(batch_raw, list):
                continue
            batch = [task_map[tid] for tid in batch_raw if tid in valid_ids]
            if batch:
                batches.append(batch)

        if batches:
            log.info("LLM orchestrator: %d batches planned", len(batches))
            return batches

        log.warning("LLM orchestrator returned empty batches, falling back")
        return plan_batches(ready_tasks, max_parallel)

    except Exception as e:
        log.warning("LLM orchestrator error: %s, falling back to file-based", e)
        return plan_batches(ready_tasks, max_parallel)


def detect_module_overlap(batch: list) -> dict[str, list]:
    """Detect tasks in batch that share the same module (first 2 path components).

    Returns {module: [task_ids]} for modules with >1 task.
    """
    module_tasks: defaultdict[str, list[str]] = defaultdict(list)
    for task in batch:
        for fpath in task.key_files:
            parts = fpath.split("/")
            mod = "/".join(parts[:2]) if len(parts) >= 2 else parts[0] if parts else ""
            if mod:
                module_tasks[mod].append(task.id)

    return {mod: list(dict.fromkeys(tids)) for mod, tids in module_tasks.items() if len(set(tids)) > 1}


def build_interface_agreement_prompt(module: str, task_ids: list[str], tasks_file: str) -> str:
    """Build prompt for interface agreement between parallel tasks sharing a module."""
    return (
        f"@{tasks_file}\n\n"
        f'Несколько задач будут выполняться ПАРАЛЛЕЛЬНО в модуле "{module}".\n'
        f"Задачи: {', '.join(task_ids)}\n\n"
        "Определи ИНТЕРФЕЙСНЫЙ КОНТРАКТ между этими задачами:\n"
        "1. Общие типы/интерфейсы с точными сигнатурами\n"
        "2. Именование функций/классов/переменных\n"
        "3. Import paths\n"
        "4. Shared constants/enums\n\n"
        f"Создай файл .planning/interfaces/{module.replace('/', '_')}_contract.md\n"
        "Выведи <promise>COMPLETE</promise> когда контракт создан."
    )


def run_interface_agreement(
    module: str, task_ids: list[str], tasks_file: str, agent_model: str, log_dir: Path
) -> None:
    """Run LLM to define interface contract for shared module tasks."""
    log.info("Interface agreement: module=%s tasks=%s", module, task_ids)
    prompt = build_interface_agreement_prompt(module, task_ids, tasks_file)

    from ralph.agent_runner import run_agent

    result = run_agent(prompt, model=agent_model, timeout=180)

    log_file = log_dir / f"interface_{module.replace('/', '_')}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(result.result_text, encoding="utf-8")

    if result.is_complete:
        log.info("Interface agreement COMPLETE for %s", module)
    else:
        log.warning("Interface agreement: no COMPLETE signal for %s", module)
