"""``whilly worker launch [PLAN_ID]`` — one-command worker bring-up.

Hotfix companion to :mod:`whilly.cli.worker`. The register-then-run
flow used to require:

* ``whilly worker register --connect ... --bootstrap-token ...`` to mint
  per-worker creds, then
* a 12-env-var ``env -i ... whilly-worker`` line (which dropped
  ``USER``/``LOGNAME``/``SHELL`` and broke macOS keychain access for
  ``claude``).

This subcommand collapses both steps. It:

1. Resolves connect URL + bootstrap token from ``--flag`` → env var → ``.env``.
2. Loads / saves per-(control_url, plan_id) worker creds from
   ``~/.config/whilly/worker.json`` so the second invocation needs no flags.
3. Auto-detects ``CLAUDE_BIN`` via ``shutil.which("claude")`` when not set.
4. Inherits the current shell env (no ``env -i``) so keychain-backed CLIs
   keep working on macOS.
5. Hands off to :func:`whilly.cli.worker.run_worker_command` in-process —
   no shell exec, no double-fork.

Usage::

    whilly worker launch                       # uses default plan from config
    whilly worker launch jira-demo-9843        # binds to specific plan
    whilly worker launch --force-register      # mint a fresh worker_id
    whilly worker launch --register-only       # cache creds and exit
    whilly worker launch --print-env           # show what would be exported
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_ENVIRONMENT_ERROR = 2

DEFAULT_CONTROL_URL = "http://127.0.0.1:8000"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
CONFIG_PATH = Path(os.environ.get("WHILLY_WORKER_CONFIG", Path.home() / ".config" / "whilly" / "worker.json"))


def _read_config(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        sys.stderr.write(f"whilly worker launch: config {path} unreadable ({exc}); starting fresh\n")
        return {}


def _write_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser — ``KEY=VALUE`` lines, ignore comments. No shell expansion."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("\"'")
    return out


def _resolve_bootstrap_token(arg_value: str | None) -> str | None:
    if arg_value:
        return arg_value
    env_val = os.environ.get("WHILLY_WORKER_BOOTSTRAP_TOKEN")
    if env_val:
        return env_val
    dotenv = _read_dotenv(Path.cwd() / ".env")
    return dotenv.get("WHILLY_WORKER_BOOTSTRAP_TOKEN")


def _resolve_claude_bin(arg_value: str | None) -> str | None:
    if arg_value:
        return arg_value
    env_val = os.environ.get("CLAUDE_BIN")
    if env_val:
        return env_val
    found = shutil.which("claude")
    if found:
        return found
    return None


def _config_key(control_url: str, plan_id: str) -> str:
    return f"{control_url.rstrip('/')}|{plan_id}"


def _persist_default(key: str, supplied_arg: str | None, resolved_value: str, config: dict[str, Any]) -> None:
    """Write ``key`` to ``config`` honouring CLI-supplied-vs-defaulted intent.

    H21 fix: ``dict.setdefault`` silently ignores the new value when the
    key already exists, so a user passing ``--model X`` on a config that
    already has ``default_model: Y`` would see Y persist. Replace with
    "overwrite if the CLI flag was explicitly supplied OR the key is
    absent". ``supplied_arg is None`` is argparse's signal for "user did
    not pass this flag" — at that point the previously-stored default
    (if any) is correct, so we only seed when the key is missing.
    """
    if supplied_arg is not None or key not in config:
        config[key] = resolved_value


def _pick_plan_interactive(control_url: str) -> str | None:
    """Last-resort plan picker — only used when neither flag nor config has one.

    Walks the local Postgres if ``WHILLY_DATABASE_URL`` is set, otherwise
    asks for a typed answer. Kept deliberately small — operators with many
    plans almost always pass ``--plan`` from a script.
    """
    sys.stdout.write(f"\nNo plan_id provided. Enter the plan to bind this worker to (control: {control_url}): ")
    sys.stdout.flush()
    line = sys.stdin.readline().strip()
    return line or None


async def _register(control_url: str, bootstrap_token: str, hostname: str) -> tuple[str, str]:
    """Call ``POST /workers/register`` via :class:`RemoteWorkerClient`.

    Returns ``(worker_id, token)``. Raises the underlying transport
    exception on failure so the caller can format it consistently.
    """
    from whilly.adapters.transport.client import RemoteWorkerClient

    async with RemoteWorkerClient(
        control_url,
        token="register-placeholder",
        bootstrap_token=bootstrap_token,
    ) as client:
        resp = await client.register(hostname)
    return resp.worker_id, resp.token


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whilly worker launch",
        description=(
            "One-command worker bring-up. Auto-registers + caches per-worker "
            "credentials in ~/.config/whilly/worker.json so re-running is "
            "just `whilly worker launch <plan_id>`."
        ),
    )
    parser.add_argument(
        "plan_id",
        nargs="?",
        default=None,
        help=("Plan to bind this worker to. Falls back to the saved default for the resolved control URL."),
    )
    parser.add_argument(
        "--connect",
        dest="control_url",
        default=None,
        help=f"Control-plane base URL (env: WHILLY_CONTROL_URL, config default, then {DEFAULT_CONTROL_URL}).",
    )
    parser.add_argument(
        "--bootstrap-token",
        dest="bootstrap_token",
        default=None,
        help=(
            "Cluster-join secret (env: WHILLY_WORKER_BOOTSTRAP_TOKEN, "
            "or read from ./.env). Only needed on first register."
        ),
    )
    parser.add_argument(
        "--model",
        dest="model",
        default=None,
        help=f"Claude model id (env: WHILLY_MODEL, config default, then {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--claude-bin",
        dest="claude_bin",
        default=None,
        help="Path to claude CLI (env: CLAUDE_BIN, else `which claude`).",
    )
    parser.add_argument(
        "--hostname",
        dest="hostname",
        default=None,
        help="Self-reported hostname for register (default: socket.gethostname()).",
    )
    parser.add_argument(
        "--allow-shell",
        dest="allow_shell",
        action="store_true",
        default=True,
        help="Set WHILLY_AGENT_ALLOW_SHELL=1 (default on — unattended workers need it).",
    )
    parser.add_argument(
        "--no-allow-shell",
        dest="allow_shell",
        action="store_false",
        help="Run with deny-by-default tool restrictions (Claude default).",
    )
    parser.add_argument(
        "--force-register",
        dest="force_register",
        action="store_true",
        help="Discard cached creds for this (control_url, plan_id) and mint fresh ones.",
    )
    parser.add_argument(
        "--register-only",
        dest="register_only",
        action="store_true",
        help="Register + save credentials, then exit without starting the worker loop.",
    )
    parser.add_argument(
        "--print-env",
        dest="print_env",
        action="store_true",
        help="Print the resolved env vars (POSIX export lines) and exit without running.",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help=f"Alternative config file (default: {CONFIG_PATH}).",
    )
    return parser


def run_launch_command(argv: list[str]) -> int:
    """Entry point for ``whilly worker launch [PLAN_ID] ...``."""
    parser = _build_parser()
    args = parser.parse_args(list(argv))

    cfg_path = Path(args.config_path) if args.config_path else CONFIG_PATH
    config = _read_config(cfg_path)

    control_url = (
        args.control_url
        or os.environ.get("WHILLY_CONTROL_URL")
        or config.get("default_control_url")
        or DEFAULT_CONTROL_URL
    )
    control_url = control_url.rstrip("/")

    plan_id = args.plan_id or os.environ.get("WHILLY_PLAN_ID") or config.get("last_plan_id")
    if not plan_id:
        plan_id = _pick_plan_interactive(control_url)
        if not plan_id:
            sys.stderr.write("whilly worker launch: plan_id is required (positional arg or saved default)\n")
            return EXIT_ENVIRONMENT_ERROR

    model = args.model or os.environ.get("WHILLY_MODEL") or config.get("default_model") or DEFAULT_MODEL
    claude_bin = _resolve_claude_bin(args.claude_bin)
    if not claude_bin:
        sys.stderr.write(
            "whilly worker launch: claude CLI not found. Pass --claude-bin or install via "
            "`npm install -g @anthropic-ai/claude-code`.\n"
        )
        return EXIT_ENVIRONMENT_ERROR

    workers_section = config.setdefault("workers", {})
    cache_key = _config_key(control_url, plan_id)
    cached = workers_section.get(cache_key)

    if cached and not args.force_register:
        worker_id = cached["worker_id"]
        worker_token = cached["token"]
        sys.stderr.write(f"whilly worker launch: reusing cached worker {worker_id} for plan {plan_id}\n")
    else:
        bootstrap_token = _resolve_bootstrap_token(args.bootstrap_token)
        if not bootstrap_token:
            sys.stderr.write(
                "whilly worker launch: bootstrap token required for first register. Pass "
                "--bootstrap-token, set WHILLY_WORKER_BOOTSTRAP_TOKEN, or put it in ./.env.\n"
            )
            return EXIT_ENVIRONMENT_ERROR
        hostname = args.hostname or socket.gethostname()
        sys.stderr.write(f"whilly worker launch: registering new worker against {control_url} ...\n")
        try:
            worker_id, worker_token = asyncio.run(_register(control_url, bootstrap_token, hostname))
        except Exception as exc:  # noqa: BLE001 — surface the actual transport error
            sys.stderr.write(f"whilly worker launch: register failed: {exc}\n")
            return EXIT_ENVIRONMENT_ERROR
        workers_section[cache_key] = {
            "worker_id": worker_id,
            "token": worker_token,
            "plan_id": plan_id,
            "control_url": control_url,
            "registered_at": int(time.time()),
            "hostname": hostname,
        }
        config["last_plan_id"] = plan_id
        # PRD-post-auth-hardening §Epic H Item 21 — explicit overwrite on
        # supplied CLI flags. setdefault would silently ignore the new value
        # if the key already exists; that defeats the user's intent when
        # they pass --connect / --model expecting it to stick.
        _persist_default("default_control_url", args.control_url, control_url, config)
        _persist_default("default_model", args.model, model, config)
        _write_config(cfg_path, config)
        sys.stderr.write(f"whilly worker launch: saved creds to {cfg_path} (worker_id={worker_id})\n")

    # H21: also honour --connect / --model on the reuse path (cached creds).
    # Without this, a user who runs `whilly worker launch plan --model X` on
    # an existing config sees the model NOT updated because the fresh-register
    # branch never ran. Only update + write when the value actually changes
    # to avoid pointless file churn on every reuse invocation.
    cfg_dirty = False
    if args.control_url is not None and config.get("default_control_url") != control_url:
        config["default_control_url"] = control_url
        cfg_dirty = True
    if args.model is not None and config.get("default_model") != model:
        config["default_model"] = model
        cfg_dirty = True
    if config.get("last_plan_id") != plan_id:
        config["last_plan_id"] = plan_id
        cfg_dirty = True
    if cfg_dirty:
        _write_config(cfg_path, config)

    if args.register_only:
        sys.stdout.write(f"worker_id: {worker_id}\ntoken: {worker_token}\n")
        return EXIT_OK

    resolved_env: dict[str, str] = {
        "WHILLY_CONTROL_URL": control_url,
        "WHILLY_PLAN_ID": plan_id,
        "WHILLY_WORKER_ID": worker_id,
        "WHILLY_WORKER_TOKEN": worker_token,
        "WHILLY_MODEL": model,
        "CLAUDE_BIN": claude_bin,
    }
    if args.allow_shell:
        resolved_env["WHILLY_AGENT_ALLOW_SHELL"] = "1"

    if args.print_env:
        for key, value in resolved_env.items():
            sys.stdout.write(f"export {key}={json.dumps(value)}\n")
        return EXIT_OK

    os.environ.update(resolved_env)

    from whilly.cli.worker import run_worker_command

    return run_worker_command([])


# ---------------------------------------------------------------------------
# ``whilly worker list`` / ``whilly worker remove`` — config inspection
# ---------------------------------------------------------------------------


def _format_ts(ts: int | float | None) -> str:
    if not ts:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return "—"


def run_list_command(argv: list[str]) -> int:
    """``whilly worker list`` — print every cached (control_url, plan_id) pair.

    Output is a fixed-width table on stdout so it pipes cleanly into
    ``grep`` / ``awk``. Empty config still exits 0 — operators script
    around this and would not appreciate a non-zero exit for "nothing
    cached yet".
    """
    parser = argparse.ArgumentParser(
        prog="whilly worker list",
        description="Print cached worker credentials from the launch config.",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help=f"Alternative config file (default: {CONFIG_PATH}).",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit the raw config dict as JSON instead of the table.",
    )
    args = parser.parse_args(list(argv))

    cfg_path = Path(args.config_path) if args.config_path else CONFIG_PATH
    config = _read_config(cfg_path)
    workers = config.get("workers") or {}

    if args.as_json:
        sys.stdout.write(json.dumps(config, indent=2, sort_keys=True) + "\n")
        return EXIT_OK

    if not workers:
        sys.stdout.write(f"(no cached workers in {cfg_path})\n")
        return EXIT_OK

    last_plan = config.get("last_plan_id")
    default_url = config.get("default_control_url") or ""
    sys.stdout.write(f"config: {cfg_path}\n")
    sys.stdout.write(f"default_control_url: {default_url or '—'}\n")
    sys.stdout.write(f"last_plan_id: {last_plan or '—'}\n\n")
    header = f"{'plan_id':<28}  {'worker_id':<18}  {'control_url':<32}  {'registered':<16}  hostname"
    sys.stdout.write(header + "\n")
    sys.stdout.write("-" * len(header) + "\n")
    for entry in sorted(workers.values(), key=lambda w: (w.get("control_url", ""), w.get("plan_id", ""))):
        sys.stdout.write(
            f"{entry.get('plan_id', '—'):<28}  "
            f"{entry.get('worker_id', '—'):<18}  "
            f"{entry.get('control_url', '—'):<32}  "
            f"{_format_ts(entry.get('registered_at')):<16}  "
            f"{entry.get('hostname', '—')}\n"
        )
    return EXIT_OK


def run_remove_command(argv: list[str]) -> int:
    """``whilly worker remove <PLAN_ID>`` — drop cached creds.

    Variants:

    * ``whilly worker remove jira-demo-9843`` — drop one (uses default
      control_url, errors out if ambiguous).
    * ``whilly worker remove jira-demo-9843 --connect http://...`` —
      disambiguate when multiple control URLs are in the cache.
    * ``whilly worker remove --all`` — wipe the ``workers`` section.

    Exits 0 on successful removal, 2 if nothing matched.
    """
    parser = argparse.ArgumentParser(
        prog="whilly worker remove",
        description="Drop cached worker credentials. Does NOT revoke on the server.",
    )
    parser.add_argument(
        "plan_id",
        nargs="?",
        default=None,
        help="Plan ID whose cached creds to remove.",
    )
    parser.add_argument(
        "--connect",
        dest="control_url",
        default=None,
        help="Control URL to disambiguate when the same plan_id exists under multiple servers.",
    )
    parser.add_argument(
        "--all",
        dest="wipe_all",
        action="store_true",
        help="Drop every cached worker entry.",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help=f"Alternative config file (default: {CONFIG_PATH}).",
    )
    args = parser.parse_args(list(argv))

    if not args.wipe_all and not args.plan_id:
        parser.error("plan_id is required (or pass --all to wipe everything)")

    cfg_path = Path(args.config_path) if args.config_path else CONFIG_PATH
    config = _read_config(cfg_path)
    workers = config.get("workers") or {}
    if not workers:
        sys.stderr.write(f"whilly worker remove: nothing cached in {cfg_path}\n")
        return EXIT_ENVIRONMENT_ERROR

    if args.wipe_all:
        count = len(workers)
        config["workers"] = {}
        config.pop("last_plan_id", None)
        _write_config(cfg_path, config)
        sys.stdout.write(f"removed {count} cached worker(s) from {cfg_path}\n")
        return EXIT_OK

    matches = [
        key
        for key, entry in workers.items()
        if entry.get("plan_id") == args.plan_id
        and (args.control_url is None or entry.get("control_url", "").rstrip("/") == args.control_url.rstrip("/"))
    ]
    if not matches:
        sys.stderr.write(f"whilly worker remove: no cached entry for plan_id={args.plan_id!r}\n")
        return EXIT_ENVIRONMENT_ERROR
    if len(matches) > 1 and not args.control_url:
        urls = sorted({workers[k].get("control_url", "—") for k in matches})
        sys.stderr.write(
            f"whilly worker remove: ambiguous — plan_id={args.plan_id!r} is cached under "
            f"{len(matches)} control URLs: {', '.join(urls)}. Pass --connect to disambiguate.\n"
        )
        return EXIT_ENVIRONMENT_ERROR

    for key in matches:
        del workers[key]
    if config.get("last_plan_id") == args.plan_id and not any(
        entry.get("plan_id") == args.plan_id for entry in workers.values()
    ):
        config.pop("last_plan_id", None)
    config["workers"] = workers
    _write_config(cfg_path, config)
    sys.stdout.write(f"removed {len(matches)} cached entry(ies) for plan_id={args.plan_id}\n")
    return EXIT_OK
