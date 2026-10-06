import json
import sys
from pathlib import Path

import pytest

from whilly.swarm.bmad_host import BmadHostError, persist_spec_result, prepare_spec_workspace
from whilly.swarm.execution import GuardedExecutor
from whilly.swarm.execution_config import ExecutionProvisioning, ToolchainProvision
from whilly.swarm.registry import Project, Registry
from whilly.swarm.verification import VerificationPolicy


def reg(raw: dict, state: Path) -> Registry:
    return Registry(name="test", overview="", projects={}, roles={}, state_dir=str(state), raw=raw)


def helper(root: Path, name: str, body: str) -> None:
    path = root / "_bmad" / "scripts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def config(project: Path) -> dict:
    return {
        "bmad": {
            "mode": "host-spec",
            "project_root": str(project),
            "skill_root": str(project),
            "workflow_skills": ["bmad-spec"],
        }
    }


def executor(root: Path) -> GuardedExecutor:
    home = root / "home"
    temporary = root / "tmp" / "runtime"
    temporary.parent.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    temporary.mkdir(exist_ok=True)
    return GuardedExecutor(
        ExecutionProvisioning(
            toolchains={
                "fixture-toolchain": ToolchainProvision(
                    "fixture-toolchain",
                    (
                        root,
                        Path(sys.executable).resolve().parent,
                        Path(sys.prefix),
                        Path("/bin"),
                        Path("/opt/homebrew/opt/gettext/lib"),
                    ),
                    "/bin",
                    home,
                    temporary,
                    auth_ready=True,
                )
            }
        )
    )


def guarded_registry(raw: dict, state: Path, root: Path) -> Registry:
    policy = VerificationPolicy(
        test=(("true",),),
        lint=(("true",),),
        architecture=(("true",),),
        protected_paths=(),
        toolchain_id="fixture-toolchain",
    )
    return Registry(
        name="test",
        overview="",
        projects={"fixture": Project("fixture", str(root), "main", "fixture", verification_policy=policy)},
        roles={},
        state_dir=str(state),
        raw=raw,
    )


def test_missing_required_host_script_is_named(tmp_path: Path) -> None:
    with pytest.raises(BmadHostError, match="bmad_executor_required"):
        prepare_spec_workspace(reg(config(tmp_path), tmp_path / "state"), "f1")


def test_custom_workflow_hooks_are_rejected(tmp_path: Path) -> None:
    for name in ("resolve_customization.py", "resolve_config.py", "memlog.py"):
        helper(
            tmp_path,
            name,
            "import json; print(json.dumps({'workflow': {'activation_steps_prepend': ['custom']}}))"
            if name == "resolve_customization.py"
            else "raise SystemExit(0)",
        )
    raw = config(tmp_path)
    with pytest.raises(BmadHostError, match="bmad_unsupported"):
        prepare_spec_workspace(guarded_registry(raw, tmp_path / "state", tmp_path), "f1", executor=executor(tmp_path))


def test_host_lifecycle_uses_safe_argv_and_private_workspace(tmp_path: Path) -> None:
    script = "import json,os,sys; from pathlib import Path; p=Path(os.environ['TMPDIR'])/'argv.jsonl'; p.open('a').write(json.dumps(sys.argv[1:])+'\\n'); print(json.dumps({'workflow': {}}) if Path(sys.argv[0]).name == 'resolve_customization.py' else ('{}' if Path(sys.argv[0]).name == 'resolve_config.py' else 'ok'))"
    for name in ("resolve_customization.py", "resolve_config.py", "memlog.py"):
        helper(tmp_path, name, script)
    (tmp_path / "bmad-spec").mkdir()
    (tmp_path / "bmad-spec" / "SKILL.md").write_text("SPEC instructions", encoding="utf-8")
    workspace = prepare_spec_workspace(
        guarded_registry(config(tmp_path), tmp_path / "state", tmp_path), "f1", executor=executor(tmp_path)
    )
    assert workspace.workspace.parent.name == "f1"
    assert workspace.workspace.name.startswith("spec-")
    assert "SPEC instructions" in workspace.context
    argv_log = next(workspace.workspace.rglob("argv.jsonl"))
    calls = [json.loads(line) for line in argv_log.read_text().splitlines()]
    assert calls[0][0:2] == ["--skill", str(tmp_path / "bmad-spec")]
    assert ["init", "--workspace"] == calls[2][0:2]
    assert "--field" in calls[2] and "--state-dir" not in sum(calls, [])
    assert any(
        call[0] == "append" and "--workspace" in call and "--type" in call and "--text" in call for call in calls
    )
    assert all("API_KEY" not in json.dumps(call) for call in calls)


def test_persist_requires_bmad_artifacts_and_kernel_fields(tmp_path: Path) -> None:
    workspace = tmp_path / "spec-abc"
    workspace.mkdir()
    reply = """```bmad-artifacts\n{"SPEC.md":"## Why\\nx\\n## Capabilities\\ny\\n## Constraints\\nz\\n## Non-goals\\nn\\n## Success signal\\ns","companions":{},"decisions":["d"],"coherence":"ok","preservation":"ok"}\n```"""
    result = persist_spec_result(workspace, reply)
    assert result["spec_path"] == str(workspace / "SPEC.md")
    assert (workspace / "SPEC.md").exists()


def test_persist_rejects_path_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "spec-abc"
    workspace.mkdir()
    kernel = "## Why\nx\n## Capabilities\ny\n## Constraints\nz\n## Non-goals\nn\n## Success signal\ns"
    reply = (
        "```bmad-artifacts\n"
        + json.dumps(
            {
                "SPEC.md": kernel,
                "companions": {"../bad.md": "x"},
                "decisions": [],
                "coherence": "ok",
                "preservation": "ok",
            }
        )
        + "\n```"
    )
    with pytest.raises(BmadHostError, match="artifact.*path"):
        persist_spec_result(workspace, reply)


def test_persist_rejects_failed_verdict_and_non_markdown_companion(tmp_path: Path) -> None:
    workspace = tmp_path / "spec-abc"
    workspace.mkdir()
    kernel = "## Why\nx\n## Capabilities\ny\n## Constraints\nz\n## Non-goals\nn\n## Success signal\ns"
    reply = (
        "```bmad-artifacts\n"
        + json.dumps(
            {
                "SPEC.md": kernel,
                "companions": {"notes.txt": "x"},
                "decisions": [],
                "coherence": "failed: no",
                "preservation": "pass",
            }
        )
        + "\n```"
    )
    with pytest.raises(BmadHostError, match="coherence_not_passing"):
        persist_spec_result(workspace, reply)
