"""Pure environment builder for Whilly-owned coding-agent subprocesses."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

BASE_RUNNER_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "WHILLY_CLI",
    "WHILLY_MODEL",
    "CLAUDE_BIN",
    "WHILLY_OPENCODE_BIN",
    "WHILLY_CLAUDE_SAFE",
    "WHILLY_AGENT_ALLOW_SHELL",
    "WHILLY_OPENCODE_SAFE",
    "WHILLY_HANDOFF_DIR",
    "WHILLY_HANDOFF_TIMEOUT",
)


def required_env_for_model(model: str | None, *, backend: str) -> tuple[str, ...]:
    """Return credential env names required by the selected backend/model."""
    backend_name = backend.strip().lower()
    model_name = (model or "").strip().lower()

    if backend_name == "handoff":
        return ()
    if backend_name == "claude":
        return ("ANTHROPIC_API_KEY",)
    if not model_name:
        return ()

    if model_name.startswith("openrouter/"):
        return ("OPENROUTER_API_KEY",)
    if model_name.startswith("opencode/"):
        if model_name == "opencode/big-pickle":
            return ()
        return ("OPENCODE_API_KEY", "OPENCODE_ZEN_API_KEY")
    if model_name.startswith("groq/"):
        return ("GROQ_API_KEY",)
    if model_name.startswith("anthropic/") or model_name.startswith("claude"):
        return ("ANTHROPIC_API_KEY",)
    if model_name.startswith(("openai/", "gpt", "o1", "o3", "o4")):
        return ("OPENAI_API_KEY",)
    if model_name.startswith(("google/", "gemini")) or "/gemini" in model_name:
        return ("GEMINI_API_KEY",)

    return ()


def build_runner_env(
    parent: Mapping[str, str],
    *,
    required_env: Iterable[str] = (),
    model: str | None = None,
    backend: str = "",
) -> dict[str, str]:
    """Build a scrubbed child env from ``parent`` using only explicit names."""
    names = set(BASE_RUNNER_ENV_ALLOWLIST)
    names.update(name for name in required_env if name)
    names.update(required_env_for_model(model or parent.get("WHILLY_MODEL"), backend=backend))
    return {name: parent[name] for name in sorted(names) if name in parent}


__all__ = [
    "BASE_RUNNER_ENV_ALLOWLIST",
    "build_runner_env",
    "required_env_for_model",
]
