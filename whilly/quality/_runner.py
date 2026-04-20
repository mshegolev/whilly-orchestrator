"""Shared subprocess primitive for QualityGate impls.

Everything hitting ``subprocess.run`` in a gate impl should go through
:func:`run_stage` so that:

* Timeouts don't hang the pipeline.
* Missing binaries fail gracefully (``passed=False``, clear summary).
* Stdout/stderr truncation is uniform — reviewers don't read 5 MB of
  pytest output in a PR body.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from whilly.quality.base import StageResult


DEFAULT_TIMEOUT = 600  # 10 minutes per stage
MAX_SUMMARY_CHARS = 2000


def _truncate(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """Keep the tail of *text* — the end usually carries the error message."""
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return "…(truncated)…\n" + text[-limit:]


def run_stage(
    name: str,
    cmd: list[str],
    cwd: Path,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    require_binary: bool = True,
) -> StageResult:
    """Run *cmd* under *cwd* and return a :class:`StageResult`.

    Args:
        name: label for the result (e.g. ``"pytest"``).
        cmd: argv to invoke; ``cmd[0]`` is the binary name.
        cwd: working directory for the subprocess.
        timeout: stage-wide timeout in seconds.
        require_binary: when True (default), we pre-check PATH and fail
            with a "not installed" summary rather than surfacing a
            FileNotFoundError. Useful for optional stages (e.g., a
            repo without eslint).

    Returns:
        :class:`StageResult` — never raises.
    """
    start = time.monotonic()
    if require_binary and shutil.which(cmd[0]) is None:
        return StageResult(
            name=name,
            passed=False,
            summary=f"{cmd[0]!r} not found on PATH",
            duration_s=0.0,
        )
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return StageResult(
            name=name,
            passed=False,
            summary=f"{name} timed out after {timeout}s",
            duration_s=float(timeout),
        )
    except FileNotFoundError:
        return StageResult(
            name=name,
            passed=False,
            summary=f"{cmd[0]!r} not found",
            duration_s=time.monotonic() - start,
        )

    passed = proc.returncode == 0
    body = proc.stdout or proc.stderr or ""
    summary = "" if passed else _truncate(body)
    return StageResult(
        name=name,
        passed=passed,
        summary=summary,
        duration_s=time.monotonic() - start,
    )


def combine(kind: str, stages: list[StageResult]) -> str:
    """Render a multi-stage gate result into a human-readable summary."""
    lines = []
    for st in stages:
        mark = "✓" if st.passed else "✗"
        lines.append(f"{mark} {st.name} ({st.duration_s:.1f}s)")
        if not st.passed and st.summary:
            # Indent each line of the failure output for readability.
            for line in st.summary.splitlines():
                lines.append(f"    {line}")
    header = f"[{kind}] {sum(1 for s in stages if s.passed)}/{len(stages)} stages passed"
    return header + "\n" + "\n".join(lines) if stages else header
