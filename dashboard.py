"""Rich Live TUI dashboard for the ralph task orchestrator.

Replaces the bash TUI that crashed due to stty/tput issues.
Uses rich.live.Live with screen=True for full-screen refresh.
"""

from __future__ import annotations

import os
import sys
import termios
import threading
import time
import tty
from collections.abc import Callable
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from ralph.reporter import CostTotals, fmt_duration, fmt_tokens
from ralph.task_manager import PRIORITY_ORDER, TaskManager


class Dashboard:
    """Rich Live TUI dashboard displaying task progress, queue, agents, and cost."""

    def __init__(self, task_manager: TaskManager, agent_name: str, max_iterations: int) -> None:
        self.tm = task_manager
        self.agent_name = agent_name
        self.max_iterations = max_iterations
        self.console = Console()
        self.live: Live | None = None
        self.keyboard = KeyboardHandler()

        # Mutable state updated by the orchestrator
        self.iteration: int = 0
        self.phase: str = ""  # "plan" or "work"
        self.start_time: float = time.monotonic()
        self.status_msg: str = ""
        self.heartbeat_msg: str = ""
        self.totals: CostTotals = CostTotals()
        self.initial_task_count: int = 0
        self.active_agents: list[dict] = []  # [{task_id, start_time, log_file, status}]
        self._overlay_text: str | None = None  # When set, render overlay instead of main dashboard
        self._overlay_mode: str | None = None  # 'log' = auto-refresh from RALPH_LOG_PATH
        self.budget_usd: float = 0.0  # 0 = unlimited
        self.session_cost_usd: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the Live display (full-screen, 1 fps)."""
        self.live = Live(self._render(), console=self.console, refresh_per_second=1, screen=True)
        self.live.start()
        self.keyboard.register("d", self._show_task_detail)
        self.keyboard.register("l", self._show_log)
        self.keyboard.register("t", self._show_all_tasks)
        self.keyboard.register("s", self._show_stats)
        self.keyboard.register("p", self._show_prd_info)
        self.keyboard.register("g", self._generate_plan)
        self.keyboard.register("c", self._challenge_plan)
        self.keyboard.register("n", self._new_idea)
        self.keyboard.register("h", self._show_help)
        self.keyboard.register("?", self._show_help)
        self.keyboard.start()
        self._prd_wizard = None
        self._wizard_result = None

    def stop(self) -> None:
        """Stop the Live display and restore the terminal."""
        self.keyboard.stop()
        if self.live:
            self.live.stop()
            self.live = None

    def update(self) -> None:
        """Push a new render frame to the terminal."""
        if self.live:
            self.live.update(self._render())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _elapsed(self) -> str:
        d = int(time.monotonic() - self.start_time)
        return f"{d // 3600:02d}:{(d % 3600) // 60:02d}:{d % 60:02d}"

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def _render(self) -> Group:  # noqa: C901 — intentionally monolithic render
        """Build the full dashboard as a single Rich ``Group`` renderable."""
        if self._overlay_mode == "log" and self._overlay_text is not None:
            self._refresh_log_overlay()
        elif self._overlay_text is None:
            self._overlay_mode = None
        if self._overlay_text is not None:
            try:
                content = Text.from_markup(self._overlay_text)
            except Exception:
                content = Text(self._overlay_text)
            return Group(
                content,
                Text(""),
                Text.from_markup("[dim]Press any key to dismiss  \u2502  q=quit  d=detail  l=log  t=tasks  s=stats  h=help[/]"),
            )

        self.tm.reload()
        parts: list[Text] = []

        # ── Header bar ───────────────────────────────────────────────
        from ralph import __version__

        header = Text()
        header.append(
            f"  RALPH v{__version__}  \u25c6  {self._elapsed()}  \u25c6  {self.agent_name.upper()}"
            f"  \u25c6  iter {self.iteration}/{self.max_iterations}",
            style="bold white on blue",
        )
        parts.append(header)

        # ── Progress bar ─────────────────────────────────────────────
        counts = self.tm.counts_by_status()
        total = self.tm.total_count
        done = counts.get("done", 0)
        pending = counts.get("pending", 0)
        ip = counts.get("in_progress", 0)
        failed = counts.get("failed", 0)
        skipped = counts.get("skipped", 0)
        pct = (done * 100 // total) if total > 0 else 0

        phase_badge = {"plan": "[bold magenta]PLAN[/]", "work": "[bold cyan]WORK[/]"}.get(self.phase, "[dim]\u2014\u2014[/]")
        bar_width = 50
        filled = pct * bar_width // 100
        bar = "\u2588" * filled + "\u2591" * (bar_width - filled)
        parts.append(
            Text.from_markup(f" {phase_badge}  [green]{bar}[/]  {pct}%  [bold]{done}[/]/[dim]{total}[/] done")
        )

        # ── Status counts ────────────────────────────────────────────
        cnt = f"  [green]\u25cf{done} done[/]  [yellow]\u25cf{pending} pend[/]"
        if ip > 0:
            cnt += f"  [cyan]\u25cf{ip} wip[/]"
        if failed > 0:
            cnt += f"  [red]\u25cf{failed} fail[/]"
        if skipped > 0:
            cnt += f"  [dim]\u25cf{skipped} skip[/]"
        if self.initial_task_count > 0 and total != self.initial_task_count:
            cnt += f"  [dim]\u2502[/] [magenta]total {self.initial_task_count}\u2192{total}[/]"
        parts.append(Text.from_markup(cnt))

        # ── Token / cost summary ─────────────────────────────────────
        if self.totals.input_tokens + self.totals.output_tokens > 0:
            tok = (
                f"  [dim]tokens:[/] [cyan]\u2193{fmt_tokens(self.totals.input_tokens)}[/]"
                f" [green]\u2191{fmt_tokens(self.totals.output_tokens)}[/]"
            )
            if self.totals.cache_read_tokens:
                tok += f"  [dim]cache-r:{fmt_tokens(self.totals.cache_read_tokens)}[/]"
            if self.totals.cache_create_tokens:
                tok += f"  [dim]cache-w:{fmt_tokens(self.totals.cache_create_tokens)}[/]"
            tok += f"  [yellow]${self.totals.cost_usd:.2f}[/]"
            parts.append(Text.from_markup(tok))

        # ── Budget line ─────────────────────────────────────────────
        if self.budget_usd > 0:
            pct = (self.session_cost_usd / self.budget_usd * 100) if self.budget_usd else 0
            if pct > 80:
                color = "red"
            elif pct > 50:
                color = "yellow"
            else:
                color = "green"
            parts.append(
                Text.from_markup(
                    f"  [{color}]budget: ${self.session_cost_usd:.2f} / ${self.budget_usd:.2f}[/]"
                    f"  [{color}]({pct:.0f}%)[/]"
                )
            )

        parts.append(Text(""))

        # ── In Progress ──────────────────────────────────────────────
        ip_tasks = [t for t in self.tm.tasks if t.status == "in_progress"]
        if ip_tasks:
            parts.append(Text.from_markup(" [bold cyan]\u25b6 IN PROGRESS[/]"))
            for t in ip_tasks:
                prio_style = "bold red" if t.priority == "critical" else "magenta" if t.priority == "high" else None
                prio_text = f"[{prio_style}]{t.priority}[/]" if prio_style else t.priority
                parts.append(
                    Text.from_markup(
                        f"   [cyan]{t.id:<9}[/] [dim]{t.category:<12}[/]"
                        f" {prio_text} [dim]{t.description[:60]}[/]"
                    )
                )
            parts.append(Text(""))

        # ── Queue ────────────────────────────────────────────────────
        status_map = {t.id: t.status for t in self.tm.tasks}
        pending_tasks = sorted(
            [t for t in self.tm.tasks if t.status == "pending"],
            key=lambda t: (PRIORITY_ORDER.get(t.priority, 9), t.phase),
        )
        if pending_tasks:
            parts.append(Text.from_markup(" [bold yellow]\u25ce QUEUE[/] [dim](next by priority)[/]"))
            parts.append(
                Text.from_markup(
                    f"   [dim]{'#':<4} {'ID':<9} {'PRIO':<8} {'CAT':<12} {'PH':<9} {'ST':<4} {'DEPS':<12} DESCRIPTION[/]"
                )
            )
            for rank, t in enumerate(pending_tasks[:10], 1):
                deps = t.dependencies
                blocked = any(status_map.get(d) != "done" for d in deps) if deps else False
                flag = "[red]BLK[/]" if blocked else "[green]RDY[/]"
                dep_str = ",".join(d.replace("TASK-", "T") for d in deps[:3]) or "-"
                style = "dim" if blocked else "yellow"

                prio_colors = {"critical": "red", "high": "magenta"}
                pc = prio_colors.get(t.priority)
                prio_text = f"[{pc}]{t.priority}[/]" if pc else t.priority

                line = f"   [{style}]{rank:<4} {t.id:<9}[/] {prio_text} [{style}]{t.category:<12} {t.phase:<9}[/] {flag} [{style}]{dep_str:<12} {t.description[:50]}[/]"
                parts.append(Text.from_markup(line))
            parts.append(Text(""))

        # ── Completed (last 5) ───────────────────────────────────────
        done_tasks = [t for t in self.tm.tasks if t.status == "done"]
        if done_tasks:
            parts.append(Text.from_markup(" [bold green]\u2713 COMPLETED[/] [dim](last 5)[/]"))
            for t in done_tasks[-5:]:
                parts.append(
                    Text.from_markup(f"   [dim green]{t.id:<9} {t.category:<12} {t.description[:60]}[/]")
                )
            parts.append(Text(""))

        # ── Failed ───────────────────────────────────────────────────
        failed_tasks = [t for t in self.tm.tasks if t.status == "failed"]
        if failed_tasks:
            parts.append(Text.from_markup(" [bold red]\u2717 FAILED[/]"))
            for t in failed_tasks:
                parts.append(
                    Text.from_markup(f"   [red]{t.id:<9} {t.category:<12} {t.description[:60]}[/]")
                )
            parts.append(Text(""))

        # ── Separator ────────────────────────────────────────────────
        parts.append(Text("\u2500" * 80, style="dim"))

        # ── Status / heartbeat / active agents ───────────────────────
        if self.status_msg:
            parts.append(Text.from_markup(f" {self.status_msg}"))

        if self.heartbeat_msg:
            parts.append(Text.from_markup(f" {self.heartbeat_msg}"))

        for ag in self.active_agents:
            elapsed = fmt_duration(time.monotonic() - ag.get("start_time", time.monotonic()))
            tid = ag.get("task_id", "?")
            status = ag.get("status", "running")

            size = 0
            log_file = ag.get("log_file")
            if log_file:
                log_path = Path(log_file)
                if log_path.exists():
                    size = log_path.stat().st_size

            if status == "done":
                parts.append(Text.from_markup(f"   [green]\u2713 {tid}[/] [dim]{elapsed}[/] done"))
            elif status == "error":
                parts.append(Text.from_markup(f"   [red]\u2717 {tid}[/] [dim]{elapsed}[/] error"))
            else:
                parts.append(
                    Text.from_markup(f"   [cyan]\u28fe {tid}[/] [dim]{elapsed}[/] [green]+{size // 1024}KB[/]")
                )

        # ── Hotkey bar (always visible at bottom) ────────────────────
        parts.append(Text(""))
        parts.append(
            Text.from_markup(
                " [bold reverse]  q [/]Quit "
                "[bold reverse]  d [/]Detail "
                "[bold reverse]  l [/]Log "
                "[bold reverse]  t [/]Tasks "
                "[bold reverse]  s [/]Stats "
                "[bold reverse]  n [/][bold cyan]New Idea[/] "
                "[bold reverse]  g [/]ТРИЗ "
                "[bold reverse]  c [/]Challenge "
                "[bold reverse]  p [/]PRD "
                "[bold reverse]  h [/]Help"
            )
        )

        return Group(*parts)

    # ------------------------------------------------------------------
    # Hotkey overlay callbacks (R2-004 .. R2-007)
    # ------------------------------------------------------------------

    def _show_task_detail(self) -> None:
        """Hotkey d: show task detail for first in-progress or first pending task."""
        if self._overlay_text is not None:
            self._overlay_text = None
            self.update()
            return
        self.tm.reload()
        target = next((t for t in self.tm.tasks if t.status == "in_progress"), None)
        if not target:
            target = next((t for t in self.tm.tasks if t.status == "pending"), None)
        if not target:
            return
        lines = [
            f"[bold]{target.id}[/]  [{target.status}]  priority={target.priority}  phase={target.phase}",
            f"category: {target.category}",
            "",
            "[bold]Description:[/]",
            f"  {target.description}",
        ]
        if target.dependencies:
            lines.append(f"\n[bold]Dependencies:[/] {', '.join(target.dependencies)}")
        if target.acceptance_criteria:
            lines.append("\n[bold]Acceptance Criteria:[/]")
            for i, ac in enumerate(target.acceptance_criteria, 1):
                lines.append(f"  {i}. {ac}")
        if target.test_steps:
            lines.append("\n[bold]Test Steps:[/]")
            for i, ts in enumerate(target.test_steps, 1):
                lines.append(f"  {i}. {ts}")
        self._overlay_text = "\n".join(lines)
        self.update()

    def _show_log(self) -> None:
        """Hotkey l: toggle live ralph.log overlay (auto-refresh from RALPH_LOG_PATH)."""
        if self._overlay_mode == "log":
            self._overlay_mode = None
            self._overlay_text = None
            self.update()
            return
        if self._overlay_text is not None:
            self._overlay_text = None
            self.update()
            return
        self._overlay_mode = "log"
        self._refresh_log_overlay()
        self.update()

    def _refresh_log_overlay(self) -> None:
        """Re-read ralph.log and rebuild overlay text. Called each render tick in log mode."""
        log_path = Path(os.environ.get("RALPH_LOG_PATH", "ralph.log"))
        if not log_path.exists():
            self._overlay_text = f"[dim]No log file found ({log_path})[/]"
            return
        raw_lines = log_path.read_text(errors="replace").splitlines()[-30:]
        escaped = "\n".join(line.replace("[", "\\[") for line in raw_lines)
        self._overlay_text = (
            f"[bold]Log: {log_path.name}[/] [dim](live, last 30 lines — press l to close)[/]\n\n" + escaped
        )

    def _show_all_tasks(self) -> None:
        """Hotkey t: show all tasks with status icons."""
        if self._overlay_text is not None:
            self._overlay_text = None
            self.update()
            return
        self.tm.reload()
        icons = {
            "done": "[green]\u2713[/]",
            "pending": "[yellow]\u25cb[/]",
            "in_progress": "[cyan]\u25b6[/]",
            "failed": "[red]\u2717[/]",
            "skipped": "[dim]\u2212[/]",
        }
        lines = ["[bold]All Tasks[/]\n"]
        for t in self.tm.tasks:
            ic = icons.get(t.status, "?")
            lines.append(f" {ic} {t.id:<12} {t.status:<12} {t.priority:<8} {t.category:<12} {t.description[:50]}")
        self._overlay_text = "\n".join(lines)
        self.update()

    def _new_idea(self) -> None:
        """Hotkey n: launch PRD wizard for a new idea with inline text input."""
        # If in input mode — ignore (let _on_input_char handle keys)
        if getattr(self, "_input_mode", False):
            return

        # If any non-wizard overlay is showing — dismiss it
        if self._overlay_text is not None and self._wizard_result is None and not (
            self._prd_wizard and self._prd_wizard.is_running
        ):
            self._overlay_text = None
            self.update()
            return

        # If wizard result is ready — show post-gen choices directly
        if self._wizard_result is not None:
            self._show_post_gen_choices()
            return

        # If wizard already running — show status
        if self._prd_wizard and self._prd_wizard.is_running:
            self._overlay_text = (
                "[bold cyan]PRD Wizard работает...[/]\n\n"
                "[dim]Claude генерирует PRD. Основной цикл Ralph продолжает работу.\n"
                "Результат появится автоматически.[/]"
            )
            self.update()
            return

        # Enter input mode — collect text from keyboard
        self._input_buffer = ""
        self._input_mode = True
        self._update_input_overlay()
        self.keyboard.enter_input_mode(self._on_input_char)

    def _update_input_overlay(self) -> None:
        """Update overlay with current input buffer content."""
        cursor = "\u2588"  # block cursor
        buf = self._input_buffer.replace("[", "\\[")  # escape Rich markup
        self._overlay_text = (
            "[bold cyan]New Idea \u2192 PRD \u2192 Tasks[/]\n\n"
            "[bold]Опишите идею (Enter \u2014 далее, Esc \u2014 отмена):[/]\n\n"
            f"  [bold white on blue] > {buf}{cursor} [/]\n\n"
            "[dim]Примеры:[/]\n"
            "  [dim]\u2022 CLI tool для мониторинга API endpoints с алертами в Slack[/]\n"
            "  [dim]\u2022 Web dashboard для визуализации test coverage по микросервисам[/]\n"
            "  [dim]\u2022 Telegram бот для оповещения о failed pipelines в GitLab[/]\n"
        )
        self.update()

    def _on_input_char(self, ch: str) -> None:
        """Handle a character in input mode."""
        if ch == "\n" or ch == "\r":
            # Enter — show mode selection
            idea = self._input_buffer.strip()
            self._input_mode = False
            self.keyboard.exit_input_mode()

            if not idea:
                self._overlay_text = None
                self.update()
                return

            # Store idea, show mode choice
            self._pending_idea = idea
            idea_escaped = idea.replace("[", "\\[")
            self._overlay_text = (
                f"[bold cyan]Идея:[/] {idea_escaped}\n\n"
                "[bold]Выберите режим:[/]\n\n"
                "  [bold reverse]  1 [/]  [bold green]Интерактивный[/] \u2014 Claude задаст вопросы прямо здесь\n"
                "     [dim]Ralph приостановит TUI, откроется диалог с Claude.\n"
                "     Ты ответишь на вопросы, Claude создаст PRD.\n"
                "     После завершения TUI восстановится.[/]\n\n"
                "  [bold reverse]  2 [/]  [bold cyan]Фоновый[/] \u2014 Claude сам сгенерирует PRD\n"
                "     [dim]Claude сам ответит на вопросы по описанию.\n"
                "     Ralph продолжит работу. Результат через 1-2 мин.[/]\n\n"
                "  [bold reverse]  3 [/]  [bold yellow]Tmux[/] \u2014 Claude в отдельном терминале\n"
                "     [dim]Откроется tmux pane, подключись из другого терминала:\n"
                "     tmux attach -t ralph-prd-wizard[/]\n\n"
                "  [bold dim]Esc[/]  Отмена\n"
            )
            self.keyboard.register("1", self._wizard_mode_interactive)
            self.keyboard.register("2", self._wizard_mode_background)
            self.keyboard.register("3", self._wizard_mode_tmux)
            self.update()

        elif ch == "\x1b":
            # Escape — cancel
            self._input_mode = False
            self.keyboard.exit_input_mode()
            self._overlay_text = None
            self.update()

        elif ch == "\x7f" or ch == "\b":
            # Backspace
            if self._input_buffer:
                self._input_buffer = self._input_buffer[:-1]
                self._update_input_overlay()

        elif ch.isprintable():
            self._input_buffer += ch
            self._update_input_overlay()

    def _wizard_cleanup_mode_keys(self) -> None:
        self.keyboard.register("1", lambda: None)
        self.keyboard.register("2", lambda: None)
        self.keyboard.register("3", lambda: None)

    def _wizard_mode_interactive(self) -> None:
        """Mode 1: Interactive — pause TUI, run Claude in this terminal."""
        self._wizard_cleanup_mode_keys()
        idea = getattr(self, "_pending_idea", "")
        if not idea:
            return

        # Pause TUI completely (restore terminal)
        self._overlay_text = None
        self.stop()

        import subprocess
        from ralph.prd_wizard import _load_system_prompt

        system_prompt = _load_system_prompt()
        prd_dir = Path("docs")
        prd_dir.mkdir(exist_ok=True)
        slug = "".join(c for c in idea[:40].replace(" ", "-") if c.isalnum() or c in "-_")
        prd_path = prd_dir / f"PRD-{slug}.md"

        # Build system prompt with idea context
        full_system = (
            f"{system_prompt}\n\n"
            f"---\n"
            f"Контекст: пользователь описал идею:\n{idea}\n\n"
            f"Когда соберёшь достаточно информации — сгенерируй PRD "
            f"и сохрани в файл: {prd_path}\n"
            f"Начни с приветствия и первого вопроса."
        )

        print(f"\n\033[36m\033[1m{'='*60}\033[0m")
        print(f"\033[36m\033[1m  PRD Wizard — Интерактивный режим\033[0m")
        print(f"\033[36m\033[1m  Идея: {idea}\033[0m")
        print(f"\033[36m\033[1m  PRD будет сохранён: {prd_path}\033[0m")
        print(f"\033[36m\033[1m  Для выхода: /exit или Ctrl+C\033[0m")
        print(f"\033[36m\033[1m{'='*60}\033[0m\n")

        # Launch Claude in INTERACTIVE mode (no -p, no --print)
        # Claude opens its own REPL where user types messages
        try:
            subprocess.run(
                [
                    "claude",
                    "--model", getattr(self, "_model", "claude-opus-4-6[1m]"),
                    "--system-prompt", full_system,
                ],
                timeout=1800,  # 30 min max
            )
        except subprocess.TimeoutExpired:
            print("\n\033[33mTimeout (30 min). PRD wizard завершён.\033[0m")
        except KeyboardInterrupt:
            print("\n\033[33mПрервано пользователем.\033[0m")
        except FileNotFoundError:
            print("\n\033[31mClaude CLI не найден. Установите: npm install -g @anthropic-ai/claude-code\033[0m")

        # Restart TUI
        self.start()

        # Check if PRD was created
        if prd_path.exists():
            self.status_msg = f"[green]PRD создан: {prd_path.name}[/]"
            # Show post-PRD overlay with choice to generate tasks
            self._overlay_text = (
                "[bold green]PRD сохранён![/]\n\n"
                f"  [cyan]Файл:[/] {prd_path}\n\n"
                "  [bold reverse]  y [/]  Сгенерировать задачи\n"
                "  [bold reverse]  n [/]  Пропустить\n"
            )
            self.update()

            # Register temp hotkeys for post-PRD choice
            def _post_prd_yes():
                self.keyboard.register("y", lambda: None)
                self._overlay_text = (
                    "[bold cyan]Генерация задач из PRD...[/]\n\n"
                    "[dim]Claude анализирует PRD и создаёт план задач.[/]"
                )
                self.update()
                self._launch_task_gen_from_prd(idea, prd_path, prd_dir)

            def _post_prd_no():
                self.keyboard.register("y", lambda: None)
                self._overlay_text = None
                # Restore original 'n' handler
                self.keyboard.register("n", self._new_idea)
                self.update()

            self.keyboard.register("y", _post_prd_yes)
            self.keyboard.register("n", _post_prd_no)
        else:
            self.status_msg = "[yellow]PRD файл не создан[/]"
        self.update()

    def _launch_task_gen_from_prd(self, idea: str, prd_path: Path, prd_dir: Path) -> None:
        """Launch background task generation from a PRD file."""
        from ralph.prd_wizard import PrdWizard

        self._prd_wizard = PrdWizard(
            on_complete=self._wizard_on_complete,
            on_status=lambda msg: setattr(self, "status_msg", msg),
        )
        self._prd_wizard._output_dir = prd_dir
        self._prd_wizard._tasks_dir = Path(".planning")

        def _gen_tasks():
            from ralph.prd_wizard import WizardResult

            result = WizardResult(idea=idea, prd_path=prd_path)
            try:
                tasks_path = self._prd_wizard._generate_tasks(prd_path, prd_path.read_text())
                if tasks_path:
                    import json

                    data = json.loads(tasks_path.read_text())
                    result.tasks_path = tasks_path
                    result.task_count = len(data.get("tasks", []))
                result.success = True
            except Exception as e:
                result.error = str(e)
            self._wizard_result = result
            self._show_post_gen_choices()

        threading.Thread(target=_gen_tasks, daemon=True).start()

    def _show_post_gen_choices(self) -> None:
        """Show action choices after tasks have been generated.

        Called automatically after task generation completes (modes 1/2/3).
        Replaces the old flow where user had to press 'n' again.
        """
        r = self._wizard_result
        if not r:
            return
        if r.success:
            lines = [
                "[bold green]PRD Wizard завершён![/]\n",
                f"  [cyan]PRD:[/] {r.prd_path}",
                f"  [cyan]Tasks:[/] {r.tasks_path} ({r.task_count} задач)",
                f"  [cyan]Время:[/] {r.elapsed_sec:.0f}s",
                "",
                "[bold]Что делать с задачами?[/]",
                "",
                "  [bold reverse]  a [/]  Add — добавить в текущий запуск",
                "  [bold reverse]  f [/]  File — сохранить как отдельный план",
                "  [bold reverse]  x [/]  Skip — пропустить",
                "",
                "[dim]Нажмите a/f/x...[/]",
            ]
            self._overlay_text = "\n".join(lines)
            self.keyboard.register("a", self._wizard_action_add)
            self.keyboard.register("f", self._wizard_action_file)
            self.keyboard.register("x", self._wizard_action_skip)
        else:
            self._overlay_text = f"[red]PRD Wizard ошибка:[/] {r.error}"
            self._wizard_result = None
        self.update()

    def _wizard_mode_background(self) -> None:
        """Mode 2: Background — Claude generates PRD autonomously."""
        self._wizard_cleanup_mode_keys()
        idea = getattr(self, "_pending_idea", "")
        if not idea:
            return

        from ralph.prd_wizard import PrdWizard

        self._prd_wizard = PrdWizard(
            on_complete=self._wizard_on_complete,
            on_status=lambda msg: setattr(self, "status_msg", msg),
            model=getattr(self, "_model", "claude-opus-4-6[1m]"),
        )
        self._overlay_text = (
            "[bold cyan]Фоновый режим: Claude генерирует PRD автономно...[/]\n\n"
            "[dim]Без вопросов — Claude сам ответит на всё по описанию.\n"
            "Ralph продолжает работу. Нажми n когда будет готово.[/]"
        )
        self.update()
        self._prd_wizard.start(idea)

    def _wizard_mode_tmux(self) -> None:
        """Mode 3: Tmux — Claude in separate terminal."""
        self._wizard_cleanup_mode_keys()
        idea = getattr(self, "_pending_idea", "")
        if not idea:
            return

        from ralph.prd_wizard import PrdWizard

        self._prd_wizard = PrdWizard(
            on_complete=self._wizard_on_complete,
            on_status=lambda msg: setattr(self, "status_msg", msg),
            model=getattr(self, "_model", "claude-opus-4-6[1m]"),
        )
        idea_escaped = idea.replace("[", "\\[")
        self._overlay_text = (
            f"[bold yellow]Tmux режим: Claude в отдельном терминале[/]\n\n"
            f"  Идея: {idea_escaped}\n\n"
            "[bold]Подключись из другого терминала:[/]\n\n"
            "  [bold white on blue] tmux attach -t ralph-prd-wizard [/]\n\n"
            "[dim]Ralph продолжает работу. Нажми n когда завершишь.[/]"
        )
        self.update()
        self._prd_wizard.start(idea)

    def _wizard_on_complete(self, result) -> None:
        """Callback from PrdWizard background thread.

        Called by modes 2 (background) and 3 (tmux).
        Directly shows post-gen choices instead of requiring user to press 'n'.
        """
        self._wizard_result = result
        self._show_post_gen_choices()

    def _wizard_action_add(self) -> None:
        """Merge wizard tasks into current running plan."""
        r = self._wizard_result
        if not r or not r.tasks_path:
            return
        try:
            from ralph.prd_wizard import merge_tasks_into_plan

            plan_path = Path(self.tm.plan_file) if hasattr(self.tm, "plan_file") else None
            if not plan_path or not plan_path.exists():
                self._overlay_text = "[red]Текущий план не найден для merge[/]"
                self.update()
                return

            added = merge_tasks_into_plan(r.tasks_path, plan_path)
            self.tm.reload()
            self._overlay_text = f"[bold green]Добавлено {added} задач в текущий план![/]\n[dim]Plan: {plan_path.name}[/]"
            self.status_msg = f"[green]+{added} задач из PRD Wizard[/]"
        except Exception as e:
            self._overlay_text = f"[red]Merge ошибка: {e}[/]"
        self._wizard_result = None
        self._cleanup_wizard_keys()
        self.update()

    def _wizard_action_file(self) -> None:
        """Keep wizard tasks as separate plan file."""
        r = self._wizard_result
        if not r or not r.tasks_path:
            return
        self._overlay_text = (
            f"[bold green]Сохранено как отдельный план:[/]\n"
            f"  PRD: {r.prd_path}\n"
            f"  Tasks: {r.tasks_path} ({r.task_count} задач)\n\n"
            f"[dim]Запустить позже: ralph.py {r.tasks_path}[/]"
        )
        self._wizard_result = None
        self._cleanup_wizard_keys()
        self.update()

    def _wizard_action_skip(self) -> None:
        """Skip/dismiss wizard result."""
        self._wizard_result = None
        self._overlay_text = None
        self._cleanup_wizard_keys()
        self.update()

    def _cleanup_wizard_keys(self) -> None:
        """Unregister one-shot wizard action keys and restore 'n' handler."""
        self.keyboard.register("a", lambda: None)
        self.keyboard.register("f", lambda: None)
        self.keyboard.register("x", lambda: None)
        # Restore 'n' to its original handler
        self.keyboard.register("n", self._new_idea)

    def _generate_plan(self) -> None:
        """Hotkey g: generate PRD + tasks from current plan context."""
        if self._overlay_text is not None:
            self._overlay_text = None
            self.update()
            return
        self._overlay_text = (
            "[bold cyan]Generating ТРИЗ-optimized plan...[/]\n\n"
            "[dim]Analyzing current tasks with TRIZ methodology...[/]"
        )
        self.update()

        try:
            from ralph.triz_analyzer import analyze_plan_triz, format_triz_report

            tasks_data = [
                {
                    "id": t.id,
                    "description": t.description,
                    "status": t.status,
                    "priority": t.priority,
                    "phase": t.phase,
                    "dependencies": t.dependencies,
                    "category": t.category,
                }
                for t in self.tm.tasks
            ]
            project = getattr(self.tm, "_raw_data", {}).get("project", "")
            report = analyze_plan_triz(tasks_data, project)
            self._overlay_text = format_triz_report(report)
        except Exception as e:
            self._overlay_text = f"[red]ТРИЗ-анализ ошибка: {e}[/]"
        self.update()

    def _challenge_plan(self) -> None:
        """Hotkey c: challenge current plan with Devil's Advocate + TRIZ."""
        if self._overlay_text is not None:
            self._overlay_text = None
            self.update()
            return
        self._overlay_text = (
            "[bold red]Challenging plan (Devil's Advocate + ТРИЗ)...[/]\n\n"
            "[dim]Searching for contradictions, over-engineering, risks...[/]"
        )
        self.update()

        try:
            from ralph.triz_analyzer import challenge_plan, format_challenge_report

            tasks_data = [
                {
                    "id": t.id,
                    "description": t.description,
                    "status": t.status,
                    "priority": t.priority,
                    "phase": t.phase,
                    "dependencies": t.dependencies,
                    "acceptance_criteria": t.acceptance_criteria,
                    "category": t.category,
                }
                for t in self.tm.tasks
            ]
            # Load PRD if available
            prd_content = ""
            prd_file = getattr(self.tm, "_raw_data", {}).get("prd_file", "")
            if prd_file:
                from pathlib import Path

                p = Path(prd_file)
                if p.exists():
                    prd_content = p.read_text(encoding="utf-8")[:3000]

            report = challenge_plan(tasks_data, prd_content)
            self._overlay_text = format_challenge_report(report)
        except Exception as e:
            self._overlay_text = f"[red]Challenge ошибка: {e}[/]"
        self.update()

    def _show_prd_info(self) -> None:
        """Hotkey p: show PRD/plan generation info."""
        if self._overlay_text is not None:
            self._overlay_text = None
            self.update()
            return
        self._overlay_text = (
            "[bold]PRD & Task Plan Generator[/]\n\n"
            "[bold cyan]Create PRD from description:[/]\n"
            "  [dim]$ ralph.py --init \"CLI tool для автоматизации QA\"[/]\n\n"
            "[bold cyan]Generate tasks from PRD:[/]\n"
            "  [dim]$ ralph.py --plan docs/PRD-MyProject.md[/]\n\n"
            "[bold cyan]Both in one step:[/]\n"
            "  [dim]$ ralph.py --init \"описание проекта\" --plan[/]\n\n"
            "[bold]Pipeline:[/]\n"
            "  1. [cyan]--init[/] → Claude генерирует PRD (markdown)\n"
            "     Разделы: Контекст, User Stories, Функциональные требования,\n"
            "     Архитектура, Фазы, Метрики, Тесты, Зависимости, Риски\n\n"
            "  2. [cyan]--plan[/] → Claude разбивает PRD на задачи (JSON)\n"
            "     Формат: id, phase, priority, category, description,\n"
            "     dependencies, key_files, acceptance_criteria, test_steps\n\n"
            "  3. [cyan]ralph.py tasks.json[/] → Запуск оркестратора\n"
        )
        self.update()

    def _show_stats(self) -> None:
        """Hotkey s: show session statistics summary."""
        if self._overlay_text is not None:
            self._overlay_text = None
            self.update()
            return
        self.tm.reload()
        counts = self.tm.counts_by_status()
        elapsed = int(time.monotonic() - self.start_time)
        elapsed_str = f"{elapsed // 3600}h {(elapsed % 3600) // 60}m {elapsed % 60}s"

        lines = [
            "[bold]Session Statistics[/]\n",
            f"  [cyan]Elapsed:[/]      {elapsed_str}",
            f"  [cyan]Iteration:[/]    {self.iteration}/{self.max_iterations}",
            f"  [cyan]Phase:[/]        {self.phase or 'idle'}",
            "",
            "  [bold]Tasks:[/]",
            f"    [green]\u2713 Done:[/]       {counts.get('done', 0)}",
            f"    [yellow]\u25cb Pending:[/]    {counts.get('pending', 0)}",
            f"    [cyan]\u25b6 In Progress:[/] {counts.get('in_progress', 0)}",
            f"    [red]\u2717 Failed:[/]     {counts.get('failed', 0)}",
            f"    [dim]\u2212 Skipped:[/]    {counts.get('skipped', 0)}",
            "",
            "  [bold]Tokens:[/]",
            f"    Input:     {fmt_tokens(self.totals.input_tokens)}",
            f"    Output:    {fmt_tokens(self.totals.output_tokens)}",
            f"    Cache-R:   {fmt_tokens(self.totals.cache_read_tokens)}",
            f"    Cache-W:   {fmt_tokens(self.totals.cache_create_tokens)}",
            f"    [yellow]Cost: ${self.totals.cost_usd:.2f}[/]",
        ]
        if self.budget_usd > 0:
            pct = self.session_cost_usd / self.budget_usd * 100
            lines.append(f"    [{'red' if pct > 80 else 'yellow'}]Budget: ${self.session_cost_usd:.2f} / ${self.budget_usd:.2f} ({pct:.0f}%)[/]")

        if self.active_agents:
            lines.append("\n  [bold]Active Agents:[/]")
            for ag in self.active_agents:
                tid = ag.get("task_id", "?")
                elapsed_ag = fmt_duration(time.monotonic() - ag.get("start_time", time.monotonic()))
                lines.append(f"    [cyan]{tid}[/] running {elapsed_ag}")

        self._overlay_text = "\n".join(lines)
        self.update()

    def _show_help(self) -> None:
        """Hotkey h: show help screen."""
        if self._overlay_text is not None:
            self._overlay_text = None
            self.update()
            return
        from ralph import __version__

        budget_str = f"${self.budget_usd:.2f}" if self.budget_usd > 0 else "unlimited"
        plan_name = self.tm.plan_file if hasattr(self.tm, "plan_file") else "?"
        self._overlay_text = (
            f"[bold]RALPH v{__version__} \u2014 Task Orchestrator for Claude Agents[/]\n"
            f"[dim]\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500[/]\n\n"

            "[bold]Quick Start[/]\n"
            "  Ralph \u2014 оркестратор, запускающий Claude CLI агентов по JSON-плану.\n"
            "  Каждая задача = один агент. Агенты работают параллельно (tmux).\n"
            "  Ralph следит за прогрессом, стоимостью и deadlock'ами.\n\n"

            "[bold]\u2666 Рабочий цикл[/]\n"
            "  [cyan]1.[/] Создать PRD:   [dim]ralph.py --init \"описание\" --plan[/]\n"
            "  [cyan]2.[/] Запустить:     [dim]ralph.py .planning/tasks.json[/]\n"
            "  [cyan]3.[/] Наблюдать:     Dashboard обновляется каждую секунду\n"
            "  [cyan]4.[/] Новая идея:    Нажми [bold]n[/] \u2014 PRD Wizard создаст задачи на лету\n"
            "  [cyan]5.[/] Проверить:     Нажми [bold]g[/] (ТРИЗ) или [bold]c[/] (Challenge)\n\n"

            "[bold]\u2666 Hotkeys[/]\n"
            "  [bold cyan]q[/]  Quit       \u2014 остановить агентов, сохранить отчёт\n"
            "  [bold cyan]d[/]  Detail     \u2014 описание текущей задачи + AC + deps\n"
            "  [bold cyan]l[/]  Log        \u2014 последние 30 строк ralph.log\n"
            "  [bold cyan]t[/]  Tasks      \u2014 все задачи со статусами\n"
            "  [bold cyan]s[/]  Stats      \u2014 токены, стоимость, время, бюджет\n"
            "  [bold cyan]n[/]  New Idea   \u2014 PRD Wizard: идея \u2192 PRD \u2192 tasks (фоновый)\n"
            "  [bold cyan]g[/]  ТРИЗ       \u2014 анализ плана по ТРИЗ (противоречия, ИКР)\n"
            "  [bold cyan]c[/]  Challenge  \u2014 Devil's Advocate (over-eng, risks, scope)\n"
            "  [bold cyan]p[/]  PRD info   \u2014 как создавать PRD и планы\n"
            "  [bold cyan]h[/]  Help       \u2014 этот экран\n"
            "  [bold dim]Любая[/]          \u2014 закрыть overlay\n\n"

            "[bold]\u2666 CLI команды[/]\n"
            "  [dim]ralph.py[/]                          Интерактивное меню\n"
            "  [dim]ralph.py plan.json[/]                Запуск плана\n"
            "  [dim]ralph.py --all[/]                    Все планы\n"
            "  [dim]ralph.py --init \"desc\" --plan[/]     PRD + задачи\n"
            "  [dim]ralph.py --init \"desc\" --go[/]       PRD + задачи + запуск\n"
            "  [dim]ralph.py --plan PRD.md[/]            Задачи из PRD\n"
            "  [dim]ralph.py --resume[/]                 Продолжить после crash\n"
            "  [dim]ralph.py --headless[/]               CI режим (JSON stdout)\n"
            "  [dim]ralph.py --reset plan.json[/]        Сбросить все в pending\n\n"

            "[bold]\u2666 Environment[/]\n"
            "  [dim]RALPH_MAX_PARALLEL=3[/]     Параллельных агентов (1=sequential)\n"
            "  [dim]RALPH_BUDGET_USD=0[/]       Лимит стоимости (0=unlimited)\n"
            "  [dim]RALPH_MODEL=opus-4-6[/]     Модель Claude\n"
            "  [dim]RALPH_VOICE=0[/]            Отключить голосовые уведомления\n"
            "  [dim]RALPH_WEB=1[/]              HTTP статус на localhost:9191\n"
            "  [dim]RALPH_WORKTREE=1[/]         Git worktree изоляция агентов\n"
            "  [dim]RALPH_VERIFY=1[/]           Lint+test после каждой задачи\n\n"

            f"[bold]\u2666 Текущая сессия[/]\n"
            f"  Plan:       {plan_name}\n"
            f"  Agent:      {self.agent_name}\n"
            f"  Iterations: {self.max_iterations or 'unlimited'}\n"
            f"  Budget:     {budget_str}\n"
            f"  Logs:       ralph.log, ralph_logs/TASK-*.log\n"
        )
        self.update()


