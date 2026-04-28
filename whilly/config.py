"""Whilly orchestrator configuration.

Config resolves from five layered sources, last wins:

    defaults (dataclass) < user TOML < repo TOML < .env < shell env < CLI flags

The user TOML lives at the OS-native location resolved via ``platformdirs``
(macOS: ``~/Library/Application Support/whilly/config.toml``;
Linux: ``$XDG_CONFIG_HOME/whilly/config.toml``;
Windows: ``%APPDATA%\\whilly\\config.toml``). The repo TOML is ``./whilly.toml``.

Any TOML value may reference an OS secret store instead of a literal —
see :mod:`whilly.secrets` for the ``env:`` / ``keyring:`` / ``file:`` schemes.

Public API (stable):
- :func:`load_dotenv` — the original ``.env`` loader (kept for back-compat).
- :class:`WhillyConfig` — the dataclass consumers read from.
- :meth:`WhillyConfig.from_env` — thin wrapper over :func:`load_layered`.
- :func:`load_layered` — the new full layered loader.
- :func:`user_config_path` — resolves the per-user config file path.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("whilly")


def load_dotenv(path: str | os.PathLike[str] = ".env", *, override: bool = False) -> int:
    """Load KEY=VALUE pairs from a dotenv-style file into os.environ.

    Silently no-ops if the file doesn't exist. Existing environment values win unless
    ``override=True``. Supports ``#`` comments, blank lines, optional ``export`` prefix,
    and single/double-quoted values. No shell expansion — keep it predictable.

    Returns the number of variables actually set.
    """
    file = Path(path)
    if not file.is_file():
        return 0
    count = 0
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (len(value) >= 2) and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not key:
            continue
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        count += 1
    return count


@dataclass
class WhillyConfig:
    """Configuration loaded from environment variables with WHILLY_ prefix."""

    MAX_ITERATIONS: int = 0
    MAX_PARALLEL: int = 3
    HEARTBEAT_INTERVAL: int = 1
    DECOMPOSE_EVERY: int = 5
    AGENT: str = ""
    USE_TMUX: bool = False
    LOG_DIR: str = "whilly_logs"
    MODEL: str = "claude-opus-4-6[1m]"
    VOICE: bool = True
    ORCHESTRATOR: str = "file"
    RICH_DASHBOARD: bool = True
    BUDGET_USD: float = 0.0  # 0 = unlimited
    MAX_TASK_RETRIES: int = 5
    HEADLESS: bool = False
    TIMEOUT: int = 0
    STATE_FILE: str = ".whilly_state.json"
    WORKTREE: bool = False  # WHILLY_WORKTREE=1 — per-task git worktree (только при MAX_PARALLEL > 1)
    USE_WORKSPACE: bool = False  # WHILLY_USE_WORKSPACE=1 (или --workspace) — включить plan-level worktree

    # Logging verbosity + retention
    VERBOSE: bool = False  # WHILLY_VERBOSE=1 (или --verbose/-v) — Whilly debug + ANTHROPIC_LOG=info
    TRACE_HTTP: bool = False  # WHILLY_TRACE_HTTP=1 (или --trace) — ANTHROPIC_LOG=debug + HTTP body capture
    LOG_TTL_DAYS: int = 14  # WHILLY_LOG_TTL_DAYS — age-based cleanup of agent logs at run_plan start (0 = disabled)

    # Agent backend selection (OC-109) — drives whilly.agents.get_backend()
    AGENT_BACKEND: str = "claude"  # "claude" | "opencode"
    OPENCODE_BIN: str = "opencode"  # path to the opencode CLI binary
    OPENCODE_SAFE: bool = False  # OPENCODE_SAFE=1 → safe mode (prompt before tool use)
    OPENCODE_SERVER_URL: str = ""  # optional remote OpenCode server URL (empty = local CLI)

    # Resource protection limits
    MAX_CPU_PERCENT: float = 80.0  # Max total CPU usage before throttling
    MAX_MEMORY_PERCENT: float = 75.0  # Max memory usage before throttling
    MIN_FREE_SPACE_GB: float = 5.0  # Min free disk space required
    PROCESS_TIMEOUT_MINUTES: int = 30  # Max process runtime
    RESOURCE_CHECK_ENABLED: bool = True  # Enable resource monitoring

    # External integrations (GitHub Issues, Jira, etc)
    CLOSE_EXTERNAL_TASKS: bool = True  # WHILLY_CLOSE_EXTERNAL_TASKS=0 → disable auto-closing
    GITHUB_AUTO_CLOSE: bool = True  # Auto-close GitHub Issues
    GITHUB_ADD_COMMENTS: bool = True  # Add completion comments to GitHub Issues
    JIRA_ENABLED: bool = False  # Enable Jira integration
    JIRA_SERVER_URL: str = ""  # Jira server URL
    JIRA_USERNAME: str = ""  # Jira username
    JIRA_AUTO_CLOSE: bool = True  # Auto-close Jira tasks
    JIRA_ADD_COMMENTS: bool = True  # Add completion comments to Jira
    JIRA_TRANSITION_TO: str = "Done"  # Target status for closing Jira tasks

    @classmethod
    def from_env(cls) -> WhillyConfig:
        """Load config using the full layered pipeline.

        Kept as the public entry point every caller already uses. Delegates to
        :func:`load_layered`, so TOML support is picked up automatically by
        every existing call site (cli, resource monitor, dashboard, agents).
        """
        return load_layered()

    @classmethod
    def from_env_only(cls) -> WhillyConfig:
        """Backwards-compatible env-only loader for tests that need to bypass TOML."""
        kwargs: dict = {}
        for f in fields(cls):
            env_key = f"WHILLY_{f.name}"
            env_val = os.environ.get(env_key)
            if env_val is None:
                continue
            kwargs[f.name] = _coerce(f.type, env_val)
        return cls(**kwargs)

    def resolved(self) -> WhillyConfig:
        """Return a copy with every string field resolved through :mod:`whilly.secrets`.

        Only applies to string-typed dataclass fields — numerics/bools already
        went through ``_coerce`` and have no scheme prefix to act on. Fields that
        reference a secret (``env:`` / ``keyring:`` / ``file:``) are replaced with
        the resolved plaintext; missing secrets become empty strings.
        """
        from whilly.secrets import resolve as _resolve_secret

        kwargs: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, str):
                kwargs[f.name] = _resolve_secret(value)
            else:
                kwargs[f.name] = value
        return WhillyConfig(**kwargs)

    def get_external_integrations_config(self) -> dict[str, dict]:
        """Returns configuration for external integrations."""
        return {
            "enabled": self.CLOSE_EXTERNAL_TASKS,
            "github": {
                "enabled": True,  # GitHub always available if CLI present
                "auto_close": self.GITHUB_AUTO_CLOSE,
                "add_comments": self.GITHUB_ADD_COMMENTS,
            },
            "jira": {
                "enabled": self.JIRA_ENABLED,
                "server_url": self.JIRA_SERVER_URL or os.getenv("JIRA_SERVER_URL", ""),
                "username": self.JIRA_USERNAME or os.getenv("JIRA_USERNAME", ""),
                "token": os.getenv("JIRA_API_TOKEN", ""),  # Always from env for security
                "auto_close": self.JIRA_AUTO_CLOSE,
                "add_comments": self.JIRA_ADD_COMMENTS,
                "transition_to": self.JIRA_TRANSITION_TO,
            },
        }


# ── Layered TOML loading ──────────────────────────────────────────────────────


# Public-ish map of extra non-dataclass namespaces we read from TOML. Values are
# returned by :func:`load_layered` via ``get_toml_section`` so downstream modules
# (gh_utils, secrets consumers) can reach them without parsing TOML themselves.
_EXTRA_TOML_NAMESPACES = ("github", "jira", "project_board")


def user_config_path() -> Path:
    """Return the OS-native user config file location (may not exist)."""
    try:
        import platformdirs
    except ImportError:
        # platformdirs is a hard dep in pyproject.toml; this branch only triggers
        # if the package is imported from a not-yet-installed checkout.
        log.debug("platformdirs missing — falling back to ~/.config/whilly")
        return Path.home() / ".config" / "whilly" / "config.toml"
    return Path(platformdirs.user_config_dir("whilly")) / "config.toml"


def _load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file as a dict; silently return ``{}`` when missing or empty."""
    if not path.is_file():
        return {}
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover - exercised on 3.10 only
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Invalid TOML at %s: %s — ignoring file", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _coerce(field_type: Any, raw: str) -> Any:
    """Coerce a string value from TOML/env to the dataclass field type."""
    # Dataclass field types come back as strings with `from __future__ import annotations`.
    t = field_type if isinstance(field_type, str) else getattr(field_type, "__name__", str(field_type))
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() not in ("0", "false", "no", "off", "")
    if t == "int":
        return int(raw)
    if t == "float":
        return float(raw)
    return raw if isinstance(raw, str) else str(raw)


