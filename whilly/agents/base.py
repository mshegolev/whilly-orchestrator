"""AgentBackend Protocol + shared dataclasses.

Two backends ship today: :class:`whilly.agents.claude.ClaudeBackend` and
:class:`whilly.agents.opencode.OpenCodeBackend`. They are interchangeable
behind this Protocol — selected via ``--agent {claude,opencode}`` CLI flag
(or ``WHILLY_AGENT_BACKEND`` env).

`AgentResult` and `AgentUsage` live here so both backends — and the legacy
`whilly.agent_runner` shim — produce the same shape.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class AgentUsage:
    """Token / cost accounting for a single agent run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_create_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0


@dataclass
class AgentResult:
    """Outcome of one agent invocation. Same shape across all backends."""

    result_text: str = ""
    usage: AgentUsage = field(default_factory=AgentUsage)
    exit_code: int = 0
    duration_s: float = 0.0
    is_complete: bool = False

    def __repr__(self) -> str:
        return (
            f"AgentResult(exit_code={self.exit_code}, duration_s={self.duration_s:.1f}, "
            f"is_complete={self.is_complete}, cost_usd={self.usage.cost_usd:.4f}, "
            f"text={self.result_text[:80]!r}...)"
        )


# Universal completion marker — instructed in the system prompt; not LLM-specific.
COMPLETION_MARKER = "<promise>COMPLETE</promise>"


class AgentBackend(Protocol):
    """Stable contract every coding-agent backend must implement.

    Subprocess-based by design (matches ADR-004); a future SDK-based backend
    could implement the same Protocol with different internals.
    """

    name: str
    """Backend identifier used in CLI/env, e.g. ``"claude"`` or ``"opencode"``."""

    def default_model(self) -> str:
        """Return the model id used when the user does not pass one explicitly."""
        ...

    def normalize_model(self, model: str) -> str:
        """Normalize an incoming model id to backend-native form.

        Example: ``"claude-opus-4-6"`` → ``"anthropic/claude-opus-4-6"`` for OpenCode.
        Returning *model* unchanged is a valid implementation when the backend
        already accepts the input format.
        """
        ...

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        *,
        safe_mode: bool | None = None,
    ) -> list[str]:
        """Build the argv used to invoke the CLI in non-interactive mode."""
        ...

    def parse_output(self, raw: str) -> tuple[str, AgentUsage]:
        """Extract (result_text, usage) from CLI stdout.

        Implementations must be defensive: malformed output should yield a
        sensible AgentUsage with zeroed fields rather than raising.
        """
        ...

    def is_complete(self, text: str) -> bool:
        """Return True when *text* signals the task is done.

        Default behaviour for both backends: presence of
        :data:`COMPLETION_MARKER`. Backends may override.
        """
        ...

    def run(
        self,
        prompt: str,
        model: str | None = None,
        timeout: int | None = None,
        cwd: Path | None = None,
    ) -> AgentResult:
        """Run synchronously, return parsed result. Never raises on subprocess errors."""
        ...

    def run_async(
        self,
        prompt: str,
        model: str | None = None,
        log_file: Path | None = None,
        cwd: Path | None = None,
    ) -> subprocess.Popen:
        """Spawn the CLI in background, returning the Popen handle.

        Writes a small preamble to *log_file* before exec so dashboards can
        show "agent started" immediately.
        """
        ...

    def collect_result(
        self,
        proc: subprocess.Popen,
        log_file: Path | None = None,
        start_time: float = 0,
    ) -> AgentResult:
        """Pull AgentResult out of a finished Popen (waits if still running)."""
        ...

    def collect_result_from_file(
        self,
        log_file: Path,
        start_time: float = 0,
    ) -> AgentResult:
        """Read AgentResult from a log file written by tmux/subprocess wrapper."""
        ...
