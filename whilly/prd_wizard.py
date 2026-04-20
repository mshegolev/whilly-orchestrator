"""Interactive PRD Wizard — inline PRD creation from running Whilly TUI.

Launches Claude CLI in conversational mode with the PRD-creator system prompt.
Runs in a background thread or tmux pane so Whilly's main loop continues.
After conversation completes: PRD.md → tasks.json → optionally merge into current plan.

Architecture:
    hotkey 'n' → Dashboard._new_idea()
        → PrdWizard.start(idea)           # background thread
            → Claude CLI (--system-prompt) # interactive conversation
            → saves PRD.md
            → generates tasks.json
        → PrdWizard.on_complete callback
            → Dashboard shows result overlay
            → User chooses: [a]dd to current / [n]ew plan / [s]kip

Usage:
    from whilly.prd_wizard import PrdWizard

    wizard = PrdWizard(on_complete=callback, model="claude-opus-4-6[1m]")
    wizard.start("Хочу сделать CLI tool для мониторинга API")
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("whilly.prd_wizard")

# Load system prompt from file or use embedded
_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent.parent / "config" / "prd_wizard_prompt.md"

_EMBEDDED_SYSTEM_PROMPT = """\
Ты — профессиональный product manager и software developer.
Помоги спланировать идею программного продукта через структурированные вопросы.

Задавай вопросы ПО ОДНОМУ в разговорном формате:
1. Основные функции и функциональность
2. Целевая аудитория
3. Платформа (web, mobile, desktop, CLI)
4. UI/UX концепция
5. Данные и хранение
6. Безопасность и аутентификация
7. Интеграции с внешними сервисами
8. Масштабируемость
9. Технические сложности
10. Затраты (API, hosting)

После сбора информации СГЕНЕРИРУЙ полный PRD.md:
- Обзор и цели
- Целевая аудитория
- Функции с acceptance criteria
- Технический стек
- Модель данных
- UI принципы
- Безопасность
- Этапы разработки / milestones
- Проблемы и решения
- Будущее расширение

Формат вывода: чистый markdown PRD. В конце напиши: ---END_PRD---

Начни с приветствия и попроси описать идею.
"""


def _shell_escape(s: str) -> str:
    """Escape string for safe use in shell commands (double-quote context)."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")


def _load_system_prompt() -> str:
    """Load system prompt from file or use embedded."""
    if _SYSTEM_PROMPT_PATH.exists():
        return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    # Try user's custom prompt
    custom = Path.home() / "Downloads" / "saas-from-0" / "prd" / "инструкция_PRD-создатель.md"
    if custom.exists():
        return custom.read_text(encoding="utf-8")
    return _EMBEDDED_SYSTEM_PROMPT


@dataclass
class WizardResult:
    """Result of PRD wizard session."""

    success: bool = False
    prd_path: Path | None = None
    tasks_path: Path | None = None
    task_count: int = 0
    error: str = ""
    idea: str = ""
    elapsed_sec: float = 0.0


