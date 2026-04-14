import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("ralph")


@dataclass
class AgentUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_create_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0


@dataclass
class AgentResult:
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


API_ERRORS = {403, 500, 529}
MAX_RETRIES = 3
BACKOFF = [5, 15, 30]


def _parse_claude_output(raw: str) -> tuple[str, AgentUsage]:
    """Parse JSON output from claude CLI into result text and usage."""
    data = json.loads(raw)
    result_text = data.get("result", "")
    usage_data = data.get("usage", {})
    usage = AgentUsage(
        input_tokens=usage_data.get("input_tokens", 0),
        output_tokens=usage_data.get("output_tokens", 0),
        cache_read_tokens=usage_data.get("cache_read_input_tokens", 0),
        cache_create_tokens=usage_data.get("cache_creation_input_tokens", 0),
        cost_usd=data.get("total_cost_usd", 0.0),
        num_turns=data.get("num_turns", 0),
        duration_ms=data.get("duration_ms", 0),
    )
    return result_text, usage


def _claude_bin() -> str:
    """Resolve claude CLI path. CLAUDE_BIN env overrides (for corporate proxy setups)."""
    import os as _os
    return _os.environ.get("CLAUDE_BIN") or "claude"


def _claude_permission_args() -> list[str]:
    """Build permission-related CLI args.

    By default uses --dangerously-skip-permissions so Bash/test commands run in
    fully autonomous mode (no TTY prompts). This mirrors what claudeproxy did before.
    Set RALPH_CLAUDE_SAFE=1 to revert to --permission-mode acceptEdits (manual approve
    needed for Bash — only useful with attached TTY).
    """
    import os as _os
    if _os.environ.get("RALPH_CLAUDE_SAFE") in ("1", "true", "yes"):
        return ["--permission-mode", "acceptEdits"]
    return ["--dangerously-skip-permissions"]


def run_agent(prompt: str, model: str = "claude-opus-4-6[1m]", timeout: int | None = None) -> AgentResult:
    """Run claude CLI and parse JSON result."""
    start = time.monotonic()
    cmd = [
        _claude_bin(),
        *_claude_permission_args(),
        "--output-format",
        "json",
        "--model",
        model,
        "-p",
        prompt,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        duration = time.monotonic() - start
        result = AgentResult(exit_code=proc.returncode, duration_s=duration)

        raw = proc.stdout
        try:
            result.result_text, result.usage = _parse_claude_output(raw)
        except (json.JSONDecodeError, KeyError):
            result.result_text = raw or proc.stderr

        result.is_complete = "<promise>COMPLETE</promise>" in result.result_text
        return result

    except subprocess.TimeoutExpired:
        return AgentResult(exit_code=-1, duration_s=time.monotonic() - start, result_text="TIMEOUT")
    except FileNotFoundError:
        return AgentResult(exit_code=-2, result_text="claude CLI not found")


def run_agent_async(
    prompt: str,
    model: str = "claude-opus-4-6[1m]",
    log_file: Path | None = None,
    cwd: Path | None = None,
) -> subprocess.Popen:
    """Start claude CLI in background. Returns Popen object.

    Args:
        cwd: Working directory (e.g., git worktree path for isolation).
    """
    cmd = [
        _claude_bin(),
        *_claude_permission_args(),
        "--output-format",
        "json",
        "--model",
        model,
        "-p",
        prompt,
    ]
    stdout_target = open(log_file, "w") if log_file else subprocess.PIPE  # noqa: SIM115
    return subprocess.Popen(
        cmd, stdout=stdout_target, stderr=subprocess.STDOUT, cwd=str(cwd) if cwd else None
    )


def collect_result(proc: subprocess.Popen, log_file: Path | None = None, start_time: float = 0) -> AgentResult:
    """Collect result from finished Popen process."""
    duration = time.monotonic() - start_time if start_time else 0
    result = AgentResult(exit_code=proc.returncode or 0, duration_s=duration)

    if log_file and log_file.exists():
        raw = log_file.read_text()
    elif proc.stdout:
        raw = proc.stdout.read() if hasattr(proc.stdout, "read") else ""
    else:
        raw = ""

    try:
        result.result_text, result.usage = _parse_claude_output(raw)
    except (json.JSONDecodeError, KeyError, TypeError):
        result.result_text = raw if isinstance(raw, str) else ""

    result.is_complete = "<promise>COMPLETE</promise>" in result.result_text
    return result


def collect_result_from_file(log_file: Path, start_time: float = 0) -> AgentResult:
    """Parse agent result from a log file (written by tmux wrapper or subprocess)."""
    duration = time.monotonic() - start_time if start_time else 0
    result = AgentResult(exit_code=0, duration_s=duration)

    if not log_file.exists():
        result.exit_code = -1
        result.result_text = "Log file not found"
        return result

    raw = log_file.read_text(encoding="utf-8", errors="replace")

    # Check for EXIT_CODE marker appended by tmux wrapper
    for line in reversed(raw.splitlines()[-5:]):
        if line.startswith("EXIT_CODE="):
            result.exit_code = int(line.split("=", 1)[1])
            break

    try:
        result.result_text, result.usage = _parse_claude_output(raw)
    except (json.JSONDecodeError, KeyError, TypeError):
        result.result_text = raw

    result.is_complete = "<promise>COMPLETE</promise>" in result.result_text
    return result


def is_api_error(result: AgentResult) -> bool:
    """Check if result is a retriable API error."""
    text = result.result_text.lower()
    return any(f"api error: {code}" in text for code in API_ERRORS) or (
        '"type":"error"' in text or "failed to authenticate" in text
    )


def is_auth_error(result: AgentResult) -> bool:
    """Check if result is a non-retriable auth error (403 forbidden)."""
    text = result.result_text.lower()
    return "failed to authenticate" in text or ("403" in text and "forbidden" in text)
