#!/usr/bin/env python3
"""Whilly task orchestrator — Python rewrite of whilly.sh.

Usage:
    whilly                          Use tasks.json or interactive menu
    whilly plan1.json plan2.json    Run specific plan files
    whilly --all                    Run all discovered plans
    whilly -h, --help               Show help
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List

from whilly.agent_runner import collect_result, collect_result_from_file, is_api_error, is_auth_error, run_agent_async
from whilly.resource_monitor import get_monitor
from whilly.config import WhillyConfig, load_dotenv
from whilly.dashboard import Dashboard, NullDashboard
from whilly.external_integrations import create_integration_manager
from whilly.decomposer import needs_decompose, run_decompose
from whilly.notifications import (
    notify_all_done,
    notify_budget_exceeded,
    notify_budget_warning,
    notify_deadlock,
    notify_decompose,
    notify_plan_done,
    notify_task_done,
)
from whilly.orchestrator import plan_batches
from whilly.reporter import IterationReport, Reporter, fmt_duration, generate_summary
from whilly.state_store import StateStore
from whilly.task_manager import Task, TaskManager
from whilly.tmux_runner import TmuxAgent, kill_all_whilly_sessions, launch_agent, tmux_available

log = logging.getLogger("whilly")

# ── Exit codes ───────────────────────────────────────────────
EXIT_SUCCESS = 0
EXIT_SOME_FAILED = 1
EXIT_BUDGET_EXCEEDED = 2
EXIT_TIMEOUT = 3


def _emit_json(event: dict) -> None:
    """Write a JSON line to stdout for CI/headless consumption."""
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log_event(log_dir: Path, event: str, **kwargs) -> None:
    """Append a structured JSON event to whilly_events.jsonl."""
    from datetime import datetime, timezone

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **kwargs,
    }
    events_file = log_dir / "whilly_events.jsonl"
    with open(events_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── ANSI helpers ──────────────────────────────────────────────

R = "\033[0m"
B = "\033[1m"
D = "\033[2m"
GR = "\033[32m"
YL = "\033[33m"
CY = "\033[36m"
RD = "\033[31m"
MG = "\033[35m"
WH = "\033[97m"
BGB = "\033[44m"


def _ansi(msg: str) -> None:
    sys.stderr.write(msg + R + "\n")
    sys.stderr.flush()


def _extract_repo_args(args: List[str]) -> tuple[str | None, str | None]:
    """Extract repo owner and name from CLI args, with auto-detection fallback."""
    repo_owner = None
    repo_name = None

    if "--repo" in args:
        repo_idx = args.index("--repo")
        if repo_idx + 1 < len(args):
            repo_spec = args[repo_idx + 1]
            if "/" in repo_spec:
                repo_owner, repo_name = repo_spec.split("/", 1)

    # Auto-detect repo if not specified
    if not repo_owner or not repo_name:
        try:
            import subprocess

            result = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=True)
            remote_url = result.stdout.strip()
            # Parse git@github.com:owner/repo.git or https://github.com/owner/repo.git
            if "github.com" in remote_url:
                import re

                match = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", remote_url)
                if match:
                    repo_owner = repo_owner or match.group(1)
                    repo_name = repo_name or match.group(2)
        except Exception:
            pass

    return repo_owner, repo_name


def _handle_task_completion(task: Task, tm: TaskManager, config: WhillyConfig) -> None:
    """Обрабатывает завершение задачи - закрывает внешние Issues/Jira задачи."""
    if not config.CLOSE_EXTERNAL_TASKS:
        return

    try:
        # Создаем менеджер интеграций
        integrations_config = config.get_external_integrations_config()
        integration_manager = create_integration_manager(integrations_config)

        # Извлекаем ссылки на внешние задачи
        task_dict = task.to_dict()
        external_refs = integration_manager.extract_external_refs_from_task(task_dict)

        if not external_refs:
            log.debug("No external task references found for task %s", task.id)
            return

        # Получаем последний коммит (если есть)
        commit_sha = _get_latest_commit_sha()

        # Закрываем каждую внешнюю задачу
        for ref in external_refs:
            log.info("🔗 Closing external task: %s %s", ref.system.upper(), ref.task_id)
            success = integration_manager.close_external_task(ref, task.id, commit_sha)

            if success:
                log.info("✅ Closed %s %s successfully", ref.system.upper(), ref.task_id)
            else:
                log.warning("⚠️  Failed to close %s %s", ref.system.upper(), ref.task_id)

    except Exception as e:
        log.error("Error handling task completion for %s: %s", task.id, e)


def _get_latest_commit_sha() -> str | None:
    """Получает SHA последнего коммита."""
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def _get_version_info() -> tuple[str, str, str]:
    """Return (version, short_sha, dirty_flag). Reads git info from the whilly source repo.

    Resolved via git-dir of this file's parent — works even when whilly runs inside a
    worktree (where CWD is different from the repo with whilly).
    """
    from whilly import __version__

    repo_dir = Path(__file__).resolve().parent
    try:
        sha = (
            subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
            or "?"
        )
        dirty = subprocess.run(
            ["git", "-C", str(repo_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        return __version__, sha, " (dirty)" if dirty else ""
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return __version__, "?", ""


def _run_config_command(sub: str) -> int:
    """Handle ``whilly --config <sub>``: show / path / migrate / edit."""
    from dataclasses import fields

    from whilly.config import WhillyConfig, get_toml_section, load_layered, user_config_path
    from whilly.secrets import redact

    if sub == "path":
        print(user_config_path())
        return 0

    if sub == "migrate":
        return _run_config_migrate()

    if sub == "edit":
        return _run_config_edit()

    if sub not in ("show", None, ""):
        print(f"Unknown --config subcommand: {sub!r}", file=sys.stderr)
        print("Supported: show, path, migrate, edit", file=sys.stderr)
        return 2

    cfg = load_layered()
    resolved = cfg.resolved()

    print(f"User config path : {user_config_path()}")
    print(f"Repo TOML        : {Path.cwd() / 'whilly.toml'}")
    print(f".env             : {Path.cwd() / '.env'}")
    print()
    print("Merged WhillyConfig (secrets redacted):")
    _SECRET_FIELDS = {"JIRA_USERNAME", "JIRA_SERVER_URL"}  # non-secret strings stay visible
    for f in fields(WhillyConfig):
        value = getattr(resolved, f.name)
        # Redact anything that passed through whilly.secrets (its output is always str)
        raw_value = getattr(cfg, f.name)
        if isinstance(raw_value, str) and raw_value != value:
            # A secret scheme was resolved — redact the resolved value.
            shown = redact(value)
        elif f.name in _SECRET_FIELDS and isinstance(value, str) and value:
            shown = value  # still just a URL/username, safe to print
        else:
            shown = value
        print(f"  {f.name:<28} = {shown!r}")

    for section_name in ("github", "jira"):
        section = get_toml_section(section_name)
        if not section:
            continue
        print()
        print(f"[{section_name}]")
        for k, v in section.items():
            redacted = redact(v) if k in {"token", "api_token", "password"} else v
            print(f"  {k:<28} = {redacted!r}")
    return 0


def _run_config_migrate() -> int:
    """Convert legacy `.env` into `whilly.toml` + push secrets into keyring."""
    from whilly.config import migrate_env_to_toml

    env_path = Path.cwd() / ".env"
    toml_path = Path.cwd() / "whilly.toml"

    if not env_path.is_file():
        _ansi(f"{YL}No .env found at {env_path} — nothing to migrate.{R}")
        return 0

    if toml_path.is_file() and "--force" not in sys.argv:
        _ansi(f"{RD}{toml_path} already exists. Re-run with --force to overwrite.{R}")
        return 1

    # Dry-run first so we can confirm before touching disk.
    preview = migrate_env_to_toml(env_path, toml_path, dry_run=True)
    _ansi(f"\n{B}Migration plan:{R}")
    _ansi(f"  .env        : {env_path}")
    _ansi(f"  whilly.toml : {toml_path}")
    _ansi(f"  fields      : {len(preview['scalar_fields'])} scalar key(s)")
    _ansi(f"  sections    : {', '.join(k for k, v in preview['sections'].items() if v) or '(none)'}")
    _ansi(f"  secrets     : {len(preview['secrets_found'])} detected")
    for s in preview["secrets_found"]:
        _ansi(f"    • {s['var']} → [{s['target']}]  (stored as keyring:{s['keyring']})")

    if preview["secrets_found"]:
        _ansi(f"\n{YL}Secrets will be written to the OS keyring. You will be prompted to confirm each one.{R}")

    if sys.stdin.isatty():
        choice = input("\nProceed with migration? [y/N]: ").strip().lower()
        if choice not in ("y", "yes"):
            _ansi(f"{D}Aborted — nothing changed.{R}")
            return 0

    # Push secrets to keyring BEFORE writing TOML so we don't end up with a
    # dangling keyring reference if the user cancels mid-migration.
    for s in preview["secrets_found"]:
        value = _read_env_value(env_path, s["var"])
        if not value:
            continue
        service, _, user = s["keyring"].partition("/")
        try:
            import keyring

            keyring.set_password(service, user or "default", value)
            _ansi(f"{GR}  ✓ stored {s['var']} in keyring ({s['keyring']}){R}")
        except Exception as exc:
            _ansi(f"{RD}  ✗ keyring write failed for {s['keyring']}: {exc}{R}")
            _ansi(f"{D}    Migration aborted; .env kept intact.{R}")
            return 1

    result = migrate_env_to_toml(env_path, toml_path, dry_run=False)
    _ansi(f"\n{GR}✓ Wrote {result['toml_path']}{R}")
    if result["backup"]:
        _ansi(f"{GR}✓ Backed up original .env to {result['backup']}{R}")
    _ansi(f"\n{D}Run {B}whilly --config show{R}{D} to verify the merged config.{R}")
    return 0


def _read_env_value(env_path: Path, var: str) -> str:
    """Small re-parser of .env to pick a single value — used during migrate."""
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != var:
            continue
        value = value.strip()
        if (len(value) >= 2) and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        return value
    return ""


def _run_config_edit() -> int:
    """Open the user config file in `$EDITOR` (or a sensible OS default)."""
    import platform

    from whilly.config import user_config_path

    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        path.write_text(
            "# Whilly user config — edit freely.\n# See `whilly.example.toml` in the repo for supported keys.\n",
            encoding="utf-8",
        )
        _ansi(f"{D}Created fresh {path}{R}")

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if editor:
        cmd = [editor, str(path)]
    elif platform.system() == "Windows":
        os.startfile(str(path))  # type: ignore[attr-defined]  # Windows-only API
        return 0
    elif platform.system() == "Darwin":
        cmd = ["open", str(path)]
    else:
        cmd = ["xdg-open", str(path)]

    try:
        return subprocess.call(cmd)
    except FileNotFoundError:
        _ansi(f"{RD}Editor not found: {cmd[0]}{R}")
        _ansi(f"{D}Set $EDITOR or install a default editor, then retry.{R}")
        return 127


def _show_startup_banner() -> None:
    """Large pre-flight reminder with version/commit info. Shown for 5 seconds before work starts."""
    version, sha, dirty = _get_version_info()
    os.environ["WHILLY_GIT_SHA"] = f"{sha}{dirty}"
    version_line = f"WHILLY v{version}  @  {sha}{dirty}"
    banner = (
        "\n"
        f"{B}{YL}{'=' * 72}{R}\n"
        f"{B}{RD}{BGB}{WH}                                                                        {R}\n"
        f"{B}{RD}{BGB}{WH}          !!!  ЗАПУСТИ   zshp   ПЕРЕД РАБОТОЙ !!!                      {R}\n"
        f"{B}{RD}{BGB}{WH}                                                                        {R}\n"
        f"{B}{YL}{'=' * 72}{R}\n"
        f"{B}{CY}  {version_line}{R}\n"
        f"{D}(баннер закроется через 5 секунд){R}\n"
    )
    sys.stderr.write(banner)
    sys.stderr.flush()
    log.info("whilly start: v%s @ %s%s", version, sha, dirty)
    time.sleep(5)


# ── Schema validation ─────────────────────────────────────────


def validate_schema(path: Path) -> bool:
    """Check that a JSON file has a valid tasks array."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tasks = data.get("tasks")
        if not isinstance(tasks, list) or len(tasks) == 0:
            return False
        return all("id" in t and "status" in t for t in tasks[:3])
    except Exception:
        return False


