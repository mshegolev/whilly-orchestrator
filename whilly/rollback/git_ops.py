"""Small Git subprocess adapter for rollback operations."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class RollbackError(RuntimeError):
    """Raised when rollback safety-net operations cannot proceed."""


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    """Captured result from a Git command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class GitClient:
    """Run Git commands with list argv, explicit cwd, and captured output."""

    repo: Path | str = "."
    git_bin: str = "git"

    def run(self, *args: str, timeout: float = 30.0) -> GitCommandResult:
        argv = [self.git_bin, *args]
        proc = subprocess.run(
            argv,
            cwd=Path(self.repo),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return GitCommandResult(tuple(argv), proc.returncode, proc.stdout, proc.stderr)

    def require(self, *args: str, timeout: float = 30.0) -> str:
        result = self.run(*args, timeout=timeout)
        if result.returncode != 0:
            output = (result.stderr or result.stdout or "no output").strip()
            raise RollbackError(f"{' '.join(result.argv)} failed in {Path(self.repo)}: {output}")
        return result.stdout
