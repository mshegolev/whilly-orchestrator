"""Pure-function tests for the ``whilly plan show`` graph renderer (TASK-015).

The :func:`whilly.cli.plan.render_plan_graph` function takes core models
(no DB, no asyncio) and returns a plain string. This file pins its
output with byte-level snapshots so any future formatting drift surfaces
as a literal failure instead of silent regression.

Why split unit + integration?
-----------------------------
The integration tests in :mod:`tests.integration.test_plan_show` need
testcontainers Postgres because they exercise the full
``import → SELECT → render → print`` pipeline. Layout pinning belongs
*here* instead — re-pinning the snapshot inside an integration test
would force a Docker reboot every time we tweak a separator character,
and would couple a layout test to an external service it doesn't need.

The split mirrors the same one already in place for ``plan import`` /
``plan export``: pure parsing / serialisation is unit-tested in
:mod:`tests.unit.test_plan_io`, and the SQL plumbing on top is
integration-tested in :mod:`tests.integration.test_plan_io`.
"""

from __future__ import annotations

from whilly.cli.plan import render_plan_graph
from whilly.core.models import Plan, Priority, Task, TaskStatus
from whilly.core.scheduler import detect_cycles


def test_render_plan_graph_snapshot_acyclic_three_tasks() -> None:
    """Renderer output is byte-identical to the canonical snapshot.

    Three tasks at three priorities and three statuses with deterministic
    dependency edges. The expected string below is the canonical layout
    contract; any change to badge width, separator characters, or
    summary formatting must update this fixture deliberately.
    """
    t1 = Task(
        id="T-001",
        status=TaskStatus.DONE,
        priority=Priority.CRITICAL,
        description="Bootstrap.",
    )
    t2 = Task(
        id="T-002",
        status=TaskStatus.IN_PROGRESS,
        dependencies=("T-001",),
        priority=Priority.HIGH,
        description="Build.",
    )
    t3 = Task(
        id="T-003",
        status=TaskStatus.PENDING,
        dependencies=("T-001", "T-002"),
        priority=Priority.LOW,
        description="Ship.",
    )
    plan = Plan(id="demo-plan", name="Demo", tasks=(t1, t2, t3))
    cycles = detect_cycles(plan)
    rendered = render_plan_graph(plan, (t1, t2, t3), cycles, use_color=False)

    expected = (
        "Plan: demo-plan — Demo\n"
        "────────────────────────────────────────\n"
        "[DONE       ] T-001  (critical)\n"
        "[IN_PROGRESS] T-002  (high)\n"
        "    └─ depends on: T-001\n"
        "[PENDING    ] T-003  (low)\n"
        "    └─ depends on: T-001, T-002\n"
        "\n"
        "Summary: 3 tasks · PENDING=1 · IN_PROGRESS=1 · DONE=1\n"
    )
    assert rendered == expected


def test_render_plan_graph_two_calls_are_byte_identical() -> None:
    """Determinism property: two renders of the same inputs match exactly.

    Operationally important — if two consecutive renders diverge (dict
    iteration order leaking through, set hash randomisation, etc.),
    operators ``diff``-ing snapshots between runs would see noise.
    """
    t1 = Task(id="A", status=TaskStatus.PENDING, priority=Priority.MEDIUM, description="")
    t2 = Task(id="B", status=TaskStatus.PENDING, dependencies=("A",), priority=Priority.MEDIUM, description="")
    plan = Plan(id="p", name="p", tasks=(t1, t2))
    first = render_plan_graph(plan, (t1, t2), detect_cycles(plan), use_color=False)
    second = render_plan_graph(plan, (t1, t2), detect_cycles(plan), use_color=False)
    assert first == second


