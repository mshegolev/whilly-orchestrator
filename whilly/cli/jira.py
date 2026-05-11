"""``whilly jira`` subcommand surface.

This module connects the existing Jira source adapter to the v4 CLI so
operators can fetch a Jira issue into a Whilly plan without a Python heredoc:

    whilly jira import ABC-123 --import-db
    whilly jira import ABC-123 --run

The implementation deliberately reuses ``whilly.sources.jira`` for source
translation, ``whilly plan import`` for database persistence, and ``whilly run``
for execution. The CLI layer only owns argument parsing, default output paths,
and the Jira-specific plan id convention.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import webbrowser
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from whilly.sources.jira import fetch_single_jira_issue, parse_jira_key

EXIT_OK = 0
EXIT_VALIDATION_ERROR = 1
JIRA_CLOUD_API_TOKEN_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"

Fetcher = Callable[..., tuple[Path, Any]]
Importer = Callable[[str], int]
Runner = Callable[[Sequence[str]], int]
ConfigLoader = Callable[[], Any]
ConfigReader = Callable[[], dict[str, Any]]
Prompt = Callable[[str], str]
BrowserOpener = Callable[[str], bool]
IsATTY = Callable[[], bool]


@dataclass(frozen=True)
class JiraConfigState:
    server_url: str
    username: str
    token: str
    auth_scheme: str


def build_jira_parser() -> argparse.ArgumentParser:
    """Build the ``whilly jira ...`` argparse tree."""

    parser = argparse.ArgumentParser(
        prog="whilly jira",
        description="Import Jira issues into Whilly plans.",
    )
    sub = parser.add_subparsers(dest="action", required=True, metavar="ACTION")

    p_import = sub.add_parser(
        "import",
        help="Fetch one Jira issue and write a Whilly plan JSON.",
    )
    p_import.add_argument(
        "jira_ref",
        help="Jira key or browse URL, e.g. ABC-123 or https://jira.example/browse/ABC-123.",
    )
    p_import.add_argument(
        "--out",
        default=None,
        help="Output plan JSON path (default: out/jira-<KEY>.json).",
    )
    p_import.add_argument(
        "--plan-id",
        default=None,
        help="Plan id to write into the JSON (default: jira-<key-lowercase>).",
    )
    p_import.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="Per Jira HTTP request timeout in seconds (default: 15).",
    )
    p_import.add_argument(
        "--import-db",
        action="store_true",
        help="Run `whilly plan import` on the written plan after fetching Jira.",
    )
    p_import.add_argument(
        "--run",
        action="store_true",
        help="Import the written plan and immediately run a local worker for it.",
    )
    p_import.add_argument(
        "--interactive-config",
        action="store_true",
        help="Prompt for missing Jira settings before fetching the issue.",
    )
    p_import.add_argument(
        "--no-interactive-config",
        action="store_true",
        help="Never prompt for missing Jira settings; print setup instructions instead.",
    )
    p_import.add_argument(
        "--max-iterations",
        default=None,
        help="Pass through to `whilly run --max-iterations` when --run is used.",
    )
    p_import.add_argument(
        "--worker-id",
        default=None,
        help="Pass through to `whilly run --worker-id` when --run is used.",
    )
    p_import.add_argument(
        "--verify-command",
        dest="verify_commands",
        action="append",
        default=[],
        metavar="NAME=COMMAND",
        help="Pass through to `whilly run --verify-command`; repeatable.",
    )
    p_import.add_argument(
        "--optional-verify-command",
        dest="optional_verify_commands",
        action="append",
        default=[],
        metavar="NAME=COMMAND",
        help="Pass through to `whilly run --optional-verify-command`; repeatable.",
    )
    p_import.add_argument(
        "--verify-timeout",
        default=None,
        help="Pass through to `whilly run --verify-timeout` when --run is used.",
    )
    return parser


def run_jira_command(
    argv: Sequence[str],
    *,
    fetcher: Fetcher | None = None,
    importer: Importer | None = None,
    runner: Runner | None = None,
    config_loader: ConfigLoader | None = None,
    config_reader: ConfigReader | None = None,
    prompt: Prompt | None = None,
    secret_prompt: Prompt | None = None,
    browser_opener: BrowserOpener | None = None,
    environ: MutableMapping[str, str] | None = None,
    stdin_isatty: IsATTY | None = None,
) -> int:
    """Entry point for ``whilly jira ...``; returns a process exit code."""

    parser = build_jira_parser()
    args = parser.parse_args(list(argv))
    if args.action == "import":
        return _run_import(
            args,
            fetcher=fetcher or fetch_single_jira_issue,
            importer=importer,
            runner=runner,
            config_loader=config_loader,
            config_reader=config_reader,
            prompt=prompt,
            secret_prompt=secret_prompt,
            browser_opener=browser_opener,
            environ=environ,
            stdin_isatty=stdin_isatty,
        )
    parser.error(f"unknown action {args.action!r}")  # pragma: no cover
    return EXIT_VALIDATION_ERROR


def _run_import(
    args: argparse.Namespace,
    *,
    fetcher: Fetcher,
    importer: Importer | None,
    runner: Runner | None,
    config_loader: ConfigLoader | None,
    config_reader: ConfigReader | None,
    prompt: Prompt | None,
    secret_prompt: Prompt | None,
    browser_opener: BrowserOpener | None,
    environ: MutableMapping[str, str] | None,
    stdin_isatty: IsATTY | None,
) -> int:
    try:
        key = parse_jira_key(args.jira_ref)
    except ValueError as exc:
        print(f"whilly jira import: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    out_path = Path(args.out) if args.out else Path("out") / f"jira-{key}.json"
    plan_id = args.plan_id or f"jira-{key.lower()}"

    effective_config_loader = config_loader if config_loader is not None else _load_config
    effective_config_reader = config_reader if config_reader is not None else _read_jira_config_section
    effective_env = environ if environ is not None else os.environ
    try:
        effective_config_loader()
        config_rc = _ensure_jira_config(
            args,
            config_reader=effective_config_reader,
            env=effective_env,
            prompt=prompt or input,
            secret_prompt=secret_prompt or getpass.getpass,
            browser_opener=browser_opener or webbrowser.open,
            stdin_isatty=stdin_isatty or sys.stdin.isatty,
        )
        if config_rc != EXIT_OK:
            return config_rc
        plan_path, stats = fetcher(key, out_path=out_path, timeout=args.timeout)
        _write_plan_id(Path(plan_path), plan_id)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"whilly jira import: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    print(
        "whilly jira import: "
        f"plan={plan_path} plan_id={plan_id} "
        f"new={getattr(stats, 'new', 0)} updated={getattr(stats, 'updated', 0)}"
    )

    if args.import_db or args.run:
        effective_importer = importer if importer is not None else _import_plan
        import_rc = effective_importer(str(plan_path))
        if import_rc != EXIT_OK:
            return import_rc

    if args.run:
        effective_runner = runner if runner is not None else _run_plan_worker
        return effective_runner(_run_argv(plan_id, args))

    return EXIT_OK


def _ensure_jira_config(
    args: argparse.Namespace,
    *,
    config_reader: ConfigReader,
    env: MutableMapping[str, str],
    prompt: Prompt,
    secret_prompt: Prompt,
    browser_opener: BrowserOpener,
    stdin_isatty: IsATTY,
) -> int:
    state = _jira_config_state(config_reader(), env)
    missing = _missing_jira_settings(state)
    if not missing:
        return EXIT_OK

    interactive = bool(args.interactive_config) or (not bool(args.no_interactive_config) and stdin_isatty())
    if not interactive:
        _print_missing_jira_config(missing)
        return EXIT_VALIDATION_ERROR

    print("whilly jira import: Jira config is incomplete; enter missing values.", file=sys.stderr)
    try:
        if "JIRA_SERVER_URL" in missing:
            server_url = prompt("Jira server URL (for example https://company.atlassian.net): ").strip()
            if server_url:
                env["JIRA_SERVER_URL"] = server_url.rstrip("/")
        if "JIRA_USERNAME" in missing:
            username = prompt("Jira username/email (leave empty for bearer PAT auth): ").strip()
            if username:
                env["JIRA_USERNAME"] = username
            else:
                env["JIRA_AUTH_SCHEME"] = "bearer"
        if "JIRA_API_TOKEN" in missing:
            print(
                f"whilly jira import: opening Jira Cloud API token page: {JIRA_CLOUD_API_TOKEN_URL}",
                file=sys.stderr,
            )
            try:
                browser_opener(JIRA_CLOUD_API_TOKEN_URL)
            except Exception as exc:
                print(f"whilly jira import: could not open browser: {exc}", file=sys.stderr)
            token = secret_prompt("Jira API token / PAT: ").strip()
            if token:
                env["JIRA_API_TOKEN"] = token
    except EOFError:
        print("whilly jira import: interactive Jira setup was interrupted before input completed.", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    state_after_prompt = _jira_config_state(config_reader(), env)
    missing_after_prompt = _missing_jira_settings(state_after_prompt)
    if missing_after_prompt:
        _print_missing_jira_config(missing_after_prompt)
        return EXIT_VALIDATION_ERROR
    return EXIT_OK


def _jira_config_state(section: Mapping[str, Any], env: Mapping[str, str]) -> JiraConfigState:
    auth_scheme = _normalize_auth_scheme(
        env.get("JIRA_AUTH_SCHEME")
        or env.get("JIRA_TOKEN_TYPE")
        or _string_section_value(section, "auth_scheme")
        or "basic"
    )
    server_url = (
        env.get("JIRA_SERVER_URL") or env.get("WHILLY_JIRA_SERVER_URL") or _string_section_value(section, "server_url")
    ).strip()
    username = (
        env.get("JIRA_USERNAME") or env.get("WHILLY_JIRA_USERNAME") or _string_section_value(section, "username")
    ).strip()
    token_raw = env.get("JIRA_API_TOKEN") or _string_section_value(section, "token")
    token = _resolve_config_secret(token_raw).strip()
    return JiraConfigState(
        server_url=server_url,
        username=username,
        token=token,
        auth_scheme=auth_scheme,
    )


def _missing_jira_settings(state: JiraConfigState) -> list[str]:
    missing: list[str] = []
    if not state.server_url:
        missing.append("JIRA_SERVER_URL")
    if state.auth_scheme == "basic" and not state.username:
        missing.append("JIRA_USERNAME")
    if not state.token:
        missing.append("JIRA_API_TOKEN")
    return missing


def _print_missing_jira_config(missing: Sequence[str]) -> None:
    print("whilly jira import: Jira config is incomplete.", file=sys.stderr)
    print("Missing: " + ", ".join(missing), file=sys.stderr)
    print(
        "Set these as environment variables, or add [jira] server_url/username/token to whilly.toml.",
        file=sys.stderr,
    )
    print(
        f"Jira Cloud API token page: {JIRA_CLOUD_API_TOKEN_URL}",
        file=sys.stderr,
    )
    print(
        "To let Whilly ask for missing values, rerun with `--interactive-config` from a terminal.",
        file=sys.stderr,
    )


def _normalize_auth_scheme(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"bearer", "pat", "token", "personal_access_token"}:
        return "bearer"
    return "basic"


def _string_section_value(section: Mapping[str, Any], key: str) -> str:
    value = section.get(key)
    return value if isinstance(value, str) else ""


def _resolve_config_secret(value: str) -> str:
    if not value:
        return ""
    try:
        from whilly.secrets import resolve

        resolved = resolve(value)
    except Exception:
        return ""
    return resolved if isinstance(resolved, str) else ""


def _write_plan_id(plan_path: Path, plan_id: str) -> None:
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{plan_path} must contain a JSON object")
    data["plan_id"] = plan_id
    plan_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _run_argv(plan_id: str, args: argparse.Namespace) -> list[str]:
    run_args = ["--plan", plan_id]
    if args.max_iterations is not None:
        run_args += ["--max-iterations", str(args.max_iterations)]
    if args.worker_id is not None:
        run_args += ["--worker-id", args.worker_id]
    for value in args.verify_commands:
        run_args += ["--verify-command", value]
    for value in args.optional_verify_commands:
        run_args += ["--optional-verify-command", value]
    if args.verify_timeout is not None:
        run_args += ["--verify-timeout", str(args.verify_timeout)]
    return run_args


def _load_config() -> Any:
    from whilly.config import load_layered

    return load_layered()


def _read_jira_config_section() -> dict[str, Any]:
    from whilly.config import get_toml_section

    return get_toml_section("jira")


def _import_plan(plan_file: str) -> int:
    from whilly.cli.plan import _run_import as run_plan_import

    return run_plan_import(plan_file)


def _run_plan_worker(argv: Sequence[str]) -> int:
    from whilly.cli.run import run_run_command

    return run_run_command(argv)


__all__ = ["JIRA_CLOUD_API_TOKEN_URL", "build_jira_parser", "run_jira_command"]
