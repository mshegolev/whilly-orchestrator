"""Shared helpers for swarm tests: neutral demo Git repositories and registries."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

FAKE_AGENT = Path(__file__).resolve().parent / "integration" / "swarm_fake_agent.py"

_GIT_ENV_ARGS = ["-c", "user.name=Swarm Test", "-c", "user.email=swarm@example.com", "-c", "init.defaultBranch=main"]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *_GIT_ENV_ARGS, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def make_repo(root: Path, name: str, *, files: dict[str, str] | None = None) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    for rel, text in (files or {"README.md": f"# {name}\n"}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "initial")
    return repo


def make_bare_repo(root: Path, name: str) -> Path:
    source = make_repo(root / "_src", name, files={"README.md": f"# {name}\n", "AGENTS.md": "Keep changes small.\n"})
    bare = root / f"{name}.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(source), str(bare)], check=True, capture_output=True)
    return bare


def write_registry(
    path: Path,
    projects: dict[str, dict[str, Any]],
    *,
    roles: dict[str, dict[str, Any]] | None = None,
    limits: dict[str, Any] | None = None,
    agent: dict[str, Any] | None = None,
    state_dir: Path | None = None,
) -> Path:
    data: dict[str, Any] = {
        "version": 1,
        "name": "demo ecosystem",
        "overview": "Neutral demo projects used by tests.",
        "projects": projects,
        "roles": roles or {"implementer": {"purpose": "Writes code", "projects": sorted(projects)}},
    }
    if limits is not None:
        data["limits"] = limits
    data["agent"] = agent if agent is not None else {"executable": [sys.executable, str(FAKE_AGENT)]}
    if state_dir is not None:
        data["state_dir"] = str(state_dir)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path
