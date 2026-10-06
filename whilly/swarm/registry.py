"""Local swarm registry: projects, roles, limits and agent configuration.

The registry is a private, local JSON file (never committed to a public
repository) describing the product ecosystem the swarm may work on::

    {
      "version": 1,
      "name": "demo ecosystem",
      "overview": "What the products are and how they fit together.",
      "projects": {
        "demo-lib": {
          "path": "../demo-lib",            # relative to the registry file
          "base_ref": "main",
          "purpose": "Shared library",
          "depends_on": [],
          "context_files": ["README.md"],
          "verification": [["python", "-m", "pytest", "-q"]]
        }
      },
      "roles": {
        "implementer": {"purpose": "Writes code", "projects": ["demo-lib"]}
      },
      "limits": {"max_parallel": 2},
      "agent": {"executable": "claude"}
    }

Validation is strict: unknown keys, unknown project/role references,
dependency cycles, missing paths, non-Git paths and out-of-range limits are
all rejected with a :class:`RegistryError` naming every problem found.
Bare repositories are supported because work always happens in new
worktrees, never in the registered checkout itself.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from typing import TYPE_CHECKING

from whilly.swarm.verification import VerificationError, VerificationPolicy

if TYPE_CHECKING:
    from whilly.swarm.product_registry import ProductProjectPolicy
    from whilly.swarm.profiles import ExecutionProfile

__all__ = [
    "AgentConfig",
    "EngineConfig",
    "ID_RE",
    "Limits",
    "Project",
    "Registry",
    "RegistryError",
    "Role",
    "load_registry",
    "registry_from_dict",
]

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

_TOP_KEYS = {
    "version",
    "name",
    "overview",
    "state_dir",
    "projects",
    "roles",
    "limits",
    "agent",
    "engines",
    "profiles",
    "bmad",
    "publication",
    "execution",
}
_TOP_KEYS.add("profiles")
_PROJECT_KEYS = {
    "product_policy",
    "path",
    "base_ref",
    "purpose",
    "depends_on",
    "context_files",
    "verification",
    "verification_policy",
    "hook_policy",
}
_ROLE_KEYS = {"purpose", "projects", "reviewer", "engine"}
_AGENT_KEYS = {
    "executable",
    "model",
    "worker_args",
    "inherit_mcp",
    "planner_max_turns",
    "planner_budget_usd",
    "planner_timeout_seconds",
    "review_max_turns",
    "review_budget_usd",
    "review_timeout_seconds",
    "planner_engine",
    "review_engine",
}
_ENGINE_KEYS = {"executable", "model", "worker_args", "inherit_mcp"}

# (default, minimum, maximum) for every numeric limit. Bounded configuration
# is a requirement: an operator typo must not create 500 parallel agents.
_LIMIT_BOUNDS: dict[str, tuple[float, float, float]] = {
    "max_parallel": (2, 1, 8),
    "max_tasks": (20, 1, 50),
    "max_attempts": (2, 1, 5),
    "agent_timeout_seconds": (1800, 1, 4 * 3600),
    "verification_timeout_seconds": (600, 1, 3600),
    "max_turns": (40, 1, 200),
    "max_budget_usd": (5.0, 0.01, 100.0),
    "heartbeat_seconds": (15, 1, 120),
    "lease_seconds": (90, 3, 900),
}
_AGENT_NUMERIC_BOUNDS: dict[str, tuple[float, float, float]] = {
    "planner_max_turns": (8, 1, 50),
    "planner_budget_usd": (1.0, 0.01, 20.0),
    "planner_timeout_seconds": (600, 10, 3600),
    "review_max_turns": (8, 1, 50),
    "review_budget_usd": (1.0, 0.01, 20.0),
    "review_timeout_seconds": (600, 10, 3600),
}
_MAX_CONTEXT_FILES = 10
_MAX_VERIFICATION_COMMANDS = 10
_MAX_PROJECTS = 50
_MAX_TEXT = 4000


class RegistryError(ValueError):
    """Raised when a registry file is missing, malformed or inconsistent."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("invalid swarm registry: " + "; ".join(problems))


