import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("ralph")

TMUX = shutil.which("tmux")


@dataclass
class TmuxAgent:
    task_id: str
    session_name: str
    log_file: Path
    start_time: float = 0.0

    @property
    def is_running(self) -> bool:
        if not TMUX:
            return False
        r = subprocess.run([TMUX, "has-session", "-t", self.session_name], capture_output=True)
        return r.returncode == 0

    def capture_output(self, lines: int = 20) -> str:
        """Capture last N lines from tmux pane (live output)."""
        if not TMUX:
            return ""
        r = subprocess.run(
            [TMUX, "capture-pane", "-t", self.session_name, "-p", "-S", f"-{lines}"],
            capture_output=True,
            text=True,
        )
        return r.stdout.strip() if r.returncode == 0 else ""

    def kill(self) -> None:
        if TMUX:
            subprocess.run([TMUX, "kill-session", "-t", self.session_name], capture_output=True)


def tmux_available() -> bool:
    return TMUX is not None


def launch_agent(
    task_id: str, prompt: str, model: str, log_dir: Path, cwd: Path | None = None
) -> TmuxAgent:
    """Launch claude agent in a new tmux session.

    Args:
        cwd: Working directory (e.g., git worktree path for isolation).
    """
    if not TMUX:
        raise RuntimeError("tmux is not installed or not in PATH")

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{task_id}.log"
    session_name = f"ralph-{task_id}"

    subprocess.run([TMUX, "kill-session", "-t", session_name], capture_output=True)

    prompt_file = log_dir / f"{task_id}_prompt.txt"
    prompt_file.write_text(prompt)

    cd_prefix = f'cd "{cwd}" && ' if cwd else ""

    # Use CLAUDE_BIN if set (bypasses shell function resolution, e.g. corporate proxy wrappers).
    # Otherwise rely on interactive zsh which sources ~/.zshrc and exposes `claude` function/alias.
    claude_cmd = os.environ.get("CLAUDE_BIN") or "claude"
    # Default: --dangerously-skip-permissions (autonomous, no Bash approval prompts).
    # RALPH_CLAUDE_SAFE=1 → revert to --permission-mode acceptEdits (manual approve).
    perm_args = (
        "--permission-mode acceptEdits"
        if os.environ.get("RALPH_CLAUDE_SAFE") in ("1", "true", "yes")
        else "--dangerously-skip-permissions"
    )
    # Preamble: пишем сразу чтобы tail -f / TUI сразу видели активность,
    # т.к. claude --output-format json пишет результат только в конце.
    preamble_cmd = (
        f'printf "# ralph agent preamble\\n'
        f'# timestamp : $(date \'+%Y-%m-%d %H:%M:%S\')\\n'
        f'# session   : {session_name}\\n'
        f'# task_id   : {task_id}\\n'
        f'# model     : {model}\\n'
        f'# cwd       : {cwd or "inherited"}\\n'
        f'# note      : claude пишет результат в КОНЦЕ работы\\n'
        f'# ---\\n" > "{log_file}"; '
    )
    wrapper = (
        f"{cd_prefix}"
        f"{preamble_cmd}"
        f'{claude_cmd} {perm_args} --output-format json '
        f'--model "{model}" -p "$(cat {prompt_file})" '
        f'>> "{log_file}" 2>&1; '
        f'echo "EXIT_CODE=$?" >> "{log_file}"'
    )

    # zsh -ic sources ~/.zshrc so user-defined functions (e.g. claudeproxy wrappers) resolve.
    subprocess.run(
        [TMUX, "new-session", "-d", "-s", session_name, "zsh", "-ic", wrapper],
        check=True,
    )

    log.info("Launched tmux session %s for %s", session_name, task_id)
    return TmuxAgent(
        task_id=task_id,
        session_name=session_name,
        log_file=log_file,
        start_time=time.monotonic(),
    )


def wait_for_agent(agent: TmuxAgent, poll_interval: float = 1.0) -> int:
    """Wait for tmux agent to finish. Returns exit code."""
    while agent.is_running:
        time.sleep(poll_interval)

    if agent.log_file.exists():
        lines = agent.log_file.read_text().splitlines()
        for line in reversed(lines[-5:]):
            if line.startswith("EXIT_CODE="):
                return int(line.split("=", 1)[1])
    return -1


def kill_all_ralph_sessions() -> None:
    """Kill all tmux sessions starting with 'ralph-'."""
    if not TMUX:
        return
    r = subprocess.run([TMUX, "list-sessions", "-F", "#{session_name}"], capture_output=True, text=True)
    if r.returncode != 0:
        return
    for name in r.stdout.strip().splitlines():
        if name.startswith("ralph-"):
            subprocess.run([TMUX, "kill-session", "-t", name], capture_output=True)
            log.info("Killed tmux session %s", name)
