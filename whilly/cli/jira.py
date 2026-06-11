"""``whilly jira`` subcommand surface.

This module connects the existing Jira source adapter to the v4 CLI so
operators can fetch a Jira issue into a Whilly plan without a Python heredoc:

    whilly jira import ABC-123 --import-db
    whilly jira import ABC-123 --run
    whilly jira intake ABC-123

The implementation deliberately reuses ``whilly.sources.jira`` for source
translation, ``whilly plan import`` for database persistence, and ``whilly run``
for execution. The CLI layer owns argument parsing, default output paths, repo
target selection for Jira-only tasks, and the Jira-specific plan id convention.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import subprocess
import sys
import webbrowser
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from whilly.cli.smoke import (
    EXIT_CHECK_FAILED as _SMOKE_EXIT_CHECK_FAILED,
    EXIT_CONFIG_MISSING,
    SmokeReport,
    _redact_url,
    _smoke_report_dir,
    write_smoke_report,
)
from whilly.jira_work import build_jira_work_metadata, classify_jira_work, probe_code_readiness
from whilly.jira_watch import JiraWorkSnapshot, collect_jira_work_snapshot, persist_jira_work_snapshot
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
RepoDetector = Callable[[], str]
SnapshotCollector = Callable[..., JiraWorkSnapshot]


@dataclass(frozen=True)
class JiraConfigState:
    server_url: str
    username: str
    token: str
    auth_scheme: str


@dataclass(frozen=True)
class IntakeRepoChoice:
    kind: str
    target: dict[str, str] | None


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
    p_intake = sub.add_parser(
        "intake",
        help="Interactively import one Jira issue, choose a repo target, then pick PRD/plan/run.",
    )
    p_intake.add_argument(
        "jira_ref",
        help="Jira key or browse URL, e.g. ABC-123 or https://jira.example/browse/ABC-123.",
    )
    p_intake.add_argument(
        "--out",
        default=None,
        help="Output plan JSON path (default: out/jira-<KEY>.json).",
    )
    p_intake.add_argument(
        "--plan-id",
        default=None,
        help="Plan id to write into the JSON (default: jira-<key-lowercase>).",
    )
    p_intake.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="Per Jira HTTP request timeout in seconds (default: 15).",
    )
    p_intake.add_argument(
        "--interactive-config",
        action="store_true",
        help="Prompt for missing Jira settings before fetching the issue.",
    )
    p_intake.add_argument(
        "--no-interactive-config",
        action="store_true",
        help="Never prompt for missing Jira settings; print setup instructions instead.",
    )
    p_intake.add_argument(
        "--repo-kind",
        choices=("same", "new", "other", "skip"),
        default=None,
        help="Repo target mode: current repo, new repo, other existing repo, or no repo target.",
    )
    p_intake.add_argument(
        "--repo-url",
        default=None,
        help="Git clone URL for the selected repo target. GitHub and GitLab URLs are parsed automatically.",
    )
    p_intake.add_argument(
        "--repo-provider",
        default=None,
        help="Override repo provider id, e.g. github or gitlab (default: infer from URL host; unknown hosts default to gitlab).",
    )
    p_intake.add_argument(
        "--default-branch",
        default="main",
        help="Default branch to store in repo_targets (default: main).",
    )
    p_intake.add_argument(
        "--action",
        dest="intake_action",
        choices=("prd", "plan", "run", "save"),
        default=None,
        help=(
            "Next step after import: write PRD/context, run strict apply + TRIZ, "
            "run strict apply + worker, or only save JSON."
        ),
    )
    p_intake.add_argument(
        "--context-out",
        default=None,
        help="Path for PRD/context markdown when --action prd is selected (default: beside the plan JSON).",
    )
    p_intake.add_argument(
        "--max-iterations",
        default=None,
        help="Pass through to `whilly run --max-iterations` when action=run is used.",
    )
    p_intake.add_argument(
        "--worker-id",
        default=None,
        help="Pass through to `whilly run --worker-id` when action=run is used.",
    )
    p_intake.add_argument(
        "--verify-command",
        dest="verify_commands",
        action="append",
        default=[],
        metavar="NAME=COMMAND",
        help="Pass through to `whilly run --verify-command`; repeatable.",
    )
    p_intake.add_argument(
        "--optional-verify-command",
        dest="optional_verify_commands",
        action="append",
        default=[],
        metavar="NAME=COMMAND",
        help="Pass through to `whilly run --optional-verify-command`; repeatable.",
    )
    p_intake.add_argument(
        "--verify-timeout",
        default=None,
        help="Pass through to `whilly run --verify-timeout` when action=run is used.",
    )
    p_intake.add_argument(
        "--readiness-repo-path",
        default=None,
        help="Local checkout path to inspect for test commands and unit tests before action=run.",
    )
    p_intake.add_argument(
        "--allow-unready-run",
        action="store_true",
        help="Allow action=run even when --readiness-repo-path reports missing test evidence.",
    )
    p_classify = sub.add_parser(
        "classify",
        help="Classify an already imported Jira plan JSON into a Whilly work flow.",
    )
    p_classify.add_argument("plan_file", help="Path to a Jira plan JSON written by `whilly jira import/intake`.")
    p_classify.add_argument("--json", action="store_true", help="Print the classification as JSON.")
    p_readiness = sub.add_parser(
        "readiness",
        help="Inspect a local checkout for test commands and unit-test evidence.",
    )
    p_readiness.add_argument("repo_path", help="Local repository path to inspect.")
    p_readiness.add_argument("--json", action="store_true", help="Print the readiness result as JSON.")
    p_readiness.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when the readiness verdict is not ready_for_testing.",
    )
    p_poll = sub.add_parser(
        "poll",
        help="Run one Jira refresh cycle: issue, comments, changelog, remote links, and repo hints.",
    )
    p_poll.add_argument("jira_ref", help="Jira key or browse URL.")
    p_poll.add_argument("--timeout", type=int, default=15, help="Per Jira HTTP request timeout in seconds.")
    p_poll.add_argument("--plan-id", default="", help="Optional Whilly plan id to store with --persist.")
    p_poll.add_argument("--persist", action="store_true", help="Persist the refreshed snapshot to Postgres.")
    p_poll.add_argument("--json", action="store_true", help="Print the full snapshot as JSON.")
    p_smoke = sub.add_parser(
        "smoke",
        help=(
            "Run read-only Jira smoke checks (auth, issue fetch, comments, changelog, "
            "remote links, classify) and write a redacted report."
        ),
    )
    p_smoke.add_argument("--issue", required=True, help="Jira key or browse URL, e.g. ABC-123.")
    p_smoke.add_argument("--timeout", type=int, default=15, help="Per Jira HTTP request timeout in seconds.")
    p_smoke.add_argument(
        "--persist", action="store_true", help="Persist smoke event to Postgres (requires WHILLY_DATABASE_URL)."
    )
    p_smoke.add_argument("--json", action="store_true", help="Print the full report payload as JSON.")
    p_smoke.add_argument(
        "--interactive-config",
        action="store_true",
        help="Prompt for missing Jira settings before running smoke checks.",
    )
    p_smoke.add_argument(
        "--no-interactive-config",
        action="store_true",
        help="Never prompt for missing Jira settings; print setup instructions instead.",
    )
    p_tui = sub.add_parser(
        "tui",
        help="Interactive TUI intake for a single Jira issue.",
    )
    p_tui.add_argument(
        "jira_ref",
        help="Jira key or browse URL, e.g. ABC-123 or https://jira.example/browse/ABC-123.",
    )
    p_tui.add_argument(
        "--action",
        dest="tui_action",
        choices=["prd", "plan", "run", "interactive", "save"],
        default=None,
        help="Non-interactive action (skips menu); useful for scripting.",
    )
    p_tui.add_argument(
        "--plan-id",
        dest="plan_id",
        default=None,
        help="Plan id to write into the JSON (default: jira-<key-lowercase>).",
    )
    p_tui.add_argument(
        "--out",
        default=None,
        help="Output plan JSON path (default: out/jira-<KEY>.json).",
    )
    p_tui.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="Per Jira HTTP request timeout in seconds (default: 15).",
    )
    p_tui.add_argument(
        "--interactive-config",
        action="store_true",
        help="Prompt for missing Jira settings before fetching the issue.",
    )
    p_tui.add_argument(
        "--no-interactive-config",
        action="store_true",
        help="Never prompt for missing Jira settings; print setup instructions instead.",
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
    repo_detector: RepoDetector | None = None,
    plan_runner: Runner | None = None,
    snapshot_collector: SnapshotCollector | None = None,
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
    if args.action == "intake":
        return _run_intake(
            args,
            fetcher=fetcher or fetch_single_jira_issue,
            importer=importer,
            runner=runner,
            plan_runner=plan_runner,
            config_loader=config_loader,
            config_reader=config_reader,
            prompt=prompt,
            secret_prompt=secret_prompt,
            browser_opener=browser_opener,
            environ=environ,
            stdin_isatty=stdin_isatty,
            repo_detector=repo_detector,
        )
    if args.action == "classify":
        return _run_classify(args)
    if args.action == "readiness":
        return _run_readiness(args)
    if args.action == "poll":
        return _run_poll(args, snapshot_collector=snapshot_collector or collect_jira_work_snapshot)
    if args.action == "smoke":
        return _run_jira_smoke(
            args,
            snapshot_collector=snapshot_collector or collect_jira_work_snapshot,
            config_loader=config_loader,
            config_reader=config_reader,
            environ=environ,
            prompt=prompt,
            secret_prompt=secret_prompt,
            browser_opener=browser_opener,
            stdin_isatty=stdin_isatty,
        )
    if args.action == "tui":
        from whilly.cli.jira_tui import run_jira_tui_command

        return run_jira_tui_command(
            args,
            fetcher=fetcher or fetch_single_jira_issue,
            plan_runner=plan_runner or _run_plan_command,
            config_loader=config_loader,
            config_reader=config_reader,
            prompt=prompt,
            secret_prompt=secret_prompt,
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
            command_label="whilly jira import",
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


def _run_intake(
    args: argparse.Namespace,
    *,
    fetcher: Fetcher,
    importer: Importer | None,
    runner: Runner | None,
    plan_runner: Runner | None,
    config_loader: ConfigLoader | None,
    config_reader: ConfigReader | None,
    prompt: Prompt | None,
    secret_prompt: Prompt | None,
    browser_opener: BrowserOpener | None,
    environ: MutableMapping[str, str] | None,
    stdin_isatty: IsATTY | None,
    repo_detector: RepoDetector | None,
) -> int:
    try:
        key = parse_jira_key(args.jira_ref)
    except ValueError as exc:
        print(f"whilly jira intake: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    out_path = Path(args.out) if args.out else Path("out") / f"jira-{key}.json"
    plan_id = args.plan_id or f"jira-{key.lower()}"
    effective_config_loader = config_loader if config_loader is not None else _load_config
    effective_config_reader = config_reader if config_reader is not None else _read_jira_config_section
    effective_env = environ if environ is not None else os.environ
    effective_stdin_isatty = stdin_isatty or sys.stdin.isatty
    effective_prompt = prompt or input

    try:
        effective_config_loader()
        config_rc = _ensure_jira_config(
            args,
            config_reader=effective_config_reader,
            env=effective_env,
            prompt=effective_prompt,
            secret_prompt=secret_prompt or getpass.getpass,
            browser_opener=browser_opener or webbrowser.open,
            stdin_isatty=effective_stdin_isatty,
            command_label="whilly jira intake",
        )
        if config_rc != EXIT_OK:
            return config_rc
        plan_path, stats = fetcher(key, out_path=out_path, timeout=args.timeout)
        plan_path = Path(plan_path)
        _write_plan_id(plan_path, plan_id)
        print(
            "whilly jira intake: "
            f"loaded {key} plan={plan_path} plan_id={plan_id} "
            f"new={getattr(stats, 'new', 0)} updated={getattr(stats, 'updated', 0)}"
        )
        repo_choice = _resolve_intake_repo_choice(
            args,
            prompt=effective_prompt,
            stdin_isatty=effective_stdin_isatty,
            repo_detector=repo_detector or _detect_current_repo_url,
        )
        if repo_choice.target is None:
            _clear_repo_target(plan_path)
            repo_target_id = ""
            print("whilly jira intake: repo_target=skipped")
        else:
            repo_target_id = _write_repo_target(plan_path, repo_choice.target)
            print(f"whilly jira intake: repo_target={repo_target_id} repo_kind={repo_choice.kind}")
        work_metadata = _write_jira_work_metadata(
            plan_path,
            key=key,
            repo_path=Path(args.readiness_repo_path) if args.readiness_repo_path else None,
        )
        classification = work_metadata["classification"]
        print(
            "whilly jira intake: "
            f"classification={classification['kind']} "
            f"urgency={classification['urgency']} "
            f"flow={classification['recommended_flow']}"
        )
        readiness = work_metadata.get("readiness")
        if isinstance(readiness, dict):
            commands = ",".join(readiness.get("test_commands") or []) or "none"
            print(f"whilly jira intake: readiness={readiness.get('verdict')} test_commands={commands}")
        next_action = _resolve_intake_action(args, prompt=effective_prompt, stdin_isatty=effective_stdin_isatty)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"whilly jira intake: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    if next_action == "prd":
        try:
            context_path = _write_intake_context(
                plan_path,
                key=key,
                plan_id=plan_id,
                repo_choice=repo_choice,
                context_out=Path(args.context_out) if args.context_out else None,
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            print(f"whilly jira intake: {exc}", file=sys.stderr)
            return EXIT_VALIDATION_ERROR
        print(f"whilly jira intake: context={context_path}")
        print(f"whilly jira intake: ready for PRD discussion; repo_target={repo_target_id or 'none'}")
        return EXIT_OK

    if next_action == "plan":
        effective_plan_runner = plan_runner if plan_runner is not None else _run_plan_command
        return _run_intake_plan_preflight(plan_path, plan_id, effective_plan_runner)

    if next_action == "run":
        readiness = _read_jira_work_readiness(plan_path)
        if readiness and readiness.get("verdict") != "ready_for_testing" and not bool(args.allow_unready_run):
            print(
                "whilly jira intake: readiness gate failed; "
                f"verdict={readiness.get('verdict')} missing={','.join(readiness.get('missing_context') or [])}. "
                "Use --allow-unready-run to override.",
                file=sys.stderr,
            )
            return EXIT_VALIDATION_ERROR
        effective_plan_runner = plan_runner if plan_runner is not None else _run_plan_command
        preflight_rc = effective_plan_runner(["apply", str(plan_path), "--strict"])
        if preflight_rc != EXIT_OK:
            return preflight_rc
        effective_runner = runner if runner is not None else _run_plan_worker
        return effective_runner(_run_argv(plan_id, args))

    return EXIT_OK


def _run_intake_plan_preflight(plan_path: Path, plan_id: str, plan_runner: Runner) -> int:
    apply_rc = plan_runner(["apply", str(plan_path), "--strict"])
    if apply_rc != EXIT_OK:
        return apply_rc
    return plan_runner(["triz", plan_id, "--strict"])


def _run_classify(args: argparse.Namespace) -> int:
    try:
        data = _read_plan_object(Path(args.plan_file))
        classification = classify_jira_work(data)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"whilly jira classify: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR
    if args.json:
        print(json.dumps(classification.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            "whilly jira classify: "
            f"kind={classification.kind} urgency={classification.urgency} "
            f"flow={classification.recommended_flow} confidence={classification.confidence}"
        )
    return EXIT_OK


def _run_readiness(args: argparse.Namespace) -> int:
    result = probe_code_readiness(Path(args.repo_path))
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        commands = ",".join(result.test_commands) or "none"
        print(f"whilly jira readiness: verdict={result.verdict} test_commands={commands}")
    if bool(args.strict) and result.verdict != "ready_for_testing":
        return EXIT_VALIDATION_ERROR
    return EXIT_OK


def _run_poll(args: argparse.Namespace, *, snapshot_collector: SnapshotCollector) -> int:
    try:
        snapshot = snapshot_collector(args.jira_ref, timeout=args.timeout)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"whilly jira poll: {exc}", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    if args.persist:
        dsn = os.environ.get("WHILLY_DATABASE_URL", "").strip()
        if not dsn:
            print("whilly jira poll: WHILLY_DATABASE_URL is required for --persist.", file=sys.stderr)
            return EXIT_VALIDATION_ERROR
        try:
            asyncio.run(_persist_poll_snapshot(dsn=dsn, snapshot=snapshot, plan_id=str(args.plan_id or "")))
        except Exception as exc:  # noqa: BLE001 - present a clean CLI diagnostic
            print(f"whilly jira poll: persist failed: {exc}", file=sys.stderr)
            return EXIT_VALIDATION_ERROR

    if args.json:
        print(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2))
    else:
        classification = snapshot.classification
        print(
            "whilly jira poll: "
            f"issue={snapshot.issue_key} "
            f"classification={classification.get('kind', '')} "
            f"urgency={classification.get('urgency', '')} "
            f"comments={len(snapshot.comments)} "
            f"changelog={len(snapshot.changelog_ids)} "
            f"links={len(snapshot.links)} "
            f"repo_targets={len(snapshot.repo_targets)} "
            f"last_comment={snapshot.last_seen_comment_id or 'none'}"
        )
    return EXIT_OK


def _run_jira_smoke(
    args: argparse.Namespace,
    *,
    snapshot_collector: SnapshotCollector,
    config_loader: ConfigLoader | None,
    config_reader: ConfigReader | None,
    environ: MutableMapping[str, str] | None,
    prompt: Prompt | None,
    secret_prompt: Prompt | None,
    browser_opener: BrowserOpener | None,
    stdin_isatty: IsATTY | None,
) -> int:
    """Execute read-only Jira smoke checks and write a redacted report.

    Exit codes: 0 = all checks passed, 1 = one or more checks failed,
    2 = configuration missing (credential gate returned non-zero).
    """
    # --- V5 input validation: reject malformed keys before any network or config call ---
    try:
        issue_key = parse_jira_key(args.issue)
    except ValueError as exc:
        print(f"whilly jira smoke: {exc}", file=sys.stderr)
        print(
            "whilly jira smoke: pass a valid Jira key (e.g. ABC-123) or issue browse URL.",
            file=sys.stderr,
        )
        return EXIT_CONFIG_MISSING

    project_key = issue_key.rsplit("-", 1)[0]

    # --- Credential gate: must complete before snapshot_collector is called ---
    effective_config_loader = config_loader if config_loader is not None else _load_config
    effective_config_reader = config_reader if config_reader is not None else _read_jira_config_section
    effective_env: MutableMapping[str, str] = environ if environ is not None else os.environ
    effective_config_loader()
    config_rc = _ensure_jira_config(
        args,
        config_reader=effective_config_reader,
        env=effective_env,
        prompt=prompt or input,
        secret_prompt=secret_prompt or getpass.getpass,
        browser_opener=browser_opener or webbrowser.open,
        stdin_isatty=stdin_isatty or sys.stdin.isatty,
        command_label="whilly jira smoke",
    )
    if config_rc != EXIT_OK:
        # Map EXIT_VALIDATION_ERROR → EXIT_CONFIG_MISSING for the smoke command.
        return EXIT_CONFIG_MISSING

    # Derive the redacted target host for the report payload (never the full URL with auth).
    server_url = effective_env.get("JIRA_SERVER_URL") or effective_env.get("WHILLY_JIRA_SERVER_URL") or ""
    target_host = _redact_url(server_url)

    # --- Accumulate per-check results ---
    report = SmokeReport(kind="jira")
    snapshot: JiraWorkSnapshot | None = None

    try:
        snapshot = snapshot_collector(args.issue, timeout=args.timeout)
        report.add_check("auth", passed=True)
        report.add_check(
            "issue_fetch",
            passed=bool(snapshot.issue_key),
            hint="" if snapshot.issue_key else f"Verify JIRA_SERVER_URL and project key {project_key!r}.",
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        hint = f"Check JIRA_SERVER_URL, JIRA_API_TOKEN, and that project key {project_key!r} exists. Error: {exc}"
        report.add_check("auth", passed=False, hint=hint)
        report.add_check("issue_fetch", passed=False, hint=hint)

    if snapshot is not None:
        report.add_check(
            "comments",
            passed=snapshot.comments is not None,
            hint="" if snapshot.comments is not None else "Comments field missing in snapshot.",
        )
        report.add_check(
            "changelog",
            passed=len(snapshot.changelog_ids) >= 0,
            hint="",
        )
        report.add_check(
            "remote_links",
            passed=snapshot.links is not None,
            hint="" if snapshot.links is not None else "Remote links field missing in snapshot.",
        )
        classification = snapshot.classification
        classify_ok = bool(classification)
        report.add_check(
            "classify",
            passed=classify_ok,
            hint="" if classify_ok else f"classify_jira_work returned empty result for {issue_key!r}.",
        )
    else:
        # Snapshot failed — mark field-derived checks as failed with actionable hints.
        field_hint = f"Verify JIRA_SERVER_URL, JIRA_API_TOKEN, and project key {project_key!r}."
        report.add_check("comments", passed=False, hint=field_hint)
        report.add_check("changelog", passed=False, hint=field_hint)
        report.add_check("remote_links", passed=False, hint=field_hint)
        report.add_check("classify", passed=False, hint=field_hint)

    # --- Compose and write the redacted report ---
    payload = report.to_payload()
    payload["target_host"] = target_host
    payload["project_key"] = project_key
    payload["issue_key"] = issue_key

    report_path = write_smoke_report(_smoke_report_dir(), "jira", payload)

    # --- Optional DB persist (same gate as _run_poll) ---
    if args.persist:
        dsn = os.environ.get("WHILLY_DATABASE_URL", "").strip()
        if not dsn:
            print("whilly jira smoke: WHILLY_DATABASE_URL is required for --persist.", file=sys.stderr)
            return EXIT_CONFIG_MISSING
        try:
            asyncio.run(_persist_smoke_event(dsn=dsn, payload=payload))
        except Exception as exc:  # noqa: BLE001
            print(f"whilly jira smoke: persist failed: {exc}", file=sys.stderr)
            return _SMOKE_EXIT_CHECK_FAILED

    # --- Output ---
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        summary = payload["summary"]
        status = "PASS" if summary["all_passed"] else "FAIL"
        print(f"whilly jira smoke: {status} issue={issue_key} passed={summary['passed']}/{summary['total']}")
        for check in payload["checks"]:
            check_status = "pass" if check["passed"] else "FAIL"
            line = f"  [{check_status}] {check['name']}"
            if not check["passed"] and check.get("hint"):
                line += f" — {check['hint']}"
            print(line)
        print(f"  report={report_path}")

    return EXIT_OK if report.all_passed else EXIT_VALIDATION_ERROR


async def _persist_smoke_event(*, dsn: str, payload: dict[str, Any]) -> None:
    """Append a smoke event to Postgres (best-effort; not a hard requirement)."""
    from whilly.adapters.db import close_pool, create_pool
    from whilly.adapters.db.repository import TaskRepository

    pool = await create_pool(dsn)
    try:
        repo = TaskRepository(pool)
        await repo.append_jira_work_event(
            issue_key=payload.get("issue_key", ""),
            event_type="smoke",
            payload=payload,
        )
    finally:
        await close_pool(pool)


async def _persist_poll_snapshot(*, dsn: str, snapshot: JiraWorkSnapshot, plan_id: str) -> None:
    from whilly.adapters.db import close_pool, create_pool
    from whilly.adapters.db.repository import TaskRepository

    pool = await create_pool(dsn)
    try:
        repo = TaskRepository(pool)
        await persist_jira_work_snapshot(repo, snapshot, plan_id=plan_id)
    finally:
        await close_pool(pool)


_SSH_CLONE_RE = re.compile(r"^git@(?P<host>[^:]+):(?P<path>.+)$")
_REPO_KIND_LABELS = {
    "same": "same repo",
    "new": "new repo",
    "other": "other existing repo",
    "skip": "skip repo target",
}
_ACTION_LABELS = {
    "prd": "PRD/context first",
    "plan": "plan preflight",
    "run": "run autonomously now",
    "save": "save plan only",
}


def _resolve_intake_repo_choice(
    args: argparse.Namespace,
    *,
    prompt: Prompt,
    stdin_isatty: IsATTY,
    repo_detector: RepoDetector,
) -> IntakeRepoChoice:
    kind = str(args.repo_kind or "").strip().lower()
    repo_url = str(args.repo_url or "").strip()
    if not kind and repo_url:
        kind = "same"
    if not kind and stdin_isatty():
        _print_intake_repo_menu()
        kind = _parse_menu_choice(
            prompt("Repo target [1]: "),
            numbered={"1": "same", "2": "new", "3": "other", "4": "skip"},
            named=set(_REPO_KIND_LABELS),
            default="same",
        )
    if not kind:
        kind = "skip"
    if kind not in _REPO_KIND_LABELS:
        raise ValueError(f"unsupported repo kind {kind!r}")
    if kind == "skip":
        return IntakeRepoChoice(kind=kind, target=None)

    if not repo_url and kind == "same":
        repo_url = repo_detector().strip()
        if repo_url:
            print(f"whilly jira intake: detected current repo origin {repo_url}")
    if not repo_url:
        if not stdin_isatty():
            raise ValueError("repo URL is required for Jira intake; pass --repo-url or --repo-kind skip")
        repo_url = prompt(_repo_url_prompt(kind)).strip()
    if not repo_url:
        raise ValueError("repo URL cannot be empty")
    target = _repo_target_from_url(
        repo_url,
        provider_hint=str(args.repo_provider or ""),
        default_branch=str(args.default_branch or "main"),
    )
    return IntakeRepoChoice(kind=kind, target=target)


def _resolve_intake_action(args: argparse.Namespace, *, prompt: Prompt, stdin_isatty: IsATTY) -> str:
    action = str(args.intake_action or "").strip().lower()
    if not action and stdin_isatty():
        _print_intake_action_menu()
        action = _parse_menu_choice(
            prompt("Next step [1]: "),
            numbered={"1": "prd", "2": "plan", "3": "run", "4": "save"},
            named=set(_ACTION_LABELS),
            default="prd",
        )
    if not action:
        action = "save"
    if action not in _ACTION_LABELS:
        raise ValueError(f"unsupported intake action {action!r}")
    return action


def _print_intake_repo_menu() -> None:
    print("Which repo should this Jira task use?")
    print("  1. same repo (current checkout origin)")
    print("  2. new repo (paste the clone URL after creating it)")
    print("  3. other existing repo")
    print("  4. skip repo target")


def _print_intake_action_menu() -> None:
    print("What should Whilly do next?")
    print("  1. PRD/context first")
    print("  2. plan preflight")
    print("  3. run autonomously now")
    print("  4. save plan only")


def _parse_menu_choice(
    raw: str,
    *,
    numbered: Mapping[str, str],
    named: set[str],
    default: str,
) -> str:
    value = raw.strip().lower()
    if not value:
        return default
    if value in numbered:
        return numbered[value]
    if value in named:
        return value
    raise ValueError(f"unknown menu choice {raw!r}")


def _repo_url_prompt(kind: str) -> str:
    if kind == "same":
        return "Current repo clone URL: "
    if kind == "new":
        return "New repo clone URL: "
    return "Other repo clone URL: "


def _repo_target_from_url(repo_url: str, *, provider_hint: str, default_branch: str) -> dict[str, str]:
    clone_url = repo_url.strip()
    host, repo_full_name = _split_repo_url(clone_url)
    provider = _infer_repo_provider(host, provider_hint)
    return {
        "id": f"{provider}:{repo_full_name}",
        "provider": provider,
        "repo_full_name": repo_full_name,
        "clone_url": clone_url,
        "default_branch": default_branch.strip() or "main",
    }


def _split_repo_url(repo_url: str) -> tuple[str, str]:
    ssh_match = _SSH_CLONE_RE.match(repo_url)
    if ssh_match:
        host = ssh_match.group("host")
        path = ssh_match.group("path")
    else:
        parsed = urlparse(repo_url)
        if not parsed.scheme or not parsed.hostname or not parsed.path:
            raise ValueError(
                "repo URL must be a Git clone URL, for example "
                "git@gitlab.example:group/repo.git or https://github.com/owner/repo.git"
            )
        host = parsed.hostname
        path = parsed.path
    repo_full_name = _normalize_repo_path(path)
    if "/" not in repo_full_name:
        raise ValueError(f"repo URL must include owner/group and repo name: {repo_url!r}")
    return host, repo_full_name


def _normalize_repo_path(path: str) -> str:
    clean = path.strip().strip("/")
    if "/-/" in clean:
        clean = clean.split("/-/", 1)[0]
    if clean.endswith(".git"):
        clean = clean[:-4]
    return clean.strip("/")


def _infer_repo_provider(host: str, provider_hint: str) -> str:
    hint = provider_hint.strip().lower()
    if hint:
        return hint
    clean_host = host.lower()
    if "github" in clean_host:
        return "github"
    return "gitlab"


def _write_repo_target(plan_path: Path, target: dict[str, str]) -> str:
    data = _read_plan_object(plan_path)
    data["repo_targets"] = [target]
    for task in _plan_tasks(data):
        task["repo_target_id"] = target["id"]
    _write_plan_object(plan_path, data)
    return target["id"]


def _clear_repo_target(plan_path: Path) -> None:
    data = _read_plan_object(plan_path)
    data["repo_targets"] = []
    for task in _plan_tasks(data):
        task.pop("repo_target_id", None)
    _write_plan_object(plan_path, data)


def _write_jira_work_metadata(plan_path: Path, *, key: str, repo_path: Path | None) -> dict[str, Any]:
    data = _read_plan_object(plan_path)
    metadata = build_jira_work_metadata(data, issue_key=key, repo_path=repo_path)
    data["jira_work"] = metadata
    origin = data.get("origin")
    if isinstance(origin, dict):
        origin.setdefault("system", "jira_issue")
        origin.setdefault("ref", key)
        origin["content_hash"] = metadata["context_hashes"]["combined_hash"]
    else:
        data["origin"] = {
            "system": "jira_issue",
            "ref": key,
            "content_hash": metadata["context_hashes"]["combined_hash"],
        }
    _write_plan_object(plan_path, data)
    return metadata


def _read_jira_work_readiness(plan_path: Path) -> dict[str, Any] | None:
    data = _read_plan_object(plan_path)
    jira_work = data.get("jira_work")
    if not isinstance(jira_work, dict):
        return None
    readiness = jira_work.get("readiness")
    return readiness if isinstance(readiness, dict) else None


def _write_intake_context(
    plan_path: Path,
    *,
    key: str,
    plan_id: str,
    repo_choice: IntakeRepoChoice,
    context_out: Path | None,
) -> Path:
    data = _read_plan_object(plan_path)
    task = next(iter(_plan_tasks(data)), {})
    context_path = context_out or plan_path.with_name(f"jira-{key}-context.md")
    context_path.parent.mkdir(parents=True, exist_ok=True)
    repo_target_id = repo_choice.target["id"] if repo_choice.target else "none"
    clone_url = repo_choice.target.get("clone_url", "") if repo_choice.target else ""
    title = _context_value(task.get("title") or data.get("name") or data.get("plan_name") or f"Jira issue {key}")
    description = _context_value(task.get("description") or task.get("prd_requirement") or "")
    context = "\n".join(
        [
            f"# Jira {key} Context",
            "",
            f"- Plan file: {plan_path}",
            f"- Plan id: {plan_id}",
            f"- Repo mode: {_REPO_KIND_LABELS[repo_choice.kind]}",
            f"- Repo target: {repo_target_id}",
            f"- Clone URL: {clone_url or 'none'}",
            "",
            "## Jira Summary",
            "",
            title,
            "",
            "## Jira Description",
            "",
            description or "Add the missing task context here before planning.",
            "",
            "## Decisions To Make",
            "",
            "- Desired outcome",
            "- Scope boundaries",
            "- Target paths or modules",
            "- Verification command",
            "- Commit and PR destination",
            "",
        ]
    )
    context_path.write_text(context, encoding="utf-8")
    origin = data.get("origin")
    if isinstance(origin, dict):
        origin["prd_file"] = str(context_path)
    else:
        data["origin"] = {"system": "jira_issue", "ref": key, "prd_file": str(context_path)}
    _write_plan_object(plan_path, data)
    return context_path


def _context_value(value: Any) -> str:
    return str(value or "").strip()


def _read_plan_object(plan_path: Path) -> dict[str, Any]:
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{plan_path} must contain a JSON object")
    return data


def _write_plan_object(plan_path: Path, data: Mapping[str, Any]) -> None:
    plan_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _plan_tasks(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_tasks = data.get("tasks", [])
    if not isinstance(raw_tasks, list):
        raise ValueError("'tasks' must be a JSON array")
    tasks: list[dict[str, Any]] = []
    for index, raw_task in enumerate(raw_tasks):
        if not isinstance(raw_task, dict):
            raise ValueError(f"tasks[{index}] must be a JSON object")
        tasks.append(raw_task)
    return tasks


def _detect_current_repo_url() -> str:
    try:
        completed = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _ensure_jira_config(
    args: argparse.Namespace,
    *,
    config_reader: ConfigReader,
    env: MutableMapping[str, str],
    prompt: Prompt,
    secret_prompt: Prompt,
    browser_opener: BrowserOpener,
    stdin_isatty: IsATTY,
    command_label: str,
) -> int:
    state = _jira_config_state(config_reader(), env)
    missing = _missing_jira_settings(state)
    if not missing:
        return EXIT_OK

    interactive = bool(args.interactive_config) or (not bool(args.no_interactive_config) and stdin_isatty())
    if not interactive:
        _print_missing_jira_config(missing, command_label=command_label)
        return EXIT_VALIDATION_ERROR

    print(f"{command_label}: Jira config is incomplete; enter missing values.", file=sys.stderr)
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
                f"{command_label}: opening Jira Cloud API token page: {JIRA_CLOUD_API_TOKEN_URL}",
                file=sys.stderr,
            )
            try:
                browser_opener(JIRA_CLOUD_API_TOKEN_URL)
            except Exception as exc:
                print(f"{command_label}: could not open browser: {exc}", file=sys.stderr)
            token = secret_prompt("Jira API token / PAT: ").strip()
            if token:
                env["JIRA_API_TOKEN"] = token
    except EOFError:
        print(f"{command_label}: interactive Jira setup was interrupted before input completed.", file=sys.stderr)
        return EXIT_VALIDATION_ERROR

    state_after_prompt = _jira_config_state(config_reader(), env)
    missing_after_prompt = _missing_jira_settings(state_after_prompt)
    if missing_after_prompt:
        _print_missing_jira_config(missing_after_prompt, command_label=command_label)
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


def _print_missing_jira_config(missing: Sequence[str], *, command_label: str) -> None:
    print(f"{command_label}: Jira config is incomplete.", file=sys.stderr)
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


def _run_plan_command(argv: Sequence[str]) -> int:
    from whilly.cli.plan import run_plan_command

    return run_plan_command(argv)


__all__ = ["JIRA_CLOUD_API_TOKEN_URL", "build_jira_parser", "run_jira_command"]
