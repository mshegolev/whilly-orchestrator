"""Claude CLI backend (the original whilly agent backend).

Wraps ``claude --output-format json -p "<prompt>"`` and parses the final JSON
summary into :class:`AgentResult`. Extracted into a dedicated class so
OpenCode and future backends can live beside it behind the same Protocol.

Matches the behaviour previously inlined into ``whilly.agent_runner``; that
module is kept as a compat shim so existing imports continue to work.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

from whilly.agents.base import AgentResult, AgentUsage, COMPLETION_MARKER

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
        return [
            self._claude_bin(),
            *self._permission_args(safe_mode=safe_mode),
            "--output-format",
            "json",
            "--model",
            resolved,
            "-p",
            prompt,
        ]

    def is_complete(self, text: str) -> bool:
        return COMPLETION_MARKER in (text or "")

    # ── Parsing ────────────────────────────────────────────────────────────

    def parse_output(self, raw: str) -> tuple[str, AgentUsage]:
        """Parse the final JSON summary from Claude CLI.

        Expected shape (abridged)::

            {
              "result": "...",
              "total_cost_usd": 0.0042,
              "num_turns": 3,
              "duration_ms": 12345,
              "usage": {
                 "input_tokens": 100,
                 "output_tokens": 50,
                 "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 0
              }
            }

        Malformed input falls back to an empty AgentUsage + the raw text.
        """
        if not raw:
            return "", AgentUsage()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw, AgentUsage()

        result_text = data.get("result", "") or ""
        usage_data = data.get("usage") or {}
        usage = AgentUsage(
            input_tokens=usage_data.get("input_tokens", 0) or 0,
            output_tokens=usage_data.get("output_tokens", 0) or 0,
            cache_read_tokens=usage_data.get("cache_read_input_tokens", 0) or 0,
            cache_create_tokens=usage_data.get("cache_creation_input_tokens", 0) or 0,
            cost_usd=data.get("total_cost_usd", 0.0) or 0.0,
            num_turns=data.get("num_turns", 0) or 0,
            duration_ms=data.get("duration_ms", 0) or 0,
        )
        return result_text, usage

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
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(cwd) if cwd else None,
                check=False,
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
        dashboard ``l`` hotkey shows something immediately. Claude CLI only
        writes its big JSON at the end (``--output-format json``).
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
                "# note      : claude --output-format json пишет результат в КОНЦЕ.\n"
                "# ---\n"
            )
            stdout_target.write(preamble)
            stdout_target.flush()
        else:
            stdout_target = subprocess.PIPE

        return subprocess.Popen(
            cmd,
            stdout=stdout_target,
            stderr=subprocess.STDOUT,
            cwd=str(cwd) if cwd else None,
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
