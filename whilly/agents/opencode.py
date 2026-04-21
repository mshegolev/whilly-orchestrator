"""OpenCode CLI backend (``sst/opencode``).

Wraps ``opencode run --format json --model <provider>/<model> "<prompt>"`` and
extracts an :class:`AgentResult` from whichever JSON shape the CLI version at
hand emits.

OpenCode's ``--format json`` output is **event-stream-shaped** (a sequence of
JSON objects, one per line OR a single top-level array, depending on version)
rather than a single summary object like Claude CLI returns. The parser here
is intentionally defensive — see ``parse_output`` for the full set of shapes
we handle and ``tests/test_agent_backend_opencode.py`` for examples.

Configuration via env:

    WHILLY_OPENCODE_BIN     override the binary path (default: ``opencode``)
    WHILLY_OPENCODE_SAFE    truthy → omit ``--dangerously-skip-permissions``
                            so the CLI's per-tool permission policy applies
                            (requires ``.opencode/opencode.json`` setup)
    WHILLY_MODEL            model id; if missing a provider prefix (e.g.
                            ``claude-opus-4-6``) it's auto-prefixed with
                            ``anthropic/``
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

from whilly.agents.base import AgentResult, AgentUsage, COMPLETION_MARKER

log = logging.getLogger("whilly")


DEFAULT_MODEL = "anthropic/claude-opus-4-6"
DEFAULT_BIN = "opencode"

# Heuristic provider prefixes — auto-applied to bare model ids.
_PROVIDER_BY_PREFIX: list[tuple[str, str]] = [
    ("claude", "anthropic"),
    ("gpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("gemini", "google"),
    ("llama", "meta"),
    ("mistral", "mistral"),
    ("deepseek", "deepseek"),
    ("qwen", "qwen"),
]


class OpenCodeBackend:
    """OpenCode (``sst/opencode``) CLI subprocess wrapper.

    Designed to be drop-in compatible with :class:`whilly.agents.claude.ClaudeBackend`
    behind the :class:`AgentBackend` Protocol — same return shapes, same
    completion-marker contract.
    """

    name = "opencode"

    # ── Tool resolution ────────────────────────────────────────────────────

    def _opencode_bin(self) -> str:
        return os.environ.get("WHILLY_OPENCODE_BIN") or DEFAULT_BIN

    def _permission_args(self, safe_mode: bool | None = None) -> list[str]:
        """Return the permission-related CLI args.

        Defaults to ``--dangerously-skip-permissions`` (matches Claude flow).
        Set ``WHILLY_OPENCODE_SAFE=1`` (or pass ``safe_mode=True``) to omit
        the flag — then OpenCode falls back to whatever ``.opencode/opencode.json``
        in the project (or ``~/.config/opencode/opencode.json`` globally) defines.
        """
        if safe_mode is None:
            safe_mode = os.environ.get("WHILLY_OPENCODE_SAFE") in ("1", "true", "yes")
        if safe_mode:
            return []
        return ["--dangerously-skip-permissions"]

    # ── Model id handling ──────────────────────────────────────────────────

    def default_model(self) -> str:
        env_model = os.environ.get("WHILLY_MODEL")
        if env_model:
            return self.normalize_model(env_model)
        return DEFAULT_MODEL

    def normalize_model(self, model: str) -> str:
        """Ensure ``provider/model`` form for OpenCode.

        Already-prefixed ids (``anthropic/claude-...``) pass through. Bare ids
        whose prefix matches a known provider get auto-prefixed. Unknown
        bare ids are returned unchanged — let OpenCode complain rather than
        silently rewriting to the wrong provider.
        """
        if not model:
            return DEFAULT_MODEL
        m = model.strip()
        if "/" in m:
            return m
        # Strip any [-bracketed] suffix Claude uses (e.g. "[1m]") — OpenCode
        # doesn't recognise it and it confuses the registry lookup.
        bare = m.split("[", 1)[0]
        lower = bare.lower()
        for prefix, provider in _PROVIDER_BY_PREFIX:
            if lower.startswith(prefix):
                return f"{provider}/{bare}"
        return bare

    # ── Command building ───────────────────────────────────────────────────

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        *,
        safe_mode: bool | None = None,
    ) -> list[str]:
        resolved = self.normalize_model(model or self.default_model())
        return [
            self._opencode_bin(),
            "run",
            *self._permission_args(safe_mode=safe_mode),
            "--format",
            "json",
            "--model",
            resolved,
            prompt,
        ]

    def is_complete(self, text: str) -> bool:
        return COMPLETION_MARKER in (text or "")

    # ── Output parsing ─────────────────────────────────────────────────────

    def parse_output(self, raw: str) -> tuple[str, AgentUsage]:
        """Best-effort parse of OpenCode's ``--format json`` output.

        OpenCode versions vary; we try, in order:

        1. **Top-level JSON object** with ``result``/``output``/``text`` and
           ``usage`` / ``cost`` fields (Claude-like single summary).
        2. **Top-level JSON array** of events.
        3. **NDJSON / line-delimited events** — one JSON object per non-blank line.
        4. **Mixed plaintext + embedded JSON blobs** — fall back to extracting
           any ``{"type":"...","text":"..."}`` blocks.

        Across all shapes we accumulate text into a single string and sum
        any cost / token fields we recognise. Missing fields default to 0.
        """
        if not raw:
            return "", AgentUsage()

        text_chunks: list[str] = []
        usage = AgentUsage()
        cost_seen = False

        events = self._extract_events(raw)
        if events:
            for ev in events:
                self._merge_event(ev, text_chunks, usage_box := [usage])
                usage = usage_box[0]
                if usage.cost_usd:
                    cost_seen = True
        else:
            # Nothing JSON-shaped at all → return raw text, empty usage.
            return raw.strip(), AgentUsage()

        result_text = self._join_chunks(text_chunks) or self._fallback_text(raw)
        if not cost_seen:
            log.debug("opencode parse: cost_usd not present in output, defaulting to 0.0")
        return result_text, usage

    # ── Internal: shape detection ──────────────────────────────────────────

    @staticmethod
    def _extract_events(raw: str) -> list[dict]:
        """Return a list of dict events found in *raw*, or [] if none."""
        s = raw.strip()
        if not s:
            return []

        # 1) try a single top-level JSON value
        try:
            obj = json.loads(s)
            if isinstance(obj, list):
                return [e for e in obj if isinstance(e, dict)]
            if isinstance(obj, dict):
                return [obj]
        except json.JSONDecodeError:
            pass

        # 2) try NDJSON — one object per non-blank line
        events: list[dict] = []
        for line in s.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    events.append(obj)
                elif isinstance(obj, list):
                    events.extend(e for e in obj if isinstance(e, dict))
            except json.JSONDecodeError:
                continue
        if events:
            return events

        # 3) try to dig embedded {...} blobs out of mixed text
        for blob in re.findall(r"\{[^{}]*\"(?:type|text|result|output)\"\s*:[^{}]*\}", s):
            try:
                obj = json.loads(blob)
                if isinstance(obj, dict):
                    events.append(obj)
            except json.JSONDecodeError:
                continue
        return events

    # ── Internal: per-event merging ────────────────────────────────────────

    @staticmethod
    def _merge_event(ev: dict, text_chunks: list[str], usage_box: list[AgentUsage]) -> None:
        """Pull text + usage fields out of a single event into the accumulators."""
        # ── Text ────────────────────────────────────────────────────────
        for key in ("result", "output", "text", "content", "message"):
            v = ev.get(key)
            if isinstance(v, str) and v:
                text_chunks.append(v)
                break
        # Anthropic-style nested content blocks
        content = ev.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    t = block.get("text") or block.get("output")
                    if isinstance(t, str) and t:
                        text_chunks.append(t)

        # ── Usage ───────────────────────────────────────────────────────
        u = usage_box[0]
        usage_dict = ev.get("usage") if isinstance(ev.get("usage"), dict) else None

        def _pick(*candidates) -> int:
            for src in candidates:
                if src is None:
                    continue
                for k in ("input_tokens", "prompt_tokens", "in"):
                    if isinstance(src.get(k), int):
                        return src[k]
            return 0

        def _pick_out(*candidates) -> int:
            for src in candidates:
                if src is None:
                    continue
                for k in ("output_tokens", "completion_tokens", "out"):
                    if isinstance(src.get(k), int):
                        return src[k]
            return 0

        u.input_tokens += _pick(usage_dict, ev)
        u.output_tokens += _pick_out(usage_dict, ev)

        cache_read = (
            (usage_dict or {}).get("cache_read_input_tokens")
            or (usage_dict or {}).get("cache_read_tokens")
            or ev.get("cache_read_tokens")
            or 0
        )
        cache_create = (
            (usage_dict or {}).get("cache_creation_input_tokens")
            or (usage_dict or {}).get("cache_create_tokens")
            or ev.get("cache_create_tokens")
            or 0
        )
        if isinstance(cache_read, int):
            u.cache_read_tokens += cache_read
        if isinstance(cache_create, int):
            u.cache_create_tokens += cache_create

        # ── Cost ───────────────────────────────────────────────────────
        for key in ("total_cost_usd", "cost_usd", "cost"):
            v = ev.get(key)
            if isinstance(v, (int, float)) and v > 0:
                u.cost_usd += float(v)
                break

        # ── Misc ───────────────────────────────────────────────────────
        if isinstance(ev.get("num_turns"), int):
            u.num_turns = max(u.num_turns, ev["num_turns"])
        if isinstance(ev.get("duration_ms"), int):
            u.duration_ms = max(u.duration_ms, ev["duration_ms"])

        usage_box[0] = u

    @staticmethod
    def _join_chunks(chunks: list[str]) -> str:
        """Join accumulated text fragments into a single result string.

        Removes trivial duplicates that arise when both ``text`` and a
        nested ``content[].text`` carry the same fragment.
        """
        seen: list[str] = []
        for c in chunks:
            if c not in seen:
                seen.append(c)
        return "\n".join(s.strip() for s in seen if s.strip())

    @staticmethod
    def _fallback_text(raw: str) -> str:
        """When no text fields were found, give the raw output (trimmed)."""
        return raw.strip()

    # ── Runners (mirrors ClaudeBackend) ────────────────────────────────────

    def run(
        self,
        prompt: str,
        model: str | None = None,
        timeout: int | None = None,
        cwd: Path | None = None,
    ) -> AgentResult:
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
                result_text=f"{self._opencode_bin()} CLI not found",
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
                f"# cmd       : {' '.join(cmd[:2])} ... <prompt {len(prompt)} chars>\n"
                "# note      : opencode --format json streams events; final result\n"
                "#             is assembled from the full stream.\n"
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
        duration = time.monotonic() - start_time if start_time else 0
        result = AgentResult(exit_code=0, duration_s=duration)

        if not log_file.exists():
            result.exit_code = -1
            result.result_text = "Log file not found"
            return result

        raw = log_file.read_text(encoding="utf-8", errors="replace")

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
