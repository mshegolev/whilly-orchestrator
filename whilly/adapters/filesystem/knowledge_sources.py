"""Bounded, read-only source verification for learning memory."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import PurePosixPath

from whilly.swarm.learning.domain import KnowledgeRevision, SourceCheck


_SHA = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


class GitSourceVerifier:
    def __init__(self, projects: dict[str, tuple[str, str]]) -> None:
        self._projects = dict(projects)

    async def check(self, item: KnowledgeRevision) -> SourceCheck:
        if not item.source_uri.startswith("git:") or item.source_sha is None:
            return SourceCheck("unavailable", None)
        project = self._projects.get(item.project_id or "")
        relative = item.source_uri[4:]
        if project is None or not _SHA.fullmatch(item.source_sha) or not self._safe_path(relative):
            return SourceCheck("unavailable", None)
        repo_path, base_ref = project
        if not base_ref or not os.path.isdir(repo_path):
            return SourceCheck("unavailable", None)
        try:
            actual = await self._git(repo_path, "rev-parse", f"{base_ref}^{{commit}}")
            if actual != item.source_sha.lower():
                return SourceCheck("changed", actual)
            tree_entry = await self._git(repo_path, "ls-tree", actual, "--", relative)
            mode = tree_entry.split(maxsplit=1)[0] if tree_entry else ""
            if mode not in {"100644", "100755"}:
                return SourceCheck("unavailable", None)
            await self._git(repo_path, "cat-file", "-e", f"{actual}:{relative}")
        except (OSError, asyncio.TimeoutError, ValueError):
            return SourceCheck("unavailable", None)
        return SourceCheck("verified", actual)

    @staticmethod
    def _safe_path(value: str) -> bool:
        path = PurePosixPath(value)
        return bool(value) and not value.startswith("/") and "\x00" not in value and ".." not in path.parts

    @staticmethod
    async def _git(repo_path: str, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=3)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            raise ValueError("git verification failed")
        return stdout.decode("ascii").strip()
