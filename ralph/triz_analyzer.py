"""TRIZ-based plan analyzer and challenger for Ralph orchestrator.

Applies TRIZ (Theory of Inventive Problem Solving) methodology to:
1. Identify technical contradictions in task plans
2. Challenge assumptions and dependencies
3. Suggest improvements based on TRIZ principles
4. Rate plan "ideality" (useful functions / harmful + cost)

Usage:
    from ralph.triz_analyzer import analyze_plan_triz, challenge_plan, format_triz_report

    report = analyze_plan_triz(tasks, project_description)
    challenge = challenge_plan(tasks, prd_content)
    text = format_triz_report(report)
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("ralph.triz")

# ── TRIZ Principles Reference ────────────────────────────────────────

TRIZ_PRINCIPLES = {
    1: ("Сегментация", "Разделить объект на независимые части"),
    2: ("Вынесение", "Отделить «мешающую» часть или свойство"),
    5: ("Объединение", "Объединить однородные объекты/операции"),
    10: ("Предварительное действие", "Выполнить действие заранее"),
    13: ("Наоборот", "Вместо действия — обратное действие"),
    15: ("Динамичность", "Характеристики должны быть изменяемыми"),
    22: ("Обратить вред в пользу", "Использовать вредные факторы для получения пользы"),
    25: ("Самообслуживание", "Объект сам себя обслуживает"),
    35: ("Изменение параметров", "Изменить агрегатное состояние / параметр"),
    40: ("Композитные материалы", "Перейти к составным объектам"),
}

_TRIZ_ANALYZE_PROMPT = """\
Ты — ТРИЗ-эксперт (Теория Решения Изобретательских Задач).

Проанализируй план задач проекта используя методику ТРИЗ:

## 1. Технические противоречия (ТП)
Для каждого ТП:
- Что улучшается vs что ухудшается
- Какие задачи затрагивает (task IDs)
- Применимые принципы ТРИЗ (номер + название)

## 2. Физические противоречия (ФП)
- Объект должен иметь свойство X и НЕ-X одновременно
- Приём разрешения: разделение в пространстве / времени / условии

## 3. Идеальность плана
- Идеальный конечный результат (ИКР): что было бы, если бы план выполнился сам?
- Текущий уровень идеальности: (полезные функции) / (вредные + затраты)
- Что можно убрать без потери результата?

## 4. Ресурсы
- Какие ресурсы не используются? (время, информация, связи между задачами)
- Какие задачи можно объединить? (Принцип 5 — Объединение)
- Какие задачи дублируют работу?

## 5. Противоречия зависимостей
- Циклические зависимости
- Ложные зависимости (можно убрать)
- Пропущенные зависимости (нужно добавить)

## 6. Рекомендации
Для каждой рекомендации:
- Конкретное действие (add/remove/merge/split task)
- Какой принцип ТРИЗ применяется
- Ожидаемый эффект

Формат ответа — JSON:
{
  "contradictions": [
    {"type": "technical|physical", "description": "...", "task_ids": ["TASK-001"], "triz_principles": [1, 10], "resolution": "..."}
  ],
  "ideality": {
    "ikr": "описание ИКР",
    "current_score": 0.7,
    "removable_tasks": ["TASK-003"],
    "reasoning": "..."
  },
  "resources": {
    "unused": ["..."],
    "mergeable_groups": [["TASK-001", "TASK-002"]],
    "duplicates": []
  },
  "dependency_issues": [
    {"type": "false|missing|circular", "task_ids": ["TASK-001", "TASK-002"], "suggestion": "..."}
  ],
  "recommendations": [
    {"action": "merge|split|remove|add|reorder", "task_ids": ["TASK-001"], "triz_principle": 5, "description": "...", "expected_effect": "..."}
  ],
  "overall_assessment": "..."
}

Выдай ТОЛЬКО JSON.
"""

_CHALLENGE_PROMPT = """\
Ты — Devil's Advocate + ТРИЗ-эксперт. Твоя задача — ЧЕЛЛЕНДЖИТЬ план.