def _dataclass_values_from_toml(toml_data: dict[str, Any]) -> dict[str, Any]:
    """Extract dataclass-field-compatible values from a parsed TOML dict.

    Supports both flat keys (``MAX_PARALLEL = 3``) and case-insensitive matches
    (``max_parallel = 3``). Unknown keys are ignored so TOML can hold extra
    sections like ``[github]`` without tripping the dataclass constructor.
    """
    # Build a case-insensitive lookup of top-level TOML scalars.
    flat: dict[str, Any] = {}
    for key, value in toml_data.items():
        if isinstance(value, dict):
            continue  # nested sections handled separately
        flat[key.upper()] = value

    out: dict[str, Any] = {}
    for f in fields(WhillyConfig):
        if f.name in flat:
            out[f.name] = _coerce(f.type, flat[f.name])
    return out


def _extract_toml_sections(toml_data: dict[str, Any], names: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Pull named nested sections from a TOML dict (missing sections become ``{}``)."""
    result: dict[str, dict[str, Any]] = {}
    for name in names:
        value = toml_data.get(name)
        result[name] = value if isinstance(value, dict) else {}
    return result


# Module-level cache for non-dataclass TOML sections. Populated by
# :func:`load_layered` and consumed via :func:`get_toml_section`.
_toml_sections_cache: dict[str, dict[str, Any]] = {name: {} for name in _EXTRA_TOML_NAMESPACES}


def get_toml_section(name: str) -> dict[str, Any]:
    """Return the merged TOML section by name (``github``, ``jira``, …).

    Returns a copy so callers can't accidentally mutate the cache. Empty dict
    when the section is absent from both user and repo TOML files.
    """
    return dict(_toml_sections_cache.get(name, {}))


def load_layered(cwd: Path | str | None = None) -> WhillyConfig:
    """Resolve config through the full five-layer pipeline.

    Layers, last wins:

        1. dataclass defaults
        2. user TOML  (:func:`user_config_path`)
        3. repo TOML  (``cwd/whilly.toml``)
        4. ``.env``   (loaded into ``os.environ`` via :func:`load_dotenv`)
        5. shell env  (``WHILLY_*`` read in :meth:`WhillyConfig.from_env_only`)

    CLI flags are applied by cli.py *after* this function returns, which makes
    them the effective sixth (highest) layer.

    Side effects: ``load_dotenv`` writes into ``os.environ`` so downstream
    ``os.environ.get("WHILLY_...")`` calls see the merged values.
    """
    base_dir = Path(cwd) if cwd is not None else Path.cwd()

    user_toml = _load_toml(user_config_path())
    repo_toml = _load_toml(base_dir / "whilly.toml")

    # Start from defaults, overlay user TOML, then repo TOML.
    cfg = WhillyConfig(**_dataclass_values_from_toml(user_toml))
    for name, value in _dataclass_values_from_toml(repo_toml).items():
        setattr(cfg, name, value)

    # Cache nested TOML sections (repo overrides user). Reset first so a second
    # call doesn't accumulate stale state across tests.
    merged_sections: dict[str, dict[str, Any]] = {name: {} for name in _EXTRA_TOML_NAMESPACES}
    for source in (user_toml, repo_toml):
        for name, section in _extract_toml_sections(source, _EXTRA_TOML_NAMESPACES).items():
            merged_sections[name].update(section)
    _toml_sections_cache.clear()
    _toml_sections_cache.update(merged_sections)

    # .env feeds os.environ (no overwrite of real env vars). Warn if present so
    # users are nudged toward `whilly --config migrate`.
    dotenv_path = base_dir / ".env"
    _maybe_warn_dotenv_deprecation(dotenv_path)
    load_dotenv(dotenv_path)

    # Shell env wins over TOML for every WHILLY_* field explicitly set.
    env_layer = WhillyConfig.from_env_only()
    for f in fields(WhillyConfig):
        if f"WHILLY_{f.name}" in os.environ:
            setattr(cfg, f.name, getattr(env_layer, f.name))

    return cfg


__all__ = [
    "WhillyConfig",
    "load_dotenv",
    "load_layered",
    "migrate_env_to_toml",
    "user_config_path",
    "get_toml_section",
]


# ── .env → whilly.toml migration ──────────────────────────────────────────────


# Variables that must go through the OS secret store, never into TOML plaintext.
_SECRET_ENV_VARS: dict[str, tuple[str, str]] = {
    # env var name → (TOML section path, keyring reference suffix)
    "GITHUB_TOKEN": ("github.token", "whilly/github"),
    "WHILLY_GH_TOKEN": ("github.token", "whilly/github"),
    "JIRA_API_TOKEN": ("jira.token", "whilly/jira"),
}


def migrate_env_to_toml(
    env_path: Path | str = ".env",
    toml_path: Path | str = "whilly.toml",
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Parse ``env_path`` and write a ``whilly.toml`` next to it.

    Returns a summary dict:

        {
            "written": bool,
            "toml_path": Path,
            "scalar_fields": [...],   # WHILLY_* → dataclass fields
            "sections": {"github": {...}, "jira": {...}},
            "secrets_found": [{"var": str, "target": str, "keyring": str}, ...],
            "backup": Path | None,    # .env.bak, when rename succeeded
        }

    The caller decides whether to actually push secrets into keyring — this
    function just reports where they would go.
    """
    env_path = Path(env_path)
    toml_path = Path(toml_path)

    parsed: dict[str, str] = {}
    if env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].lstrip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if (len(value) >= 2) and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key:
                parsed[key] = value

    dataclass_values: dict[str, Any] = {}
    github_section: dict[str, Any] = {}
    jira_section: dict[str, Any] = {}
    secrets_found: list[dict[str, str]] = []

    field_names = {f.name for f in fields(WhillyConfig)}
    field_types = {f.name: f.type for f in fields(WhillyConfig)}

    for key, raw in parsed.items():
        if key in _SECRET_ENV_VARS and raw:
            section_path, keyring_ref = _SECRET_ENV_VARS[key]
            secrets_found.append({"var": key, "target": section_path, "keyring": keyring_ref})
            # Point the TOML field at the keyring rather than embedding the token.
            section_name, _, field_name = section_path.partition(".")
            (github_section if section_name == "github" else jira_section)[field_name] = f"keyring:{keyring_ref}"
            continue

        if not key.startswith("WHILLY_"):
            # Non-WHILLY vars like JIRA_SERVER_URL fall into the right section.
            if key == "JIRA_SERVER_URL" and raw:
                jira_section["server_url"] = raw
            elif key == "JIRA_USERNAME" and raw:
                jira_section["username"] = raw
            continue

        field = key[len("WHILLY_") :]
        if field in field_names:
            dataclass_values[field] = _coerce(field_types[field], raw)

    summary: dict[str, Any] = {
        "written": False,
        "toml_path": toml_path,
        "scalar_fields": sorted(dataclass_values),
        "sections": {"github": github_section, "jira": jira_section},
        "secrets_found": secrets_found,
        "backup": None,
    }

    if dry_run:
        return summary

    rendered = _render_toml(dataclass_values, github_section, jira_section)
    toml_path.write_text(rendered, encoding="utf-8")
    summary["written"] = True

    if env_path.is_file():
        backup = env_path.with_suffix(env_path.suffix + ".bak")
        env_path.rename(backup)
        summary["backup"] = backup

    return summary


