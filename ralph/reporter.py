import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("ralph")


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_duration(seconds: int | float) -> str:
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


@dataclass
class IterationReport:
    iteration: int = 0
    duration_s: float = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_create_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    tasks_before: int = 0
    tasks_after: int = 0
    task_completed: bool = False
    agent_exit: int = 0
    task_ids: list[str] = field(default_factory=list)


@dataclass
class CostTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_create_tokens: int = 0
    cost_usd: float = 0.0

    def add_usage(self, usage) -> None:
        """Add AgentUsage to totals."""
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_create_tokens += usage.cache_create_tokens
        self.cost_usd += usage.cost_usd


class Reporter:
    """Generates JSON and Markdown cost/progress reports."""

    def __init__(self, plan_file: str, project: str, agent: str, report_dir: str = ".planning/reports"):
        self.plan_file = plan_file
        self.project = project
        self.agent = agent
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = Path(plan_file).stem
        self.json_path = self.report_dir / f"ralph_{base}_{ts}.json"

        self.started_at = datetime.now(timezone.utc).isoformat()
        self.iterations: list[IterationReport] = []
        self.totals = CostTotals()
        self._save_initial()

    def _save_initial(self) -> None:
        report = {
            "plan_file": self.plan_file,
            "project": self.project,
            "agent": self.agent,
            "started_at": self.started_at,
            "finished_at": None,
            "iterations": [],
            "totals": {},
        }
        self.json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        log.info(f"Report: {self.json_path}")

    def add_iteration(self, report: IterationReport) -> None:
        self.iterations.append(report)
        self._save_json()

    def finalize(
        self,
        total_iterations: int,
        duration_s: float,
        initial_tasks: int,
        final_tasks: int,
        done_tasks: int,
    ) -> None:
        self._save_json(
            finalize=True,
            total_iters=total_iterations,
            duration_s=duration_s,
            initial_tasks=initial_tasks,
            final_tasks=final_tasks,
            done_tasks=done_tasks,
        )

    def _save_json(self, finalize: bool = False, **kwargs) -> None:
        report = {
            "plan_file": self.plan_file,
            "project": self.project,
            "agent": self.agent,
            "started_at": self.started_at,
            "finished_at": datetime.now(timezone.utc).isoformat() if finalize else None,
            "iterations": [asdict(it) for it in self.iterations],
            "totals": (
                asdict(self.totals)
                if not finalize
                else {
                    **asdict(self.totals),
                    "iterations": kwargs.get("total_iters", len(self.iterations)),
                    "duration_s": kwargs.get("duration_s", 0),
                    "tasks_initial": kwargs.get("initial_tasks", 0),
                    "tasks_final": kwargs.get("final_tasks", 0),
                    "tasks_done": kwargs.get("done_tasks", 0),
                }
            ),
        }
        self.json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))


def generate_summary(report_files: list[Path], output_dir: Path) -> Path | None:
    """Generate markdown summary across multiple plan reports."""
    reports = []
    for f in report_files:
        try:
            reports.append(json.loads(f.read_text()))
        except Exception:
            continue

    if not reports:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_path = output_dir / f"ralph_summary_{ts}.md"

    grand = {"iters": 0, "in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0, "dur": 0, "done": 0}
    for r in reports:
        t = r.get("totals", {})
        grand["iters"] += t.get("iterations", 0)
        grand["in"] += t.get("input_tokens", 0)
        grand["out"] += t.get("output_tokens", 0)
        grand["cr"] += t.get("cache_read_tokens", 0)
        grand["cw"] += t.get("cache_create_tokens", 0)
        grand["cost"] += t.get("cost_usd", 0)
        grand["dur"] += t.get("duration_s", 0)
        grand["done"] += t.get("tasks_done", 0)

    lines = [
        "# Ralph Cost Report\n",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}  ",
        f"**Plans executed:** {len(reports)}  \n",
        "## Summary\n",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total iterations | {grand['iters']} |",
        f"| Total duration | {fmt_duration(grand['dur'])} |",
        f"| Tasks completed | {grand['done']} |",
        f"| Input tokens | {fmt_tokens(grand['in'])} |",
        f"| Output tokens | {fmt_tokens(grand['out'])} |",
        f"| Cache read | {fmt_tokens(grand['cr'])} |",
        f"| Cache create | {fmt_tokens(grand['cw'])} |",
        f"| **Total cost** | **${grand['cost']:.4f}** |\n",
    ]

    lines.extend(
        [
            "## Plans\n",
            "| Plan | Project | Iters | Duration | Tasks | In | Out | Cost |",
            "|------|---------|-------|----------|-------|----|-----|------|",
        ]
    )
    for r in reports:
        t = r.get("totals", {})
        done = t.get("tasks_done", 0)
        total = t.get("tasks_final", t.get("tasks_initial", "?"))
        lines.append(
            f"| `{r.get('plan_file', '')}` "
            f"| {r.get('project', '')} "
            f"| {t.get('iterations', 0)} "
            f"| {fmt_duration(t.get('duration_s', 0))} "
            f"| {done}/{total} "
            f"| {fmt_tokens(t.get('input_tokens', 0))} "
            f"| {fmt_tokens(t.get('output_tokens', 0))} "
            f"| ${t.get('cost_usd', 0):.4f} |"
        )

    summary_path.write_text("\n".join(lines) + "\n")
    log.info(f"Summary: {summary_path}")
    return summary_path