@dataclass(frozen=True)
class Project:
    id: str
    path: str
    base_ref: str
    purpose: str
    depends_on: tuple[str, ...] = ()
    context_files: tuple[str, ...] = ()
    verification: tuple[tuple[str, ...], ...] = ()
    bare: bool = False
    verification_policy: VerificationPolicy | None = None
    hook_policy: dict[str, Any] | None = None
    product_policy: ProductProjectPolicy | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "base_ref": self.base_ref,
            "purpose": self.purpose,
            "depends_on": list(self.depends_on),
            "context_files": list(self.context_files),
            "verification": [list(cmd) for cmd in self.verification],
            **({"verification_policy": self.verification_policy.to_dict()} if self.verification_policy else {}),
            **({"hook_policy": self.hook_policy} if self.hook_policy is not None else {}),
            **({"product_policy": self.product_policy.to_dict()} if self.product_policy is not None else {}),
        }


@dataclass(frozen=True)
class Role:
    id: str
    purpose: str
    projects: tuple[str, ...]
    reviewer: bool = False
    engine: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "projects": list(self.projects),
            "reviewer": self.reviewer,
            **({"engine": self.engine} if self.engine else {}),
        }


@dataclass(frozen=True)
class Limits:
    max_parallel: int = 2
    max_tasks: int = 20
    max_attempts: int = 2
    agent_timeout_seconds: int = 1800
    verification_timeout_seconds: int = 600
    max_turns: int = 40
    max_budget_usd: float = 5.0
    heartbeat_seconds: int = 15
    lease_seconds: int = 90


@dataclass(frozen=True)
class AgentConfig:
    executable: tuple[str, ...] = ("claude",)
    model: str | None = "haiku"
    worker_args: tuple[str, ...] = ("--dangerously-skip-permissions",)
    inherit_mcp: bool = False
    planner_max_turns: int = 8
    planner_budget_usd: float | None = 1.0
    planner_timeout_seconds: int = 600
    review_max_turns: int = 8
    review_budget_usd: float | None = 1.0
    review_timeout_seconds: int = 600
    planner_engine: str = "claude"
    review_engine: str = "claude"


@dataclass(frozen=True)
class EngineConfig:
    executable: tuple[str, ...]
    model: str | None = None
    worker_args: tuple[str, ...] = ()
    inherit_mcp: bool = False
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class Registry:
    name: str
    overview: str
    projects: dict[str, Project]
    roles: dict[str, Role]
    limits: Limits = field(default_factory=Limits)
    agent: AgentConfig = field(default_factory=AgentConfig)
    engines: dict[str, EngineConfig] = field(default_factory=dict)
    profiles: dict[str, "ExecutionProfile"] = field(default_factory=dict)
    state_dir: str | None = None
    source_path: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_snapshot(self) -> dict[str, Any]:
        """Return the resolved registry (absolute paths) for durable storage."""
        snapshot = dict(self.raw)
        snapshot["projects"] = {pid: p.to_dict() for pid, p in self.projects.items()}
        if self.state_dir is not None:
            # Bind the declared directory, not the optional runtime env override.
            snapshot["state_dir"] = str(Path(self.state_dir).expanduser().resolve())
        snapshot["_source_path"] = self.source_path
        return snapshot

    def resolved_state_dir(self) -> Path:
        env = os.environ.get("WHILLY_SWARM_STATE_DIR")
        if env:
            return Path(env).expanduser().resolve()
        if self.state_dir:
            return Path(self.state_dir).expanduser().resolve()
        return (Path.home() / ".whilly" / "swarm").resolve()


def load_registry(path: str | os.PathLike[str], *, check_git: bool = True) -> Registry:
    """Load and validate a registry JSON file."""
    reg_path = Path(path).expanduser()
    try:
        text = reg_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryError([f"cannot read registry {str(reg_path)!r}: {exc.strerror or exc}"]) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegistryError([f"registry is not valid JSON: {exc.msg} at line {exc.lineno}"]) from exc
    return registry_from_dict(
        data, base_dir=reg_path.resolve().parent, source_path=str(reg_path.resolve()), check_git=check_git
    )


def registry_from_dict(
    data: Any,
    *,
    base_dir: Path | None = None,
    source_path: str | None = None,
    check_git: bool = True,
) -> Registry:
    """Validate a decoded registry mapping; see module docstring for shape.

    ``check_git=False`` skips filesystem and Git checks; it is used when a
    stored snapshot is re-hydrated for display only.
    """
    problems: list[str] = []
    if not isinstance(data, dict):
        raise RegistryError(["registry must be a JSON object"])
    unknown = set(data) - _TOP_KEYS
    if unknown:
        problems.append(f"unknown top-level keys: {sorted(unknown)}")
    if data.get("version", 1) != 1:
        problems.append("unsupported registry version (expected 1)")
    name = _text(data.get("name", "swarm"), "name", problems)
    overview = _text(data.get("overview", ""), "overview", problems, allow_empty=True)

    raw_projects = data.get("projects")
    projects: dict[str, Project] = {}
    if not isinstance(raw_projects, dict) or not raw_projects:
        problems.append("'projects' must be a non-empty object keyed by project id")
        raw_projects = {}
    if len(raw_projects) > _MAX_PROJECTS:
        problems.append(f"too many projects ({len(raw_projects)} > {_MAX_PROJECTS})")
    for pid, raw in raw_projects.items():
        project = _parse_project(pid, raw, base_dir, problems, check_git)
        if project is not None:
            projects[pid] = project

    for pid, project in projects.items():
        for dep in project.depends_on:
            if dep not in raw_projects:
                problems.append(f"project {pid!r} depends on unknown project {dep!r}")
            if dep == pid:
                problems.append(f"project {pid!r} depends on itself")
    cycle = find_cycle({pid: list(p.depends_on) for pid, p in projects.items()})
    if cycle:
        problems.append("project dependency cycle: " + " -> ".join(cycle))

    raw_roles = data.get("roles")
    roles: dict[str, Role] = {}
    if not isinstance(raw_roles, dict) or not raw_roles:
        problems.append("'roles' must be a non-empty object keyed by role id")
        raw_roles = {}
    for rid, raw in raw_roles.items():
        where = f"role {rid!r}"
        if not isinstance(rid, str) or not ID_RE.match(rid):
            problems.append(f"invalid role id {rid!r} (expected {ID_RE.pattern})")
            continue
        if not isinstance(raw, dict):
            problems.append(f"{where} must be an object")
            continue
        unknown = set(raw) - _ROLE_KEYS
        if unknown:
            problems.append(f"{where}: unknown keys {sorted(unknown)}")
        purpose = _text(raw.get("purpose", ""), f"{where}.purpose", problems)
        role_projects = _str_list(raw.get("projects", []), f"{where}.projects", problems)
        if not role_projects:
            problems.append(f"{where}.projects must list at least one project")
        for ref in role_projects:
            if ref not in raw_projects:
                problems.append(f"{where} references unknown project {ref!r}")
        reviewer = raw.get("reviewer", False)
        if not isinstance(reviewer, bool):
            problems.append(f"{where}.reviewer must be a boolean")
            reviewer = False
        engine = raw.get("engine")
        if engine is not None and (not isinstance(engine, str) or engine not in {"claude", "codex"}):
            problems.append(f"{where}.engine must be 'claude' or 'codex'")
            engine = None
        roles[rid] = Role(id=rid, purpose=purpose, projects=tuple(role_projects), reviewer=reviewer, engine=engine)

    limits = _parse_limits(data.get("limits", {}), problems)
    agent = _parse_agent(data.get("agent", {}), problems)
    engines = _parse_engines(data.get("engines"), agent, problems)
    for field_name in ("planner_engine", "review_engine"):
        selected_engine = getattr(agent, field_name)
        if not isinstance(selected_engine, str) or selected_engine not in engines:
            problems.append(f"agent.{field_name} must name a configured engine")
    profiles = _parse_profiles(data.get("profiles"), engines, problems)
    _parse_execution(data.get("execution"), problems)
    state_dir = data.get("state_dir")
    if state_dir is not None and (not isinstance(state_dir, str) or not state_dir.strip()):
        problems.append("state_dir must be a non-empty string when set")
        state_dir = None
    if isinstance(state_dir, str) and base_dir is not None and not Path(state_dir).expanduser().is_absolute():
        state_dir = str((base_dir / state_dir).resolve())

    if problems:
        raise RegistryError(problems)
    return Registry(
        name=name,
        overview=overview,
        projects=projects,
        roles=roles,
        limits=limits,
        agent=agent,
        engines=engines,
        profiles=profiles,
        state_dir=state_dir,
        source_path=source_path,
        raw=data,
    )


def find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """Return one dependency cycle as a node path, or ``None`` if acyclic."""
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for dep in graph.get(node, []):
            if dep not in graph:
                continue
            if state.get(dep) == 1:
                return stack[stack.index(dep) :] + [dep]
            if state.get(dep) is None:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in sorted(graph):
        if state.get(node) is None:
            found = visit(node)
            if found:
                return found
    return None


def _parse_project(pid: Any, raw: Any, base_dir: Path | None, problems: list[str], check_git: bool) -> Project | None:
    if not isinstance(pid, str) or not ID_RE.match(pid):
        problems.append(f"invalid project id {pid!r} (expected {ID_RE.pattern})")
        return None
    where = f"project {pid!r}"
    if not isinstance(raw, dict):
        problems.append(f"{where} must be an object")
        return None
    unknown = set(raw) - _PROJECT_KEYS
    if unknown:
        problems.append(f"{where}: unknown keys {sorted(unknown)}")
    raw_path = raw.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        problems.append(f"{where}.path must be a non-empty string")
        return None
    path = Path(raw_path).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    path = path.resolve()
    base_ref = raw.get("base_ref", "HEAD")
    if not isinstance(base_ref, str) or not base_ref.strip() or base_ref.startswith("-"):
        problems.append(f"{where}.base_ref must be a non-empty ref name")
        base_ref = "HEAD"
    purpose = _text(raw.get("purpose", ""), f"{where}.purpose", problems)
    depends_on = _str_list(raw.get("depends_on", []), f"{where}.depends_on", problems)
    context_files = _str_list(raw.get("context_files", []), f"{where}.context_files", problems)
    if len(context_files) > _MAX_CONTEXT_FILES:
        problems.append(f"{where}.context_files: at most {_MAX_CONTEXT_FILES} entries")
    for rel in context_files:
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            problems.append(f"{where}.context_files entry {rel!r} must be relative without '..'")
    verification = parse_argv_list(raw.get("verification", []), f"{where}.verification", problems)
    verification_policy = None
    if raw.get("verification_policy") is not None:
        try:
            verification_policy = VerificationPolicy.from_dict(raw["verification_policy"])
        except VerificationError as exc:
            problems.append(f"{where}.verification_policy: {exc}")
    hook_policy = raw.get("hook_policy")
    if hook_policy is not None and not isinstance(hook_policy, dict):
        problems.append(f"{where}.hook_policy must be an object")
        hook_policy = None
    elif isinstance(hook_policy, dict):
        for hook_name, entry in hook_policy.items():
            if not isinstance(hook_name, str) or not hook_name or not isinstance(entry, dict):
                problems.append(f"{where}.hook_policy entries must be named objects")
                continue
            if set(entry) != {"sha256", "dependencies", "phase", "expected_exit"}:
                problems.append(f"{where}.hook_policy.{hook_name} keys are not pinned")
            if not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", "")):
                problems.append(f"{where}.hook_policy.{hook_name}.sha256 is invalid")
            if entry.get("phase") != "git" or entry.get("expected_exit") != 0:
                problems.append(f"{where}.hook_policy.{hook_name} phase/expected_exit is unsupported")
            dependencies = entry.get("dependencies")
            if not isinstance(dependencies, dict) or any(
                not isinstance(path, str) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                for path, digest in dependencies.items()
            ):
                problems.append(f"{where}.hook_policy.{hook_name}.dependencies are invalid")

    product_policy = None
    if "product_policy" in raw:
        from whilly.swarm.product_registry import ProductRegistryError, parse_product_policy

        try:
            product_policy = parse_product_policy(
                raw["product_policy"], project_id=pid, local_path=str(path), depends_on=tuple(depends_on)
            )
        except ProductRegistryError as exc:
            problems.append(f"{where}.product_policy: {exc.error_code}")

    bare = False
    if check_git:
        if not path.exists():
            problems.append(f"{where}.path does not exist: {str(path)!r}")
            return None
        probe = _git(path, "rev-parse", "--is-bare-repository")
        if probe is None:
            problems.append(f"{where}.path is not a Git repository: {str(path)!r}")
            return None
        bare = probe == "true"
        root = _git(path, "rev-parse", "--absolute-git-dir" if bare else "--show-toplevel")
        resolved_root = Path(root).resolve() if root is not None else None
        nested_bare_store = path / ".git"
        is_project_root_with_bare_store = bool(
            bare
            and nested_bare_store.is_dir()
            and not nested_bare_store.is_symlink()
            and resolved_root == nested_bare_store.resolve()
        )
        if resolved_root != path and not is_project_root_with_bare_store:
            # A plain directory nested inside some other repository would
            # otherwise silently resolve to the enclosing repository.
            problems.append(f"{where}.path is not the root of a Git repository: {str(path)!r}")
            return None
        if _git(path, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}") is None:
            problems.append(f"{where}.base_ref {base_ref!r} does not resolve to a commit")
    return Project(
        id=pid,
        path=str(path),
        base_ref=base_ref,
        purpose=purpose,
        depends_on=tuple(depends_on),
        context_files=tuple(context_files),
        verification=verification,
        verification_policy=verification_policy,
        hook_policy=dict(hook_policy) if hook_policy is not None else None,
        bare=bare,
        product_policy=product_policy,
    )


def _parse_execution(raw: Any, problems: list[str]) -> None:
    """Validate non-secret host capability declarations without loading auth."""
    if raw is None:
        return
    if not isinstance(raw, dict) or set(raw) - {"toolchains", "phases"}:
        problems.append("execution must contain only toolchains and phases")
        return
    toolchains = raw.get("toolchains")
    if not isinstance(toolchains, dict) or not toolchains:
        problems.append("execution.toolchains must be a non-empty object")
        return
    for toolchain_id, value in toolchains.items():
        where = f"execution.toolchains.{toolchain_id}"
        if not isinstance(toolchain_id, str) or not ID_RE.match(toolchain_id) or not isinstance(value, dict):
            problems.append(f"{where} is invalid")
            continue
        allowed = {"read_roots", "path", "denied_roots", "provider", "auth", "home", "temporary"}
        if set(value) - allowed:
            problems.append(f"{where} has unknown keys")
        roots = value.get("read_roots")
        if not isinstance(roots, list) or not all(isinstance(item, str) and item.startswith("/") for item in roots):
            problems.append(f"{where}.read_roots must contain absolute paths")
        if not isinstance(value.get("path"), str) or not value["path"]:
            problems.append(f"{where}.path is required")
        provider = value.get("provider")
        if provider not in {None, "claude", "codex"}:
            problems.append(f"{where}.provider is unsupported")
        auth = value.get("auth", {})
        if not isinstance(auth, dict) or set(auth) - {"mode", "reason", "source_path", "service"}:
            problems.append(f"{where}.auth contains unsupported or secret fields")
        elif "source_path" in auth and not isinstance(auth["source_path"], str):
            problems.append(f"{where}.auth.source_path must be a path string")
        elif "ready" in auth:
            problems.append(f"{where}.auth.ready is not an auth proof; use a scoped auth mode")
        if isinstance(auth, dict):
            mode = auth.get("mode")
            if mode not in {
                None,
                "api_key",
                "codex_subscription_file",
                "claude_subscription_dir",
                "claude_subscription_keychain",
            }:
                problems.append(f"{where}.auth.mode is unsupported")
            if mode == "codex_subscription_file" and provider != "codex":
                problems.append(f"{where}.auth.mode requires provider codex")
            if mode == "claude_subscription_dir" and provider != "claude":
                problems.append(f"{where}.auth.mode requires provider claude")
            if mode == "claude_subscription_keychain" and provider != "claude":
                problems.append(f"{where}.auth.mode requires provider claude")
            if mode == "claude_subscription_keychain" and not isinstance(auth.get("service"), str):
                problems.append(f"{where}.auth.service is required")
    phases = raw.get("phases", {})
    if not isinstance(phases, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in phases.items()
    ):
        problems.append("execution.phases must map phase names to toolchain ids")


def parse_argv_list(value: Any, where: str, problems: list[str]) -> tuple[tuple[str, ...], ...]:
    """Validate a list of argv commands (each a non-empty list of strings)."""
    if value is None:
        return ()
    if not isinstance(value, list):
        problems.append(f"{where} must be a list of argv lists")
        return ()
    if len(value) > _MAX_VERIFICATION_COMMANDS:
        problems.append(f"{where}: at most {_MAX_VERIFICATION_COMMANDS} commands")
    commands: list[tuple[str, ...]] = []
    for index, cmd in enumerate(value):
        if (
            not isinstance(cmd, list)
            or not cmd
            or not all(isinstance(part, str) and part and len(part) <= _MAX_TEXT for part in cmd)
        ):
            problems.append(f"{where}[{index}] must be a non-empty list of non-empty strings (argv, not shell)")
            continue
        commands.append(tuple(cmd))
    return tuple(commands)


def _parse_limits(raw: Any, problems: list[str]) -> Limits:
    if not isinstance(raw, dict):
        problems.append("'limits' must be an object")
        return Limits()
    unknown = set(raw) - set(_LIMIT_BOUNDS)
    if unknown:
        problems.append(f"limits: unknown keys {sorted(unknown)}")
    values = {key: _bounded(raw, key, bounds, "limits", problems) for key, bounds in _LIMIT_BOUNDS.items()}
    if values["heartbeat_seconds"] * 3 > values["lease_seconds"]:
        problems.append("limits.lease_seconds must be at least 3x limits.heartbeat_seconds")
    return Limits(**values)  # type: ignore[arg-type]