def get_project_name(path: Path) -> str:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("project", "(unnamed)")
    except Exception:
        return "(unnamed)"


# ── Plan discovery ────────────────────────────────────────────


def discover_plans() -> list[Path]:
    """Find valid plan files in cwd and .planning/."""
    plans: list[Path] = []
    tasks_json = Path("tasks.json")
    if tasks_json.is_file() and validate_schema(tasks_json):
        plans.append(tasks_json)
    planning = Path(".planning")
    if planning.is_dir():
        for f in sorted(planning.glob("*tasks*.json")):
            if validate_schema(f):
                plans.append(f)
    return plans


def discover_prds() -> list[Path]:
    """Find PRD-like markdown files in docs/ and cwd."""
    prds: list[Path] = []
    for root in (Path("docs"), Path(".")):
        if not root.is_dir():
            continue
        for f in sorted(root.glob("PRD-*.md")):
            prds.append(f)
        for f in sorted(root.glob("prd-*.md")):
            prds.append(f)
    # Dedupe preserving order
    seen: set[Path] = set()
    result = []
    for p in prds:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            result.append(p)
    return result


def task_status_summary(plan: Path) -> str:
    """Return like '3 done, 1 failed, 21 pending'."""
    try:
        tasks = json.loads(plan.read_text(encoding="utf-8")).get("tasks", [])
    except Exception:
        return "?"
    counts: dict[str, int] = {}
    for t in tasks:
        s = t.get("status", "pending")
        counts[s] = counts.get(s, 0) + 1
    order = ["done", "in_progress", "failed", "pending", "skipped"]
    parts = [f"{counts[s]} {s}" for s in order if s in counts]
    return f"{len(tasks)} tasks: " + ", ".join(parts) if parts else f"{len(tasks)} tasks"


def prd_has_plan(prd: Path, plans: list[Path]) -> Path | None:
    """Heuristic: match PRD to a plan by common token in the filename."""
    stem = prd.stem.lower().lstrip("prd-").lstrip("_- ")
    for p in plans:
        name = p.stem.lower().replace("_tasks", "").replace("-tasks", "")
        if not name or not stem:
            continue
        if name in stem or stem in name:
            return p
    return None


