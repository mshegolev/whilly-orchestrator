import logging
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("whilly")

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
    task_id: str,
    prompt: str,
    model: str,
    log_dir: Path,
    cwd: Path | None = None,
    backend: "object | None" = None,  # AgentBackend — avoid import cycle at module load
) -> TmuxAgent:
    """Launch an agent backend CLI in a new tmux session (OC-112).

    Args:
        backend: AgentBackend instance. When ``None`` the active backend is
            resolved from ``WHILLY_AGENT_BACKEND`` (default: ``claude``),
            preserving legacy behaviour.
        cwd: Working directory (e.g., git worktree path for isolation).

    The tmux wrapper reads the prompt from a file (``{task_id}_prompt.txt``)
    via ``$(cat ...)`` so special characters in long prompts don't break shell
    quoting. The rest of argv comes from ``backend.build_command`` — the last
    positional slot (which every backend reserves for the prompt) is replaced
    with the cat-substitution. Both Claude and OpenCode backends conform to
    this convention.
    """
    if not TMUX:
        raise RuntimeError("tmux is not installed or not in PATH")

    if backend is None:
        from whilly.agents import active_backend_from_env

        backend = active_backend_from_env()

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{task_id}.log"
    session_name = f"whilly-{task_id}"

    subprocess.run([TMUX, "kill-session", "-t", session_name], capture_output=True)

    prompt_file = log_dir / f"{task_id}_prompt.txt"
    prompt_file.write_text(prompt)

    cd_prefix = f'cd "{cwd}" && ' if cwd else ""

    # Ask the backend for argv; the last element is the prompt placeholder we
    # will replace with a shell cat-substitution. Assertion guards against any
    # future backend that reorders argv — catch it here rather than silently
    # double-escaping the prompt.
    argv = backend.build_command(prompt, model=model)
    if not argv or argv[-1] != prompt:
        raise RuntimeError(
            f"backend {getattr(backend, 'name', type(backend).__name__)!r} build_command "
            "must place the prompt as the last argv element for tmux_runner to work"
        )
    prefix_argv = argv[:-1]
    prefix_cmd = " ".join(shlex.quote(a) for a in prefix_argv)

    # Preamble: пишем сразу чтобы tail -f / TUI сразу видели активность ещё
    # до первого события агента. С --output-format stream-json Claude CLI
    # пишет JSONL events инкрементально — preamble просто гарантирует, что
    # файл существует и непустой даже на холодном старте.
    backend_name = getattr(backend, "name", "claude")
    preamble_cmd = (
        f'printf "# whilly agent preamble\\n'
        f"# timestamp : $(date '+%Y-%m-%d %H:%M:%S')\\n"
        f"# session   : {session_name}\\n"
        f"# task_id   : {task_id}\\n"
        f"# backend   : {backend_name}\\n"
        f"# model     : {model}\\n"
        f"# cwd       : {cwd or 'inherited'}\\n"
        f"# note      : stream-json: events JSONL появляются live, tail -f покажет прогресс\\n"
        f'# ---\\n" > "{log_file}"; '
    )
    wrapper = (
        f"{cd_prefix}"
        f"{preamble_cmd}"
        f'{prefix_cmd} "$(cat {shlex.quote(str(prompt_file))})" '
        f'>> "{log_file}" 2>&1; '
        f'echo "EXIT_CODE=$?" >> "{log_file}"'
    )

    # zsh -ic sources ~/.zshrc so user-defined functions (e.g. claudeproxy wrappers) resolve.
    subprocess.run(
        [TMUX, "new-session", "-d", "-s", session_name, "zsh", "-ic", wrapper],
        check=True,
    )

    log.info("Launched tmux session %s for %s (backend=%s)", session_name, task_id, backend_name)
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


def kill_all_whilly_sessions() -> None:
    """Kill all tmux sessions starting with 'whilly-'."""
    if not TMUX:
        return
    r = subprocess.run([TMUX, "list-sessions", "-F", "#{session_name}"], capture_output=True, text=True)
    if r.returncode != 0:
        return
    for name in r.stdout.strip().splitlines():
        if name.startswith("whilly-"):
            subprocess.run([TMUX, "kill-session", "-t", name], capture_output=True)
            log.info("Killed tmux session %s", name)