Для каждой задачи задай 3 вопроса:
1. **Зачем?** — Какую проблему решает? Что будет если НЕ делать?
2. **Почему так?** — Есть ли более простой способ? (Принцип ТРИЗ #13 — Наоборот)
3. **Что если?** — Что сломается если сделать наоборот? Какой worst case?

Также проверь:
- **Over-engineering**: задачи, которые добавляют сложность без пропорциональной ценности
- **Missing risks**: риски, не покрытые задачами
- **Scope creep**: задачи, выходящие за рамки PRD
- **Sequencing errors**: задачи в неправильном порядке

Формат — JSON:
{
  "challenges": [
    {
      "task_id": "TASK-001",
      "severity": "critical|high|medium|low",
      "question": "...",
      "alternative": "...",
      "triz_principle": 13
    }
  ],
  "over_engineering": ["TASK-005: description why"],
  "missing_risks": ["risk description"],
  "scope_creep": ["TASK-007: why out of scope"],
  "sequencing_errors": [{"tasks": ["TASK-003", "TASK-002"], "reason": "..."}],
  "verdict": "approve|revise|reject",
  "summary": "..."
}

Будь ЖЁСТКИМ. Отвергни слабые задачи. Предложи альтернативы.
Выдай ТОЛЬКО JSON.
"""


@dataclass
class TrizReport:
    """Result of TRIZ analysis."""

    contradictions: list[dict] = field(default_factory=list)
    ideality: dict = field(default_factory=dict)
    resources: dict = field(default_factory=dict)
    dependency_issues: list[dict] = field(default_factory=list)
    recommendations: list[dict] = field(default_factory=list)
    overall_assessment: str = ""
    raw_json: dict = field(default_factory=dict)


@dataclass
class ChallengeReport:
    """Result of plan challenge."""

    challenges: list[dict] = field(default_factory=list)
    over_engineering: list[str] = field(default_factory=list)
    missing_risks: list[str] = field(default_factory=list)
    scope_creep: list[str] = field(default_factory=list)
    sequencing_errors: list[dict] = field(default_factory=list)
    verdict: str = ""
    summary: str = ""
    raw_json: dict = field(default_factory=dict)


def analyze_plan_triz(
    tasks: list[dict],
    project_description: str = "",
    model: str = "claude-opus-4-6[1m]",
) -> TrizReport:
    """Analyze a task plan using TRIZ methodology.

    Args:
        tasks: List of task dicts from tasks.json.
        project_description: Brief project context.
        model: Claude model.

    Returns:
        TrizReport with contradictions, ideality, resources, recommendations.
    """
    tasks_text = json.dumps(tasks, indent=2, ensure_ascii=False)

    prompt = (
        f"{_TRIZ_ANALYZE_PROMPT}\n\n"
        f"Проект: {project_description}\n\n"
        f"Задачи ({len(tasks)} шт):\n```json\n{tasks_text}\n```\n"
    )

    raw = _call_claude(prompt, model)
    data = _parse_json(raw)

    return TrizReport(
        contradictions=data.get("contradictions", []),
        ideality=data.get("ideality", {}),
        resources=data.get("resources", {}),
        dependency_issues=data.get("dependency_issues", []),
        recommendations=data.get("recommendations", []),
        overall_assessment=data.get("overall_assessment", ""),
        raw_json=data,
    )


def challenge_plan(
    tasks: list[dict],
    prd_content: str = "",
    model: str = "claude-opus-4-6[1m]",
) -> ChallengeReport:
    """Challenge a task plan with Devil's Advocate + TRIZ.

    Args:
        tasks: List of task dicts.
        prd_content: PRD markdown content (for scope checking).
        model: Claude model.

    Returns:
        ChallengeReport with challenges, verdict, recommendations.
    """
    tasks_text = json.dumps(tasks, indent=2, ensure_ascii=False)
    prd_section = f"\nPRD:\n```\n{prd_content[:3000]}\n```\n" if prd_content else ""

    prompt = (
        f"{_CHALLENGE_PROMPT}\n\n"
        f"Задачи ({len(tasks)} шт):\n```json\n{tasks_text}\n```\n"
        f"{prd_section}"
    )

    raw = _call_claude(prompt, model)
    data = _parse_json(raw)

    return ChallengeReport(
        challenges=data.get("challenges", []),
        over_engineering=data.get("over_engineering", []),
        missing_risks=data.get("missing_risks", []),
        scope_creep=data.get("scope_creep", []),
        sequencing_errors=data.get("sequencing_errors", []),
        verdict=data.get("verdict", ""),
        summary=data.get("summary", ""),
        raw_json=data,
    )


def format_triz_report(report: TrizReport) -> str:
    """Format TRIZ report as Rich markup text for dashboard overlay."""
    lines = ["[bold]ТРИЗ-Анализ Плана[/]\n"]

    # Contradictions
    if report.contradictions:
        lines.append("[bold red]Противоречия:[/]")
        for i, c in enumerate(report.contradictions, 1):
            tp = c.get("type", "?").upper()
            desc = c.get("description", "")
            tasks = ", ".join(c.get("task_ids", []))
            principles = c.get("triz_principles", [])
            p_names = [f"#{p} {TRIZ_PRINCIPLES.get(p, ('?', ''))[0]}" for p in principles]
            lines.append(f"  [red]{i}.[/] [{tp}] {desc}")
            if tasks:
                lines.append(f"     [dim]Задачи: {tasks}[/]")
            if p_names:
                lines.append(f"     [cyan]Принципы: {', '.join(p_names)}[/]")
            resolution = c.get("resolution", "")
            if resolution:
                lines.append(f"     [green]Решение: {resolution}[/]")
        lines.append("")

    # Ideality
    if report.ideality:
        score = report.ideality.get("current_score", 0)
        ikr = report.ideality.get("ikr", "")
        color = "green" if score >= 0.7 else "yellow" if score >= 0.5 else "red"
        lines.append(f"[bold]Идеальность:[/] [{color}]{score:.0%}[/]")
        if ikr:
            lines.append(f"  ИКР: {ikr}")
        removable = report.ideality.get("removable_tasks", [])
        if removable:
            lines.append(f"  [yellow]Можно убрать: {', '.join(removable)}[/]")
        lines.append("")

    # Recommendations
    if report.recommendations:
        lines.append("[bold green]Рекомендации:[/]")
        for r in report.recommendations:
            action = r.get("action", "?")
            desc = r.get("description", "")
            effect = r.get("expected_effect", "")
            p = r.get("triz_principle")
            p_name = f" (ТРИЗ #{p} {TRIZ_PRINCIPLES.get(p, ('', ''))[0]})" if p else ""
            lines.append(f"  [green]\u2022[/] [{action.upper()}]{p_name} {desc}")
            if effect:
                lines.append(f"    [dim]Эффект: {effect}[/]")
        lines.append("")

    # Overall
    if report.overall_assessment:
        lines.append(f"[bold]Оценка:[/] {report.overall_assessment}")

    return "\n".join(lines)


def format_challenge_report(report: ChallengeReport) -> str:
    """Format challenge report as Rich markup text for dashboard overlay."""
    verdict_colors = {"approve": "green", "revise": "yellow", "reject": "red"}
    v_color = verdict_colors.get(report.verdict, "white")

    lines = [f"[bold]Challenge Report[/]  Verdict: [bold {v_color}]{report.verdict.upper()}[/]\n"]

    if report.summary:
        lines.append(f"[dim]{report.summary}[/]\n")

    # Critical challenges
    critical = [c for c in report.challenges if c.get("severity") in ("critical", "high")]
    if critical:
        lines.append("[bold red]Критические вопросы:[/]")
        for c in critical:
            tid = c.get("task_id", "?")
            q = c.get("question", "")
            alt = c.get("alternative", "")
            lines.append(f"  [red]\u2022 {tid}:[/] {q}")
            if alt:
                lines.append(f"    [green]Альтернатива: {alt}[/]")
        lines.append("")

    # Over-engineering
    if report.over_engineering:
        lines.append("[bold yellow]Over-engineering:[/]")
        for item in report.over_engineering:
            lines.append(f"  [yellow]\u2022[/] {item}")
        lines.append("")

    # Missing risks
    if report.missing_risks:
        lines.append("[bold red]Пропущенные риски:[/]")
        for risk in report.missing_risks:
            lines.append(f"  [red]\u2022[/] {risk}")
        lines.append("")

    # Scope creep
    if report.scope_creep:
        lines.append("[bold magenta]Scope creep:[/]")
        for item in report.scope_creep:
            lines.append(f"  [magenta]\u2022[/] {item}")
        lines.append("")

    # Sequencing
    if report.sequencing_errors:
        lines.append("[bold cyan]Ошибки последовательности:[/]")
        for err in report.sequencing_errors:
            tasks = ", ".join(err.get("tasks", []))
            lines.append(f"  [cyan]\u2022[/] {tasks}: {err.get('reason', '')}")

    return "\n".join(lines)


def _call_claude(prompt: str, model: str) -> str:
    """Call Claude CLI and return response."""
    cmd = ["claude", "--model", model, "--print", "--no-input", "-p", prompt]
    log.info("TRIZ analysis: calling Claude (%d chars prompt)...", len(prompt))
    t0 = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        log.info("Claude responded in %.1fs", time.time() - t0)
        if result.returncode != 0:
            log.error("Claude error: %s", result.stderr[:200])
            return "{}"
        return result.stdout.strip()
    except Exception as e:
        log.error("Claude call failed: %s", e)
        return "{}"


def _parse_json(text: str) -> dict:
    """Parse JSON from Claude response (handles markdown fences)."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import json_repair
            return json_repair.loads(text)
        except Exception:
            log.warning("Failed to parse TRIZ JSON response")
            return {}
