"""Fail-closed macOS sandbox-exec adapter with bounded process transport."""

from __future__ import annotations

import asyncio
import inspect
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path

from whilly.core.swarm_execution import ExecutionBlocked, ExecutionPolicy, SandboxResult

__all__ = ["SandboxResult", "probe_sandbox", "run_sandboxed", "run_sandboxed_sync", "sandbox_argv"]

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
TERMINATE_GRACE_SECONDS = 0.5
PUMP_DRAIN_SECONDS = 0.5
PUMP_CHUNK_BYTES = 64 * 1024


def _path_is_within(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath((str(child), str(parent))) == str(parent)
    except ValueError:
        return False


def _canonical_roots(
    policy: ExecutionPolicy,
) -> tuple[tuple[Path, ...], tuple[Path, ...], tuple[Path, ...], tuple[Path, ...]]:
    reads = tuple(Path(root).resolve(strict=False) for root in policy.read_roots)
    writes = tuple(Path(root).resolve(strict=False) for root in policy.write_roots)
    denied = tuple(Path(root).resolve(strict=False) for root in policy.denied_roots)
    protected = tuple(Path(root).resolve(strict=False) for root in policy.protected_write_roots)
    for allowed_root in (*reads, *writes):
        for denied_root in denied:
            if _path_is_within(allowed_root, denied_root) or _path_is_within(denied_root, allowed_root):
                raise ExecutionBlocked("policy_root_overlap")
    for protected_root in protected:
        if not any(_path_is_within(protected_root, write_root) for write_root in writes):
            raise ExecutionBlocked("protected_write_root_outside_write")
    return reads, writes, denied, protected


def _profile_string(path: Path) -> str:
    value = str(path)
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _profile(policy: ExecutionPolicy, executable: Path) -> str:
    reads, writes, denied, protected = _canonical_roots(policy)
    lines = [
        "(version 1)",
        '(import "bsd.sb")',
        "(deny default)",
        "(allow process-fork)",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        f"(allow process-exec (literal {_profile_string(executable.resolve(strict=False))}))",
    ]
    for root in denied:
        literal = _profile_string(root)
        lines.extend((f"(deny file-read* (subpath {literal}))", f"(deny file-write* (subpath {literal}))"))
    for root in sorted(set(reads) | set(writes), key=str):
        lines.append(f"(allow file-read* (subpath {_profile_string(root)}))")
    for root in sorted(set(reads) | set(writes), key=str):
        lines.append(f"(allow process-exec (subpath {_profile_string(root)}))")
    for root in writes:
        lines.append(f"(allow file-write* (subpath {_profile_string(root)}))")
    for root in protected:
        lines.append(f"(deny file-write* (subpath {_profile_string(root)}))")
    if policy.network:
        lines.append("(allow network-outbound)")
        if policy.phase != "publication":
            lines.append("(allow network-inbound)")
        # Native TLS clients (codex/reqwest via Security.framework) validate
        # certificates through these Mach services; without them every HTTPS
        # request fails ("workspace routing discovery failed"). Only network
        # phases get them; verify/git stay fully closed.
        lines.append(
            '(allow mach-lookup (global-name "com.apple.trustd") '
            '(global-name "com.apple.trustd.agent") (global-name "com.apple.SecurityServer"))'
        )
    return "\n".join(lines)


def _resolve_executable(argv0: str, *, cwd: Path, environment: Mapping[str, str], policy: ExecutionPolicy) -> str:
    if not argv0:
        raise ValueError("argv must start with a non-empty executable")
    if Path(argv0).is_absolute():
        candidate = Path(argv0)
    elif "/" in argv0:
        candidate = cwd / argv0
    else:
        if "PATH" not in environment:
            raise ExecutionBlocked("executable_path_unavailable")
        candidate = None
        for entry in environment["PATH"].split(os.pathsep):
            base = Path(entry) if entry else cwd
            if not base.is_absolute():
                base = cwd / base
            option = base / argv0
            if option.is_file() and os.access(option, os.X_OK):
                candidate = option
                break
        if candidate is None:
            raise ExecutionBlocked("executable_not_found")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ExecutionBlocked("executable_not_found") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ExecutionBlocked("executable_not_found")
    reads, _, _, _ = _canonical_roots(policy)
    if not any(_path_is_within(resolved, root) for root in reads):
        raise ExecutionBlocked("executable_outside_policy")
    return str(resolved)


def sandbox_argv(argv: tuple[str, ...], policy: ExecutionPolicy) -> tuple[str, ...]:
    """Wrap an absolute, policy-approved argv in the macOS profile."""
    if sys.platform != "darwin" or not Path(SANDBOX_EXEC).is_file():
        raise ExecutionBlocked("execution_isolation_unavailable")
    if (
        not argv
        or not isinstance(argv[0], str)
        or not argv[0]
        or not all(isinstance(argument, str) for argument in argv)
    ):
        raise ValueError("argv must be a non-empty tuple of strings")
    if any("\x00" in argument for argument in argv):
        raise ValueError("argv contains NUL")
    if not Path(argv[0]).is_absolute():
        raise ValueError("sandbox_argv requires an absolute executable")
    executable = Path(argv[0]).resolve(strict=True)
    _resolve_executable(str(executable), cwd=Path.cwd(), environment={"PATH": ""}, policy=policy)
    return (SANDBOX_EXEC, "-p", _profile(policy, executable), str(executable), *argv[1:])


def _kill_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def _stop_group(proc: asyncio.subprocess.Process, pgid: int) -> None:
    _kill_group(pgid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=TERMINATE_GRACE_SECONDS)
    except asyncio.TimeoutError:
        pass
    _kill_group(pgid, signal.SIGKILL)
    try:
        await asyncio.wait_for(proc.wait(), timeout=TERMINATE_GRACE_SECONDS)
    except asyncio.TimeoutError:
        pass


async def _pump(stream: asyncio.StreamReader, destination: object, limit: int, overflow: asyncio.Event) -> None:
    written = 0
    while True:
        chunk = await stream.read(PUMP_CHUNK_BYTES)
        if not chunk:
            return
        remaining = limit - written
        if remaining <= 0:
            overflow.set()
            return
        destination.write(chunk[:remaining])  # type: ignore[attr-defined]
        destination.flush()  # type: ignore[attr-defined]
        written += min(len(chunk), remaining)
        if len(chunk) > remaining:
            overflow.set()
            return


async def _drain_pumps(pump_tasks: set[asyncio.Task[None]]) -> None:
    if not pump_tasks:
        return
    done, pending = await asyncio.wait(pump_tasks, timeout=PUMP_DRAIN_SECONDS)
    for task in pending:
        task.cancel()
    await asyncio.gather(*done, *pending, return_exceptions=True)


def _close_process_transport(process: asyncio.subprocess.Process) -> None:
    transport = getattr(process, "_transport", None)
    if transport is not None:
        transport.close()


async def _cleanup_attempt(
    process: asyncio.subprocess.Process,
    pgid: int,
    pump_tasks: set[asyncio.Task[None]],
    waiter_tasks: set[asyncio.Task[object]],
) -> None:
    await asyncio.shield(_stop_group(process, pgid))
    for task in waiter_tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*waiter_tasks, return_exceptions=True)
    await _drain_pumps(pump_tasks)
    _close_process_transport(process)