def test_render_plan_graph_cycle_is_announced_above_the_graph() -> None:
    """A cycle prepends ``Cycle detected: A → B → C → A`` to the output.

    AC verbatim: "Цикл → exit code 1 + сообщение 'Cycle detected: A →
    B → C → A'". The exit-code half is verified by the integration
    tests; here we lock the *banner* shape so any machine-parsers
    (CI, dashboards) grepping for the format see something stable.
    """
    ta = Task(id="A", status=TaskStatus.PENDING, dependencies=("C",), priority=Priority.HIGH, description="")
    tb = Task(id="B", status=TaskStatus.PENDING, dependencies=("A",), priority=Priority.HIGH, description="")
    tc = Task(id="C", status=TaskStatus.PENDING, dependencies=("B",), priority=Priority.HIGH, description="")
    plan = Plan(id="cycle", name="Cycle", tasks=(ta, tb, tc))
    cycles = detect_cycles(plan)

    rendered = render_plan_graph(plan, (ta, tb, tc), cycles, use_color=False)
    assert rendered.startswith("Cycle detected: A → B → C → A\n"), (
        f"cycle banner missing or malformed; first line: {rendered.splitlines()[0]!r}"
    )
    # The graph still renders below so the operator sees the cycle
    # members in context, not just the banner.
    assert "[PENDING    ] A  (high)" in rendered
    assert "[PENDING    ] B  (high)" in rendered
    assert "[PENDING    ] C  (high)" in rendered


def test_render_plan_graph_self_loop_is_announced() -> None:
    """A self-loop ``A → A`` renders as ``Cycle detected: A → A``.

    Mirrors the rendering rule in
    :func:`whilly.cli.plan._format_cycle` (a single-node cycle still
    closes the loop visually so the user sees that the cycle exists
    rather than being puzzled by a single id with no arrow). We assert
    the banner shape here so the rendering rule and the cycle-detector
    output stay in sync — :func:`detect_cycles` returns ``[["A"]]`` for
    a self-loop, and we want the plan-show banner to read the same way
    every time.
    """
    ta = Task(id="A", status=TaskStatus.PENDING, dependencies=("A",), priority=Priority.HIGH, description="")
    plan = Plan(id="self", name="Self", tasks=(ta,))
    cycles = detect_cycles(plan)
    rendered = render_plan_graph(plan, (ta,), cycles, use_color=False)
    assert rendered.startswith("Cycle detected: A → A\n")


def test_render_plan_graph_color_branch_emits_rich_tags() -> None:
    """``use_color=True`` wraps each badge in ``[<color>]...[/<color>]`` tags.

    Future renderers that embed the graph inside a larger Rich layout
    (TASK-027 dashboard, for instance) depend on these tags being
    parseable by ``rich.console.Console``. We assert the tag shape per
    status; the actual ANSI emission is Rich's business and not part of
    our contract.
    """
    t_done = Task(id="X", status=TaskStatus.DONE, priority=Priority.HIGH, description="")
    t_failed = Task(id="Y", status=TaskStatus.FAILED, priority=Priority.HIGH, description="")
    plan = Plan(id="p", name="p", tasks=(t_done, t_failed))

    rendered = render_plan_graph(plan, (t_done, t_failed), detect_cycles(plan), use_color=True)
    assert "[green]DONE" in rendered, "DONE status must be wrapped in [green]...[/green]"
    assert "[red]FAILED" in rendered, "FAILED status must be wrapped in [red]...[/red]"
    assert "[/green]" in rendered
    assert "[/red]" in rendered


def test_render_plan_graph_no_dependencies_omits_arrow() -> None:
    """Tasks with no in-plan deps don't get the ``└─ depends on:`` line.

    Layout choice: dep-less tasks are leaves at the top of the DAG and
    the arrow line would be visual noise. The summary line still
    accounts for them; only the per-task arrow is suppressed.
    """
    t = Task(id="solo", status=TaskStatus.PENDING, priority=Priority.MEDIUM, description="")
    plan = Plan(id="p", name="p", tasks=(t,))
    rendered = render_plan_graph(plan, (t,), detect_cycles(plan), use_color=False)
    assert "depends on" not in rendered
    assert "[PENDING    ] solo" in rendered


def test_render_plan_graph_summary_omits_zero_buckets() -> None:
    """Summary only lists status buckets with ``count > 0``.

    Less visual noise than always printing ``DONE=0 · FAILED=0 · ...``,
    and matches the way ``git status`` only mentions sections that have
    content. The total ``N tasks`` prefix is always present so a reader
    can sanity-check arithmetic.
    """
    t1 = Task(id="A", status=TaskStatus.DONE, priority=Priority.HIGH, description="")
    t2 = Task(id="B", status=TaskStatus.DONE, priority=Priority.HIGH, description="")
    plan = Plan(id="p", name="p", tasks=(t1, t2))
    rendered = render_plan_graph(plan, (t1, t2), detect_cycles(plan), use_color=False)
    summary = rendered.splitlines()[-1]
    assert summary == "Summary: 2 tasks · DONE=2", f"got: {summary!r}"
