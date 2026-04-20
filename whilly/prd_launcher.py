"""Interactive PRD wizard — launches claude CLI directly with PRD system prompt.

В отличие от prd_wizard.py (tmux + headless -p), этот модуль запускает claude в
интерактивном режиме прямо в текущем терминале. Пользователь сразу видит промпт
PRD-мастера и пишет идею в живом диалоге, как с обычным claude.

Flow:
    1. Попросить slug (если не задан) — для имени файла PRD-{slug}.md
    2. Собрать system prompt: config/prd_wizard_prompt.md + инструкция куда сохранять
    3. Запустить `claude --append-system-prompt @file` интерактивно (stdin/stdout наследуются)
    4. После выхода claude — если PRD создан, запустить generate_tasks()
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("whilly.prd_launcher")

_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent.parent / "config" / "prd_wizard_prompt.md"


def _sanitize_slug(text: str) -> str:
    """Keep alnum, dashes and underscores; collapse spaces to dashes."""
    text = text.strip().lower().replace(" ", "-").replace("/", "-")
    text = re.sub(r"[^a-z0-9а-яё_-]+", "", text)
    return text[:60] or "untitled"


def _generate_short_slug(description: str) -> str:
    """Generate a short slug (≤10 chars) from description by extracting key words."""
    # Remove brackets, tags, and common words
    cleaned = re.sub(r"[\[\](){}]", "", description.lower())
    # Split into words and filter out common/stop words
    stop_words = {
        "add",
        "remove",
        "fix",
        "update",
        "create",
        "implement",
        "the",
        "to",
        "a",
        "an",
        "and",
        "or",
        "in",
        "on",
        "at",
        "for",
        "with",
        "by",
    }
    words = [w for w in re.findall(r"\b\w+\b", cleaned) if w not in stop_words and len(w) > 2]

    if not words:
        # Fallback to first few chars of original text
        fallback = re.sub(r"[^a-z0-9]", "", description.lower())[:10]
        return fallback or "task"

    # Try to build a meaningful short slug
    if len(words) == 1:
        return words[0][:10]
    elif len(words) == 2:
        # Try to fit both words
        first, second = words[0][:5], words[1][:5]
        if len(first + second) <= 10:
            return first + second
        else:
            return first[:6] + second[:4]
    else:
        # Use first letters of first 3-4 words or abbreviate
        if any(len(w) >= 4 for w in words[:3]):
            # Take first 3-4 chars from 2-3 most meaningful words
            result = words[0][:4] + words[1][:3]
            if len(result) < 10 and len(words) > 2:
                result += words[2][:3]
            return result[:10]
        else:
            # Create acronym from first letters
            acronym = "".join(w[0] for w in words[:10])
            return acronym[:10]


def _build_system_prompt(prd_path: Path) -> str:
    """Load PRD master prompt with forceful override of default agentic behavior."""
    if not _SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(f"PRD system prompt not found: {_SYSTEM_PROMPT_PATH}")
    base = _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    head = (
        "# КРИТИЧЕСКИ ВАЖНО: РЕЖИМ PRD-ИНТЕРВЬЮ\n\n"
        "Ты НЕ в режиме разработки. Ты в режиме СБОРА ТРЕБОВАНИЙ через диалог.\n\n"
        "## ЗАПРЕЩЕНО:\n"
        "- Использовать Read / Grep / Glob / Bash / WebFetch / WebSearch для исследования идеи\n"
        "- Начинать реализацию, писать код, искать файлы в проекте\n"
        "- Предлагать решения до того как задал минимум 6 уточняющих вопросов\n"
        "- Отвечать развёрнуто несколькими абзацами\n\n"
        "## ОБЯЗАТЕЛЬНО:\n"
        "- Поздоровайся одной короткой фразой и задай ПЕРВЫЙ уточняющий вопрос\n"
        "- ОДИН вопрос за раз, жди ответ пользователя\n"
        "- После 8-12 вопросов составь PRD по шаблону ниже\n"
        f"- Сохрани PRD ТОЛЬКО в файл через Write tool: {prd_path}\n"
        "- После сохранения скажи 'PRD готов, выйди через /exit' и жди /exit\n\n"
        "**Первое сообщение пользователя — это его идея. НЕ РЕАЛИЗУЙ ЕЁ. Задай уточняющий вопрос.**\n\n"
        "---\n\n"
    )
    tail = f"\n\n---\n\n## Путь сохранения PRD\n\nИспользуй ТОЛЬКО этот путь при Write: `{prd_path}`\n"
    return head + base + tail


def run_prd_wizard(
    slug: str | None = None,
    output_dir: Path | str = "docs",
    generate_tasks_after: bool = True,
    model: str | None = None,
) -> int:
    """Launch interactive Claude CLI preloaded with PRD master system prompt.

    Args:
        slug: Имя для PRD файла (PRD-{slug}.md). Если None — спросим интерактивно.
        output_dir: Директория для PRD файла.
        generate_tasks_after: После выхода claude запустить generate_tasks().
        model: Модель claude (None = claude default).

    Returns:
        0 если PRD создан, 1 если нет.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not slug:
        try:
            slug = input("Slug для PRD (например, feature-auth-v2): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nОтменено.", file=sys.stderr)
            return 1

    # If slug looks like a description (>15 chars or contains spaces/brackets), generate a short one
    if len(slug) > 15 or any(char in slug for char in " [](){}") or not re.match(r"^[a-z0-9_-]+$", slug.lower()):
        original_input = slug
        slug = _generate_short_slug(slug)
        print(f"Автоматически сгенерирован короткий slug: '{slug}' (из '{original_input}')")
    else:
        slug = _sanitize_slug(slug)
    prd_path = (output_dir / f"PRD-{slug}.md").resolve()

    if prd_path.exists():
        print(f"⚠️  {prd_path} уже существует. Будет перезаписан если claude сохранит туда.")

    system_prompt = _build_system_prompt(prd_path)
    # Сохраняем копию для отладки, но передаём содержимое напрямую —
    # claude CLI не разворачивает @file для --append-system-prompt.
    debug_copy = Path("/tmp/whilly_prd_system_prompt.md")
    debug_copy.write_text(system_prompt, encoding="utf-8")

    cmd = [
        "claude",
        "--append-system-prompt",
        system_prompt,
        "--permission-mode",
        "acceptEdits",
    ]
    if model:
        cmd.extend(["--model", model])

    print()
    print(f"→ Открываю Claude с PRD-мастером. Файл будет сохранён: {prd_path}")
    print("→ Пиши свою идею первым сообщением. Claude задаст уточняющие вопросы.")
    print("→ Когда закончишь — /exit или Ctrl+D. Whilly продолжит генерацию tasks.json.")
    print()

    try:
        rc = subprocess.run(cmd).returncode
    except FileNotFoundError:
        print("❌ claude CLI не найден в PATH. Установи Claude Code.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n⚠️  Прервано пользователем.", file=sys.stderr)
        rc = 130

    if rc != 0:
        log.warning("claude CLI exit code: %s", rc)

    if not prd_path.exists():
        print(f"\n❌ PRD файл не создан: {prd_path}")
        print("   Вероятно ты вышел раньше, чем Claude успел сохранить.")
        return 1

    size = prd_path.stat().st_size
    print(f"\n✅ PRD сохранён: {prd_path} ({size} байт)")

    if generate_tasks_after:
        from whilly.prd_generator import generate_tasks

        print("→ Генерирую tasks.json из PRD...")
        try:
            tasks_path = generate_tasks(prd_path, model=model) if model else generate_tasks(prd_path)
            import json

            task_count = len(json.loads(tasks_path.read_text(encoding="utf-8")).get("tasks", []))
            print(f"✅ Tasks: {tasks_path} ({task_count} задач)")
        except Exception as e:
            print(f"⚠️  Генерация tasks провалилась: {e}")
            print(f"   PRD сохранён, запусти позже: whilly.py --tasks-from {prd_path}")

    return 0
