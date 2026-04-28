"""Claude CLI backend (the original whilly agent backend).

Wraps ``claude --output-format stream-json --verbose -p "<prompt>"`` and parses
the JSONL stream into :class:`AgentResult`. Each line of stdout is one JSON
event (system/init, assistant deltas, tool_use, rate_limit_event, final result),
so ``tail -f`` shows live progress instead of waiting for one big blob at the end.

The final ``{"type":"result"...}`` event carries the same shape that the legacy
``--output-format json`` produced, so the parser still extracts ``result``,
``total_cost_usd`` and ``usage`` from a single record. ``parse_output`` also
accepts a single JSON object as a fallback so existing ``collect_result_from_file``
callers and tests keep working.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

from whilly.agents.base import AgentResult, AgentUsage, COMPLETION_MARKER, spawn_with_eagain_retry

log = logging.getLogger("whilly")


DEFAULT_MODEL = "claude-opus-4-6[1m]"
DEFAULT_BIN = "claude"


class ClaudeBackend:
    """Claude Code CLI subprocess wrapper.

    Configuration via env:
        CLAUDE_BIN            override the binary path (default: ``claude``)
        WHILLY_CLAUDE_SAFE    when truthy, use ``--permission-mode acceptEdits``
                              instead of ``--dangerously-skip-permissions``
    """

    name = "claude"

    # ── Tool resolution ────────────────────────────────────────────────────

    def _claude_bin(self) -> str:
        return os.environ.get("CLAUDE_BIN") or DEFAULT_BIN

    def _permission_args(self, safe_mode: bool | None = None) -> list[str]:
        """Return the permission-related CLI args.

        Defaults to ``--dangerously-skip-permissions`` so Bash/test commands
        run fully autonomously. Set ``safe_mode=True`` (or
        ``WHILLY_CLAUDE_SAFE=1``) to revert to ``--permission-mode acceptEdits``
        (requires an attached TTY).
        """
        if safe_mode is None:
            safe_mode = os.environ.get("WHILLY_CLAUDE_SAFE") in ("1", "true", "yes")
        if safe_mode:
            return ["--permission-mode", "acceptEdits"]
        return ["--dangerously-skip-permissions"]

    # ── Protocol surface ───────────────────────────────────────────────────

    def default_model(self) -> str:
        return os.environ.get("WHILLY_MODEL", DEFAULT_MODEL)

    def normalize_model(self, model: str) -> str:
        """Claude CLI accepts bare ids (``claude-opus-4-6[1m]``) — no mapping."""
        return model

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        *,
        safe_mode: bool | None = None,
    ) -> list[str]:
        resolved = self.normalize_model(model or self.default_model())
        # stream-json + --verbose: Claude CLI writes JSONL incrementally so
        # ``tail -f {task_id}.log`` shows live progress. ``--verbose`` is
        # required by the CLI when stream-json is paired with ``-p``.
        return [
            self._claude_bin(),
            *self._permission_args(safe_mode=safe_mode),
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            resolved,
            "-p",
            prompt,
        ]

    def is_complete(self, text: str) -> bool:
        return COMPLETION_MARKER in (text or "")

    # ── Parsing ────────────────────────────────────────────────────────────

    def parse_output(self, raw: str) -> tuple[str, AgentUsage]:
        """Parse stdout from Claude CLI into ``(result_text, usage)``.

        Two accepted shapes:

        * **stream-json** (current default): JSONL with one JSON object per
          line — ``system``/``init``, ``assistant`` messages with usage,
          ``tool_use``, ``rate_limit_event``, and a final ``result`` event
          carrying the full summary. We pick the last ``type=="result"`` record.
        * **single object** (legacy ``--output-format json``): one JSON object
          with ``result``/``total_cost_usd``/``usage`` at the top level.

        Falls back to ``(raw, AgentUsage())`` when both parses fail — this
        matches the prior contract for unparseable subprocess output.
        """
        if not raw:
            return "", AgentUsage()

        result_obj = self._extract_result_record(raw)
        if result_obj is None:
            return raw, AgentUsage()

        result_text = result_obj.get("result", "") or ""
        usage_data = result_obj.get("usage") or {}
        usage = AgentUsage(
            input_tokens=usage_data.get("input_tokens", 0) or 0,
            output_tokens=usage_data.get("output_tokens", 0) or 0,
            cache_read_tokens=usage_data.get("cache_read_input_tokens", 0) or 0,
            cache_create_tokens=usage_data.get("cache_creation_input_tokens", 0) or 0,
            cost_usd=result_obj.get("total_cost_usd", 0.0) or 0.0,
            num_turns=result_obj.get("num_turns", 0) or 0,
            duration_ms=result_obj.get("duration_ms", 0) or 0,
        )
        return result_text, usage

    @staticmethod
    def _extract_result_record(raw: str) -> dict | None:
        """Return the result-bearing dict from raw stdout, or ``None``.

        Tries (1) JSONL stream — last line with ``type == "result"``,
        (2) single JSON object as a fallback. Returning ``None`` signals the
        caller to fall back to the raw text.
        """
        # Fast path: legacy single-object JSON. Try first because it's cheap.
        stripped = raw.strip()
        if stripped.startswith("{") and stripped.endswith("}") and "\n" not in stripped:
            try:
                obj = json.loads(stripped)
            except (json.JSONDecodeError, TypeError):
                return None
            if isinstance(obj, dict):
                return obj
            return None

        # Stream-json path: scan from the end so we hit the final result fast.
        lines = [line for line in raw.splitlines() if line.strip()]
        for line in reversed(lines):
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(obj, dict) and obj.get("type") == "result":
                return obj

        # Some recorded transcripts only have a single line that *is* the result
        # but lacks ``type``. Try once more, top-down.
        for line in lines:
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(obj, dict) and "result" in obj:
                return obj

        return None

    # ── Runners ────────────────────────────────────────────────────────────

    def run(
        self,
        prompt: str,
        model: str | None = None,
        timeout: int | None = None,
        cwd: Path | None = None,
    ) -> AgentResult:
        """Run Claude CLI synchronously and return the parsed result.

        Returns an AgentResult with ``exit_code=-1`` on timeout and
        ``exit_code=-2`` when the binary cannot be found — never raises.
        """
        start = time.monotonic()
        cmd = self.build_command(prompt, model=model)
        try:
            proc = spawn_with_eagain_retry(
                lambda: subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=str(cwd) if cwd else None,
                    check=False,
                )
            )
        except subprocess.TimeoutExpired:
            return AgentResult(
                exit_code=-1,
                duration_s=time.monotonic() - start,
                result_text="TIMEOUT",
            )
        except FileNotFoundError:
            return AgentResult(
                exit_code=-2,
                duration_s=time.monotonic() - start,
                result_text=f"{self._claude_bin()} CLI not found",
            )
        except BlockingIOError as e:
            return AgentResult(
                exit_code=-3,
                duration_s=time.monotonic() - start,
                result_text=f"spawn EAGAIN after retries: {e}",
            )

        duration = time.monotonic() - start
        result = AgentResult(exit_code=proc.returncode, duration_s=duration)
        raw = proc.stdout or proc.stderr or ""
        result.result_text, result.usage = self.parse_output(raw)
        if not result.result_text and raw:
            result.result_text = raw
        result.is_complete = self.is_complete(result.result_text)
        return result

    def run_async(
        self,
        prompt: str,
        model: str | None = None,
        log_file: Path | None = None,
        cwd: Path | None = None,
    ) -> subprocess.Popen:
        """Start Claude CLI in the background and return the Popen handle.

        Writes a preamble block (timestamp, cwd, model, cmd shape) into
        *log_file* BEFORE spawning the subprocess so a ``tail -f`` or the
        dashboard ``l`` hotkey shows something immediately. With stream-json
        format Claude CLI also writes JSONL events live — events appear in the
        log as soon as the model produces them, not only at the end.
        """
        cmd = self.build_command(prompt, model=model)

        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            # Force UTF-8 — Windows defaults to cp1252 and trips on Cyrillic/emoji in the preamble.
            stdout_target = open(log_file, "w", encoding="utf-8")  # noqa: SIM115
            preamble = (
                "# whilly agent preamble\n"
                f"# timestamp : {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"# backend   : {self.name}\n"
                f"# model     : {model or self.default_model()}\n"
                f"# cwd       : {cwd or 'inherited'}\n"
                f"# cmd       : {' '.join(cmd[:2])} ... -p <prompt {len(prompt)} chars>\n"
                "# note      : claude --output-format stream-json пишет JSONL events live (tail -f работает).\n"
                "# ---\n"
            )
            stdout_target.write(preamble)
            stdout_target.flush()
        else:
            stdout_target = subprocess.PIPE

        return spawn_with_eagain_retry(
            lambda: subprocess.Popen(
                cmd,
                stdout=stdout_target,
                stderr=subprocess.STDOUT,
                cwd=str(cwd) if cwd else None,
            )
        )

    def collect_result(
        self,
        proc: subprocess.Popen,
        log_file: Path | None = None,
        start_time: float = 0,
    ) -> AgentResult:
        """Collect result from a finished Popen process."""
        duration = time.monotonic() - start_time if start_time else 0
        result = AgentResult(exit_code=proc.returncode or 0, duration_s=duration)

        if log_file and log_file.exists():
            raw = log_file.read_text(encoding="utf-8", errors="replace")
        elif proc.stdout and hasattr(proc.stdout, "read"):
            try:
                raw = proc.stdout.read() or ""
            except Exception:  # noqa: BLE001
                raw = ""
        else:
            raw = ""

        result.result_text, result.usage = self.parse_output(raw)
        if not result.result_text and raw:
            result.result_text = raw
        result.is_complete = self.is_complete(result.result_text)
        return result

    def collect_result_from_file(
        self,
        log_file: Path,
        start_time: float = 0,
    ) -> AgentResult:
        """Read AgentResult from a log file produced by tmux/subprocess wrapper."""
        duration = time.monotonic() - start_time if start_time else 0
        result = AgentResult(exit_code=0, duration_s=duration)

        if not log_file.exists():
            result.exit_code = -1
            result.result_text = "Log file not found"
            return result

        raw = log_file.read_text(encoding="utf-8", errors="replace")

        # The tmux wrapper appends ``EXIT_CODE=N`` on the last few lines.
        for line in reversed(raw.splitlines()[-5:]):
            if line.startswith("EXIT_CODE="):
                try:
                    result.exit_code = int(line.split("=", 1)[1])
                except ValueError:
                    pass
                break

        result.result_text, result.usage = self.parse_output(raw)
        if not result.result_text and raw:
            result.result_text = raw
        result.is_complete = self.is_complete(result.result_text)
        return result