def _is_async_callback(callback: Callable[..., object]) -> bool:
    return inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(getattr(callback, "__call__", None))


async def _invoke_callback(callback: Callable[[int], Awaitable[None]], pgid: int) -> None:
    await callback(pgid)


async def run_sandboxed(
    argv: tuple[str, ...],
    *,
    policy: ExecutionPolicy,
    cwd: str | Path,
    environment: Mapping[str, str],
    stdout_path: str | Path,
    stderr_path: str | Path,
    stdin_path: str | Path | None = None,
    on_start: Callable[[int], Awaitable[None]] | None = None,
) -> SandboxResult:
    """Run one bounded sandbox attempt, including callback and pipe cleanup."""
    started = time.monotonic()
    stdout_file = Path(stdout_path)
    stderr_file = Path(stderr_path)
    stdout_file.parent.mkdir(parents=True, exist_ok=True)
    stderr_file.parent.mkdir(parents=True, exist_ok=True)
    backend = "macos-sandbox-exec"
    if on_start is not None and not _is_async_callback(on_start):
        return SandboxResult(
            None, "on_start_unsupported", False, False, 0.0, str(stdout_file), str(stderr_file), backend
        )
    try:
        executable = _resolve_executable(argv[0] if argv else "", cwd=Path(cwd), environment=environment, policy=policy)
        guarded_argv = sandbox_argv((executable, *argv[1:]), policy)
    except ExecutionBlocked as exc:
        return SandboxResult(None, exc.reason, False, False, 0.0, str(stdout_file), str(stderr_file), backend)
    except (OSError, ValueError) as exc:
        return SandboxResult(
            None, f"spawn_failed:{type(exc).__name__}", False, False, 0.0, str(stdout_file), str(stderr_file), backend
        )

    stdin_handle = open(stdin_path, "rb") if stdin_path is not None else None  # noqa: SIM115
    process: asyncio.subprocess.Process | None = None
    pump_tasks: set[asyncio.Task[None]] = set()
    waiter_tasks: set[asyncio.Task[object]] = set()
    try:
        with stdout_file.open("wb") as stdout_handle, stderr_file.open("wb") as stderr_handle:
            try:
                process = await asyncio.create_subprocess_exec(
                    *guarded_argv,
                    cwd=str(cwd),
                    env=dict(environment),
                    stdin=stdin_handle if stdin_handle is not None else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except FileNotFoundError:
                return SandboxResult(
                    None,
                    "executable_not_found",
                    False,
                    False,
                    time.monotonic() - started,
                    str(stdout_file),
                    str(stderr_file),
                    backend,
                )
            except OSError as exc:
                return SandboxResult(
                    None,
                    f"spawn_failed:{type(exc).__name__}",
                    False,
                    False,
                    time.monotonic() - started,
                    str(stdout_file),
                    str(stderr_file),
                    backend,
                )

            assert process.stdout is not None and process.stderr is not None
            overflow = asyncio.Event()
            pump_tasks = {
                asyncio.create_task(_pump(process.stdout, stdout_handle, policy.max_output_bytes, overflow)),
                asyncio.create_task(_pump(process.stderr, stderr_handle, policy.max_output_bytes, overflow)),
            }
            pgid = process.pid
            process_wait_task = asyncio.create_task(process.wait())
            overflow_wait_task = asyncio.create_task(overflow.wait())
            waiter_tasks = {process_wait_task, overflow_wait_task}
            callback_task: asyncio.Task[object] | None = None
            callback_pending = on_start is not None
            if on_start is not None:
                callback_task = asyncio.create_task(_invoke_callback(on_start, pgid))
                waiter_tasks.add(callback_task)
            deadline = asyncio.get_running_loop().time() + policy.timeout_seconds
            reason: str | None = None
            timed_out = False
            try:
                while True:
                    if callback_task is not None and callback_task.done():
                        try:
                            callback_task.result()
                        except asyncio.CancelledError:
                            raise
                        except BaseException as exc:
                            reason = f"on_start_failed:{type(exc).__name__}"
                            waiter_tasks.discard(callback_task)
                            callback_pending = False
                            break
                        waiter_tasks.discard(callback_task)
                        callback_pending = False
                    if overflow.is_set():
                        reason = "output_limit_exceeded"
                        break
                    # ``Process.wait`` may remain pending while a descendant holds
                    # inherited pipes; the leader returncode is the completion
                    # signal needed to enter descendant cleanup.
                    if process.returncode is not None and not callback_pending:
                        break
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        reason = "timeout"
                        timed_out = True
                        break
                    done, _ = await asyncio.wait(
                        waiter_tasks, timeout=min(remaining, 0.05), return_when=asyncio.FIRST_COMPLETED
                    )
                    if not done and asyncio.get_running_loop().time() >= deadline:
                        reason = "timeout"
                        timed_out = True
                        break
                await _cleanup_attempt(process, pgid, pump_tasks, waiter_tasks)
                if reason is None and overflow.is_set():
                    reason = "output_limit_exceeded"
                return SandboxResult(
                    process.returncode,
                    reason,
                    timed_out,
                    False,
                    time.monotonic() - started,
                    str(stdout_file),
                    str(stderr_file),
                    backend,
                )
            except asyncio.CancelledError:
                await _cleanup_attempt(process, pgid, pump_tasks, waiter_tasks)
                return SandboxResult(
                    process.returncode,
                    "cancelled",
                    False,
                    True,
                    time.monotonic() - started,
                    str(stdout_file),
                    str(stderr_file),
                    backend,
                )
            except BaseException:
                await _cleanup_attempt(process, pgid, pump_tasks, waiter_tasks)
                raise
    finally:
        if stdin_handle is not None:
            stdin_handle.close()


def run_sandboxed_sync(
    argv: tuple[str, ...],
    *,
    policy: ExecutionPolicy,
    cwd: str | Path,
    environment: Mapping[str, str],
    stdout_path: str | Path,
    stderr_path: str | Path,
    stdin_path: str | Path | None = None,
    on_start: Callable[[int], Awaitable[None]] | None = None,
) -> SandboxResult:
    """Drive the async adapter in a private event loop for sync callers."""
    return asyncio.run(
        run_sandboxed(
            argv,
            policy=policy,
            cwd=cwd,
            environment=environment,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdin_path=stdin_path,
            on_start=on_start,
        )
    )


def _completed(result: SandboxResult) -> bool:
    return result.reason is None and isinstance(result.exit_code, int) and not result.timed_out and not result.cancelled


def _succeeded(result: SandboxResult, expected_stdout: str | None = None) -> bool:
    if not _completed(result) or result.exit_code != 0:
        return False
    if expected_stdout is None:
        return True
    try:
        return Path(result.stdout_path).read_text(encoding="utf-8") == expected_stdout
    except OSError:
        return False


def _denied(result: SandboxResult) -> bool:
    return _completed(result) and result.exit_code != 0


def _safe_probe_fixture(fixture_root: Path) -> Path:
    root = Path(fixture_root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ExecutionBlocked("probe_fixture_unsafe")
    resolved = root.resolve(strict=True)
    fresh = Path(tempfile.mkdtemp(prefix=".task3-probe-", dir=str(resolved)))
    if fresh.is_symlink() or not _path_is_within(fresh.resolve(strict=True), resolved):
        raise ExecutionBlocked("probe_fixture_unsafe")
    return fresh


def _probe_result(root: Path, name: str, policy: ExecutionPolicy, probe_argv: tuple[str, ...]) -> SandboxResult:
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    return run_sandboxed_sync(
        probe_argv,
        policy=policy,
        cwd=root / "allowed",
        environment={"PATH": "/bin:/usr/bin", "HOME": str(root / "output"), "TMPDIR": str(root / "output")},
        stdout_path=logs / f"{name}.out",
        stderr_path=logs / f"{name}.err",
    )


def probe_sandbox(policy: ExecutionPolicy, fixture_root: Path) -> dict:
    """Exercise sandbox denial using only a fresh fixture owned by the caller."""
    backend = "macos-sandbox-exec"
    if sys.platform != "darwin" or not Path(SANDBOX_EXEC).is_file():
        return {"available": False, "backend": backend, "reason": "execution_isolation_unavailable"}
    try:
        root = _safe_probe_fixture(fixture_root)
    except ExecutionBlocked as exc:
        return {"available": False, "backend": backend, "reason": exc.reason}
    try:
        targets = {
            "allowed": root / "allowed",
            "output": root / "output",
            "secret": root / "secret",
            "metadata": root / "output" / ".git",
            "ordinary": root / "allowed" / "ordinary.txt",
            "secret_file": root / "secret" / "sentinel.txt",
            "head": root / "output" / ".git" / "HEAD",
            "remove_positive": root / "output" / "remove-positive.txt",
            "rename_positive_source": root / "output" / "rename-positive.txt",
            "rename_positive_dest": root / "output" / "rename-positive.done",
        }
        root_resolved = root.resolve(strict=True)
        if any(not _path_is_within(path.resolve(strict=False), root_resolved) for path in targets.values()):
            return {"available": False, "backend": backend, "reason": "probe_fixture_target_unsafe"}
        try:
            probe_policy = replace(
                policy,
                read_roots=(str(targets["allowed"]), str(targets["output"]), *policy.read_roots),
                write_roots=(str(targets["output"]),),
                denied_roots=(str(targets["secret"]),),
                protected_write_roots=(str(targets["metadata"]),),
            )
            _canonical_roots(probe_policy)
        except (ExecutionBlocked, ValueError) as exc:
            return {"available": False, "backend": backend, "reason": f"probe_policy_invalid:{type(exc).__name__}"}
        targets["allowed"].mkdir()
        targets["output"].mkdir()
        targets["secret"].mkdir()
        targets["metadata"].mkdir()
        targets["ordinary"].write_text("ordinary", encoding="utf-8")
        targets["secret_file"].write_text("synthetic-secret", encoding="utf-8")
        targets["head"].write_text("protected", encoding="utf-8")
        targets["remove_positive"].write_text("remove", encoding="utf-8")
        targets["rename_positive_source"].write_text("rename", encoding="utf-8")
        sibling = targets["output"] / "sibling-source.txt"
        sibling.write_text("source", encoding="utf-8")
        control_read = targets["secret_file"].read_text(encoding="utf-8") == "synthetic-secret"
        targets["secret_file"].write_text("synthetic-secret-control", encoding="utf-8")
        control_write = targets["secret_file"].read_text(encoding="utf-8") == "synthetic-secret-control"
        targets["secret_file"].write_text("synthetic-secret", encoding="utf-8")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        port = listener.getsockname()[1]
        try:
            network_argv = ("/usr/bin/nc", "-z", "-w", "1", "127.0.0.1", str(port))
            try:
                control_network = (
                    subprocess.run(
                        network_argv,
                        cwd=str(targets["allowed"]),
                        env={"PATH": "/bin:/usr/bin", "HOME": str(targets["output"]), "TMPDIR": str(targets["output"])},
                        capture_output=True,
                        timeout=1,
                        check=False,
                    ).returncode
                    == 0
                )
            except (OSError, subprocess.TimeoutExpired):
                control_network = False
            if not control_network:
                return {
                    "available": False,
                    "backend": backend,
                    "reason": "execution_isolation_unavailable",
                    "controls": {"outside_read": control_read, "outside_write": control_write, "network": False},
                    "failed_checks": ["network_control"],
                }
            checks: dict[str, bool] = {}
            checks["allowed_read"] = _succeeded(
                _probe_result(root, "allowed-read", probe_policy, ("/bin/cat", str(targets["ordinary"]))), "ordinary"
            )
            checks["outside_read_denied"] = _denied(
                _probe_result(root, "outside-read", probe_policy, ("/bin/cat", str(targets["secret_file"])))
            )
            checks["sibling_write_allowed"] = (
                _succeeded(
                    _probe_result(
                        root,
                        "sibling-write",
                        probe_policy,
                        ("/bin/sh", "-c", f"printf edited > {shlex.quote(str(sibling))}"),
                    )
                )
                and sibling.read_text(encoding="utf-8") == "edited"
            )
            checks["outside_write_denied"] = _denied(
                _probe_result(
                    root,
                    "outside-write",
                    probe_policy,
                    ("/bin/sh", "-c", f"printf blocked > {shlex.quote(str(targets['secret_file']))}"),
                )
            )
            child_write = f"printf child > {shlex.quote(str(targets['secret_file']))}"
            checks["grandchild_write_denied"] = _denied(
                _probe_result(
                    root,
                    "grandchild-write",
                    probe_policy,
                    (
                        "/bin/sh",
                        "-c",
                        f"/bin/sh -c {shlex.quote(child_write)}",
                    ),
                )
            )
            if probe_policy.network:
                checks["network_allowed"] = _succeeded(_probe_result(root, "network", probe_policy, network_argv))
            else:
                checks["network_denied"] = _denied(_probe_result(root, "network", probe_policy, network_argv))
            checks["protected_metadata_write_denied"] = (
                _denied(
                    _probe_result(
                        root,
                        "metadata-write",
                        probe_policy,
                        ("/bin/sh", "-c", f"printf changed > {shlex.quote(str(targets['head']))}"),
                    )
                )
                and targets["head"].read_text(encoding="utf-8") == "protected"
            )
            unlink_positive = _probe_result(
                root, "remove-positive", probe_policy, ("/bin/rm", str(targets["remove_positive"]))
            )
            checks["remove_positive"] = _succeeded(unlink_positive) and not targets["remove_positive"].exists()
            checks["protected_metadata_unlink_denied"] = (
                _denied(_probe_result(root, "metadata-unlink", probe_policy, ("/bin/rm", str(targets["head"]))))
                and targets["head"].exists()
            )
            renamed = targets["metadata"] / "HEAD.renamed"
            rename_positive = _probe_result(
                root,
                "rename-positive",
                probe_policy,
                ("/bin/mv", str(targets["rename_positive_source"]), str(targets["rename_positive_dest"])),
            )
            checks["rename_positive"] = (
                _succeeded(rename_positive) and targets["rename_positive_dest"].read_text(encoding="utf-8") == "rename"
            )
            checks["protected_metadata_rename_denied"] = (
                _denied(
                    _probe_result(
                        root, "metadata-rename", probe_policy, ("/bin/mv", str(targets["head"]), str(renamed))
                    )
                )
                and targets["head"].exists()
                and not renamed.exists()
            )
        finally:
            listener.close()
        failed = sorted(name for name, passed in checks.items() if not passed)
        available = not failed and control_read and control_write and control_network
        return {
            "available": available,
            "backend": backend,
            "reason": None if available else "execution_isolation_unavailable",
            "controls": {"outside_read": control_read, "outside_write": control_write, "network": control_network},
            "checks": checks,
            "failed_checks": failed,
            "policy_digest": probe_policy.digest(),
        }
    except BaseException as exc:
        return {
            "available": False,
            "backend": backend,
            "reason": "execution_isolation_unavailable",
            "error": type(exc).__name__,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)
