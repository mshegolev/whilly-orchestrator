"""``whilly jira tui`` — interactive TUI for single-issue Jira intake.

This module wraps the existing Jira intake flow with a Rich TUI screen that shows
the issue summary and classification, offers four action choices (PRD, plan, run, save),
and transitions to the plan-scoped operator TUI if the user selects an execution action.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import termios
import tty
import webbrowser
from collections.abc import Callable, MutableMapping, Sequence
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from whilly.sources.jira import fetch_single_jira_issue, parse_jira_key

EXIT_OK = 0
EXIT_VALIDATION_ERROR = 1

Fetcher = Callable[..., tuple[Path, Any]]
Prompt = Callable[[str], str]
IsATTY = Callable[[], bool]
BrowserOpener = Callable[[str], bool]
ConfigLoader = Callable[[], Any]
ConfigReader = Callable[[], dict[str, Any]]
Runner = Callable[[Sequence[str]], int]


def build_jira_tui_parser() -> argparse.ArgumentParser:
    """Build the ``whilly jira tui ...`` argparse."""

    parser = argparse.ArgumentParser(
        prog="whilly jira tui",
        description="Interactive TUI intake for a single Jira issue.",
    )
    parser.add_argument(
        "jira_ref",
        help="Jira key or browse URL, e.g. ABC-123 or https://jira.example/browse/ABC-123.",
    )
    parser.add_argument(
        "--action",
        choices=["prd", "plan", "run", "interactive", "save"],
        default=None,
        help="Non-interactive action (skips menu); useful for scripting.",
    )
    parser.add_argument(
        "--plan-id",
        dest="plan_id",
        default=None,
        help="Plan id to write into the JSON (default: jira-<key-lowercase>).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output plan JSON path (default: out/jira-<KEY>.json).",
    )
    parser.add_argument(
        "--project-map",
        dest="project_map",
        default=None,
        help="Project map JSON/TOML file for automatic repository resolution.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="Per Jira HTTP request timeout in seconds (default: 15).",
    )
    return parser


def _render_intake_screen(
    console: Console,
    key: str,
    plan_path: Path,
    work_meta: dict[str, Any],
) -> None:
    """Render a Rich panel showing issue summary and classification."""

    data = json.loads(plan_path.read_text())
    classification = work_meta.get("classification", {})

    # Build summary table
    table = Table(box=None, show_header=False, padding=(0, 1))
    table.add_column(style="dim", no_wrap=True, width=14)
    table.add_column()

    table.add_row("Issue", Text(key, style="bold cyan"))

    # Task description (first task's description as a proxy for the issue summary)
    tasks = data.get("tasks", [{}])
    if tasks and tasks[0].get("description"):
        desc = tasks[0]["description"][:80]
        table.add_row("Summary", desc)

    table.add_row("Kind", Text(classification.get("kind", "—"), style="cyan"))
    table.add_row("Confidence", classification.get("confidence", "—"))
    table.add_row("Flow", Text(classification.get("recommended_flow", "—"), style="green"))

    # Repo target
    repo_targets = data.get("repo_targets", [])
    if repo_targets:
        repo_id = repo_targets[0].get("id", "—")
        table.add_row("Repo", Text(repo_id, style="magenta"))

    # Render panel
    console.print(Panel(table, title=f"[bold]{key}[/bold]", border_style="blue"))


def _apply_project_map(
    plan_path: Path,
    jira_key: str,
    project_map_path: str | None,
) -> None:
    """Load project map and resolve repositories for the issue.

    Args:
        plan_path: Path to the plan JSON file
        jira_key: Jira issue key (e.g., "ACME-8658")
        project_map_path: Path to project map file (optional)

    Raises:
        ValueError: if project map is invalid or resolution fails
    """

    if not project_map_path:
        return

    try:
        from whilly.project_config.loader import load_project_map
        from whilly.project_config.resolver import resolve_repositories, ProjectMapError
        from whilly.cli.jira import _write_repo_target

        project_map = load_project_map(project_map_path)

        plan_data = json.loads(plan_path.read_text())
        tasks = plan_data.get("tasks", [])
        if not tasks:
            return

        task = tasks[0]
        jira_issue = {
            "key": jira_key,
            "project": {"key": jira_key.split("-", 1)[0]},
            "labels": task.get("tags", []),
        }

        try:
            repos = resolve_repositories(jira_issue, project_map)
            if repos:
                repo_id = repos[0]
                repo_target = {
                    "id": repo_id,
                    "kind": "git",
                    "url": repo_id,
                }
                _write_repo_target(plan_path, repo_target)
        except ProjectMapError:
            pass
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError(f"Cannot apply project map {project_map_path}: {exc}") from exc


def _render_action_menu(console: Console) -> None:
    """Render the action choice menu."""

    menu_text = (
        "\n  [bold]1[/] PRD         [bold]2[/] Plan       "
        "[bold]3[/] Autonomous     [bold]4[/] Interactive     [bold]Q[/] Quit\n"
    )
    console.print(menu_text)


def _read_single_key() -> str:
    """Read a single keypress in non-blocking, non-echo mode.

    Falls back to input() when termios is unavailable (Windows, CI).
    """

    if not sys.stdin.isatty():
        return ""

    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            ch = sys.stdin.read(1)
            return ch.lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        return input("Choice [1-4, q]: ").lower()[:1]


def _dispatch_tui_action(
    choice: str,
    key: str,
    plan_id: str,
    plan_path: Path,
    console: Console,
    plan_runner: Runner,
    args: argparse.Namespace,
) -> int:
    """Dispatch to the selected action."""

    choice = choice.strip().lower() or ""

    if choice in ("q", ""):
        return EXIT_OK

    if choice in ("1", "prd"):
        # Write PRD context
        from whilly.cli.jira import _write_intake_context, IntakeRepoChoice

        try:
            context_path = _write_intake_context(
                plan_path,
                key=key,
                plan_id=plan_id,
                repo_choice=IntakeRepoChoice(kind="skip", target=None),
                context_out=None,
            )
            console.print(f"[green]PRD context written:[/] {context_path}")
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            console.print(f"[red]Error writing PRD:[/] {exc}")
            return EXIT_VALIDATION_ERROR
        return EXIT_OK

    if choice in ("2", "plan"):
        # Plan preflight: apply + triz

        apply_rc = plan_runner(["apply", str(plan_path), "--strict"])
        if apply_rc != EXIT_OK:
            console.print("[red]Plan apply failed[/]")
            return apply_rc
        triz_rc = plan_runner(["triz", plan_id, "--strict"])
        if triz_rc == EXIT_OK:
            console.print("[green]Plan preflight complete[/]")
        return triz_rc

    if choice in ("3", "run", "4", "interactive"):
        # Apply plan to DB; enter plan-scoped TUI
        # In Docker setup, running container workers will automatically claim tasks
        apply_rc = plan_runner(["apply", str(plan_path), "--strict"])
        if apply_rc != EXIT_OK:
            console.print("[red]Plan apply failed[/]")
            return apply_rc

        # Transition to TUI scoped to this plan
        from whilly.cli.tui import run_tui_command

        console.print(f"[green]Plan {plan_id} ready. Entering TUI...[/]\n")
        return run_tui_command(["--plan", plan_id])

    if choice in ("s", "save"):
        console.print(f"[green]Plan saved:[/] {plan_path}")
        return EXIT_OK

    console.print("[yellow]Unknown choice. Use 1-4 or Q.[/]")
    return EXIT_OK


def run_jira_tui_command(
    argv: Sequence[str],
    *,
    fetcher: Fetcher | None = None,
    plan_runner: Runner | None = None,
    config_loader: ConfigLoader | None = None,
    config_reader: ConfigReader | None = None,
    prompt: Prompt | None = None,
    secret_prompt: Prompt | None = None,
    browser_opener: BrowserOpener | None = None,
    environ: MutableMapping[str, str] | None = None,
    stdin_isatty: IsATTY | None = None,
) -> int:
    """Entry point for ``whilly jira tui ABC-123``."""

    from whilly.cli.jira import (
        _clear_repo_target,
        _ensure_jira_config,
        _run_plan_command,
        _write_jira_work_metadata,
        _write_plan_id,
    )

    parser = build_jira_tui_parser()
    args = parser.parse_args(list(argv))

    # Parse and normalize the Jira key
    try:
        key = parse_jira_key(args.jira_ref)
    except ValueError as exc:
        print(f"whilly jira tui: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    plan_id = args.plan_id or f"jira-{key.lower()}"
    out_path = Path(args.out) if args.out else Path("out") / f"jira-{key}.json"

    # Prepare defaults
    effective_config_loader = config_loader or (lambda: None)
    effective_config_reader = config_reader or (lambda: {})
    effective_env = environ or os.environ
    effective_stdin_isatty = stdin_isatty or sys.stdin.isatty
    effective_prompt = prompt or input
    effective_secret_prompt = secret_prompt or getpass.getpass
    effective_browser_opener = browser_opener or webbrowser.open
    effective_fetcher = fetcher or fetch_single_jira_issue
    effective_plan_runner = plan_runner or _run_plan_command

    # Set up console
    console = Console()

    # Step 1: Validate Jira config
    try:
        effective_config_loader()
        config_rc = _ensure_jira_config(
            args,
            config_reader=effective_config_reader,
            env=effective_env,
            prompt=effective_prompt,
            secret_prompt=effective_secret_prompt,
            browser_opener=effective_browser_opener,
            stdin_isatty=effective_stdin_isatty,
            command_label="whilly jira tui",
        )
        if config_rc != EXIT_OK:
            return config_rc
    except (OSError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Config error:[/] {exc}")
        return EXIT_VALIDATION_ERROR

    # Step 2: Fetch issue from Jira
    try:
        with console.status(f"Fetching {key}…"):
            plan_path, stats = effective_fetcher(key, out_path=out_path, timeout=args.timeout)
            plan_path = Path(plan_path)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        console.print(f"[red]Fetch failed:[/] {exc}")
        return EXIT_VALIDATION_ERROR

    # Step 3: Write plan metadata
    try:
        _write_plan_id(plan_path, plan_id)
        _clear_repo_target(plan_path)
        work_meta = _write_jira_work_metadata(plan_path, key=key, repo_path=None)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        console.print(f"[red]Metadata error:[/] {exc}")
        return EXIT_VALIDATION_ERROR

    # Step 3.5: Apply project map if provided
    if args.project_map:
        try:
            _apply_project_map(plan_path, key, args.project_map)
        except ValueError as exc:
            console.print(f"[yellow]Project map:[/] {exc}")

    # Step 4: Render intake screen
    _render_intake_screen(console, key, plan_path, work_meta)

    # Step 5: Determine action
    if args.action:
        choice = args.action
    elif effective_stdin_isatty():
        _render_action_menu(console)
        choice = _read_single_key()
    else:
        choice = "save"

    # Step 6: Dispatch
    return _dispatch_tui_action(choice, key, plan_id, plan_path, console, effective_plan_runner, args)
