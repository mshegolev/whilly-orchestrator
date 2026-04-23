"""Read-only diagnostic for Whilly — finds stale plans and runtime leftovers.

Complements `.gitignore` (which keeps generated plans out of git): doctor finds
the files that are already ignored but rot on the local disk — ghost plans
(all-pending plans whose GitHub issues are already closed), invalid filenames
leaked from URL-based names, orphan workspaces/worktrees, leftover tmux
sessions, and stale `.whilly_state.json` pointing at a missing plan.

`run_doctor()` returns a `DoctorReport`; the CLI entry (`whilly --doctor`)
formats it with colour and exits non-zero when findings exist. Doctor never
deletes — cleanup is the operator's call.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Plan-shaped files at repo root that discover_plans() does NOT pick up.
# Patterns match what `whilly --init` / GitHub import / pilot plans produce.
ORPHAN_PLAN_PATTERNS: tuple[str, ...] = (
    "tasks-*.json",
    "github-*tasks*.json",
)


@dataclass
class PlanDiagnosis:
    """Verdict for a single plan-shaped file."""

    path: Path
    kind: str  # ghost | stale | invalid_name | not_a_plan | healthy
    detail: str = ""


@dataclass
class DoctorReport:
    plans: list[PlanDiagnosis] = field(default_factory=list)
    stale_state_file: Path | None = None
    orphan_workspaces: list[Path] = field(default_factory=list)
    orphan_worktrees: list[Path] = field(default_factory=list)
    whilly_tmux_sessions: list[str] = field(default_factory=list)

    @property
    def findings(self) -> list[str]:
        """Short human-readable finding codes — empty ⇒ all clean."""
        out: list[str] = []
        for d in self.plans:
            if d.kind in ("ghost", "stale", "invalid_name"):
                out.append(f"{d.kind}:{d.path.name}")
        if self.stale_state_file is not None:
            out.append("stale_state")
        if self.orphan_workspaces:
            out.append(f"workspaces:{len(self.orphan_workspaces)}")
        if self.orphan_worktrees:
            out.append(f"worktrees:{len(self.orphan_worktrees)}")
        if self.whilly_tmux_sessions:
            out.append(f"tmux:{len(self.whilly_tmux_sessions)}")
        return out


def _orphan_plan_files(cwd: Path) -> list[Path]:
    seen: set[Path] = set()
    for pattern in ORPHAN_PLAN_PATTERNS:
        for p in cwd.glob(pattern):
            if p.name == "tasks.json":  # canonical default plan — not an orphan
                continue
            if p.is_file():
                seen.add(p)
    return sorted(seen)


def _load_plan(p: Path) -> dict | None:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and isinstance(data.get("tasks"), list):
        return data
    return None


def _extract_gh_issue_nums(tasks: list[dict]) -> list[int]:
    nums: set[int] = set()
    for t in tasks:
        url = str(t.get("prd_requirement") or "")
        if "/issues/" in url:
            tail = url.rsplit("/issues/", 1)[1]
            head = tail.split("/", 1)[0].split("#", 1)[0]
            if head.isdigit():
                nums.add(int(head))
        tid = str(t.get("id") or "")
        # Accept both "GH-157" and "gh-157-slug" shapes.
        upper = tid.upper()
        if upper.startswith("GH-"):
            body = upper[3:]
            prefix = body.split("-", 1)[0]
            if prefix.isdigit():
                nums.add(int(prefix))
    return sorted(nums)


def _gh_issues_state(numbers: list[int]) -> dict[int, str]:
    """Fetch {issue_num: state} via `gh`; silently return {} if gh is absent or fails."""
    if not numbers:
        return {}
    out: dict[int, str] = {}
    for n in numbers:
        try:
            r = subprocess.run(
                ["gh", "issue", "view", str(n), "--json", "state", "--jq", ".state"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return {}  # bail on first hard failure — no partial state
        if r.returncode == 0 and r.stdout.strip():
            out[n] = r.stdout.strip().upper()
    return out


def diagnose_plan(p: Path, issues_state: dict[int, str] | None = None) -> PlanDiagnosis:
    if ":" in p.name:
        return PlanDiagnosis(p, "invalid_name", "filename contains ':' (likely leaked from a URL)")

    data = _load_plan(p)
    if data is None:
        return PlanDiagnosis(p, "not_a_plan", "not valid plan JSON")

    tasks = data.get("tasks", [])
    if not tasks:
        return PlanDiagnosis(p, "ghost", "0 tasks")

    statuses = [str(t.get("status", "pending")) for t in tasks]
    if all(s in ("done", "skipped") for s in statuses):
        return PlanDiagnosis(p, "ghost", f"all {len(tasks)} tasks resolved — safe to archive")

    all_pending = all(s == "pending" for s in statuses)
    nums = _extract_gh_issue_nums(tasks)
    if issues_state and nums:
        known = {n: issues_state[n] for n in nums if n in issues_state}
        if known:
            closed = [n for n, s in known.items() if s == "CLOSED"]
            if closed and all_pending and len(closed) == len(known):
                return PlanDiagnosis(
                    p,
                    "ghost",
                    f"all {len(tasks)} pending, but all {len(known)} linked issues are CLOSED",
                )
            if closed:
                return PlanDiagnosis(
                    p,
                    "stale",
                    f"{len(closed)}/{len(known)} referenced issues are already CLOSED",
                )

    return PlanDiagnosis(p, "healthy", f"{len(tasks)} tasks")


def _tmux_whilly_sessions() -> list[str]:
    try:
        r = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    if r.returncode != 0:
        return []
    return sorted(s for s in r.stdout.splitlines() if s.startswith("whilly-"))


def _stale_state_file(cwd: Path) -> Path | None:
    state = cwd / ".whilly_state.json"
    if not state.exists():
        return None
    try:
        sd = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return state  # unreadable ⇒ stale
    plan = sd.get("plan_file") or sd.get("plan")
    if plan and not Path(plan).exists():
        return state
    return None


def run_doctor(cwd: Path | None = None, check_gh: bool = True) -> DoctorReport:
    cwd = cwd or Path.cwd()
    report = DoctorReport()

    orphans = _orphan_plan_files(cwd)
    all_nums: set[int] = set()
    for p in orphans:
        data = _load_plan(p)
        if data:
            all_nums.update(_extract_gh_issue_nums(data.get("tasks", [])))
    issues_state = _gh_issues_state(sorted(all_nums)) if check_gh else {}

    for p in orphans:
        report.plans.append(diagnose_plan(p, issues_state))

    report.stale_state_file = _stale_state_file(cwd)

    ws = cwd / ".whilly_workspaces"
    if ws.is_dir():
        report.orphan_workspaces = sorted(d for d in ws.iterdir() if d.is_dir())

    wt = cwd / ".whilly_worktrees"
    if wt.is_dir():
        report.orphan_worktrees = sorted(d for d in wt.iterdir() if d.is_dir())

    report.whilly_tmux_sessions = _tmux_whilly_sessions()

    return report


def format_report(report: DoctorReport, *, color: bool = True) -> str:
    # ANSI fallback-safe: callers pass color=False in non-TTY contexts.
    RD, YL, GR, CY, D, R = (
        ("\033[31m", "\033[33m", "\033[32m", "\033[36m", "\033[2m", "\033[0m") if color else ("", "", "", "", "", "")
    )
    kind_color = {
        "ghost": RD,
        "stale": YL,
        "invalid_name": RD,
        "not_a_plan": D,
        "healthy": GR,
    }

    lines = [f"{CY}Whilly doctor — diagnostic report{R}", "=" * 40]

    if report.plans:
        lines.append(f"\n{CY}Orphan plan files (not picked up by discover_plans):{R}")
        for d in report.plans:
            c = kind_color.get(d.kind, "")
            lines.append(f"  {c}[{d.kind}]{R} {d.path.name} — {d.detail}")
    else:
        lines.append(f"\n{D}Orphan plan files: none{R}")

    if report.stale_state_file:
        lines.append(f"\n{YL}Stale state file:{R} {report.stale_state_file}  {D}(plan_file missing){R}")

    if report.orphan_workspaces:
        lines.append(f"\n{YL}Leftover .whilly_workspaces:{R}")
        for w in report.orphan_workspaces:
            lines.append(f"  {w.name}")

    if report.orphan_worktrees:
        lines.append(f"\n{YL}Leftover .whilly_worktrees:{R}")
        for w in report.orphan_worktrees:
            lines.append(f"  {w.name}")

    if report.whilly_tmux_sessions:
        lines.append(f"\n{YL}Leftover tmux sessions:{R}")
        for s in report.whilly_tmux_sessions:
            lines.append(f"  {s}")

    if not report.findings:
        lines.append(f"\n{GR}All clean.{R}")
    else:
        lines.append(
            f"\n{D}Doctor is read-only. Clean up manually: "
            f"`rm` ghost plans, `tmux kill-session -t <name>`, archive workspaces.{R}"
        )

    return "\n".join(lines)
