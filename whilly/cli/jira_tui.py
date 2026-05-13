"""Interactive TUI intake for a single Jira issue.

Phase 1 of the Jira Scheduler integration (TASK-SCH-001 to TASK-SCH-006).
Provides a Rich-based intake screen with 4 action modes: PRD/Plan/Autonomous/Interactive.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import termios
import tty
from collections.abc import Callable, MutableMapping, Sequence
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from whilly.cli.jira import (
    _clear_repo_target,
    _write_intake_context,
    _write_jira_work_metadata,
    _write_plan_id,
    _ensure_jira_config,
    IntakeRepoChoice,
)
from whilly.sources.jira import fetch_single_jira_issue, parse_jira_key

Fetcher = Callable[..., tuple[Path, Any]]
Runner = Callable[[Sequence[str]], int]
ConfigLoader = Callable[[], Any]
ConfigReader = Callable[[], dict[str, Any]]
Prompt = Callable[[str], str]
IsATTY = Callable[[], bool]


def _render_intake_screen(console: Console, key: str, plan_path: Path, work_meta: dict[str, Any]) -> None:
    """Render Rich intake summary screen with classification and action menu."""
    plan_json = json.loads(plan_path.read_text(encoding="utf-8"))

    # Extract first task description
    tasks = plan_json.get("tasks", [])
    first_task = tasks[0] if tasks else {}
    task_description = first_task.get("description", "")[:80]

    # Extract repo target
    repo_targets = plan_json.get("repo_targets", [])
    repo_target_id = repo_targets[0].get("id", "—") if repo_targets else "—"

    # Build intake summary table
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Label", style="dim", width=20)
    table.add_column("Value")

    table.add_row("[bold cyan]Issue key[/]", key)
    table.add_row("[bold cyan]Description[/]", task_description or "(empty)")
    table.add_row("[bold cyan]Kind[/]", work_meta.get("kind", "unknown"))
    table.add_row("[bold cyan]Confidence[/]", work_meta.get("confidence", "unknown"))
    table.add_row("[bold cyan]Recommended flow[/]", work_meta.get("recommended_flow", "unknown"))
    table.add_row("[bold cyan]Repo target[/]", repo_target_id)

    # Wrap in panel
    panel = Panel(table, title=key, border_style="blue", expand=False)
    console.print()
    console.print(panel)
    console.print()

    # Print action menu
    menu = "  [bold]1[/] PRD   [bold]2[/] Plan   [bold]3[/] Autonomous   [bold]4[/] Interactive   [bold]Q[/] Quit"
    console.print(menu)
    console.print()


def _read_single_key() -> str:
    """Read a single keystroke without requiring Enter.

    Falls back to input() on Windows/CI where termios is unavailable.
    Returns lowercased character or empty string if EOF.
    """
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        return ch.lower()
    except (ImportError, OSError, ValueError):
        # termios unavailable (Windows) or not a TTY; fall back to input()
        try:
            return input("Choice: ")[:1].lower()
        except EOFError:
            return ""


def _dispatch_tui_action(
    choice: str,
    key: str,
    plan_id: str,
    plan_path: Path,
    console: Console,
    plan_runner: Runner,
    args: Any,
) -> int:
    """Dispatch TUI action based on user choice."""
    if choice in ("q", ""):
        return 0

    if choice == "1" or choice == "prd":
        # PRD action: write context and exit
        repo_choice = IntakeRepoChoice(kind="skip", target=None)
        context_path = _write_intake_context(
            plan_path, key=key, plan_id=plan_id, repo_choice=repo_choice, context_out=None
        )
        console.print(f"✓ PRD context written to: {context_path}")
        return 0

    if choice == "2" or choice == "plan":
        # Plan preflight: apply plan and run TRIZ
        rc1 = plan_runner(["apply", str(plan_path), "--strict"])
        if rc1 != 0:
            console.print(f"[red]Error: plan apply failed with exit code {rc1}[/]")
            return rc1

        rc2 = plan_runner(["triz", plan_id, "--strict"])
        if rc2 != 0:
            console.print(f"[red]Error: plan triz failed with exit code {rc2}[/]")
            return rc2

        console.print("[green]✓ Preflight complete[/]")
        return 0

    if choice == "3" or choice == "run":
        # Autonomous action: apply plan and enter TUI
        rc = plan_runner(["apply", str(plan_path), "--strict"])
        if rc != 0:
            console.print(f"[red]Error: plan apply failed with exit code {rc}[/]")
            return rc

        # Transition to TUI (plan-scoped)
        from whilly.cli.tui import run_tui_command

        return run_tui_command(["--plan", plan_id])

    if choice == "4" or choice == "interactive":
        # Interactive action: same as autonomous (plan applied; operator controls from TUI)
        rc = plan_runner(["apply", str(plan_path), "--strict"])
        if rc != 0:
            console.print(f"[red]Error: plan apply failed with exit code {rc}[/]")
            return rc

        from whilly.cli.tui import run_tui_command

        return run_tui_command(["--plan", plan_id])

    if choice == "s" or choice == "save":
        console.print(f"✓ Plan saved to: {plan_path}")
        return 0

    console.print(f"[yellow]Unknown choice: {choice}[/]")
    return 1


def run_jira_tui_command(
    argv: Sequence[str],
    *,
    fetcher: Fetcher | None = None,
    plan_runner: Runner | None = None,
    config_loader: ConfigLoader | None = None,
    config_reader: ConfigReader | None = None,
    prompt: Prompt | None = None,
    secret_prompt: Prompt | None = None,
    environ: MutableMapping[str, str] | None = None,
    stdin_isatty: IsATTY | None = None,
) -> int:
    """Main entry point for `whilly jira tui` command.

    Returns process exit code.
    """
    # Parse arguments
    parser = argparse.ArgumentParser(prog="whilly jira tui", description="Interactive TUI intake for a Jira issue.")
    parser.add_argument("jira_ref", help="Jira key or browse URL")
    parser.add_argument("--action", choices=["prd", "plan", "run", "interactive", "save"], default=None)
    parser.add_argument("--plan-id", dest="plan_id", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--timeout", type=int, default=15)

    args = parser.parse_args(list(argv)[1:])  # Skip 'tui' subcommand

    # Normalize config defaults
    environ = environ or os.environ
    stdin_isatty = stdin_isatty or sys.stdin.isatty
    fetcher = fetcher or fetch_single_jira_issue
    config_loader = config_loader or (lambda: None)
    config_reader = config_reader or (lambda: {})
    prompt = prompt or input
    secret_prompt = secret_prompt or getpass.getpass

    console = Console()

    # Parse and normalize Jira key
    try:
        key = parse_jira_key(args.jira_ref)
    except ValueError as e:
        console.print(f"[red]Error: {e}[/]")
        return 1

    # Generate defaults
    plan_id = args.plan_id or f"jira-{key.lower()}"
    out_path = Path(args.out or f"out/jira-{key.lower()}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Validate Jira configuration
    rc = _ensure_jira_config(
        args,
        config_loader=config_loader,
        config_reader=config_reader,
        prompt=prompt,
        secret_prompt=secret_prompt,
        environ=environ,
    )
    if rc != 0:
        return rc

    # Fetch issue
    try:
        with console.status(f"Fetching {key}…"):
            plan_path, _ = fetcher(key, out_path, timeout=args.timeout)
            _write_plan_id(plan_path, plan_id)
            _clear_repo_target(plan_path)
            work_meta = _write_jira_work_metadata(plan_path, key=key, repo_path=None)
    except Exception as e:
        console.print(f"[red]Error: Failed to fetch issue: {e}[/]")
        return 1

    # Render intake screen
    _render_intake_screen(console, key, plan_path, work_meta)

    # Determine choice: explicit flag, interactive keypress, or save in non-TTY
    if args.action:
        choice = args.action
    elif stdin_isatty():
        choice = _read_single_key()
    else:
        choice = "save"

    # Dispatch action
    return _dispatch_tui_action(choice, key, plan_id, plan_path, console, plan_runner, args)