class KeyboardHandler:
    """Non-blocking keyboard input via daemon thread.

    Supports two modes:
    - **Normal**: single-char hotkeys dispatched to registered callbacks.
    - **Input**: all chars forwarded to an input callback (for text entry).
    """

    def __init__(self) -> None:
        self._callbacks: dict[str, Callable] = {}
        self._thread: threading.Thread | None = None
        self._running = False
        self._input_callback: Callable[[str], None] | None = None

    def register(self, key: str, callback: Callable) -> None:
        self._callbacks[key.lower()] = callback

    def enter_input_mode(self, callback: Callable[[str], None]) -> None:
        """Switch to input mode — all chars forwarded to callback."""
        self._input_callback = callback

    def exit_input_mode(self) -> None:
        """Return to normal hotkey mode."""
        self._input_callback = None

    def start(self) -> None:
        if self._running or not sys.stdin.isatty():
            return
        self._running = True
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _listen(self) -> None:
        try:
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                while self._running:
                    if sys.stdin.readable():
                        ch = sys.stdin.read(1)
                        # Input mode: forward all chars to input callback
                        if self._input_callback is not None:
                            try:
                                self._input_callback(ch)
                            except Exception:
                                pass
                            continue
                        # Normal mode: dispatch hotkey
                        cb = self._callbacks.get(ch.lower())
                        if cb:
                            try:
                                cb()
                            except Exception:
                                pass
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except (termios.error, OSError, ValueError):
            pass


class NullDashboard:
    """No-op dashboard for headless/CI mode — same interface as Dashboard, does nothing."""

    def __init__(self) -> None:
        self.iteration: int = 0
        self.phase: str = ""
        self.start_time: float = time.monotonic()
        self.status_msg: str = ""
        self.heartbeat_msg: str = ""
        self.totals: CostTotals = CostTotals()
        self.initial_task_count: int = 0
        self.active_agents: list[dict] = []
        self.keyboard = _NullKeyboard()

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def update(self) -> None:
        pass


class _NullKeyboard:
    """No-op keyboard handler for headless mode."""

    def register(self, key: str, callback: Callable) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass
