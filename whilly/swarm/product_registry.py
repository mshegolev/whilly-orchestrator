"""Inert, immutable publication/merge policies and canonical product bindings.

Loading validates declarations only. Remote protection, repository identity,
pipelines and delivery readiness require later independent observations.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from whilly.swarm.registry import registry_from_dict

_POLICY_KEYS = frozenset(
    {
        "canonical_remote",
        "gitlab_project_id",
        "target_branch",
        "target_protected",
        "branch_prefix",
        "checks",
        "allowed_paths",
        "worktree_owner",
        "contracts",
        "artifacts",
        "delivery",
        "compensation",
        "manual_decisions",
    }
)
_DECISIONS = frozenset({"external_effect", "secrets", "network_policy", "kept_storage", "prod_release"})
_SECRET = re.compile(
    r"(?:glpat-[\w-]{10,}|gh[pousr]_[\w]{20,}|sk-[\w-]{20,}|xox[baprs]-[\w-]+|"
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\bBearer\s+\S+)",
    re.IGNORECASE,
)


class ProductRegistryError(ValueError):
    """A bounded error code, never raw paths, credentials or registry content."""

    def __init__(self, error_code: str):
        self.error_code = error_code
        super().__init__(error_code)


def _fail(code: str = "product_policy_invalid") -> None:
    raise ProductRegistryError(code)


def _canonical(value: Any) -> bytes:
    def validate(item: Any) -> None:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                _fail("registry_serialization_invalid")
            for child in item.values():
                validate(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                validate(child)

    try:
        validate(value)
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError):
        _fail("registry_serialization_invalid")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _object(value: Any, keys: set[str] | frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _fail()
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 4000:
        _fail()
    if any(ord(char) < 32 for char in value):
        _fail()
    return value


def _strings(value: Any, *, empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 100 or (not value and not empty):
        _fail()
    result = tuple(_text(item) for item in value)
    if len(set(result)) != len(result):
        _fail()
    return result


def _argv(value: Any) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 10:
        _fail()
    for command in value:
        if not isinstance(command, list) or not 1 <= len(command) <= 128:
            _fail()
        for arg in command:
            if not isinstance(arg, str) or not arg or len(arg) > 4000 or "\0" in arg:
                _fail()
        _text(command[0])
        shell = command[0].replace("\\", "/").rsplit("/", 1)[-1].casefold().removesuffix(".exe")
        for arg in command[1:]:
            option = arg.casefold().split(":", 1)[0]
            if shell in {"sh", "bash", "dash", "ash", "zsh", "ksh", "fish"} and (
                re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", arg) or option == "--command"
            ):
                _fail()
            if shell in {"powershell", "pwsh"} and (
                option in {"-ec", "-c"}
                or (len(option) >= 2 and ("-command".startswith(option) or "-encodedcommand".startswith(option)))
                or option.startswith(("-command", "-encodedcommand"))
            ):
                _fail()
            if shell == "cmd" and option in {"/c", "/k", "-c"}:
                _fail()


def _branch(value: Any) -> str:
    value = _text(value)
    if (
        value in {"HEAD", "@"}
        or value.startswith(("-", "/", "refs/"))
        or value.endswith(("/", "."))
        or any(token in value for token in ("..", "//", "@{", "\\"))
        or re.search(r"[\s~^:?\[\]*]", value)
        or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
    ):
        _fail()
    return value


def _remote(value: Any) -> tuple[str, str]:
    value = _text(value)
    # Only canonical HTTPS or git-user SSH forms; no credentials, helpers,
    # percent escapes, query strings, fragments or local/file transports.
    scp = re.fullmatch(r"git@([a-zA-Z0-9.-]+):([a-zA-Z0-9_./-]+)", value)
    if scp:
        host, path = scp.groups()
    else:
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"https", "ssh"} or not parsed.hostname:
                _fail()
            if (
                parsed.password
                or parsed.query
                or parsed.fragment
                or (parsed.username and not (parsed.scheme == "ssh" and parsed.username == "git"))
            ):
                _fail("registry_secret_literal" if parsed.username or parsed.password else "product_policy_invalid")
            if parsed.scheme == "ssh" and parsed.username != "git":
                _fail()
            if "%" in parsed.hostname:
                _fail()
            port = parsed.port
            if port == {"https": 443, "ssh": 22}[parsed.scheme]:
                value = urlunsplit((parsed.scheme, parsed.netloc.rsplit(":", 1)[0], parsed.path, "", ""))
                port = None
            host = parsed.hostname.lower() + (f":{port}" if port is not None else "")
            path = parsed.path.lstrip("/")
        except ValueError:
            _fail()
    if not re.fullmatch(r"[a-zA-Z0-9_./-]+", path) or len(path.split("/")) < 2:
        _fail()
    if any(part in {"", ".", ".."} for part in path.split("/")) or not path.endswith(".git"):
        _fail()
    return value, host.lower() + "/" + path[:-4]


def _reject_secrets(value: Any, depth: int = 0) -> None:
    if depth > 30:
        _fail("registry_structure_invalid")
    if isinstance(value, str):
        if _SECRET.search(value) or re.search(r"\b(?:password|token|api[_-]?key|secret)\s*=\s*\S+", value, re.I):
            _fail("registry_secret_literal")
    elif isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"password", "token", "api_key", "secret", "access_token", "private_key"}:
                _fail("registry_secret_literal")
            _reject_secrets(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _reject_secrets(item, depth + 1)


@dataclass(frozen=True)
class ProductProjectPolicy:
    project_id: str
    local_path: str
    depends_on: tuple[str, ...]
    canonical_remote: str
    gitlab_project_id: int
    target_branch: str
    target_protected: bool
    branch_prefix: str
    checks: Mapping[str, Any]
    allowed_paths: tuple[str, ...]
    worktree_owner: str
    contracts: tuple[str, ...]
    artifacts: tuple[Mapping[str, str], ...]
    delivery: Mapping[str, Any]
    compensation: Mapping[str, Any]
    manual_decisions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {name: _thaw(getattr(self, name)) for name in _POLICY_KEYS}


def parse_product_policy(
    value: Any, *, project_id: str, local_path: str, depends_on: tuple[str, ...]
) -> ProductProjectPolicy:
    _reject_secrets(value)
    p = _object(value, _POLICY_KEYS)
    remote, _ = _remote(p["canonical_remote"])
    if type(p["gitlab_project_id"]) is not int or not 1 <= p["gitlab_project_id"] <= 2**63 - 1:
        _fail()
    target = _branch(p["target_branch"])
    if p["target_protected"] is not True:
        _fail()
    prefix = _text(p["branch_prefix"])
    if not prefix.endswith("/"):
        _fail()
    _branch(prefix[:-1])
    if target == prefix[:-1] or target.startswith(prefix) or prefix[:-1].startswith(target + "/"):
        _fail()
    checks = _object(p["checks"], {"fast", "full", "ci"})
    _argv(checks["fast"])
    _argv(checks["full"])
    _strings(checks["ci"])
    allowed = _strings(p["allowed_paths"])
    for path in allowed:
        if path.startswith(("/", "\\", "~")) or "\\" in path or ":" in path or ".." in path.split("/"):
            _fail()
    owner = _text(p["worktree_owner"])
    contracts = _strings(p["contracts"], empty=True)
    artifacts = p["artifacts"]
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 100:
        _fail()
    names = []
    for artifact in artifacts:
        _object(artifact, {"name", "kind", "digest"})
        names.append(_text(artifact["name"]))
        if _text(artifact["kind"]) not in {"image", "package", "config", "source"} or artifact["digest"] != "sha256":
            _fail()
    if len(set(names)) != len(names):
        _fail()
    delivery = _object(p["delivery"], {"stage", "prod"})
    for contour in ("stage", "prod"):
        item = _object(
            delivery[contour], {"mode", "job", "observations"} | ({"requires_approval"} if contour == "prod" else set())
        )
        if _text(item["mode"]) not in {"pipeline", "artifact"}:
            _fail()
        _text(item["job"])
        _strings(item["observations"])
        if contour == "prod" and item["requires_approval"] is not True:
            _fail()
    compensation = _object(p["compensation"], {"strategy", "checks", "stage_rollback_job"})
    if compensation["strategy"] != "revert_mr":
        _fail()
    _argv(compensation["checks"])
    _text(compensation["stage_rollback_job"])
    decisions = _strings(p["manual_decisions"])
    if set(decisions) != _DECISIONS:
        _fail()
    return ProductProjectPolicy(
        project_id,
        local_path,
        tuple(depends_on),
        remote,
        p["gitlab_project_id"],
        target,
        True,
        prefix,
        _freeze(checks),
        allowed,
        owner,
        contracts,
        _freeze(artifacts),
        _freeze(delivery),
        _freeze(compensation),
        decisions,
    )


@dataclass(frozen=True)
class ProductRegistrySnapshot:
    name: str
    projects: Mapping[str, ProductProjectPolicy]
    canonical_registry_bytes: bytes
    canonical_policy_bytes: bytes
    source_path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "projects", MappingProxyType(dict(self.projects)))

    @property
    def registry_digest(self) -> str:
        return hashlib.sha256(self.canonical_registry_bytes).hexdigest()

    @property
    def policy_digest(self) -> str:
        return hashlib.sha256(self.canonical_policy_bytes).hexdigest()

    @property
    def digest(self) -> str:
        return self.registry_digest

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self.canonical_registry_bytes)

    def approval_digest(self, binding: dict[str, Any]) -> str:
        if not isinstance(binding, dict):
            _fail("approval_binding_invalid")
        return hashlib.sha256(
            _canonical(
                {"registry_digest": self.registry_digest, "policy_digest": self.policy_digest, "binding": binding}
            )
        ).hexdigest()

    def assert_unchanged(self, path: str | Path | None = None) -> None:
        current = load_product_registry(self.source_path if path is None else path)
        if current.registry_digest != self.registry_digest or current.policy_digest != self.policy_digest:
            _fail("product_registry_changed")


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        if key in result:
            _fail("registry_duplicate_key")
        result[key] = value
    return result


def load_product_registry(path: str | Path) -> ProductRegistrySnapshot:
    """Validate every declared project, never run Git, commands or remote probes."""
    try:
        source = Path(path).expanduser().resolve()
        data = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda value: _fail("registry_serialization_invalid"),
        )
        _reject_secrets(data)
        if not isinstance(data, dict) or type(data.get("version", 1)) is not int or data.get("version", 1) != 1:
            _fail("product_registry_version_invalid")
        registry = registry_from_dict(data, base_dir=source.parent, source_path=str(source), check_git=False)
        resolved = registry.to_snapshot()
    except ProductRegistryError:
        raise
    except (OSError, UnicodeError, ValueError, RuntimeError, RecursionError):
        _fail("product_registry_invalid")
    policies = {}
    identities = set()
    remotes = set()
    for project_id, project in registry.projects.items():
        if project.product_policy is None:
            _fail("product_policy_required")
        policy = project.product_policy
        _, remote_id = _remote(policy.canonical_remote)
        if policy.gitlab_project_id in identities or remote_id in remotes:
            _fail("product_project_identity_duplicate")
        identities.add(policy.gitlab_project_id)
        remotes.add(remote_id)
        policies[project_id] = policy
    resolved.pop("_source_path", None)
    serialized = {
        pid: {"local_path": p.local_path, "depends_on": list(p.depends_on), **p.to_dict()}
        for pid, p in policies.items()
    }
    return ProductRegistrySnapshot(registry.name, policies, _canonical(resolved), _canonical(serialized), str(source))
