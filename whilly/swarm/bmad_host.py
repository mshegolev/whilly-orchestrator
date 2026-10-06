"""Bounded host-side lifecycle for the configured BMAD ``host-spec`` workflow."""

from __future__ import annotations

import json
import re
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from whilly.swarm.bmad import build_bmad_context
from whilly.core.swarm_execution import ExecutionBlocked, ExecutionPolicy
from whilly.swarm.execution import GuardedExecutor
from whilly.swarm.registry import Registry

__all__ = ["BmadHostError", "BmadHostWorkspace", "persist_spec_result", "prepare_spec_workspace"]

_BLOCK = re.compile(r"```bmad-artifacts\s*\n(.*?)\n```", re.DOTALL)
_REQUIRED = ("Why", "Capabilities", "Constraints", "Non-goals", "Success signal")
_MAX_DECISIONS = 64
_MAX_TEXT = 20_000
_MAX_PAYLOAD = 120_000
_MAX_COMPANIONS = 20


class BmadHostError(ValueError):
    """Named setup, execution, or artifact validation failure."""


@dataclass(frozen=True)
class BmadHostWorkspace:
    workspace: Path
    project_root: Path
    memlog_script: Path
    context: str
    allow_colon_headings: bool = False
    executor: GuardedExecutor | None = None
    policy: ExecutionPolicy | None = None
    environment: dict[str, str] | None = None
    log_dir: Path | None = None