class PrdWizard:
    """Interactive PRD creation wizard that runs alongside Whilly."""

    def __init__(
        self,
        on_complete: Callable[[WizardResult], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        model: str = "claude-opus-4-6[1m]",
        output_dir: str = "docs",
        tasks_dir: str = ".planning",
        use_tmux: bool = True,
    ):
        self._on_complete = on_complete
        self._on_status = on_status
        self._model = model
        self._output_dir = Path(output_dir)
        self._tasks_dir = Path(tasks_dir)
        self._use_tmux = use_tmux and shutil.which("tmux") is not None
        self._thread: threading.Thread | None = None
        self._running = False
        self.result: WizardResult | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, idea: str) -> None:
        """Start PRD wizard in background thread.

        Args:
            idea: Initial project idea/description from user.
        """
        if self._running:
            log.warning("PRD wizard already running")
            return

        self._running = True
        self._thread = threading.Thread(target=self._run, args=(idea,), daemon=True)
        self._thread.start()

    def _run(self, idea: str) -> None:
        """Background: launch interactive Claude in tmux → PRD → tasks."""
        t0 = time.time()
        result = WizardResult(idea=idea)

        try:
            # Derive PRD path
            self._output_dir.mkdir(parents=True, exist_ok=True)
            slug = idea.strip()[:40].replace(" ", "-").replace("/", "-")
            slug = "".join(c for c in slug if c.isalnum() or c in "-_")
            prd_path = self._output_dir / f"PRD-{slug}.md"
            result.prd_path = prd_path

            # Step 1: Launch interactive Claude in tmux pane
            self._status("PRD Wizard: открываю Claude в tmux...")
            ok = self._run_claude_interactive(idea, prd_path)

            if not ok or not prd_path.exists():
                result.error = "Claude сессия завершена без PRD (файл не создан)"
                return

            log.info("PRD saved: %s", prd_path)
            self._status(f"PRD сохранён: {prd_path.name}")

            # Step 2: Generate tasks from PRD
            self._status("PRD Wizard: генерация задач...")
            tasks_path = self._generate_tasks(prd_path, prd_path.read_text(encoding="utf-8"))
            if tasks_path:
                result.tasks_path = tasks_path
                data = json.loads(tasks_path.read_text(encoding="utf-8"))
                result.task_count = len(data.get("tasks", []))
                log.info("Tasks saved: %s (%d tasks)", tasks_path, result.task_count)

            result.success = True

        except Exception as e:
            result.error = str(e)
            log.error("PRD wizard error: %s", e)
        finally:
            result.elapsed_sec = time.time() - t0
            self.result = result
            self._running = False
            if self._on_complete:
                try:
                    self._on_complete(result)
                except Exception:
                    pass

    def _run_claude_interactive(self, idea: str, prd_path: Path) -> bool:
        """Launch Claude CLI in a separate tmux pane for interactive PRD conversation.

        The user answers questions in the tmux pane. Claude saves PRD to prd_path.
        This method blocks until the tmux pane closes.

        Returns:
            True if PRD file was created.
        """
        system_prompt = _load_system_prompt()
        session_name = "whilly-prd-wizard"

        # Write system prompt to temp file (Claude --system-prompt flag)
        prompt_file = Path("/tmp/whilly_prd_prompt.md")
        prompt_file.write_text(system_prompt, encoding="utf-8")

        # Initial message to Claude with the idea
        initial_msg = (
            f"Пользователь хочет создать PRD для следующей идеи:\n\n"
            f"{idea}\n\n"
            f"Задавай вопросы по одному, чтобы уточнить детали. "
            f"Когда соберёшь достаточно информации — сгенерируй PRD и сохрани в файл: {prd_path}\n"
            f"Начни с приветствия и первого вопроса."
        )

        # Build claude command that runs interactively in tmux
        claude_cmd = (
            f"claude --model {self._model} "
            f'--system-prompt "$(cat /tmp/whilly_prd_prompt.md)" '
            f'-p "{_shell_escape(initial_msg)}" '
            f"--no-max-turns"
        )

        # Kill old session if exists
        subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            capture_output=True,
            timeout=5,
        )

        # Create new tmux session with Claude running inside
        create_cmd = [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session_name,
            "-x",
            "120",
            "-y",
            "40",
            "bash",
            "-c",
            f'{claude_cmd}; echo ""; echo "PRD Wizard завершён. Окно закроется через 5 сек..."; sleep 5',
        ]

        result = subprocess.run(create_cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            log.error("Failed to create tmux session: %s", result.stderr)
            # Fallback: non-interactive mode
            return self._run_claude_noninteractive(idea, prd_path)

        log.info("PRD Wizard tmux session started: %s", session_name)
        self._status(
            f"PRD Wizard: Claude открыт в tmux '{session_name}'\nПереключись: tmux attach -t whilly-prd-wizard"
        )

        # Wait for tmux session to finish (polling)
        while True:
            time.sleep(3)
            check = subprocess.run(
                ["tmux", "has-session", "-t", session_name],
                capture_output=True,
                timeout=5,
            )
            if check.returncode != 0:
                # Session closed — Claude finished
                break

        return prd_path.exists()

    def _run_claude_noninteractive(self, idea: str, prd_path: Path) -> bool:
        """Fallback: non-interactive PRD generation (no tmux)."""
        system_prompt = _load_system_prompt()
        prompt = (
            f"{system_prompt}\n\n---\n\n"
            "Пользователь описал идею. Задай себе все вопросы мысленно, "
            "ответь на них и СРАЗУ сгенерируй полный PRD.md.\n\n"
            f"Идея: {idea}\n\n"
            f"Сохрани PRD в файл {prd_path}. Только markdown."
        )

        cmd = ["claude", "--model", self._model, "--print", "--no-input", "-p", prompt]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                env={**os.environ, "NO_COLOR": "1"},
            )
            if result.returncode != 0:
                return False
            content = result.stdout.strip()
            for fence in ("```markdown", "```"):
                if content.startswith(fence):
                    content = content[len(fence) :].strip()
            if content.endswith("```"):
                content = content[:-3].strip()
            prd_path.write_text(content, encoding="utf-8")
            return True
        except Exception:
            return False

    def _generate_tasks(self, prd_path: Path, prd_content: str) -> Path | None:
        """Generate tasks.json from PRD."""
        try:
            from whilly.prd_generator import generate_tasks

            return generate_tasks(prd_path, output_dir=str(self._tasks_dir), model=self._model)
        except Exception as e:
            log.error("Task generation failed: %s", e)
            return None

    def _status(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass


def merge_tasks_into_plan(
    source_tasks_path: Path,
    target_plan_path: Path,
    prefix: str = "NEW",
) -> int:
    """Merge tasks from source into target plan, re-IDing to avoid conflicts.

    Args:
        source_tasks_path: Path to newly generated tasks.json.
        target_plan_path: Path to currently running plan.
        prefix: Prefix for new task IDs.

    Returns:
        Number of tasks added.
    """
    source = json.loads(source_tasks_path.read_text(encoding="utf-8"))
    target = json.loads(target_plan_path.read_text(encoding="utf-8"))

    existing_ids = {t["id"] for t in target.get("tasks", [])}

    # Find max numeric ID in target
    max_num = 0
    for tid in existing_ids:
        parts = tid.replace("TASK-", "").replace(prefix + "-", "")
        try:
            max_num = max(max_num, int(parts))
        except ValueError:
            pass

    added = 0
    for task in source.get("tasks", []):
        max_num += 1
        old_id = task["id"]
        new_id = f"TASK-{max_num:03d}"
        task["id"] = new_id
        task["status"] = "pending"
        # Remap dependencies
        task["dependencies"] = [d for d in task.get("dependencies", []) if d in existing_ids]
        # Tag origin
        task["_origin"] = f"prd_wizard:{source_tasks_path.name}"
        target["tasks"].append(task)
        added += 1
        log.info("Merged task %s (was %s): %s", new_id, old_id, task.get("description", "")[:50])

    target_plan_path.write_text(json.dumps(target, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Merged %d tasks into %s", added, target_plan_path.name)
    return added
