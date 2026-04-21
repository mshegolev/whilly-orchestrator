"""Handoff backend — delegate each task to a live Claude Code session over files.

Unlike :mod:`whilly.agents.claude` and :mod:`whilly.agents.opencode`, this backend
does **not** spawn a non-interactive LLM subprocess. Instead it writes the task
prompt to a well-known location inside the repo and blocks (as a tiny polling
subprocess so the existing orchestrator machinery stays unchanged) until a
result JSON file appears.

A human operator — or the Claude Code session the user is currently chatting
with — picks up the prompt, does the work **with full conversational context**,
and writes the result back via :func:`whilly --handoff-complete` / friends.

Intended for flows where:

* A per-task subprocess model is too opaque (the user wants to watch every step).
* Some tasks require human-in-the-loop judgement (``status = "human_loop"``).
* You need to hand off to an agent that already has deep context loaded.

Protocol — one directory per task under ``.whilly/handoff/<task-id>/``:

* ``prompt.md``    — written by whilly (the task prompt, verbatim).
* ``meta.json``    — task metadata (plan file, cwd, timeout, started_at, task_id).
* ``result.json``  — written by the operator / --handoff-* CLI when done.

Result JSON schema::

    {
      "status": "complete" | "failed" | "blocked" | "human_loop" | "partial",
      "message": "human-readable summary for logs / audio announcement",
      "duration_s": 42.5,            # optional
      "usage": {                     # optional — all fields optional
        "input_tokens":  0,
        "output_tokens": 0,
        "cost_usd":      0.0,
        "num_turns":     1
      }
    }

Select the backend via ``WHILLY_AGENT_BACKEND=claude_handoff`` or ``--agent claude_handoff``.
Enforces ``WHILLY_MAX_PARALLEL=1`` — handoff is inherently serial.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from whilly.agents.base import COMPLETION_MARKER, AgentResult, AgentUsage


DEFAULT_HANDOFF_DIR = ".whilly/handoff"
DEFAULT_TIMEOUT_SECONDS = 3600  # 1h — long enough for a real human-in-the-loop decision

# Mapping from result.json `status` → whilly internal task status.
# `partial` intentionally maps to `done` (agent reported progress but not complete);
# whilly's regular deadlock/retry machinery decides whether to re-dispatch.
_STATUS_TO_WHILLY: dict[str, str] = {
    "complete": "done",
    "failed": "failed",
    "blocked": "blocked",
    "human_loop": "human_loop",
    "partial": "done",
}


def handoff_root() -> Path:
    """Return the base directory where prompt/result files live.

    Honours the ``WHILLY_HANDOFF_DIR`` env var; falls back to ``.whilly/handoff``
    (created on demand by callers).
    """
    return Path(os.environ.get("WHILLY_HANDOFF_DIR") or DEFAULT_HANDOFF_DIR)


def task_dir_for(task_id: str) -> Path:
    """Path of the handoff directory for one task."""
    # Scrub characters that would escape the directory on Windows / case-insensitive FSes.
    safe_id = task_id.replace("/", "_").replace("\\", "_").replace(":", "_") or "unknown"
    return handoff_root() / safe_id


def _extract_task_id(prompt: str, log_file: Path | None) -> str:
    """Best-effort task id discovery used when dispatching.

    Looks for ``id: XYZ`` in the prompt header first (that's what
    ``build_task_prompt`` emits); falls back to the log_file stem's prefix so
    at minimum we get a directory name, even if it's approximate.
    """
    import re

    match = re.search(r"(?im)^\s*(?:id|task[_ ]?id)\s*[:=]\s*([\w.\-/]+)", prompt)
    if match:
        return match.group(1).strip()
    if log_file is not None:
        stem = log_file.stem
        # e.g. "GH-164_20260421_175000" → "GH-164"
        return stem.split("_", 1)[0] if "_" in stem else stem
    return time.strftime("unknown-%Y%m%d-%H%M%S")


def _write_prompt(task_id: str, prompt: str, *, plan_file: Path | None = None, cwd: Path | None = None) -> Path:
    """Write prompt.md + meta.json under the task's handoff directory. Returns the prompt path."""
    td = task_dir_for(task_id)
    td.mkdir(parents=True, exist_ok=True)
    prompt_path = td / "prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    meta = {
        "task_id": task_id,
        "plan_file": str(plan_file) if plan_file else None,
        "cwd": str(cwd) if cwd else None,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()),
        "timeout_s": int(os.environ.get("WHILLY_HANDOFF_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS))),
    }
    (td / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return prompt_path


def _parse_result(raw: str) -> tuple[str, AgentUsage, str]:
    """Parse a result.json payload into ``(text, usage, handoff_status)``.

    Always returns a valid triple — malformed / missing fields yield sensible
    defaults rather than raising, because orchestration should keep flowing
    when the handoff writer fat-fingers the JSON.
    """
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return raw or "", AgentUsage(), "failed"
    if not isinstance(data, dict):
        return str(data), AgentUsage(), "failed"

    message = str(data.get("message") or data.get("result_text") or "").strip()
    raw_status = str(data.get("status") or "").strip().lower()
    if raw_status not in _STATUS_TO_WHILLY:
        raw_status = "failed"

    # Normalise: mark 'complete' payloads with the completion marker so the
    # orchestrator's stock completion check works unchanged.
    if raw_status == "complete" and COMPLETION_MARKER not in message:
        message = f"{message}\n\n{COMPLETION_MARKER}" if message else COMPLETION_MARKER

    usage_dict = data.get("usage") or {}
    usage = AgentUsage(
        input_tokens=int(usage_dict.get("input_tokens") or 0),
        output_tokens=int(usage_dict.get("output_tokens") or 0),
        cache_read_tokens=int(usage_dict.get("cache_read_tokens") or 0),
        cache_create_tokens=int(usage_dict.get("cache_create_tokens") or 0),
        cost_usd=float(usage_dict.get("cost_usd") or 0.0),
        num_turns=int(usage_dict.get("num_turns") or 1),
        duration_ms=int((data.get("duration_s") or 0) * 1000),
    )
    return message, usage, raw_status


def handoff_status_to_whilly(raw_status: str) -> str:
    """Return the whilly TaskManager status for a result.json ``status`` value."""
    return _STATUS_TO_WHILLY.get((raw_status or "").strip().lower(), "failed")


class ClaudeHandoffBackend:
    """Backend that serialises task dispatch through the filesystem.

    Implements the :class:`whilly.agents.base.AgentBackend` Protocol so it
    drops into every existing whilly runner unchanged.
    """

    name = "claude_handoff"

    def default_model(self) -> str:
        # Model id is advisory only for this backend — no subprocess actually uses it.
        return os.environ.get("WHILLY_MODEL", "claude-handoff")

    def normalize_model(self, model: str) -> str:
        return model or self.default_model()

    def build_command(self, prompt: str, model: str | None = None, *, safe_mode: bool | None = None) -> list[str]:
        """Return the argv of the polling subprocess that waits for result.json."""
        del model, safe_mode  # unused
        return [sys.executable, "-c", _POLLING_SCRIPT]

    def parse_output(self, raw: str) -> tuple[str, AgentUsage]:
        text, usage, _status = _parse_result(raw)
        return text, usage

    def is_complete(self, text: str) -> bool:
        return COMPLETION_MARKER in (text or "")

    # ── Sync path ────────────────────────────────────────────────────────────

    def run(
        self,
        prompt: str,
        model: str | None = None,
        timeout: int | None = None,
        cwd: Path | None = None,
    ) -> AgentResult:
        del model  # unused — handoff doesn't care about the model id
        task_id = _extract_task_id(prompt, None)
        _write_prompt(task_id, prompt, cwd=cwd)
        result_path = task_dir_for(task_id) / "result.json"
        # `timeout=0` must mean "no wait at all" — fall back to the default only
        # when the caller passed ``None``. Otherwise a caller explicitly asking
        # for a zero deadline would get 1h by mistake (or spin forever when
        # time.sleep is patched out in tests).
        deadline = time.time() + (timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS)
        start = time.monotonic()
        _announce_dispatch(task_id)
        while time.time() < deadline:
            if result_path.is_file():
                raw = result_path.read_text(encoding="utf-8")
                text, usage, _status = _parse_result(raw)
                return AgentResult(
                    result_text=text,
                    usage=usage,
                    exit_code=0,
                    duration_s=time.monotonic() - start,
                    is_complete=self.is_complete(text),
                )
            time.sleep(1)
        return AgentResult(
            result_text=f"Handoff timed out after {timeout}s waiting for {result_path}",
            exit_code=124,
            duration_s=time.monotonic() - start,
            is_complete=False,
        )

    # ── Async path (fits the existing Popen-returning interface) ─────────────

    def run_async(
        self,
        prompt: str,
        model: str | None = None,
        log_file: Path | None = None,
        cwd: Path | None = None,
    ) -> subprocess.Popen:
        """Dispatch the task and return a Popen that exits when result.json is ready.

        The returned subprocess tails ``result.json`` for up to
        ``WHILLY_HANDOFF_TIMEOUT`` seconds. When the file appears it copies the
        contents to *log_file* and exits 0; on timeout it exits 124.
        """
        del model
        task_id = _extract_task_id(prompt, log_file)
        _write_prompt(task_id, prompt, cwd=cwd)
        result_path = task_dir_for(task_id) / "result.json"
        timeout_s = int(os.environ.get("WHILLY_HANDOFF_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS)))

        if log_file is not None:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            stdout_target = open(log_file, "w", encoding="utf-8")  # noqa: SIM115
            preamble = (
                "# whilly handoff preamble\n"
                f"# task_id     : {task_id}\n"
                f"# prompt_file : {task_dir_for(task_id) / 'prompt.md'}\n"
                f"# result_file : {result_path}\n"
                f"# timeout_s   : {timeout_s}\n"
                f"# note        : blocks until result.json appears — write with `whilly --handoff-complete`.\n"
                "# ---\n"
            )
            stdout_target.write(preamble)
            stdout_target.flush()
        else:
            stdout_target = subprocess.PIPE

        _announce_dispatch(task_id)

        env = dict(os.environ)
        env["WHILLY_HANDOFF_RESULT_PATH"] = str(result_path)
        env["WHILLY_HANDOFF_TIMEOUT"] = str(timeout_s)
        return subprocess.Popen(
            self.build_command(prompt),
            stdout=stdout_target,
            stderr=subprocess.DEVNULL,
            cwd=str(cwd) if cwd else None,
            env=env,
        )

    def collect_result(
        self,
        proc: subprocess.Popen,
        log_file: Path | None = None,
        start_time: float = 0,
    ) -> AgentResult:
        proc.wait()
        if log_file and log_file.is_file():
            return self.collect_result_from_file(log_file, start_time=start_time)
        return AgentResult(
            result_text=f"handoff polling subprocess exited {proc.returncode} without log",
            exit_code=proc.returncode or 1,
            duration_s=(time.monotonic() - start_time) if start_time else 0.0,
            is_complete=False,
        )

    def collect_result_from_file(self, log_file: Path, start_time: float = 0) -> AgentResult:
        try:
            raw = log_file.read_text(encoding="utf-8")
        except OSError:
            return AgentResult(exit_code=1, is_complete=False)
        # Strip the preamble block to get the result.json payload.
        body_lines: list[str] = []
        past_preamble = False
        for line in raw.splitlines():
            if not past_preamble:
                if line.strip() == "# ---":
                    past_preamble = True
                    continue
                if line.startswith("#") or not line.strip():
                    continue
                past_preamble = True  # no preamble at all — fall through
            body_lines.append(line)
        body = "\n".join(body_lines).strip()
        text, usage, _status = _parse_result(body or raw)
        return AgentResult(
            result_text=text,
            usage=usage,
            exit_code=0 if body else 1,
            duration_s=(time.monotonic() - start_time) if start_time else 0.0,
            is_complete=self.is_complete(text),
        )


# Polling script used as the Popen argv. Kept as a module constant so tests can
# inspect it directly without spawning a subprocess.
_POLLING_SCRIPT = r"""
import os, sys, time
from pathlib import Path
path = Path(os.environ["WHILLY_HANDOFF_RESULT_PATH"])
deadline = time.time() + int(os.environ.get("WHILLY_HANDOFF_TIMEOUT", "3600"))
while time.time() < deadline:
    if path.is_file():
        try:
            sys.stdout.write(path.read_text(encoding="utf-8"))
            sys.stdout.flush()
        except OSError:
            sys.exit(2)
        sys.exit(0)
    time.sleep(1)
sys.exit(124)
"""


def _announce_dispatch(task_id: str) -> None:
    """Print a visible banner so the interactive Claude (or a human) notices the handoff."""
    td = task_dir_for(task_id)
    print(
        f"\n📨 Handoff dispatched — task {task_id}\n"
        f"   prompt : {td / 'prompt.md'}\n"
        f"   meta   : {td / 'meta.json'}\n"
        f"   reply  : {td / 'result.json'}\n"
        f"   finish : whilly --handoff-complete {task_id} --message '…'\n",
        file=sys.stderr,
        flush=True,
    )


# ── Helpers for the --handoff-* CLI commands ─────────────────────────────────


def list_pending() -> list[dict]:
    """Return sorted metadata dicts for every task currently awaiting a result."""
    root = handoff_root()
    if not root.is_dir():
        return []
    out: list[dict] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        prompt = entry / "prompt.md"
        result = entry / "result.json"
        if not prompt.is_file() or result.is_file():
            continue
        meta = {}
        meta_file = entry / "meta.json"
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        out.append(
            {
                "task_id": meta.get("task_id") or entry.name,
                "started_at": meta.get("started_at"),
                "plan_file": meta.get("plan_file"),
                "prompt": str(prompt),
                "result": str(result),
            }
        )
    return out


def write_result(
    task_id: str,
    *,
    status: str,
    message: str = "",
    cost_usd: float = 0.0,
    num_turns: int = 1,
    duration_s: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> Path:
    """Serialise a result.json for *task_id*. Returns the written path."""
    raw_status = (status or "").strip().lower()
    if raw_status not in _STATUS_TO_WHILLY:
        raise ValueError(f"Unknown handoff status {status!r}. Expected one of: " + ", ".join(sorted(_STATUS_TO_WHILLY)))
    td = task_dir_for(task_id)
    if not td.is_dir():
        raise FileNotFoundError(
            f"No pending handoff for task {task_id!r} (directory {td} does not exist). "
            "Was this task actually dispatched by whilly?"
        )
    payload = {
        "status": raw_status,
        "message": message,
        "duration_s": duration_s,
        "usage": asdict(
            AgentUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                num_turns=num_turns,
                duration_ms=int(duration_s * 1000),
            )
        ),
    }
    result_path = td / "result.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return result_path


__all__ = [
    "ClaudeHandoffBackend",
    "DEFAULT_HANDOFF_DIR",
    "DEFAULT_TIMEOUT_SECONDS",
    "handoff_root",
    "handoff_status_to_whilly",
    "list_pending",
    "task_dir_for",
    "write_result",
]