def _safe(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise BmadHostError(f"bmad_path_escape: {label}: {path}") from exc
    return resolved


def _run(
    script: Path,
    args: list[str],
    *,
    cwd: Path,
    executor: GuardedExecutor,
    policy: ExecutionPolicy,
    environment: dict[str, str],
    log_dir: Path,
) -> str:
    try:
        result = executor.run_sync(
            (sys.executable, str(script), *args),
            phase="host_script",
            cwd=cwd,
            policy=policy,
            environment=environment,
            log_dir=log_dir,
        )
    except (OSError, ExecutionBlocked) as exc:
        raise BmadHostError(f"bmad_executor_failed: {script.name}: {exc}") from exc
    if result.reason is not None or result.exit_code != 0:
        detail = result.reason or f"exit code {result.exit_code}"
        raise BmadHostError(f"bmad_executor_failed: {script.name}: {detail}")
    return Path(result.stdout_path).read_text(encoding="utf-8", errors="replace").strip()


def prepare_spec_workspace(
    registry: Registry,
    feature_id: str,
    *,
    intent: str = "",
    previous_spec: str = "",
    executor: GuardedExecutor | None = None,
) -> BmadHostWorkspace:
    config = registry.raw.get("bmad")
    if not isinstance(config, dict) or config.get("mode") != "host-spec":
        raise BmadHostError("bmad_host_spec_required: configure bmad mode='host-spec'")
    project_root = Path(config.get("project_root", "")).expanduser().resolve()
    if not project_root.is_dir():
        raise BmadHostError("bmad_project_root_missing: configured project_root is not a directory")
    scripts_root = project_root / "_bmad" / "scripts"
    scripts = {
        name: _safe(scripts_root / name, project_root, name)
        for name in ("resolve_customization.py", "resolve_config.py", "memlog.py")
    }
    for name, path in scripts.items():
        if not path.is_file():
            raise BmadHostError(f"bmad_executor_required: missing {name}")
    if not isinstance(feature_id, str) or not feature_id or "/" in feature_id or "\\" in feature_id:
        raise BmadHostError("bmad_feature_id_invalid")
    state_root = registry.resolved_state_dir()
    workspace = _safe(state_root / "features" / feature_id / f"spec-{uuid.uuid4().hex}", state_root, "workspace")
    workspace.mkdir(parents=True, exist_ok=False)
    skill_root = Path(config.get("skill_root", "")).expanduser().resolve()
    skill_dir = _safe(skill_root / "bmad-spec", skill_root, "bmad-spec skill")
    executor = executor or GuardedExecutor()
    try:
        project = next(iter(registry.projects.values()))
    except StopIteration as exc:
        raise BmadHostError("bmad_toolchain_provisioning_required: registry has no enrolled project") from exc
    fallback = project.verification_policy.toolchain_id if project.verification_policy else None
    toolchain_id = executor.toolchain_for_phase("host_script", fallback=fallback)
    environment = executor.environment(toolchain_id=toolchain_id, phase="host_script", attempt_root=workspace)
    toolchain = executor.provisioning.toolchains[toolchain_id]
    policy = ExecutionPolicy(
        phase="host_script",
        read_roots=tuple(
            str(item)
            for item in (
                project_root,
                skill_root,
                Path(sys.executable).resolve().parent,
                Path(sys.executable).resolve().parent.parent,
                Path(sys.prefix),
                "/bin",
                *toolchain.read_roots,
            )
        ),
        write_roots=(str(workspace),),
        denied_roots=(),
        network=False,
        timeout_seconds=30,
        max_output_bytes=1024 * 1024,
    )

    def run_host(script: Path, args: list[str], *, cwd: Path) -> str:
        return _run(
            script,
            args,
            cwd=cwd,
            executor=executor,
            policy=policy,
            environment=environment,
            log_dir=workspace.parent / "host-logs" / workspace.name,
        )

    args = ["--skill", str(skill_dir), "--project-root", str(project_root), "--key", "workflow"]
    customization = run_host(scripts["resolve_customization.py"], args, cwd=project_root)
    try:
        customization_data = json.loads(customization)
    except json.JSONDecodeError as exc:
        raise BmadHostError(f"bmad_customization_invalid: {exc.msg}") from exc
    workflow = customization_data.get("workflow", customization_data) if isinstance(customization_data, dict) else {}
    for key in ("activation_steps_prepend", "activation_steps_append", "on_complete"):
        if workflow.get(key):
            raise BmadHostError(f"bmad_unsupported: workflow.{key} requires unsupported custom execution")
    fact_chunks: list[str] = []
    facts = workflow.get("persistent_facts", [])
    if not isinstance(facts, list):
        raise BmadHostError("bmad_persistent_facts_invalid: expected a list")
    for fact in facts:
        if not isinstance(fact, str) or not fact.startswith("file:"):
            raise BmadHostError("bmad_persistent_fact_invalid: expected file:path")
        relative = fact[5:].strip().replace("{project-root}", "")
        if fact[5:].strip().startswith("{project-root}"):
            relative = fact[5:].strip()[len("{project-root}") :].lstrip("/\\")
        if not relative or "*" in relative:
            raise BmadHostError(f"bmad_persistent_fact_invalid: {relative or '<missing name>'}")
        fact_path = _safe(project_root / relative, project_root, f"persistent fact '{relative}'")
        if not fact_path.is_file():
            raise BmadHostError(f"bmad_persistent_fact_missing: {relative}")
        fact_chunks.append(f"## BMAD persistent fact: {relative}\n{fact_path.read_text(encoding='utf-8')}")
    resolved_config = run_host(scripts["resolve_config.py"], ["--project-root", str(project_root)], cwd=project_root)
    run_host(
        scripts["memlog.py"],
        ["init", "--workspace", str(workspace), "--field", f"topic=spec:{feature_id}"],
        cwd=project_root,
    )
    if intent.strip():
        run_host(
            scripts["memlog.py"],
            ["append", "--workspace", str(workspace), "--type", "direction", "--text", intent.strip()],
            cwd=project_root,
        )
    run_host(
        scripts["memlog.py"],
        ["append", "--workspace", str(workspace), "--type", "event", "--text", "host-spec-prepared"],
        cwd=project_root,
    )
    context_registry = replace(
        registry, raw={**registry.raw, "bmad": {**config, "customization_content": customization}}
    )
    context = (
        build_bmad_context(context_registry, allow_executor=True) + "\n## BMAD resolved config\n" + resolved_config
    )
    if fact_chunks:
        context += "\n" + "\n\n".join(fact_chunks)
    if previous_spec:
        context += "\n## Previous SPEC.md for capability continuity\n" + previous_spec
    context += (
        "\n## Host artifact contract\n"
        "Return one fenced bmad-artifacts JSON bundle with keys SPEC.md, companions, decisions, coherence, "
        "and preservation. The host validates and writes it; do not edit a repository or claim completion. "
        "coherence and preservation must be exactly pass, ok, or pass: followed by details; failed is not approved. "
        "Any normal canonical plan is a separate JSON response owned by the host integration."
    )
    return BmadHostWorkspace(
        workspace,
        project_root,
        scripts["memlog.py"],
        context,
        bool(config.get("allow_colon_headings", False)),
        executor,
        policy,
        environment,
        workspace.parent / "host-logs" / workspace.name,
    )


def persist_spec_result(workspace: BmadHostWorkspace | Path, reply: str) -> dict[str, str]:
    root = workspace.workspace if isinstance(workspace, BmadHostWorkspace) else workspace
    block = _BLOCK.search(reply)
    if not block:
        raise BmadHostError("bmad_artifacts_missing: expected one fenced bmad-artifacts JSON block")
    try:
        payload = json.loads(block.group(1))
    except json.JSONDecodeError as exc:
        raise BmadHostError(f"bmad_artifacts_invalid_json: {exc.msg}") from exc
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("SPEC.md"), str)
        or not isinstance(payload.get("companions"), dict)
    ):
        raise BmadHostError("bmad_artifacts_invalid_shape")
    spec = payload["SPEC.md"]
    allow_colon = isinstance(workspace, BmadHostWorkspace) and workspace.allow_colon_headings
    for key in _REQUIRED:
        heading = re.compile(rf"^## {re.escape(key)}$", re.IGNORECASE | re.MULTILINE)
        colon_heading = re.compile(rf"^## {re.escape(key)}:\s*$", re.IGNORECASE | re.MULTILINE)
        if not heading.search(spec) and not (allow_colon and colon_heading.search(spec)):
            raise BmadHostError(f"bmad_artifacts_missing_kernel_heading: {key}")
    decisions = payload.get("decisions")
    if (
        not isinstance(decisions, list)
        or len(decisions) > _MAX_DECISIONS
        or any(not isinstance(item, str) or not item.strip() or len(item) > _MAX_TEXT for item in decisions)
    ):
        raise BmadHostError("bmad_artifacts_invalid_decisions")
    for verdict in ("coherence", "preservation"):
        if (
            not isinstance(payload.get(verdict), str)
            or not payload[verdict].strip()
            or len(payload[verdict]) > _MAX_TEXT
        ):
            raise BmadHostError(f"bmad_artifacts_invalid_{verdict}")
        value = payload[verdict].strip().lower()
        if value != "ok" and value != "pass" and not value.startswith("pass:"):
            raise BmadHostError(f"bmad_artifacts_{verdict}_not_passing")
    if len(payload["companions"]) > _MAX_COMPANIONS:
        raise BmadHostError(f"bmad_artifacts_too_many_companions: max {_MAX_COMPANIONS}")
    for name in payload["companions"]:
        if name in {"SPEC.md", ".memlog.md"}:
            raise BmadHostError(f"bmad_artifact_path_invalid: reserved {name}")
    files = {"SPEC.md": spec, **payload["companions"]}
    if sum(len(name) + len(content) for name, content in files.items()) > _MAX_PAYLOAD:
        raise BmadHostError(f"bmad_artifacts_payload_too_large: max {_MAX_PAYLOAD}")
    validated: list[tuple[Path, str]] = []
    for name, content in files.items():
        if name != "SPEC.md" and (not name.endswith(".md") or name.startswith(".")):
            raise BmadHostError(f"bmad_artifact_path_invalid: markdown companion required: {name}")
        if (
            not isinstance(name, str)
            or not isinstance(content, str)
            or len(content) > _MAX_TEXT
            or name != Path(name).name
            or name in ("", ".", "..")
        ):
            raise BmadHostError(f"bmad_artifact_path_invalid: {name}")
        unresolved = root / name
        for parent in (root, *unresolved.relative_to(root).parents):
            candidate = root / parent if not str(parent).startswith(str(root)) else parent
            if candidate.is_symlink():
                raise BmadHostError(f"bmad_artifact_path_invalid: symlink {name}")
        target = _safe(unresolved, root, "artifact path")
        if target.exists() and target.is_symlink():
            raise BmadHostError(f"bmad_artifact_path_invalid: symlink {name}")
        validated.append((target, content))
    if isinstance(workspace, BmadHostWorkspace):
        if (
            workspace.executor is None
            or workspace.policy is None
            or workspace.environment is None
            or workspace.log_dir is None
        ):
            raise BmadHostError("bmad_executor_required: workspace execution binding missing")
        for decision in decisions:
            _run(
                workspace.memlog_script,
                ["append", "--workspace", str(root), "--type", "decision", "--text", str(decision)],
                cwd=workspace.project_root,
                executor=workspace.executor,
                policy=workspace.policy,
                environment=workspace.environment,
                log_dir=workspace.log_dir,
            )
        _run(
            workspace.memlog_script,
            [
                "append",
                "--workspace",
                str(root),
                "--type",
                "event",
                "--text",
                f"coherence={payload['coherence']}; preservation={payload['preservation']}",
            ],
            cwd=workspace.project_root,
            executor=workspace.executor,
            policy=workspace.policy,
            environment=workspace.environment,
            log_dir=workspace.log_dir,
        )
    for target, content in validated:
        target.write_text(content, encoding="utf-8")
    return {"spec_path": str(root / "SPEC.md")}