def _render_toml(
    scalars: dict[str, Any],
    github: dict[str, Any],
    jira: dict[str, Any],
) -> str:
    """Minimal TOML writer so we don't need a separate dependency just to write.

    We only support the narrow value types the migration can produce:
    ``str``, ``bool``, ``int``, ``float``. Good enough for :class:`WhillyConfig`.
    """

    def fmt(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        # Escape backslashes + double-quotes for a basic string literal.
        text = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{text}"'

    lines = [
        "# Auto-generated by `whilly --config migrate`.",
        "# Edit freely; rerun the migration command to regenerate from an updated .env.",
        "",
    ]
    for name in sorted(scalars):
        lines.append(f"{name} = {fmt(scalars[name])}")

    if github:
        lines += ["", "[github]"]
        for k in sorted(github):
            lines.append(f"{k} = {fmt(github[k])}")

    if jira:
        lines += ["", "[jira]"]
        for k in sorted(jira):
            lines.append(f"{k} = {fmt(jira[k])}")

    return "\n".join(lines) + "\n"


# ── .env deprecation warning ──────────────────────────────────────────────────


_dotenv_warning_emitted = False


def _maybe_warn_dotenv_deprecation(path: Path) -> None:
    """Emit a one-time deprecation warning when a legacy ``.env`` is picked up.

    Users can silence it entirely with ``WHILLY_SUPPRESS_DOTENV_WARNING=1``.
    """
    global _dotenv_warning_emitted
    if _dotenv_warning_emitted:
        return
    if not path.is_file():
        return
    if os.environ.get("WHILLY_SUPPRESS_DOTENV_WARNING", "").strip().lower() in ("1", "true", "yes"):
        _dotenv_warning_emitted = True
        return
    log.warning(
        "Legacy .env detected at %s — consider migrating with `whilly --config migrate` "
        "so secrets move into the OS keyring and behaviour into whilly.toml. "
        "Silence with WHILLY_SUPPRESS_DOTENV_WARNING=1.",
        path,
    )
    _dotenv_warning_emitted = True
