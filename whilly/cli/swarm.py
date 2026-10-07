"""``whilly swarm`` — opt-in local multi-project swarm.

Subcommands::

    whilly swarm registry validate --registry PATH
    whilly swarm new       --registry PATH [--title T]
    whilly swarm chat      (--session ID | --registry PATH) [-m TEXT] [--plan]
    whilly swarm propose   --session ID --plan-file FILE
    whilly swarm run       --session ID [--revision N | --latest] [--workers N] [--kill-orphans]
    whilly swarm resume    --session ID [--workers N] [--kill-orphans]
    whilly swarm rerun     --session ID --task LOCAL_ID
    whilly swarm status    --session ID [--json]
    whilly swarm sessions  [--json]
    whilly swarm stop      --session ID [--kill]
    whilly swarm message   --session ID --from SENDER --to RECIPIENT [--task LOCAL_ID] TEXT
    whilly swarm inbox     --session ID --recipient NAME [--all] [--ack ID ...] [--json]
    whilly swarm report    --session ID [--json]

Discussion (``chat``) never starts work. Only ``run`` (or ``/run`` inside an
interactive chat) imports an explicitly chosen plan revision and executes it.

Exit codes: ``0`` success, ``1`` tasks failed or blocked, ``2`` usage /
environment / validation error, ``4`` conflict (live coordinator, orphaned
agent processes, stale or busy revision).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

__all__ = ["build_swarm_parser", "run_swarm_command"]

DATABASE_URL_ENV = "WHILLY_DATABASE_URL"
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_CONFLICT = 4


class _UsageError(Exception):
    pass


def build_swarm_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="whilly swarm", description="Local multi-project agent swarm.")
    sub = parser.add_subparsers(dest="command", required=True)

    registry = sub.add_parser("registry", help="Registry utilities.")
    registry_sub = registry.add_subparsers(dest="registry_command", required=True)
    validate = registry_sub.add_parser("validate", help="Validate a registry JSON file.")
    validate.add_argument("--registry", required=True)
    validate.add_argument("--json", action="store_true")

    new = sub.add_parser("new", help="Create a persistent session.")
    new.add_argument("--registry", required=True)
    new.add_argument("--title", default="")

    chat = sub.add_parser("chat", help="Discuss and plan with the read-only planner.")
    target = chat.add_mutually_exclusive_group(required=True)
    target.add_argument("--session")
    target.add_argument("--registry", help="Start a new session from this registry.")
    chat.add_argument("-m", "--message", help="One-shot message instead of interactive chat.")
    chat.add_argument("--plan", action="store_true", help="Ask the planner for a JSON plan revision now.")
    chat.add_argument("--title", default="")

    propose = sub.add_parser("propose", help="Propose a plan revision from a JSON file.")
    propose.add_argument("--session", required=True)
    propose.add_argument("--plan-file", required=True)

    for name, help_text in (
        ("run", "Apply a revision (optional) and run the coordinator."),
        ("resume", "Resume the applied revision."),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--session", required=True)
        cmd.add_argument("--workers", type=int, default=None, help="Parallel workers (1-8, default from registry).")
        cmd.add_argument(
            "--kill-orphans",
            action="store_true",
            help="Recovery compatibility flag; unverified process identities are never killed.",
        )
        if name == "run":
            which = cmd.add_mutually_exclusive_group()
            which.add_argument("--revision", type=int)
            which.add_argument("--latest", action="store_true", help="Apply the latest proposed revision.")

    rerun = sub.add_parser("rerun", help="Explicitly return a FAILED task to the queue.")
    rerun.add_argument("--session", required=True)
    rerun.add_argument("--task", required=True)

    for name in ("status", "report"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--session", required=True)
        cmd.add_argument("--json", action="store_true")

    sessions = sub.add_parser("sessions", help="List sessions.")
    sessions.add_argument("--json", action="store_true")

    stop = sub.add_parser("stop", help="Request a graceful coordinator stop.")
    stop.add_argument("--session", required=True)
    stop.add_argument(
        "--kill", action="store_true", help="Request crash cleanup; refuses unverified process identities."
    )

    message = sub.add_parser("message", help="Send an addressed message.")
    message.add_argument("--session", required=True)
    message.add_argument("--from", dest="sender", required=True)
    message.add_argument("--to", dest="recipient", required=True)
    message.add_argument("--task", dest="task_ref")
    message.add_argument("body")

    inbox = sub.add_parser("inbox", help="Read and acknowledge addressed messages.")
    inbox.add_argument("--session", required=True)
    inbox.add_argument("--recipient", required=True)
    inbox.add_argument("--all", action="store_true", help="Include acknowledged messages.")
    inbox.add_argument("--ack", type=int, nargs="+", default=[])
    inbox.add_argument("--json", action="store_true")
    collaboration = sub.add_parser("collaborate", help="Data-only cross-session coordinator mailbox.")
    operation = collaboration.add_mutually_exclusive_group(required=True)
    operation.add_argument("--inbox", action="store_true")
    operation.add_argument("--request", help="Bounded JSON send/ack request; never an execution command.")
    proposal = sub.add_parser("propose-task", help="Propose neighboring work through the local coordinator only.")
    proposal.add_argument("--request", required=True, help="Bounded JSON proposal; not execution approval.")
    return parser


def run_swarm_command(argv: Sequence[str]) -> int:
    parser = build_swarm_parser()
    args = parser.parse_args(list(argv))
    if args.command == "registry":
        return _registry_validate(args)
    if args.command in {"collaborate", "propose-task"}:
        return _data_mailbox_command(args)
    if os.environ.get("WHILLY_SWARM_MAILBOX") and args.command in {"message", "inbox"}:
        return _mailbox_command(args)
    dsn = os.environ.get(DATABASE_URL_ENV)
    if not dsn:
        sys.stderr.write(f"whilly swarm: {DATABASE_URL_ENV} is not set — point it at the swarm's Postgres.\n")
        return EXIT_USAGE
    if getattr(args, "workers", None) is not None and not 1 <= args.workers <= 8:
        sys.stderr.write("whilly swarm: --workers must be between 1 and 8\n")
        return EXIT_USAGE
    return asyncio.run(_dispatch(args, dsn))


def _data_mailbox_command(args: argparse.Namespace) -> int:
    from whilly.swarm.mailbox import MAX_BYTES, enqueue

    mailbox = os.environ.get("WHILLY_SWARM_MAILBOX")
    if not mailbox:
        sys.stderr.write("whilly swarm: coordinator mailbox required\n")
        return EXIT_USAGE
    try:
        proposing = args.command == "propose-task"
        channel = "proposals" if proposing else "collaboration"
        allowed = {"propose"} if proposing else {"send", "ack"}
        root = Path(mailbox) / channel
        inbox = json.loads((root / ("status.json" if proposing else "inbox.json")).read_text())
        if getattr(args, "inbox", False):
            _print_json(inbox)
            return EXIT_OK
        if inbox.get("blocker"):
            raise ValueError(str(inbox["blocker"]))
        if len(args.request.encode()) > MAX_BYTES:
            raise ValueError("request too large")
        request = json.loads(args.request)
        if not isinstance(request, dict) or request.get("op") not in allowed:
            raise ValueError(f"only {'/'.join(sorted(allowed))} data requests are accepted")
        nonce = enqueue(root, request)
        print(f"queued locally: {nonce}; durable receipt will be in {channel}/receipts/{nonce}.json")
        return EXIT_OK
    except (OSError, ValueError, KeyError) as exc:
        sys.stderr.write(f"whilly swarm {args.command}: {exc}\n")
        return EXIT_USAGE


def _mailbox_command(args: argparse.Namespace) -> int:
    """No network or database credentials are needed inside a worker sandbox."""
    from whilly.swarm.mailbox import enqueue, read_inbox

    root = Path(os.environ["WHILLY_SWARM_MAILBOX"])
    try:
        recipient = args.sender if args.command == "message" else args.recipient
        messages = read_inbox(root, args.session, recipient, include_acked=getattr(args, "all", False))
        if args.command == "message":
            nonce = enqueue(
                root,
                dict(op="send", sender=recipient, recipient=args.recipient, body=args.body, task_ref=args.task_ref),
            )
            print(f"queued locally: {nonce}; coordinator receipt will confirm delivery")
        elif args.ack:
            nonce = enqueue(root, dict(op="ack", sender=recipient, ids=args.ack))
            print(f"ack queued locally: {nonce}")
        elif args.json:
            _print_json(messages)
        else:
            for message in messages:
                print(f"#{message['id']} from {message['sender']}: {message['body']}")
            if not messages:
                print("(no messages)")
        return EXIT_OK
    except (OSError, ValueError, KeyError) as exc:
        sys.stderr.write(f"whilly swarm mailbox: {exc}\n")
        return EXIT_USAGE


def _registry_validate(args: argparse.Namespace) -> int:
    from whilly.swarm.registry import RegistryError, load_registry

    try:
        registry = load_registry(args.registry)
    except RegistryError as exc:
        if args.json:
            print(json.dumps({"valid": False, "problems": exc.problems}, indent=2))
        else:
            sys.stderr.write("invalid registry:\n" + "\n".join(f"  - {p}" for p in exc.problems) + "\n")
        return EXIT_USAGE
    info = {
        "valid": True,
        "name": registry.name,
        "projects": {
            pid: {"path": p.path, "base_ref": p.base_ref, "bare": p.bare} for pid, p in registry.projects.items()
        },
        "roles": {rid: list(r.projects) for rid, r in registry.roles.items()},
        "state_dir": str(registry.resolved_state_dir()),
    }
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        print(f"registry OK: {len(registry.projects)} project(s), {len(registry.roles)} role(s)")
        for pid, project in info["projects"].items():
            print(f"  {pid}: {project['path']} @ {project['base_ref']}{' (bare)' if project['bare'] else ''}")
    return EXIT_OK


async def _dispatch(args: argparse.Namespace, dsn: str) -> int:
    from whilly.adapters.db.pool import close_pool, create_pool
    from whilly.swarm.plan import PlanError
    from whilly.swarm.registry import RegistryError
    from whilly.swarm.runtime import OrphanProcessError, SwarmService
    from whilly.swarm.store import CoordinatorActiveError, RevisionConflictError, SwarmStoreError

    pool = await create_pool(dsn, min_size=1, max_size=10)
    try:
        service = SwarmService(pool)
        handler = _HANDLERS[args.command]
        return await handler(service, args)
    except RegistryError as exc:
        sys.stderr.write("whilly swarm: invalid registry:\n" + "\n".join(f"  - {p}" for p in exc.problems) + "\n")
        return EXIT_USAGE
    except (CoordinatorActiveError, OrphanProcessError, RevisionConflictError) as exc:
        sys.stderr.write(f"whilly swarm: {exc}\n")
        return EXIT_CONFLICT
    except (PlanError, SwarmStoreError, _UsageError) as exc:
        sys.stderr.write(f"whilly swarm: {exc}\n")
        return EXIT_USAGE
    finally:
        await close_pool(pool)


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


async def _cmd_new(service, args) -> int:
    session_id = await service.create_session(args.registry, title=args.title)
    print(session_id)
    return EXIT_OK


def _print_reply(reply) -> None:
    if reply.reply:
        print(reply.reply.rstrip())
    if reply.revision is not None:
        state = reply.revision_status
        print(f"\n[revision {reply.revision}: {state}]")
    if reply.error:
        print(f"[error] {reply.error}", file=sys.stderr)


async def _cmd_chat(service, args) -> int:
    session_id = args.session
    if session_id is None:
        session_id = await service.create_session(args.registry, title=args.title)
        print(f"[session {session_id}]")
    else:
        await service.require_session(session_id)
    if args.message is not None:
        reply = await service.chat(session_id, args.message, request_plan=args.plan)
        _print_reply(reply)
        return (
            EXIT_USAGE
            if reply.error and reply.revision_status != "invalid"
            else (EXIT_FAILED if reply.error else EXIT_OK)
        )
    print("Interactive swarm chat. Commands: /plan [text], /run [revision], /status, /quit")
    while True:
        try:
            line = await asyncio.to_thread(input, "you> ")
        except EOFError:
            return EXIT_OK
        line = line.strip()
        if not line:
            continue
        if line in {"/quit", "/exit"}:
            return EXIT_OK
        if line == "/status":
            _print_status(await service.status(session_id))
            continue
        if line.startswith("/run"):
            parts = line.split()
            revision = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            await _apply_and_run(service, session_id, revision=revision, apply=True, workers=None, kill_orphans=False)
            continue
        request_plan = line.startswith("/plan")
        text = line[len("/plan") :].strip() if request_plan else line
        reply = await service.chat(session_id, text or "Produce the plan now.", request_plan=request_plan)
        _print_reply(reply)


async def _cmd_propose(service, args) -> int:
    path = Path(args.plan_file)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _UsageError(f"cannot read plan file {str(path)!r}: {exc}") from exc
    revision, status, error = await service.propose_plan(args.session, data, raw_text=path.read_text(), source="file")
    print(f"revision {revision}: {status}")
    if error:
        print(error, file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK


async def _apply_and_run(service, session_id, *, revision, apply, workers, kill_orphans) -> int:
    from whilly.swarm.runtime import Coordinator

    if apply:
        applied, task_ids = await service.apply_revision(session_id, revision)
        print(f"applied revision {applied}: {len(task_ids)} task(s) queued")
    coordinator = Coordinator(service, session_id, max_parallel=workers, kill_orphans=kill_orphans)
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, coordinator.request_stop, f"signal {sig.name}")
            installed.append(sig)
        except (NotImplementedError, RuntimeError):  # pragma: no cover — non-main thread / platform
            pass
    try:
        summary = await coordinator.run()
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
    print(f"session {session_id}: {summary.status}")
    for item in summary.accepted:
        print(f"  accepted: {item}")
    for item in summary.failed:
        print(f"  failed:   {item}")
    for item in summary.blockers:
        print(f"  blocker:  {item}")
    print(f"  report: whilly swarm report --session {session_id}")
    return EXIT_OK if summary.ok else EXIT_FAILED


async def _cmd_run(service, args) -> int:
    session = await service.require_session(args.session)
    apply = args.revision is not None or args.latest
    if not apply and session["applied_revision"] is None:
        raise _UsageError("no revision applied yet; pass --revision N or --latest to approve one")
    return await _apply_and_run(
        service,
        args.session,
        revision=args.revision,
        apply=apply,
        workers=args.workers,
        kill_orphans=args.kill_orphans,
    )


async def _cmd_resume(service, args) -> int:
    session = await service.require_session(args.session)
    if session["applied_revision"] is None:
        raise _UsageError("no revision applied yet; use `whilly swarm run --latest`")
    return await _apply_and_run(
        service, args.session, revision=None, apply=False, workers=args.workers, kill_orphans=args.kill_orphans
    )


async def _cmd_rerun(service, args) -> int:
    task_id = await service.store.rerun_task(args.session, args.task)
    print(f"{task_id} returned to PENDING; run `whilly swarm resume --session {args.session}`")
    return EXIT_OK


def _print_status(status: dict[str, Any]) -> None:
    session = status["session"]
    live = "live" if session["coordinator_live"] else "none"
    print(
        f"session {session['id']} [{session['status']}] plan={session['plan_id']} "
        f"applied_revision={session['applied_revision']} coordinator={live}"
    )
    for rev in status["revisions"]:
        error = f" — {rev['error']}" if rev["error"] else ""
        print(f"  revision {rev['revision']}: {rev['status']} ({rev['source']}, {rev['tasks']} task(s)){error}")
    for task in status["tasks"]:
        extra = task["blocker"] or task["outcome"] or ""
        print(
            f"  r{task['revision']} {task['task']:<24} {task['status']:<11} "
            f"{task['project']}/{task['role']} attempts={task['attempts']}/{task['max_attempts']} {extra}".rstrip()
        )
    if status["dashboard"]:
        print(f"  dashboard: {status['dashboard']}")


async def _cmd_status(service, args) -> int:
    status = await service.status(args.session)
    if args.json:
        _print_json(status)
    else:
        _print_status(status)
    return EXIT_OK


async def _cmd_report(service, args) -> int:
    report = await service.report(args.session)
    if args.json:
        _print_json(report)
    else:
        summary = report["summary"]
        print(f"session {report['session']['id']} revision {report['session']['applied_revision']}")
        print(f"  accepted: {', '.join(summary['accepted']) or '-'}")
        print(f"  failed: {', '.join(summary['failed']) or '-'}")
        print(f"  not finished: {', '.join(summary['not_finished']) or '-'}")
        total_cost = summary["total_cost_usd"]
        print(f"  total cost (USD): {total_cost if total_cost is not None else 'unknown (engine did not report cost)'}")
        for blocker in report["blockers"]:
            print(f"  blocker: {blocker}")
        for attempt in report["attempts"]:
            print(
                f"  {attempt['task']} a{attempt['attempt']} {attempt['status']}: branch {attempt['branch']} "
                f"head {attempt['head_sha'] or '-'} review {attempt['review_verdict'] or '-'} logs {attempt['logs']}"
            )
        if report.get("report_path"):
            print(f"  report file: {report['report_path']}")
        for note in report["notes"]:
            print(f"  note: {note}")
    return EXIT_OK if not report["blockers"] else EXIT_FAILED


async def _cmd_sessions(service, args) -> int:
    sessions = await service.store.list_sessions()
    if args.json:
        _print_json(sessions)
        return EXIT_OK
    for session in sessions:
        live = " live" if session["coordinator_live"] else ""
        print(f"{session['id']} [{session['status']}{live}] r{session['applied_revision']} {session['title']}")
    return EXIT_OK


async def _cmd_stop(service, args) -> int:
    result = await service.stop(args.session, kill=args.kill)
    if result["coordinator_live"]:
        print(f"stop requested for {args.session}; the coordinator cancels agents at its next heartbeat")
    else:
        print(f"no live coordinator for {args.session}; session marked stopped")
    if result["killed_process_groups"]:
        print(f"killed process groups: {result['killed_process_groups']}")
    return EXIT_OK


async def _cmd_message(service, args) -> int:
    message_id = await service.store.send_message(
        args.session, sender=args.sender, recipient=args.recipient, body=args.body, task_ref=args.task_ref
    )
    print(f"message {message_id} queued for {args.recipient}")
    return EXIT_OK


async def _cmd_inbox(service, args) -> int:
    if args.ack:
        acked = await service.store.ack_messages(args.session, args.recipient, args.ack)
        print(f"acknowledged: {acked}")
        return EXIT_OK
    messages = await service.store.inbox(args.session, args.recipient, include_acked=args.all)
    await service.store.mark_delivered([m["id"] for m in messages], session_id=args.session, recipient=args.recipient)
    if args.json:
        _print_json(messages)
        return EXIT_OK
    if not messages:
        print("(no messages)")
    for msg in messages:
        acked = " (acked)" if msg["acked_at"] else ""
        task = f" [task {msg['task_ref']}]" if msg["task_ref"] else ""
        print(f"#{msg['id']} from {msg['sender']}{task}{acked}: {msg['body']}")
    return EXIT_OK


_HANDLERS = {
    "new": _cmd_new,
    "chat": _cmd_chat,
    "propose": _cmd_propose,
    "run": _cmd_run,
    "resume": _cmd_resume,
    "rerun": _cmd_rerun,
    "status": _cmd_status,
    "report": _cmd_report,
    "sessions": _cmd_sessions,
    "stop": _cmd_stop,
    "message": _cmd_message,
    "inbox": _cmd_inbox,
}