def select_plan_interactive(plans: list[Path]) -> list[Path]:
    """Interactive main menu: pick existing plan, generate from PRD, or create new PRD."""
    prds = discover_prds()
    print(f"\n{B}  WHILLY — что делаем?{R}\n")

    if plans:
        print(f"{B}Существующие планы:{R}")
        for i, p in enumerate(plans):
            name = get_project_name(p)
            summary = task_status_summary(p)
            print(f"  {GR}{i + 1}){R} {name:<36} {D}[{summary}]{R}  {p}")
        print()

    unlinked_prds = [prd for prd in prds if prd_has_plan(prd, plans) is None]
    if unlinked_prds:
        print(f"{B}PRD без плана (можно декомпозировать):{R}")
        for i, prd in enumerate(unlinked_prds):
            print(f"  {CY}p{i + 1}){R} {prd.name:<50}  {D}{prd}{R}")
        print()

    print(f"{B}Действия:{R}")
    print(f"  {YL}n){R}  Новый PRD через wizard (--prd-wizard)")
    print(f"  {YL}g){R}  🐙 GitHub интеграция (issues, projects)")
    if plans:
        print(f"  {YL}a){R}  Выполнить ВСЕ планы подряд")
    print(f"  {YL}x){R}  Удалить план/PRD (формат: x1, xp1, xall, xpall)")
    print(f"  {YL}q){R}  Выход\n")

    try:
        choice = input("  Выбор: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        sys.exit(0)

    if choice in ("q", ""):
        sys.exit(0)
    if choice == "n":
        _ansi(f"{CY}Запусти: {R}whilly --prd-wizard")
        sys.exit(0)
    if choice == "g":
        from whilly.github_interactive import github_interactive_menu

        github_plan = github_interactive_menu()
        if github_plan:
            from pathlib import Path

            return [Path(github_plan)]
        return select_plan_interactive(discover_plans())
    if choice == "a":
        if not plans:
            _ansi(f"{RD}Нет планов для выполнения{R}")
            sys.exit(1)
        return plans
    if choice.startswith("x"):
        _delete_interactive(choice, plans, unlinked_prds)
        # После удаления — перезапускаем меню с обновлённым списком
        return select_plan_interactive(discover_plans())
    if choice.startswith("p") and unlinked_prds:
        try:
            idx = int(choice[1:]) - 1
            if 0 <= idx < len(unlinked_prds):
                prd = unlinked_prds[idx]
                _ansi(f"{CY}Сгенерируй план из PRD: {R}whilly --plan {prd}")
                sys.exit(0)
        except ValueError:
            pass
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(plans):
            return [plans[idx]]
    except ValueError:
        pass
    _ansi(f"{RD}Неверный выбор{R}")
    sys.exit(1)


def _delete_interactive(choice: str, plans: list[Path], prds: list[Path]) -> None:
    """Handle delete commands: x1 (plan), xp1 (PRD), xall (all plans), xpall (all PRDs).

    Asks for confirmation. Also offers to remove related whilly workspace.
    """
    rest = choice[1:].strip()
    targets: list[tuple[str, Path]] = []  # (kind, path)

    if rest in ("all",):
        for p in plans:
            targets.append(("plan", p))
    elif rest in ("pall",):
        for prd in prds:
            targets.append(("prd", prd))
    elif rest.startswith("p"):
        try:
            idx = int(rest[1:]) - 1
            if 0 <= idx < len(prds):
                targets.append(("prd", prds[idx]))
        except ValueError:
            pass
    else:
        try:
            idx = int(rest) - 1
            if 0 <= idx < len(plans):
                targets.append(("plan", plans[idx]))
        except ValueError:
            pass

    if not targets:
        _ansi(f"{RD}Не понял что удалять. Формат: x1 / xp1 / xall / xpall{R}")
        return

    print(f"\n{B}Удалить:{R}")
    for kind, p in targets:
        size = p.stat().st_size if p.exists() else 0
        print(f"  {RD}\u2717 [{kind}] {p}{R} {D}({size} bytes){R}")

    try:
        confirm = input(f"\n  Подтвердить удаление? ({YL}y/N{R}): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        confirm = ""
    if confirm != "y":
        _ansi(f"{D}Отменено{R}")
        return

    deleted = 0
    for kind, p in targets:
        try:
            p.unlink()
            deleted += 1
            _ansi(f"{GR}\u2713 удалено: {p}{R}")
            # Для plan — предложить также workspace cleanup
            if kind == "plan":
                slug_hint = p.stem.replace("_tasks", "").replace("-tasks", "")
                ws = Path(".whilly_workspaces") / slug_hint
                if ws.exists() or any(Path(".whilly_workspaces").glob(f"{slug_hint}*")):
                    _ansi(f"  {D}Workspace .whilly_workspaces/{slug_hint}* остался — удали вручную если нужно{R}")
        except Exception as e:
            _ansi(f"{RD}\u2717 не удалось удалить {p}: {e}{R}")
    _ansi(f"\n{GR}Удалено: {deleted}/{len(targets)}{R}\n")


# ── Prompt builder ────────────────────────────────────────────


def build_task_prompt(task: Task, tasks_file: str, worktree_path: Path | None = None) -> str:
    """Build prompt for agent working on a specific task."""
    prompt = (
        f"@{tasks_file} @progress.txt\n"
        f"Тебе назначена конкретная задача: **{task.id}**\n\n"
        f"1. Работай ТОЛЬКО над задачей {task.id}. НЕ трогай другие задачи.\n"
        f"2. Проверь, что типы проходят через 'make lint' и тесты через 'make test'.\n"
        f'3. Обнови статус задачи {task.id} на "done" ТОЛЬКО после успешного прохождения тестов.\n'
        f"4. Добавь свой прогресс в файл progress.txt с пометкой [{task.id}].\n"
        f"5. Сделай git commit для этой задачи с ID в сообщении.\n\n"
        f"ВАЖНО:\n"
        f"- НЕ редактируй и НЕ меняй статус других задач\n"
        f"- Если задача полностью выполнена, выведи <promise>COMPLETE</promise>\n"
        f'- Если не можешь завершить — оставь статус "in_progress" и опиши проблему'
    )
    if worktree_path:
        prompt += f"\nWORKTREE: Working in isolated worktree at {worktree_path}. Commit here, merge is automatic.\n"
    return prompt


def build_sequential_prompt(tasks_file: str) -> str:
    """Build prompt for sequential mode (agent picks highest-priority task)."""
    return (
        f"@{tasks_file} @progress.txt\n"
        f"1. Найди фичу с наивысшим приоритетом и работай ТОЛЬКО над ней.\n"
        f"Это должна быть фича, которую ТЫ считаешь наиболее приоритетной — не обязательно первая в списке.\n"
        f"2. Проверь, что типы проходят через 'make lint' и тесты через 'make test'.\n"
        f"3. Обнови TASK с информацией о выполненной работе.\n"
        f"4. Добавь свой прогресс в файл progress.txt.\n"
        f"5. Сделай git commit для этой фичи.\n"
        f"РАБОТАЙ ТОЛЬКО НАД ОДНОЙ ФИЧЕЙ.\n"
        f"Если при реализации фичи ты заметишь, что TASK полностью выполнен, выведи <promise>COMPLETE</promise>."
    )


# ── Dashboard ────────────────────────────────────────────────
# Rich Dashboard imported from whilly/dashboard.py


# ── Batch execution (tmux) ────────────────────────────────────


def launch_batch_tmux(
    batch: list[Task],
    tasks_file: str,
    config: WhillyConfig,
    log_dir: Path,
    worktree_paths: dict[str, Path] | None = None,
) -> list[TmuxAgent]:
    """Launch agents for a batch of tasks via tmux."""
    agents = []
    for task in batch:
        wt_path = worktree_paths.get(task.id) if worktree_paths else None
        prompt = build_task_prompt(task, tasks_file, worktree_path=wt_path)
        agent = launch_agent(task.id, prompt, config.MODEL, log_dir, cwd=wt_path)
        agents.append(agent)
    return agents


def wait_and_collect_tmux(
    agents: list[TmuxAgent],
    tm: TaskManager,
    dashboard: Dashboard,
    reporter: Reporter,
    iteration: int,
    config: WhillyConfig,
    max_consecutive_errors: int = 3,
    log_dir: Path | None = None,
) -> None:
    """Poll tmux agents until all done, collecting results."""
    consecutive_errors: dict[str, int] = {}

    while any(a.is_running for a in agents):
        dashboard.active_agents = [
            {
                "task_id": a.task_id,
                "start_time": a.start_time,
                "log_file": str(a.log_file),
                "status": "running" if a.is_running else "done",
            }
            for a in agents
        ]
        dashboard.update()
        time.sleep(1)

    # Collect results
    for agent in agents:
        result = collect_result_from_file(agent.log_file, agent.start_time)
        dashboard.totals.add_usage(result.usage)
        reporter.totals.add_usage(result.usage)

        ir = IterationReport(
            iteration=iteration,
            duration_s=result.duration_s,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cache_read_tokens=result.usage.cache_read_tokens,
            cache_create_tokens=result.usage.cache_create_tokens,
            cost_usd=result.usage.cost_usd,
            num_turns=result.usage.num_turns,
            tasks_before=tm.total_count,
            tasks_after=tm.total_count,
            task_completed=result.is_complete,
            agent_exit=result.exit_code,
            task_ids=[agent.task_id],
        )
        reporter.add_iteration(ir)

        if result.is_complete:
            tm.mark_status([agent.task_id], "done")
            notify_task_done()

            # Автоматическое закрытие внешних задач
            task = next((t for t in tm.tasks if t.id == agent.task_id), None)
            if task:
                _handle_task_completion(task, tm, config)

            consecutive_errors.pop(agent.task_id, None)
            if log_dir:
                _log_event(
                    log_dir,
                    "task_complete",
                    task_id=agent.task_id,
                    duration_s=result.duration_s,
                    cost_usd=result.usage.cost_usd,
                )
        elif result.exit_code != 0 or is_api_error(result):
            if is_auth_error(result):
                log.error("Auth error for %s — marking failed (no retry)", agent.task_id)
                tm.mark_status([agent.task_id], "failed")
                continue
            err_count = consecutive_errors.get(agent.task_id, 0) + 1
            consecutive_errors[agent.task_id] = err_count
            backoff = min(5 * (2 ** (err_count - 1)), 60)  # 5, 10, 20, 40, 60
            log.warning("Task %s error (attempt %d), backoff %ds", agent.task_id, err_count, backoff)
            if err_count >= max_consecutive_errors:
                log.warning("Task %s failed %d times in a row, marking as failed", agent.task_id, err_count)
                tm.mark_status([agent.task_id], "failed")
            else:
                time.sleep(backoff)
                tm.mark_status([agent.task_id], "pending")


# ── Batch execution (subprocess fallback) ─────────────────────


def launch_batch_subprocess(
    batch: list[Task],
    tasks_file: str,
    config: WhillyConfig,
    log_dir: Path,
    worktree_paths: dict[str, Path] | None = None,
) -> list[tuple[Task, subprocess.Popen, Path, float]]:
    """Launch agents via subprocess when tmux is not available."""
    log_dir.mkdir(parents=True, exist_ok=True)
    procs = []
    for task in batch:
        wt_path = worktree_paths.get(task.id) if worktree_paths else None
        prompt = build_task_prompt(task, tasks_file, worktree_path=wt_path)
        log_file = log_dir / f"{task.id}.log"
        start = time.monotonic()
        proc = run_agent_async(prompt, config.MODEL, log_file, cwd=wt_path)
        procs.append((task, proc, log_file, start))
    return procs


def wait_and_collect_subprocess(
    procs: list[tuple[Task, subprocess.Popen, Path, float]],
    tm: TaskManager,
    dashboard: Dashboard,
    reporter: Reporter,
    iteration: int,
    config,
    max_consecutive_errors: int = 3,
    log_dir: Path | None = None,
) -> None:
    """Wait for subprocess agents to finish and collect results."""
    consecutive_errors: dict[str, int] = {}
    # Poll until all done
    while any(p.poll() is None for _, p, _, _ in procs):
        dashboard.active_agents = [
            {"task_id": t.id, "log_file": str(lf), "status": "running" if p.poll() is None else "done"}
            for t, p, lf, _ in procs
        ]
        dashboard.update()
        time.sleep(1)

    for task, proc, log_file, start in procs:
        result = collect_result(proc, log_file, start)
        dashboard.totals.add_usage(result.usage)
        reporter.totals.add_usage(result.usage)

        ir = IterationReport(
            iteration=iteration,
            duration_s=result.duration_s,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cache_read_tokens=result.usage.cache_read_tokens,
            cache_create_tokens=result.usage.cache_create_tokens,
            cost_usd=result.usage.cost_usd,
            num_turns=result.usage.num_turns,
            tasks_before=tm.total_count,
            tasks_after=tm.total_count,
            task_completed=result.is_complete,
            agent_exit=result.exit_code,
            task_ids=[task.id],
        )
        reporter.add_iteration(ir)

        if result.is_complete:
            tm.mark_status([task.id], "done")
            notify_task_done()

            # Автоматическое закрытие внешних задач
            _handle_task_completion(task, tm, config)

            consecutive_errors.pop(task.id, None)
            if log_dir:
                _log_event(
                    log_dir,
                    "task_complete",
                    task_id=task.id,
                    duration_s=result.duration_s,
                    cost_usd=result.usage.cost_usd,
                )
        elif result.exit_code != 0 or is_api_error(result):
            if is_auth_error(result):
                log.error("Auth error for %s — marking failed (no retry)", task.id)
                tm.mark_status([task.id], "failed")
                continue
            err_count = consecutive_errors.get(task.id, 0) + 1
            consecutive_errors[task.id] = err_count
            backoff = min(5 * (2 ** (err_count - 1)), 60)  # 5, 10, 20, 40, 60
            log.warning("Task %s error (attempt %d), backoff %ds", task.id, err_count, backoff)
            if err_count >= max_consecutive_errors:
                log.warning("Task %s failed %d times in a row, marking as failed", task.id, err_count)
                tm.mark_status([task.id], "failed")
            else:
                time.sleep(backoff)
                tm.mark_status([task.id], "pending")


# ── Main loop ─────────────────────────────────────────────────


def run_plan(
    plan_file: str,
    config: WhillyConfig,
    agent_name: str,
    *,
    resume: bool = False,
    state_store: StateStore | None = None,
) -> Path | None:
    """Execute one plan file. Returns reporter JSON path or None."""
    import os

    from whilly.worktree_runner import (
        create_plan_workspace,
        find_existing_workspace,
        plan_slug,
    )

    # Резолвим plan_file к абсолютному пути ДО любого chdir.
    # Plan-level workspace изолирует всю работу в .whilly_workspaces/{slug},
    # но сам файл плана остаётся в основной репе (агенты читают/обновляют через abs path).
    plan_file = str(Path(plan_file).resolve())
    _original_cwd = Path.cwd()
    _workspace = None

    if config.USE_WORKSPACE:
        try:
            plan_data = json.loads(Path(plan_file).read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Не удалось прочитать план для slug: %s — workspace выключен", e)
            plan_data = {}

        slug = plan_slug(plan_data, Path(plan_file))
        existing = find_existing_workspace(slug)
        if existing is not None:
            _ansi(f"{YL}⚠️  Workspace '{slug}' уже существует: {existing}{R}")
            _ansi(f"{YL}   Другой агент может в нём работать. Переиспользую.{R}")
        try:
            _workspace = create_plan_workspace(slug, allow_reuse=True)
            _ansi(f"{CY}📁 Workspace '{slug}': {_workspace.path}{R}")
            _ansi(f"{D}   ({'reused' if _workspace.reused else 'created'}, branch: {_workspace.branch}){R}")
            os.chdir(_workspace.path)
            log.info("chdir → %s (workspace %s)", _workspace.path, slug)
        except RuntimeError as e:
            _ansi(f"{RD}Workspace creation failed: {e}{R}")
            _ansi(f"{YL}Продолжаю без workspace (--no-worktree для подавления){R}")
            _workspace = None

    tm = TaskManager(plan_file)
    log_dir = Path(config.LOG_DIR).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    # Initialize resource monitor
    resource_monitor = None
    if config.RESOURCE_CHECK_ENABLED:
        try:
            resource_monitor = get_monitor()
            # Check initial resource state
            initial_usage = resource_monitor.get_system_usage()
            violations = resource_monitor.check_limits(initial_usage)
            if violations:
                recommendation = resource_monitor.get_recommendation(violations)
                _ansi(f"{YL}⚠️  Resource warning at startup:{R}")
                _ansi(f"{YL}   {recommendation}{R}")
                if resource_monitor.should_throttle(initial_usage):
                    _ansi(f"{RD}🚨 System overloaded. Waiting for resources...{R}")
                    if not resource_monitor.wait_for_resources(max_wait_seconds=60):
                        _ansi(f"{RD}❌ Resource limits exceeded. Aborting to prevent system damage.{R}")
                        return None
        except Exception as e:
            log.warning("Failed to initialize resource monitor: %s", e)

    # Если workspace создан с нуля (удалили руками или git worktree prune убрал
    # stale регистрацию) — сбрасываем все задачи в pending, чтобы начать план
    # заново. Переиспользованный workspace сохраняет прогресс как был.
    if _workspace is not None and not _workspace.reused:
        reset_count = 0
        for t in tm.tasks:
            if t.status != "pending":
                t.status = "pending"
                reset_count += 1
        if reset_count:
            tm.save()
            log.info("Fresh workspace %s — reset %d tasks to pending", _workspace.slug, reset_count)
            _ansi(f"{YL}🔄 Свежий workspace — сбросил {reset_count} задач в pending{R}")

    # F3: Crash resume — restore state if resuming
    resumed_iteration = 0
    resumed_cost = 0.0
    if resume and state_store:
        saved = state_store.load()
        if saved and saved.get("plan_file") == plan_file:
            resumed_iteration = saved.get("iteration", 0)
            resumed_cost = saved.get("cost_usd", 0.0)
            log.info("Resuming from iteration %d, cost $%.4f", resumed_iteration, resumed_cost)

            # Discover running tmux sessions and reconcile with tasks
            live_sessions = state_store.discover_tmux_sessions()
            live_task_ids = {s["task_id"] for s in live_sessions}

            for task in tm.tasks:
                if task.status == "in_progress":
                    if task.id in live_task_ids:
                        log.info("Task %s still running in tmux, keeping in_progress", task.id)
                    else:
                        log.warning("Task %s was in_progress but tmux session gone, marking failed", task.id)
                        task.status = "failed"
            tm.save()

            # Clean up tmux sessions for tasks not in the plan
            all_task_ids = {t.id for t in tm.tasks}
            killed = state_store.cleanup_stale_sessions(all_task_ids)
            if killed:
                log.info("Cleaned up %d stale tmux sessions", killed)
        else:
            log.info("No matching saved state for %s, starting fresh", plan_file)

    if not resume:
        # 1. Reset stale tasks (only when not resuming)
        reset_count = tm.reset_stale_tasks()
        if reset_count:
            log.info("Reset %d stale in_progress tasks to pending", reset_count)

    initial_task_count = tm.total_count

    # Worktree isolation: только для параллельной работы (избегаем лишний оверхед при одном агенте)
    from whilly.worktree_runner import WorktreeManager

    use_worktree = config.WORKTREE and config.MAX_PARALLEL > 1
    wm = WorktreeManager() if use_worktree else None
    if wm:
        log.info("Worktree isolation enabled for %d parallel agents", config.MAX_PARALLEL)
    elif config.MAX_PARALLEL == 1:
        log.info("Single agent mode - worktree isolation disabled for performance")

    # 2. Setup dashboard, reporter
    project = tm.plan.project or "(unnamed)"
    _log_event(log_dir, "plan_start", plan=plan_file, project=project, tasks=tm.total_count)

    headless = config.HEADLESS
    timeout = config.TIMEOUT

    if headless:
        dashboard: Dashboard | NullDashboard = NullDashboard()
    else:
        dashboard = Dashboard(tm, agent_name, config.MAX_ITERATIONS)
    dashboard.start()

    # Register hotkey for graceful shutdown
    shutdown_flag = threading.Event()
    timeout_flag = threading.Event()

    def _on_quit():
        shutdown_flag.set()

    dashboard.keyboard.register("q", _on_quit)

    reporter = Reporter(plan_file, project, agent_name)
    plan_start = time.monotonic()
    last_progress_emit = plan_start  # for headless JSON progress

    # 2.5 Initial decompose check
    if needs_decompose(tm):
        dashboard.status_msg = "[bold magenta]Анализ задач — декомпозиция...[/]"
        dashboard.update()
        try:
            delta = run_decompose(tm, config.MODEL, config.USE_TMUX, log_dir)
            if delta > 0:
                dashboard.status_msg = f"[bold magenta]Декомпозиция: +{delta} задач[/]"
                dashboard.update()
                notify_decompose(delta)
                initial_task_count = tm.total_count  # refresh count
        except NotImplementedError:
            pass  # decompose execution not ready yet
        except Exception:
            log.exception("Decompose failed")

    # 3. Main work loop
    iteration = resumed_iteration
    consecutive_no_ready = 0
    global_consecutive_failures = 0
    prev_done = tm.done_count
    session_cost_usd: float = resumed_cost
    budget_exceeded = False
    budget_warning_sent = False
    task_attempt_count: dict[str, int] = {}  # task_id -> consecutive iterations in_progress
    task_prev_status: dict[str, str] = {}  # task_id -> status in previous iteration
    failed_retry_count: dict[str, int] = {}  # task_id -> auto-retry count for failed tasks
    idle_ticks = 0  # consecutive iterations where no tasks were ready (blocked by deps)

    try:
        while True:
            # F4: Timeout check
            elapsed_sec = time.monotonic() - plan_start
            if timeout > 0 and elapsed_sec >= timeout:
                timeout_flag.set()
                log.warning("Timeout reached (%.0fs >= %ds) — stopping", elapsed_sec, timeout)
                kill_all_whilly_sessions()
                break

            if shutdown_flag.is_set():
                dashboard.status_msg = "[bold yellow]Shutdown requested (q)...[/]"
                dashboard.update()
                log.info("User pressed q — shutting down")
                kill_all_whilly_sessions()
                break

            tm.reload()

            # Auto-retry failed tasks: reset failed → pending if retry count < MAX_TASK_RETRIES.
            # Useful if user fixed the underlying issue (auth, proxy, env) mid-run — no need
            # to manually --reset, loop picks them up automatically.
            failed_tasks = [t for t in tm.tasks if t.status == "failed"]
            for t in failed_tasks:
                count = failed_retry_count.get(t.id, 0)
                if count < config.MAX_TASK_RETRIES:
                    failed_retry_count[t.id] = count + 1
                    t.status = "pending"
                    log.info(
                        "Auto-retry failed task %s (attempt %d/%d) → pending",
                        t.id,
                        count + 1,
                        config.MAX_TASK_RETRIES,
                    )
            if failed_tasks:
                tm.save()

            if not tm.has_pending():
                dashboard.status_msg = "[bold yellow]Ожидание задач...[/] [dim](pending=0, 'n'=new idea, 'q'=quit)[/]"
                dashboard.update()
                time.sleep(5)

                # Reload — maybe wizard/user added tasks while we slept
                tm.reload()
                if tm.has_pending():
                    consecutive_no_ready = 0
                    continue

                consecutive_no_ready += 1

                # Headless/CI: exit after 30s idle (no TUI to interact with)
                if config.HEADLESS and consecutive_no_ready > 6:
                    log.info("Headless mode: no pending tasks for 30s, finishing plan")
                    break

                # TUI mode: wait indefinitely ('n'=new idea, 'q'=quit)
                continue

            consecutive_no_ready = 0

            if 0 < config.MAX_ITERATIONS <= iteration:
                dashboard.status_msg = f"[bold red]MAX ITERATIONS ({config.MAX_ITERATIONS}) -- stopped[/]"
                dashboard.update()
                log.info("Stopped: max iterations (%d)", config.MAX_ITERATIONS)
                break

            ready = tm.get_ready_tasks()
            if not ready:
                # No work dispatchable — don't bump iteration (it reflects WORK cycles).
                idle_ticks += 1
                pending = [t for t in tm.tasks if t.status == "pending"]
                in_progress = [t for t in tm.tasks if t.status == "in_progress"]
                dashboard.status_msg = (
                    f"[yellow]All pending tasks are blocked by dependencies[/] "
                    f"[dim](pending={len(pending)} in_progress={len(in_progress)} "
                    f"idle_ticks={idle_ticks})[/]"
                )
                dashboard.update()
                # Log heartbeat every 10 idle ticks so whilly.log doesn't go silent
                if idle_ticks % 10 == 0:
                    blocked_ids = [t.id for t in pending[:5]]
                    in_prog_ids = [t.id for t in in_progress[:5]]
                    log.info(
                        "Idle tick %d: no ready tasks — pending=%d (blocked by deps: %s), in_progress=%d (%s)",
                        idle_ticks,
                        len(pending),
                        ", ".join(blocked_ids) or "-",
                        len(in_progress),
                        ", ".join(in_prog_ids) or "-",
                    )
                time.sleep(5)
                continue

            # We have ready work — reset idle counter and bump iteration.
            idle_ticks = 0
            iteration += 1
            dashboard.iteration = iteration
            dashboard.phase = "work"

            # Parallel mode
            if config.MAX_PARALLEL > 1:
                if config.ORCHESTRATOR == "llm":
                    from whilly.orchestrator import plan_batches_llm

                    batches = plan_batches_llm(ready, config.MAX_PARALLEL, plan_file, config.MODEL)
                else:
                    batches = plan_batches(ready, config.MAX_PARALLEL)
                batch = batches[0]  # Execute first batch, then re-evaluate
                task_ids = [t.id for t in batch]

                dashboard.status_msg = (
                    f"[bold cyan]Batch: {', '.join(task_ids)}[/]  [dim]({len(batches)} batches queued)[/]"
                )
                dashboard.update()
                log.info("Iter %d: batch [%s] (%d batches total)", iteration, ", ".join(task_ids), len(batches))

                # Resource check before launching batch
                if resource_monitor:
                    current_usage = resource_monitor.get_system_usage()
                    if resource_monitor.should_throttle(current_usage):
                        violations = resource_monitor.check_limits(current_usage)
                        recommendation = resource_monitor.get_recommendation(violations)
                        _ansi(f"{YL}⚠️  System overloaded. Waiting before launching batch...{R}")
                        dashboard.status_msg = "[yellow]⏱️  Waiting for system resources...[/]"
                        dashboard.update()

                        if not resource_monitor.wait_for_resources(max_wait_seconds=300):
                            _ansi(f"{RD}❌ Resource limits exceeded. Skipping batch to prevent system damage.{R}")
                            _ansi(f"{RD}   {recommendation}{R}")
                            time.sleep(10)  # Brief pause before next iteration
                            continue

                tm.mark_status(task_ids, "in_progress")
                _log_event(log_dir, "batch_start", iteration=iteration, tasks=task_ids)

                # Interface agreement for shared modules
                if config.ORCHESTRATOR == "llm" and len(batch) > 1:
                    from whilly.orchestrator import detect_module_overlap, run_interface_agreement

                    overlaps = detect_module_overlap(batch)
                    for module, overlap_tids in overlaps.items():
                        if len(overlap_tids) > 1:
                            run_interface_agreement(module, overlap_tids, plan_file, config.MODEL, log_dir)

                # Create worktrees for each task in the batch
                worktree_paths: dict[str, Path] | None = None
                if wm:
                    worktree_paths = {}
                    for task in batch:
                        try:
                            wt = wm.create(task.id)
                            worktree_paths[task.id] = wt.path
                            log.info("Created worktree for %s at %s", task.id, wt.path)
                        except Exception:
                            log.exception("Failed to create worktree for %s, running without isolation", task.id)

                if config.USE_TMUX and tmux_available():
                    agents = launch_batch_tmux(batch, plan_file, config, log_dir, worktree_paths=worktree_paths)
                    wait_and_collect_tmux(agents, tm, dashboard, reporter, iteration, config, log_dir=log_dir)
                else:
                    procs = launch_batch_subprocess(batch, plan_file, config, log_dir, worktree_paths=worktree_paths)
                    wait_and_collect_subprocess(procs, tm, dashboard, reporter, iteration, config, log_dir=log_dir)

                # Merge worktrees for completed tasks, cleanup all
                if wm and worktree_paths:
                    tm.reload()
                    for task in batch:
                        if task.id in worktree_paths:
                            task_obj = next((t for t in tm.tasks if t.id == task.id), None)
                            if task_obj and task_obj.status == "done":
                                merge_result = wm.merge_back(task.id)
                                if merge_result.success:
                                    log.info(
                                        "Merged %d commits from worktree %s",
                                        merge_result.commits_merged,
                                        task.id,
                                    )
                                else:
                                    log.warning("Merge failed for %s: %s", task.id, merge_result.error)
                            wm.cleanup(task.id)

            else:
                # Sequential mode — agent picks its own task
                dashboard.status_msg = "[bold cyan]Запуск агента...[/]"
                dashboard.update()
                log.info("Iter %d start (sequential)", iteration)

                # Resource check before launching sequential agent
                if resource_monitor:
                    current_usage = resource_monitor.get_system_usage()
                    if resource_monitor.should_throttle(current_usage):
                        violations = resource_monitor.check_limits(current_usage)
                        recommendation = resource_monitor.get_recommendation(violations)
                        _ansi(f"{YL}⚠️  System overloaded. Waiting before launching agent...{R}")
                        dashboard.status_msg = "[yellow]⏱️  Waiting for system resources...[/]"
                        dashboard.update()

                        if not resource_monitor.wait_for_resources(max_wait_seconds=300):
                            _ansi(f"{RD}❌ Resource limits exceeded. Skipping iteration to prevent system damage.{R}")
                            _ansi(f"{RD}   {recommendation}{R}")
                            time.sleep(10)  # Brief pause before next iteration
                            continue

                prompt = build_sequential_prompt(plan_file)

                if config.USE_TMUX and tmux_available():
                    agent = launch_agent("seq", prompt, config.MODEL, log_dir)
                    while agent.is_running:
                        dashboard.status_msg = (
                            f"[cyan]Agent running...[/] [dim]({fmt_duration(time.monotonic() - agent.start_time)})[/]"
                        )
                        dashboard.update()
                        time.sleep(1)
                    result = collect_result_from_file(agent.log_file, agent.start_time)
                else:
                    log_file = log_dir / f"seq_iter{iteration}.log"
                    start = time.monotonic()
                    proc = run_agent_async(prompt, config.MODEL, log_file)
                    while proc.poll() is None:
                        dashboard.status_msg = (
                            f"[cyan]Agent running...[/] [dim]({fmt_duration(time.monotonic() - start)})[/]"
                        )
                        dashboard.update()
                        time.sleep(1)
                    result = collect_result(proc, log_file, start)

                dashboard.totals.add_usage(result.usage)
                reporter.totals.add_usage(result.usage)

                ir = IterationReport(
                    iteration=iteration,
                    duration_s=result.duration_s,
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                    cache_read_tokens=result.usage.cache_read_tokens,
                    cache_create_tokens=result.usage.cache_create_tokens,
                    cost_usd=result.usage.cost_usd,
                    num_turns=result.usage.num_turns,
                    tasks_before=tm.total_count,
                    tasks_after=tm.total_count,
                    task_completed=result.is_complete,
                    agent_exit=result.exit_code,
                )
                reporter.add_iteration(ir)

            dashboard.status_msg = f"[bold green]Iteration {iteration} done[/] [dim]-- re-evaluating...[/]"
            dashboard.update()

            # F4: Headless JSON progress output (every 10 seconds)
            if headless:
                now = time.monotonic()
                if now - last_progress_emit >= 10:
                    tm.reload()
                    _emit_json(
                        {
                            "event": "progress",
                            "done": tm.done_count,
                            "total": tm.total_count,
                            "failed": sum(1 for t in tm.tasks if t.status == "failed"),
                            "cost_usd": round(reporter.totals.cost_usd, 4),
                            "elapsed_sec": round(now - plan_start, 1),
                        }
                    )
                    last_progress_emit = now

            # ── F1: Cost Budget Guard ────────────────────────────────
            session_cost_usd = reporter.totals.cost_usd
            dashboard.budget_usd = config.BUDGET_USD
            dashboard.session_cost_usd = session_cost_usd

            if config.BUDGET_USD > 0:
                budget_pct = (session_cost_usd / config.BUDGET_USD) * 100
                if budget_pct >= 100:
                    dashboard.status_msg = (
                        f"[bold red]BUDGET EXCEEDED: ${session_cost_usd:.2f} / ${config.BUDGET_USD:.2f}[/]"
                    )
                    dashboard.update()
                    log.warning("Budget exceeded: $%.2f / $%.2f — stopping", session_cost_usd, config.BUDGET_USD)
                    notify_budget_exceeded()
                    kill_all_whilly_sessions()
                    budget_exceeded = True
                    _log_event(log_dir, "budget_exceeded", cost=session_cost_usd, budget=config.BUDGET_USD)
                    break
                elif budget_pct >= 80 and not budget_warning_sent:
                    log.warning("Budget at %.0f%%: $%.2f / $%.2f", budget_pct, session_cost_usd, config.BUDGET_USD)
                    notify_budget_warning(int(budget_pct))
                    budget_warning_sent = True

            # ── F2: Deadlock Detection ───────────────────────────────
            tm.reload()
            for t in tm.tasks:
                if t.status == "in_progress":
                    prev = task_prev_status.get(t.id)
                    if prev == "in_progress":
                        task_attempt_count[t.id] = task_attempt_count.get(t.id, 1) + 1
                    else:
                        task_attempt_count[t.id] = 1
                else:
                    # Reset counter when task is no longer in_progress
                    task_attempt_count.pop(t.id, None)
                task_prev_status[t.id] = t.status

            # Check for deadlocked or over-retried tasks
            for task_id, attempts in list(task_attempt_count.items()):
                if attempts >= config.MAX_TASK_RETRIES:
                    log.warning("Task %s exceeded MAX_TASK_RETRIES (%d), skipping", task_id, config.MAX_TASK_RETRIES)
                    tm.mark_status([task_id], "skipped")
                    notify_deadlock(task_id)
                    task_attempt_count.pop(task_id, None)
                    _log_event(log_dir, "task_skipped", task_id=task_id, reason="max_retries", attempts=attempts)
                elif attempts >= 3:
                    log.warning("Task %s stuck in_progress for %d iterations (possible deadlock)", task_id, attempts)
                    tm.mark_status([task_id], "skipped")
                    notify_deadlock(task_id)
                    task_attempt_count.pop(task_id, None)
                    _log_event(log_dir, "task_skipped", task_id=task_id, reason="deadlock", attempts=attempts)

            # Periodic decompose check
            if config.DECOMPOSE_EVERY > 0 and iteration % config.DECOMPOSE_EVERY == 0:
                if needs_decompose(tm):
                    dashboard.status_msg = "[bold magenta]Periodic decompose...[/]"
                    dashboard.update()
                    try:
                        delta = run_decompose(tm, config.MODEL, config.USE_TMUX, log_dir)
                        if delta > 0:
                            notify_decompose(delta)
                    except (NotImplementedError, Exception):
                        pass

            # R2-015: Global error rate limit — pause when 5+ consecutive failures
            tm.reload()
            current_done = tm.done_count
            if current_done > prev_done:
                global_consecutive_failures = 0
                prev_done = current_done
            else:
                global_consecutive_failures += 1
                if global_consecutive_failures >= 5:
                    dashboard.status_msg = "[bold red]Too many errors, pausing 60s...[/]"
                    dashboard.update()
                    log.warning("5+ consecutive iterations without progress, pausing 60s")
                    time.sleep(60)
                    global_consecutive_failures = 0

            # F3: Save state for crash recovery
            if state_store:
                task_status = {t.id: t.status for t in tm.tasks}
                active = dashboard.active_agents if hasattr(dashboard, "active_agents") else []
                state_store.save(
                    plan_file=plan_file,
                    iteration=iteration,
                    cost_usd=reporter.totals.cost_usd,
                    active_agents=active,
                    task_status=task_status,
                )

            time.sleep(1)

    finally:
        dashboard.stop()
        if wm:
            cleaned = wm.cleanup_all()
            if cleaned:
                log.info("Cleaned up %d worktrees", cleaned)

    # F3: Clear state on clean exit
    if state_store:
        state_store.clear()

    notify_plan_done()

    # Finalize
    total_dur = time.monotonic() - plan_start
    reporter.finalize(
        total_iterations=iteration,
        duration_s=total_dur,
        initial_tasks=initial_task_count,
        final_tasks=tm.total_count,
        done_tasks=tm.done_count,
    )

    _log_event(
        log_dir,
        "plan_done",
        iterations=iteration,
        done=tm.done_count,
        total=tm.total_count,
        cost_usd=reporter.totals.cost_usd,
    )

    log.info(
        "Plan %s finished: %d/%d done in %s, $%.4f",
        plan_file,
        tm.done_count,
        tm.total_count,
        fmt_duration(total_dur),
        reporter.totals.cost_usd,
    )

    # F4: Determine exit code
    failed_count = sum(1 for t in tm.tasks if t.status == "failed")
    if timeout_flag.is_set():
        exit_code = EXIT_TIMEOUT
    elif budget_exceeded:
        exit_code = EXIT_BUDGET_EXCEEDED
    elif failed_count > 0:
        exit_code = EXIT_SOME_FAILED
    else:
        exit_code = EXIT_SUCCESS

    # F4: Emit final JSON in headless mode
    if headless:
        _emit_json(
            {
                "event": "complete",
                "done": tm.done_count,
                "total": tm.total_count,
                "failed": failed_count,
                "cost_usd": round(reporter.totals.cost_usd, 4),
                "elapsed_sec": round(total_dur, 1),
                "report": str(reporter.json_path),
                "exit_code": exit_code,
            }
        )

    # Возвращаем cwd чтобы dashboard/следующие планы работали из original root
    if _workspace is not None:
        os.chdir(_original_cwd)
        _ansi(f"{D}📁 Workspace '{_workspace.slug}' остался на ветке {_workspace.branch}")
        _ansi(f"{D}   Посмотреть изменения: git -C {_workspace.path} log --oneline{R}")

        # Опционально предложить merge workspace в master
        if not config.HEADLESS and tm.done_count == tm.total_count and tm.total_count > 0:
            _maybe_merge_workspace(_workspace, tm, config)

    return reporter.json_path, exit_code


def _maybe_merge_workspace(workspace, tm, config) -> None:
    """После полного завершения плана — предложить merge workspace в master.

    Режимы (env WHILLY_AUTO_MERGE):
      - 'ask' (default) — интерактивный prompt y/c/n/s
      - 'yes' — автоматический push + MR + merge (с pipeline check)
      - 'claude' — запуск claude CLI с merge-промптом
      - 'no' — skip
    """
    mode = (os.environ.get("WHILLY_AUTO_MERGE") or "ask").lower()
    commits = subprocess.run(
        ["git", "-C", str(workspace.path), "log", "--oneline", f"origin/master..{workspace.branch}"],
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    if not commits:
        _ansi(f"{D}Workspace {workspace.slug}: нет новых коммитов для merge{R}")
        return

    commit_count = len(commits.splitlines())
    _ansi(f"\n{B}{GR}✅ План завершён — {tm.done_count}/{tm.total_count} tasks done{R}")
    _ansi(f"{B}📦 Workspace '{workspace.slug}': {commit_count} commit(s) для merge в master{R}")
    _ansi(f"{D}   Ветка: {workspace.branch}{R}\n")
    _ansi(f"{D}{commits[:1500]}{R}")
    if len(commits) > 1500:
        _ansi(f"{D}... ещё {len(commits.splitlines()) - commits[:1500].count(chr(10))} commits{R}")
    print()

    if mode == "no":
        _ansi(f"{D}WHILLY_AUTO_MERGE=no — merge пропущен{R}")
        return
    if mode == "yes":
        _run_automated_merge(workspace, tm)
        return
    if mode == "claude":
        _run_claude_merge(workspace, tm)
        return

    # ask mode
    _ansi(f"{B}Что делаем с workspace?{R}")
    _ansi(f"  {GR}y){R}  Автомерж: push → MR → wait pipeline → merge")
    _ansi(f"  {CY}c){R}  Запустить Claude CLI для review + merge (рекомендуется)")
    _ansi(f"  {YL}s){R}  Показать full diff stat и выйти")
    _ansi(f"  {D}n){R}  Skip (оставить ветку как есть)")
    try:
        choice = input(f"\n  Выбор ({GR}y{R}/{CY}c{R}/{YL}s{R}/{D}n{R}): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        choice = "n"
    if choice in ("y", "yes"):
        _run_automated_merge(workspace, tm)
    elif choice in ("c", "claude"):
        _run_claude_merge(workspace, tm)
    elif choice in ("s", "show"):
        subprocess.run(
            ["git", "-C", str(workspace.path), "diff", "--stat", "origin/master"],
        )
    else:
        _ansi(f"{D}Skip — workspace остался на ветке {workspace.branch}{R}")


def _run_automated_merge(workspace, tm) -> None:
    """Push branch → create MR → wait pipeline → merge via check_mr_pipeline.py."""
    _ansi(f"\n{CY}🚀 Push {workspace.branch}...{R}")
    push = subprocess.run(
        ["git", "-C", str(workspace.path), "push", "-u", "origin", workspace.branch],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if push.returncode != 0:
        _ansi(f"{RD}Push failed: {push.stderr.strip()}{R}")
        return
    _ansi(f"{GR}✓ Branch pushed{R}")
    _ansi(f"\n{CY}📝 Создать MR → merge нужно вручную через gitlab UI или gh CLI{R}")
    _ansi(f"{D}Autoбранч: {workspace.branch} → master{R}")
    _ansi(f"{D}Подсказка: запустите выбранный скрипт/CI для ожидания pipeline и merge{R}")
    # Не создаём MR автоматически — это требует gitlab MCP/API и зависит от contexta.
    # Оставляем это для Claude-assisted варианта.


def _run_claude_merge(workspace, tm) -> None:
    """Запустить claude CLI с промптом для review + merge workspace в master."""
    claude = os.environ.get("CLAUDE_BIN") or "claude"
    mr_target = os.environ.get("WHILLY_MR_TARGET", "upstream main branch")
    prompt = (
        f"Workspace '{workspace.slug}' готов к merge в master.\n"
        f"Ветка: {workspace.branch}\n"
        f"Путь: {workspace.path}\n"
        f"Все {tm.total_count} задач выполнены.\n\n"
        f"Твоя задача:\n"
        f"1. Зайди в {workspace.path}\n"
        f"2. Просмотри коммиты: git log --oneline origin/master..HEAD\n"
        f"3. Запусти тесты/линтеры проекта — убедись что всё зелёное. Если нет — фикси.\n"
        f"4. Push ветку: git push -u origin {workspace.branch}\n"
        f"5. Создай PR/MR в {mr_target} через gh CLI или gitlab MCP tool\n"
        f"6. Дождись зелёного pipeline\n"
        f"7. Merge PR/MR\n"
        f"8. Отчитайся человеку что произошло\n"
    )
    _ansi(f"\n{CY}🤖 Запускаю Claude CLI для review + merge...{R}")
    _ansi(f"{D}После завершения Claude вернёт управление обратно.{R}\n")
    # Интерактивный запуск — без -p, чтобы Claude Code открылся с TUI
    try:
        subprocess.run(
            [claude, "--permission-mode", "acceptEdits", "--model", "claude-opus-4-6[1m]"],
            input=prompt + "\n",
            text=True,
        )
    except FileNotFoundError:
        _ansi(f"{RD}claude CLI не найден. Установи CLAUDE_BIN или запусти claudeproxy{R}")
    except KeyboardInterrupt:
        _ansi(f"\n{YL}Прервано пользователем{R}")


# ── Argument parsing & main ───────────────────────────────────


HELP_TEXT = """\
Usage: whilly [OPTIONS] [PLAN_FILE...]

  whilly                          Use tasks.json or interactive menu
  whilly plan1.json plan2.json    Run specific plan files
  whilly --all                    Run all discovered plans
  whilly --headless               CI mode: no TUI, JSON stdout, exit codes
  whilly --timeout 3600           Max wall time in seconds (0=unlimited)
  whilly --resume                 Resume from saved state after crash
  whilly --reset PLAN_FILE        Reset all tasks to pending
  whilly --init "description"     Generate PRD from project description
  whilly --plan PRD.md            Generate tasks.json from PRD file
  whilly --init "desc" --plan     Generate PRD + tasks.json in one step
  whilly --init "desc" --plan --go  PRD + tasks + auto-execute
  whilly --prd-wizard [slug]      Interactive PRD wizard — launches claude CLI
                                    preloaded with PRD master prompt. Диалог
                                    прямо в текущем терминале (без tmux).
  whilly --from-github [labels]   Generate tasks from GitHub Issues with
                                    specified labels (default: workshop,whilly:ready)
  whilly --from-project <url>     🆕 Convert GitHub Project board to Issues and tasks
                                    Usage: --from-project URL [--repo owner/name] [--go]
  whilly --sync-todo <url>        🆕 Sync only Todo items from Project to Issues/tasks
                                    Usage: --sync-todo URL [--repo owner/name]
  whilly --watch-project <url>    🆕 Monitor Project for Todo items and sync continuously
                                    Usage: --watch-project URL [--repo owner/name]
  whilly --sync-status <issue> <status>  🆕 Update Project item status from Issue
                                    Usage: --sync-status 123 "In Progress"
  whilly --project-sync-status    🆕 Show current project sync status
  whilly --no-worktree            Отключить изоляцию плана в отдельном git
                                    worktree (по умолчанию план исполняется в
                                    .whilly_workspaces/{slug}/ чтобы не мешать
                                    параллельным агентам в основной репе).
  whilly --agent {claude,opencode}  Select agent backend (default: claude;
                                    overrides WHILLY_AGENT_BACKEND for this run)
  whilly --workflow-analyze URL   Introspect a GitHub Project board, detect
                                    mapping gaps, and write .whilly/workflow.json.
                                    Extra flags: --apply (auto-add missing
                                    columns), --report (dry-run).
  whilly --classify "TITLE | BODY" --project URL --repo OWNER/REPO [--apply]
                                  Smart routing — classify input as
                                    Epic/Story/Task, find best parent, print
                                    decision. With --apply: create the item at
                                    the right level under the matched parent
                                    (only when classifier confidence ≥ 0.75).
  whilly --rebuild-hierarchy --project URL --repo OWNER/REPO [--label L]
                             [--infer-epics] [--apply]
                                  Classify every item in the project + repo,
                                    match parents bottom-up (Task → Story →
                                    Epic), print proposed tree + unparented
                                    items. --infer-epics: synthesise Epics
                                    from orphan Stories. --apply: link
                                    assignments + materialise inferred Epics
                                    via the tracker adapter.
  whilly -h, --help               Show this help

Exit codes (headless mode):
  0  All tasks done successfully
  1  Some tasks failed
  2  Budget exceeded
  3  Timeout reached

Environment variables:
  WHILLY_MAX_ITERATIONS=N   Max work iterations per plan (0=unlimited)
  WHILLY_MAX_PARALLEL=N     Max concurrent agents (1=sequential, 2-3=parallel)
  WHILLY_MAX_CPU_PERCENT=N  Max total CPU usage before throttling (default: 80)
  WHILLY_MAX_MEMORY_PERCENT=N Max memory usage before throttling (default: 75)
  WHILLY_MIN_FREE_SPACE_GB=N Min free disk space required (default: 5)
  WHILLY_PROCESS_TIMEOUT_MINUTES=N Max process runtime (default: 30)
  WHILLY_RESOURCE_CHECK_ENABLED=0 Disable resource monitoring
  WHILLY_USE_TMUX=1/0       Use tmux for parallel execution (default: 1)
  WHILLY_WORKTREE=1/0       Git worktree isolation (only when MAX_PARALLEL > 1)
  WHILLY_MODEL=MODEL        Model to use (default: claude-opus-4-6[1m])
  WHILLY_LOG_DIR=DIR        Directory for agent logs (default: whilly_logs)
  WHILLY_AGENT=NAME         Agent name for reports
  WHILLY_STATE_FILE=FILE    State file for crash recovery (default: .whilly_state.json)
  WHILLY_BUDGET_USD=N       Cost budget in USD (0=unlimited, default=0)
  WHILLY_MAX_TASK_RETRIES=N Max retries before skipping a task (default=5)
  WHILLY_HEADLESS=1/0       Headless/CI mode (default: auto-detect from TTY)
  WHILLY_TIMEOUT=N          Max wall time in seconds (0=unlimited)
"""


def resolve_agent_name(config: WhillyConfig) -> str:
    """Determine agent display name."""
    if config.AGENT:
        return config.AGENT
    if "opus-4-7" in config.MODEL:
        return "claude-1m-4.7"
    if "1m" in config.MODEL:
        return "claude-1m"
    return "claude"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    # Load .env from cwd (if present) before anything reads WHILLY_* vars.
    # Real shell env still wins — .env is a fallback for local convenience.
    load_dotenv(Path.cwd() / ".env")

    _log_path = Path.cwd() / "whilly.log"
    os.environ["WHILLY_LOG_PATH"] = str(_log_path)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            RotatingFileHandler(str(_log_path), maxBytes=10_000_000, backupCount=5, encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )

    if "--help" in args or "-h" in args:
        print(HELP_TEXT)
        return 0

    # --config show  — read-only diagnostic: merged layered config + resolved paths
    if "--config" in args:
        idx = args.index("--config")
        sub = args[idx + 1] if idx + 1 < len(args) else "show"
        return _run_config_command(sub)

    _show_startup_banner()

    # --rebuild-hierarchy: classify flat list + match parents → proposed tree
    if "--rebuild-hierarchy" in args:
        from whilly.classifier import apply_tree, format_tree, rebuild_hierarchy
        from whilly.hierarchy import HierarchyLevel, get_adapter

        def _flag_value_rb(name):
            if name not in args:
                return None
            i = args.index(name)
            return args[i + 1] if i + 1 < len(args) else None

        project_url = _flag_value_rb("--project")
        repo = _flag_value_rb("--repo")
        if not project_url or not repo:
            _ansi(f"{RD}--rebuild-hierarchy requires --project <URL> and --repo <OWNER/REPO>{R}")
            return 1
        label = _flag_value_rb("--label")  # optional filter, e.g. whilly:ready
        apply_flag = "--apply" in args
        infer_epics_flag = "--infer-epics" in args

        try:
            adapter = get_adapter("github", project_url=project_url, repo=repo)
        except Exception as exc:  # noqa: BLE001
            _ansi(f"{RD}failed to build adapter: {exc}{R}")
            return 1

        # Gather the flat list — stories via repo issues + drafts as epics.
        _ansi(f"{CY}Collecting items from project + repo...{R}")
        try:
            epic_items = adapter.list_at_level(HierarchyLevel.EPIC)
            story_items = adapter.list_at_level(HierarchyLevel.STORY, label=label)
        except Exception as exc:  # noqa: BLE001
            _ansi(f"{RD}collection failed: {exc}{R}")
            return 1
        flat = epic_items + story_items
        if not flat:
            _ansi(f"{YL}Nothing to rebuild — project has no items.{R}")
            return 0

        _ansi(f"{CY}Classifying {len(flat)} item(s) + matching parents (LLM, may take a minute)...{R}")
        tree = rebuild_hierarchy(flat, infer_missing_epics=infer_epics_flag)
        print(format_tree(tree))

        if apply_flag:
            applied = apply_tree(tree, adapter)
            _ansi(f"{GR}Applied {applied} of {tree.counts['assignments']} assignments.{R}")
        else:
            _ansi(
                f"{YL}Dry-run. Re-run with --apply to execute "
                f"{tree.counts['assignments']} assignments on the tracker.{R}"
            )
        return 0

    # --classify: smart routing — classify input + find best parent
    if "--classify" in args:
        from whilly.classifier import Router, format_decision
        from whilly.hierarchy import get_adapter

        idx = args.index("--classify")
        if idx + 1 >= len(args):
            _ansi(f"{RD}Usage: whilly --classify 'TITLE | BODY' --project URL --repo OWNER/REPO [--apply]{R}")
            return 1
        raw = args[idx + 1]
        # Accept "title | body" shape; body optional.
        if "|" in raw:
            title, body = raw.split("|", 1)
            title, body = title.strip(), body.strip()
        else:
            title, body = raw.strip(), ""

        def _flag_value(name):
            if name not in args:
                return None
            i = args.index(name)
            return args[i + 1] if i + 1 < len(args) else None

        project_url = _flag_value("--project")
        repo = _flag_value("--repo")
        if not project_url or not repo:
            _ansi(f"{RD}--classify requires --project <URL> and --repo <OWNER/REPO>{R}")
            return 1
        apply_flag = "--apply" in args

        try:
            adapter = get_adapter("github", project_url=project_url, repo=repo)
        except Exception as exc:  # noqa: BLE001
            _ansi(f"{RD}failed to build adapter: {exc}{R}")
            return 1

        router = Router(parent_search_label="whilly:ready")
        decision = router.route_text(title, body, adapter)
        print(format_decision(decision))
        if apply_flag and decision.classification.is_high_confidence:
            router.apply(decision, adapter)
            _ansi(f"{GR}Applied.{R}")
        elif apply_flag:
            _ansi(
                f"{YL}Not applied — confidence {decision.classification.confidence:.2f} "
                f"below 0.75 threshold. Re-run with higher-quality input or review manually.{R}"
            )
        return 0

    # --workflow-analyze: introspect a GitHub Project board + propose mapping
    if "--workflow-analyze" in args:
        from whilly.workflow import get_board
        from whilly.workflow.analyzer import analyze, format_report, load_mapping, save_mapping
        from whilly.workflow.proposer import propose

        idx = args.index("--workflow-analyze")
        if idx + 1 >= len(args):
            _ansi(f"{RD}Usage: whilly --workflow-analyze <project_url> [--apply|--report]{R}")
            return 1
        url = args[idx + 1]
        mode = "apply" if "--apply" in args else ("report" if "--report" in args else "auto")

        try:
            board = get_board("github_project", url=url)
        except ValueError as exc:
            _ansi(f"{RD}{exc}{R}")
            return 1

        existing = load_mapping()
        try:
            report = analyze(board, mapping=existing)
        except RuntimeError as exc:
            _ansi(f"{RD}workflow analysis failed: {exc}{R}")
            return 1

        print(format_report(report, title=f"{CY}{B}Workflow analysis{R}"))
        if report.is_clean and not existing:
            # Still persist a fresh mapping so subsequent runs skip analysis.
            _, mapping = propose(report, board, existing=existing, mode="report")
            path = save_mapping(mapping)
            _ansi(f"{GR}Mapping saved: {path}{R}")
            return 0
        if report.is_clean:
            return 0

        proposal, mapping = propose(report, board, existing=existing, mode=mode)
        if proposal.cancelled:
            _ansi(f"{YL}Cancelled. Mapping file untouched.{R}")
            return 1
        path = save_mapping(mapping)
        summary = f"  +{len(proposal.to_add)} added · {len(proposal.to_map)} mapped · {len(proposal.to_skip)} skipped"
        _ansi(f"{GR}Mapping saved: {path}{R}\n{summary}")
        return 0

    # --reset: сбросить все задачи в pending
    if "--reset" in args:
        rest = [a for a in args if a != "--reset"]
        if not rest:
            _ansi(f"{RD}Usage: whilly --reset PLAN_FILE{R}")
            return 1
        for f in rest:
            p = Path(f)
            if not p.is_file():
                _ansi(f"{RD}{f} not found{R}")
                return 1
            tm = TaskManager(str(p))
            before = tm.counts_by_status()
            for t in tm.tasks:
                t.status = "pending"
            tm.save()
            _ansi(f"{GR}Reset {p}: {before} → all {tm.total_count} pending{R}")
        return 0

    # --prd-wizard: interactive PRD via claude CLI (direct terminal, no tmux)
    if "--prd-wizard" in args:
        from whilly.prd_launcher import run_prd_wizard

        idx = args.index("--prd-wizard")
        slug = args[idx + 1] if idx + 1 < len(args) and not args[idx + 1].startswith("-") else None
        config = WhillyConfig.from_env()
        return run_prd_wizard(slug=slug, model=config.MODEL)

    # --from-github: generate tasks from GitHub Issues
    if "--from-github" in args:
        from whilly.github_converter import generate_tasks_from_github

        idx = args.index("--from-github")
        labels_arg = args[idx + 1] if idx + 1 < len(args) and not args[idx + 1].startswith("-") else None
        # "all" / "*" / "-" → fetch every open issue without label filter
        if labels_arg and labels_arg.lower() in ("all", "*", "-"):
            labels: list[str] = []  # explicit empty list = "no label filter"
        else:
            labels = labels_arg.split(",") if labels_arg else ["workshop", "whilly:ready"]

        output_file = "tasks-from-github.json"
        prd_file = Path("docs/PRD-workshop.md") if Path("docs/PRD-workshop.md").exists() else None

        label_desc = ", ".join(labels) if labels else "all open issues (no label filter)"
        _ansi(f"{CY}{B}Extracting GitHub Issues: {label_desc}...{R}")
        try:
            tasks_path = generate_tasks_from_github(output_path=output_file, filter_labels=labels, prd_file=prd_file)
            _ansi(f"{GR}Tasks generated: {tasks_path}{R}")

            # Nothing to run if no open issues matched — bail early.
            try:
                generated = json.loads(Path(tasks_path).read_text())
                if not generated.get("tasks"):
                    _ansi(f"{YL}No tasks were generated — nothing to run.{R}")
                    return 0
            except Exception:
                pass  # fall through; downstream loop will report a clearer error

            # --go / --yes → skip the confirmation prompt and execute immediately
            auto_go = "--go" in args or "--yes" in args
            if auto_go:
                _ansi(f"{CY}{B}--go: auto-starting Whilly orchestrator...{R}")
                args = [a for a in args if a not in ("--go", "--yes", "--from-github")]
                if labels_arg and args and args[0] == labels_arg:
                    args = args[1:]
                args = [str(tasks_path)] + args
            elif sys.stdin.isatty():
                choice = input("\nRun tasks immediately? [Y/n]: ").lower()
                if choice in ("", "y", "yes"):
                    _ansi(f"{CY}{B}Starting Whilly orchestrator...{R}")
                    args = [str(tasks_path)]
                else:
                    _ansi(f"{YL}Run later with: whilly {tasks_path}{R}")
                    return 0
            else:
                _ansi(f"{YL}Run with: whilly {tasks_path}{R}")
                return 0

        except Exception as e:
            _ansi(f"{RD}GitHub to tasks conversion failed: {e}{R}")
            return 1

    # --from-issues-project: unified source (Project v2 board filters which issues to materialise)
    if "--from-issues-project" in args:
        from whilly.sources import fetch_issues_and_project

        idx = args.index("--from-issues-project")
        if idx + 1 >= len(args):
            _ansi(f"{RD}Usage: whilly --from-issues-project <project_url> --repo owner/name [--status Todo,Ready]{R}")
            return 1
        project_url = args[idx + 1]

        repo_spec = None
        if "--repo" in args:
            repo_idx = args.index("--repo")
            if repo_idx + 1 < len(args):
                repo_spec = args[repo_idx + 1]
        if not repo_spec or "/" not in repo_spec:
            _ansi(f"{RD}Missing or malformed --repo. Expected owner/name.{R}")
            return 1

        statuses_raw = None
        if "--status" in args:
            sidx = args.index("--status")
            if sidx + 1 < len(args) and not args[sidx + 1].startswith("-"):
                statuses_raw = args[sidx + 1]
        statuses = {s.strip() for s in statuses_raw.split(",")} if statuses_raw else None

        out_file = f"tasks-{repo_spec.replace('/', '-')}-from-project.json"
        _ansi(f"{CY}{B}Unified GitHub Issues + Project source...{R}")
        _ansi(f"Project: {project_url}")
        _ansi(f"Repository: {repo_spec}")
        _ansi(f"Statuses: {sorted(statuses) if statuses else ['Todo']}")
        try:
            plan_path, stats = fetch_issues_and_project(
                repo=repo_spec, project_url=project_url, target_statuses=statuses, out_path=out_file
            )
            _ansi(f"{GR}Tasks generated: {plan_path}{R}")
            _ansi(f"{D}   new={stats.new}  updated={stats.updated}  closed_externally={stats.closed_externally}{R}")

            if stats.total_open == 0:
                _ansi(f"{YL}No matching items — nothing to run.{R}")
                return 0

            auto_go = "--go" in args or "--yes" in args
            if auto_go:
                _ansi(f"{CY}{B}--go: auto-starting Whilly orchestrator...{R}")
                args = [str(plan_path)]
            elif sys.stdin.isatty():
                choice = input("\nRun tasks immediately? [Y/n]: ").strip().lower()
                if choice in ("", "y", "yes"):
                    args = [str(plan_path)]
                else:
                    _ansi(f"{YL}Run later with: whilly {plan_path}{R}")
                    return 0
            else:
                _ansi(f"{YL}Run with: whilly {plan_path}{R}")
                return 0
        except Exception as e:
            _ansi(f"{RD}Unified GitHub source failed: {e}{R}")
            return 1

    # --from-project: generate tasks from GitHub Project board
    if "--from-project" in args:
        from whilly.github_projects import GitHubProjectsConverter

        idx = args.index("--from-project")
        if idx + 1 >= len(args):
            _ansi(f"{RD}Usage: whilly --from-project <project_url> [--repo owner/name]{R}")
            return 1

        project_url = args[idx + 1]

        # Extract repo info from additional args or derive from current repo
        repo_owner = None
        repo_name = None

        if "--repo" in args:
            repo_idx = args.index("--repo")
            if repo_idx + 1 < len(args):
                repo_spec = args[repo_idx + 1]
                if "/" in repo_spec:
                    repo_owner, repo_name = repo_spec.split("/", 1)

        # Auto-detect repo if not specified
        if not repo_owner or not repo_name:
            try:
                result = subprocess.run(
                    ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=True
                )
                remote_url = result.stdout.strip()
                # Parse git@github.com:owner/repo.git or https://github.com/owner/repo.git
                if "github.com" in remote_url:
                    import re

                    match = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", remote_url)
                    if match:
                        repo_owner = repo_owner or match.group(1)
                        repo_name = repo_name or match.group(2)
            except Exception:
                pass

        if not repo_owner or not repo_name:
            _ansi(f"{RD}Could not determine repository. Use: --repo owner/name{R}")
            return 1

        _ansi(f"{CY}{B}Converting GitHub Project to Issues and Whilly tasks...{R}")
        _ansi(f"Project: {project_url}")
        _ansi(f"Repository: {repo_owner}/{repo_name}")

        try:
            converter = GitHubProjectsConverter()
            tasks_path = converter.project_to_whilly_tasks(
                project_url, repo_owner, repo_name, "tasks-from-project.json"
            )

            _ansi(f"{GR}Tasks generated: {tasks_path}{R}")

            if "--go" in args:
                # Auto-execute with self-healing
                _ansi(f"{CY}Auto-executing with self-healing...{R}")
                script_path = Path(__file__).parent.parent / "scripts" / "whilly_with_healing.py"
                os.execv(sys.executable, [sys.executable, str(script_path), tasks_path])
            else:
                _ansi(f"{YL}Run with: python scripts/whilly_with_healing.py {tasks_path}{R}")
                return 0

        except Exception as e:
            _ansi(f"{RD}GitHub Project conversion failed: {e}{R}")
            return 1

    # --sync-todo: sync only Todo items from GitHub Project
    if "--sync-todo" in args:
        from whilly.github_projects import GitHubProjectsConverter, SyncConfig

        idx = args.index("--sync-todo")
        if idx + 1 >= len(args):
            _ansi(f"{RD}Usage: whilly --sync-todo <project_url> [--repo owner/name]{R}")
            return 1

        project_url = args[idx + 1]
        repo_owner, repo_name = _extract_repo_args(args)

        if not repo_owner or not repo_name:
            _ansi(f"{RD}Could not determine repository. Use: --repo owner/name{R}")
            return 1

        _ansi(f"{CY}{B}Syncing Todo items from GitHub Project...{R}")
        _ansi(f"Project: {project_url}")
        _ansi(f"Repository: {repo_owner}/{repo_name}")

        try:
            sync_config = SyncConfig(target_statuses={"Todo"})
            converter = GitHubProjectsConverter(sync_config=sync_config)
            stats = converter.sync_todo_items(project_url, repo_owner, repo_name)

            _ansi(f"{GR}Sync completed:{R}")
            _ansi(f"  - Created: {stats['created_count']} issues")
            _ansi(f"  - Skipped: {stats['skipped_count']} items")
            _ansi(f"  - Total Todo items: {stats['total_todo_items']}")

            return 0

        except Exception as e:
            _ansi(f"{RD}Todo sync failed: {e}{R}")
            return 1

    # --watch-project: monitor GitHub Project for Todo items
    if "--watch-project" in args:
        from whilly.github_projects import GitHubProjectsConverter, SyncConfig

        idx = args.index("--watch-project")
        if idx + 1 >= len(args):
            _ansi(f"{RD}Usage: whilly --watch-project <project_url> [--repo owner/name]{R}")
            return 1

        project_url = args[idx + 1]
        repo_owner, repo_name = _extract_repo_args(args)

        if not repo_owner or not repo_name:
            _ansi(f"{RD}Could not determine repository. Use: --repo owner/name{R}")
            return 1

        _ansi(f"{CY}{B}Watching GitHub Project for Todo items...{R}")
        _ansi(f"Project: {project_url}")
        _ansi(f"Repository: {repo_owner}/{repo_name}")

        try:
            sync_config = SyncConfig(target_statuses={"Todo"})
            converter = GitHubProjectsConverter(sync_config=sync_config)
            converter.watch_project(project_url, repo_owner, repo_name)
            return 0

        except Exception as e:
            _ansi(f"{RD}Project watching failed: {e}{R}")
            return 1

    # --sync-status: update Project item status from Issue
    if "--sync-status" in args:
        from whilly.github_projects import GitHubProjectsConverter

        idx = args.index("--sync-status")
        if idx + 2 >= len(args):
            _ansi(f"{RD}Usage: whilly --sync-status <issue_number> <status>{R}")
            return 1

        try:
            issue_number = int(args[idx + 1])
            new_status = args[idx + 2]
        except ValueError:
            _ansi(f"{RD}Invalid issue number: {args[idx + 1]}{R}")
            return 1

        _ansi(f"{CY}Updating Project status for issue #{issue_number} to '{new_status}'...{R}")

        try:
            converter = GitHubProjectsConverter()
            success = converter.sync_status_changes(issue_number, new_status)

            if success:
                _ansi(f"{GR}Status updated successfully{R}")
                return 0
            else:
                _ansi(f"{YL}Status update completed with warnings{R}")
                return 0

        except Exception as e:
            _ansi(f"{RD}Status sync failed: {e}{R}")
            return 1

    # --project-sync-status: show current sync status
    if "--project-sync-status" in args:
        from whilly.github_projects import GitHubProjectsConverter

        _ansi(f"{CY}{B}Project Sync Status{R}")

        try:
            converter = GitHubProjectsConverter()
            status = converter.get_sync_status()

            _ansi(f"Last sync: {status['last_sync'] or 'Never'}")
            _ansi(f"Project: {status['project_url'] or 'Not set'}")
            _ansi(f"Repository: {status['repo'] or 'Not set'}")
            _ansi(f"Synced items: {status['total_synced_items']}")
            _ansi(f"Target statuses: {', '.join(status['target_statuses'])}")
            _ansi(f"State file: {status['sync_state_file']}")

            return 0

        except Exception as e:
            _ansi(f"{RD}Failed to get sync status: {e}{R}")
            return 1

    # --init: generate PRD from description
    if "--init" in args:
        from whilly.prd_generator import generate_prd, generate_tasks

        idx = args.index("--init")
        if idx + 1 >= len(args):
            _ansi(f'{RD}Usage: whilly --init "project description"{R}')
            return 1
        description = args[idx + 1]
        also_plan = "--plan" in args
        also_go = "--go" in args
        config = WhillyConfig.from_env()

        _ansi(f"{CY}{B}Generating PRD...{R}")
        try:
            prd_path = generate_prd(description, model=config.MODEL)
            _ansi(f"{GR}PRD created: {prd_path}{R}")
        except Exception as e:
            _ansi(f"{RD}PRD generation failed: {e}{R}")
            return 1

        tasks_path = None
        if also_plan or also_go:
            _ansi(f"{CY}{B}Generating tasks from PRD...{R}")
            try:
                tasks_path = generate_tasks(prd_path, model=config.MODEL)
                task_count = len(json.loads(tasks_path.read_text()).get("tasks", []))
                _ansi(f"{GR}Tasks created: {tasks_path} ({task_count} tasks){R}")
            except Exception as e:
                _ansi(f"{RD}Task generation failed: {e}{R}")
                return 1

        if also_go and tasks_path:
            _ansi(f"{CY}{B}Starting execution...{R}")
            agent_name = resolve_agent_name(config)
            state_store = StateStore(config.STATE_FILE)
            result = run_plan(str(tasks_path), config, agent_name, state_store=state_store)
            if result and isinstance(result, tuple):
                return result[1]  # exit code
            return 0

        return 0

    # --plan: generate tasks.json from PRD
    if "--plan" in args:
        from whilly.prd_generator import generate_tasks

        rest = [a for a in args if a != "--plan"]
        if not rest:
            _ansi(f"{RD}Usage: whilly --plan PRD.md{R}")
            return 1
        config = WhillyConfig.from_env()
        for prd_file in rest:
            p = Path(prd_file)
            if not p.is_file():
                _ansi(f"{RD}{prd_file} not found{R}")
                return 1
            _ansi(f"{CY}{B}Generating tasks from {p.name}...{R}")
            try:
                tasks_path = generate_tasks(p, model=config.MODEL)
                task_count = len(json.loads(tasks_path.read_text()).get("tasks", []))
                _ansi(f"{GR}Tasks created: {tasks_path} ({task_count} tasks){R}")
            except Exception as e:
                _ansi(f"{RD}Task generation failed for {p}: {e}{R}")
                return 1
        return 0

    config = WhillyConfig.from_env()

    # F4: --headless and --timeout CLI flags
    if "--headless" in args:
        config.HEADLESS = True
        args = [a for a in args if a != "--headless"]
    elif not config.HEADLESS:
        # Auto-detect: enable headless when stdout is not a TTY
        config.HEADLESS = not sys.stdout.isatty()

    if "--timeout" in args:
        idx = args.index("--timeout")
        if idx + 1 < len(args):
            try:
                config.TIMEOUT = int(args[idx + 1])
            except ValueError:
                _ansi(f"{RD}--timeout requires an integer (seconds){R}")
                return 1
            args = args[:idx] + args[idx + 2 :]
        else:
            _ansi(f"{RD}--timeout requires a value{R}")
            return 1

    # --no-worktree / --no-workspace: disable plan-level git worktree isolation
    if "--no-worktree" in args or "--no-workspace" in args:
        config.USE_WORKSPACE = False
        args = [a for a in args if a not in ("--no-worktree", "--no-workspace")]

    # --agent {claude,opencode}: override backend selection for this run (OC-111)
    if "--agent" in args:
        from whilly.agents import available_backends

        idx = args.index("--agent")
        if idx + 1 >= len(args):
            _ansi(f"{RD}--agent requires a value ({'|'.join(available_backends())}){R}")
            return 1
        backend = args[idx + 1]
        if backend not in available_backends():
            _ansi(f"{RD}Unknown backend {backend!r}. Available: {', '.join(available_backends())}{R}")
            return 1
        config.AGENT_BACKEND = backend
        # Propagate to env so whilly.agent_runner._active_backend() picks it up
        # inside subprocess/tmux children too.
        os.environ["WHILLY_AGENT_BACKEND"] = backend
        args = args[:idx] + args[idx + 2 :]

    agent_name = resolve_agent_name(config)
    state_store = StateStore(config.STATE_FILE)

    # --resume: resume from saved state after crash
    resume_mode = "--resume" in args
    if resume_mode:
        args = [a for a in args if a != "--resume"]

    # Discover plan files
    plan_files: list[Path] = []

    if resume_mode and not args:
        # Try to load plan file from saved state
        saved = state_store.load()
        if saved and saved.get("plan_file"):
            saved_plan = Path(saved["plan_file"])
            if saved_plan.is_file():
                plan_files = [saved_plan]
                _ansi(f"{CY}Resuming plan from saved state: {saved_plan}{R}")
            else:
                _ansi(f"{RD}Saved plan file {saved_plan} not found{R}")
                return 1
        else:
            _ansi(f"{RD}No saved state found for --resume{R}")
            return 1
    elif "--all" in args:
        plan_files = discover_plans()
    elif args:
        for f in args:
            if f.startswith("--"):
                continue
            p = Path(f)
            if not p.is_file():
                _ansi(f"{RD}{f} not found{R}")
                return 1
            if validate_schema(p):
                plan_files.append(p)
            else:
                _ansi(f"{YL}WARN: {f} -- incompatible schema, skipping{R}")
    else:
        # Без аргументов: всегда показываем интерактивное меню (planы + PRDы + new).
        # Раньше автоматически подхватывали tasks.json из cwd — теперь нет, чтобы
        # пользователь явно выбирал что делать и видел полный список активных планов.
        plans = discover_plans()
        plan_files = select_plan_interactive(plans)

    if not plan_files:
        _ansi(f"{RD}No valid plan files selected{R}")
        return 1

    # Graceful shutdown handler
    shutdown_requested = False

    def _signal_handler(signum: int, frame) -> None:
        nonlocal shutdown_requested
        if shutdown_requested:
            _ansi(f"\n{RD}Force quit -- killing tmux sessions{R}")
            kill_all_whilly_sessions()
            sys.exit(1)
        shutdown_requested = True
        _ansi(f"\n{YL}Shutting down gracefully (Ctrl+C again to force)...{R}")

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Execute plans
    report_files: list[Path] = []
    worst_exit_code = EXIT_SUCCESS

    for i, plan_path in enumerate(plan_files):
        plan_file = str(plan_path)
        if len(plan_files) > 1:
            log.info("Plan %d/%d: %s", i + 1, len(plan_files), plan_file)

        if shutdown_requested:
            log.info("Shutdown requested, skipping remaining plans")
            break

        try:
            result = run_plan(plan_file, config, agent_name, resume=resume_mode, state_store=state_store)
            if result is not None:
                report_path, exit_code = result
                if report_path:
                    report_files.append(report_path)
                worst_exit_code = max(worst_exit_code, exit_code)
            # Only resume the first plan, then run normally
            resume_mode = False
        except KeyboardInterrupt:
            log.info("Interrupted during plan %s", plan_file)
            kill_all_whilly_sessions()
            break
        except Exception:
            log.exception("Error running plan %s", plan_file)

    # Generate summary if multiple plans
    if report_files:
        summary = generate_summary(report_files, Path(".planning/reports"))
        if summary:
            _ansi(f"\n{GR}{B}Summary:{R} {summary}")

    notify_all_done()
    _ansi(f"\n{GR}{B}Whilly finished.{R}")

    if config.HEADLESS:
        return worst_exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())