def _parse_agent(raw: Any, problems: list[str]) -> AgentConfig:
    if not isinstance(raw, dict):
        problems.append("'agent' must be an object")
        return AgentConfig()
    unknown = set(raw) - _AGENT_KEYS
    if unknown:
        problems.append(f"agent: unknown keys {sorted(unknown)}")
    executable: Any = raw.get("executable", ["claude"])
    if isinstance(executable, str):
        executable = [executable]
    exe = _str_list(executable, "agent.executable", problems)
    if not exe:
        problems.append("agent.executable must not be empty")
        exe = ["claude"]
    model = raw.get("model", "claude-haiku")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        problems.append("agent.model must be a non-empty string when set")
        model = None
    worker_args = _str_list(raw.get("worker_args", ["--dangerously-skip-permissions"]), "agent.worker_args", problems)
    inherit_mcp = raw.get("inherit_mcp", False)
    if not isinstance(inherit_mcp, bool):
        problems.append("agent.inherit_mcp must be a boolean")
        inherit_mcp = False
    numeric = {key: _bounded(raw, key, bounds, "agent", problems) for key, bounds in _AGENT_NUMERIC_BOUNDS.items()}
    return AgentConfig(
        executable=tuple(exe),
        model=model,
        worker_args=tuple(worker_args),
        inherit_mcp=inherit_mcp,
        **numeric,  # type: ignore[arg-type]
        planner_engine=raw.get("planner_engine", "claude"),
        review_engine=raw.get("review_engine", "claude"),
    )


def _parse_engines(raw: Any, agent: AgentConfig, problems: list[str]) -> dict[str, EngineConfig]:
    if raw is None:
        return {"claude": EngineConfig(agent.executable, agent.model, agent.worker_args, agent.inherit_mcp)}
    if not isinstance(raw, dict):
        problems.append("engines must be an object keyed by engine id")
        return {}
    result: dict[str, EngineConfig] = {}
    for engine_id, value in raw.items():
        if engine_id not in {"claude", "codex"}:
            problems.append(f"unknown engine {engine_id!r}; expected 'claude' or 'codex'")
            continue
        if not isinstance(value, dict):
            problems.append(f"engines.{engine_id} must be an object")
            continue
        unknown = set(value) - _ENGINE_KEYS
        if unknown:
            problems.append(f"engines.{engine_id}: unknown keys {sorted(unknown)}")
        executable = value.get("executable", [engine_id])
        if isinstance(executable, str):
            executable = [executable]
        exe = _str_list(executable, f"engines.{engine_id}.executable", problems)
        if not exe:
            continue
        model = value.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            problems.append(f"engines.{engine_id}.model must be a non-empty string when set")
            model = None
        args = _str_list(value.get("worker_args", []), f"engines.{engine_id}.worker_args", problems)
        result[engine_id] = EngineConfig(tuple(exe), model, tuple(args), bool(value.get("inherit_mcp", False)))
    if "claude" not in result:
        problems.append("engines must configure claude")
    return result


def _parse_profiles(raw: Any, engines: dict[str, EngineConfig], problems: list[str]) -> dict[str, "ExecutionProfile"]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        problems.append("profiles must be an object keyed by profile id")
        return {}
    from whilly.swarm.profiles import parse_profile

    result: dict[str, "ExecutionProfile"] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not ID_RE.match(name):
            problems.append(f"invalid profile id {name!r}")
            continue
        profile = parse_profile(name, value, engines, problems)
        if profile is not None:
            result[name] = profile
    return result


def _bounded(raw: dict[str, Any], key: str, bounds: tuple[float, float, float], where: str, problems: list[str]):
    default, low, high = bounds
    value = raw.get(key, default)
    is_int = isinstance(default, int)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (is_int and not isinstance(value, int)):
        problems.append(f"{where}.{key} must be a {'integer' if is_int else 'number'}")
        return default
    if not low <= value <= high:
        problems.append(f"{where}.{key}={value} out of range [{low}, {high}]")
        return default
    return value


def _text(value: Any, where: str, problems: list[str], *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        problems.append(f"{where} must be a non-empty string")
        return ""
    if len(value) > _MAX_TEXT:
        problems.append(f"{where} is longer than {_MAX_TEXT} characters")
    return value


def _str_list(value: Any, where: str, problems: list[str]) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        problems.append(f"{where} must be a list of non-empty strings")
        return []
    if len(set(value)) != len(value):
        problems.append(f"{where} contains duplicates")
    return list(value)


def _git(path: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()
