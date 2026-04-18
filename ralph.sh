#!/bin/bash
set -eE

MAX_ITERATIONS=${MAX_ITERATIONS:-0}
MAX_PARALLEL=${MAX_PARALLEL:-1}          # max concurrent agents (1=sequential, 2-3=parallel)
HEARTBEAT_INTERVAL=${HEARTBEAT_INTERVAL:-60}
DECOMPOSE_EVERY=${DECOMPOSE_EVERY:-5}   # re-plan every N completed tasks (0=off)
b
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ANSI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
R="\033[0m"; B="\033[1m"; D="\033[2m"
# shellcheck disable=SC2034
UL="\033[4m"
GR="\033[32m"; YL="\033[33m"; CY="\033[36m"; RD="\033[31m"
MG="\033[35m"; WH="\033[97m"; BGB="\033[44m"; BGD="\033[100m"
HI="\033[?25l"; SH="\033[?25h"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Plan selection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

validate_schema() {
    python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
tasks = d.get('tasks')
assert isinstance(tasks, list) and len(tasks) > 0
assert all('id' in t and 'status' in t for t in tasks[:3])
" "$1" 2>/dev/null
}

get_project_name() {
    python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('project','(unnamed)'))" "$1" 2>/dev/null || echo "(unnamed)"
}

discover_plans() {
    [[ -f "tasks.json" ]] && validate_schema "tasks.json" && PLAN_FILES+=("tasks.json") || true
    for f in .planning/*tasks*.json; do
        [[ -f "$f" ]] && validate_schema "$f" && PLAN_FILES+=("$f") || true
    done
}

select_plan_interactive() {
    [[ ${#PLAN_FILES[@]} -eq 0 ]] && { echo -e "${RD}No plan files found${R}" >&2; exit 1; }
    echo ""
    echo -e "${B}  RALPH — Select Plan${R}"
    echo ""
    for i in "${!PLAN_FILES[@]}"; do
        local name; name=$(get_project_name "${PLAN_FILES[$i]}")
        local tc; tc=$(python3 -c "import json; print(len(json.load(open('${PLAN_FILES[$i]}')).get('tasks',[])))" 2>/dev/null || echo "?")
        printf "  ${GR}%d)${R} %-40s ${D}[%d tasks]${R}  %s\n" $((i+1)) "$name" "$tc" "${PLAN_FILES[$i]}"
    done
    echo -e "  ${GR}a)${R} All plans"
    echo ""
    read -rp "  Select (1-${#PLAN_FILES[@]}, a=all, q=quit): " choice
    case "$choice" in
        q|Q) exit 0 ;;
        a|A) return ;;
        *)
            if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#PLAN_FILES[@]} )); then
                PLAN_FILES=("${PLAN_FILES[$((choice-1))]}")
            else
                echo -e "${RD}Invalid choice${R}" >&2; exit 1
            fi ;;
    esac
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Parse arguments / select plans
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PLAN_FILES=()

if [[ "${1:-}" == "--all" ]]; then
    discover_plans
elif [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    echo "Usage: ralph.sh [OPTIONS] [PLAN_FILE...]"
    echo ""
    echo "  ralph.sh                          Use tasks.json or interactive menu"
    echo "  ralph.sh plan1.json plan2.json    Run specific plan files"
    echo "  ralph.sh --all                    Run all discovered plans"
    echo "  ralph.sh -h, --help               Show this help"
    echo ""
    echo "Environment variables:"
    echo "  MAX_ITERATIONS=N     Max work iterations per plan (0=unlimited)"
    echo "  MAX_PARALLEL=N       Max concurrent agents (1=sequential, 2-3=parallel)"
    echo "  DECOMPOSE_EVERY=N    Re-plan every N done tasks (0=off)"
    echo "  HEARTBEAT_INTERVAL=N Dashboard refresh interval in seconds"
    echo "  RALPH_AGENT=NAME     Force agent (claude/claude-1m/claude-1m-4.7/codex)"
    exit 0
elif [[ $# -gt 0 ]]; then
    for f in "$@"; do
        [[ -f "$f" ]] || { echo -e "${RD}$f not found${R}" >&2; exit 1; }
        if validate_schema "$f"; then
            PLAN_FILES+=("$f")
        else
            echo -e "${YL}WARN: $f — incompatible schema, skipping${R}" >&2
        fi
    done
else
    if [[ -f "tasks.json" ]] && validate_schema "tasks.json"; then
        PLAN_FILES=("tasks.json")
    else
        discover_plans
        if [[ ${#PLAN_FILES[@]} -eq 0 ]]; then
            echo -e "${RD}No plan files found. Provide a path or place tasks.json in the current directory.${R}" >&2
            exit 1
        fi
        select_plan_interactive
    fi
fi

[[ ${#PLAN_FILES[@]} -eq 0 ]] && { echo -e "${RD}No valid plan files selected${R}" >&2; exit 1; }

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

elapsed() {
    local d=$(( $(date +%s) - START_TIME ))
    printf "%02d:%02d:%02d" $((d/3600)) $(((d%3600)/60)) $((d%60))
}

fmt_dur() {
    local s="$1"
    if (( s < 60 )); then printf "%ds" "$s"
    elif (( s < 3600 )); then printf "%dm%02ds" $((s/60)) $((s%60))
    else printf "%dh%02dm" $((s/3600)) $(((s%3600)/60)); fi
}

log_event() { echo "[$(date '+%H:%M:%S')] $*" >> "$LOG_FILE"; }

fmt_tokens() {
    python3 -c "
n=int(${1:-0})
if n>=1000000: print(f'{n/1000000:.1f}M',end='')
elif n>=1000: print(f'{n/1000:.1f}K',end='')
else: print(n,end='')
"
}

fmt_cost() { python3 -c "print(f'{float(${1:-0}):.2f}',end='')"; }

# Token tracking (cumulative per plan)
TOTAL_INPUT_TOKENS=0
TOTAL_OUTPUT_TOKENS=0
TOTAL_CACHE_READ=0
TOTAL_CACHE_CREATE=0
TOTAL_COST_USD="0.00"
ITER_INPUT_TOKENS=0
ITER_OUTPUT_TOKENS=0
ITER_COST_USD="0.00"
REPORT_FILE=""
REPORT_FILES=()   # collect all report paths for final summary

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Cost reporting
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

init_report() {
    local plan_file="$1" agent_name="$2"
    local project_name; project_name=$(get_project_name "$plan_file")
    REPORT_FILE=".planning/reports/ralph_$(basename "$plan_file" .json)_$(date +%Y%m%d_%H%M%S).json"
    mkdir -p "$(dirname "$REPORT_FILE")"

    python3 << PYEOF
import json
report = {
    "plan_file": "$plan_file",
    "project": "$project_name",
    "agent": "$agent_name",
    "started_at": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
    "finished_at": None,
    "iterations": [],
    "totals": {}
}
with open("$REPORT_FILE", "w") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)
PYEOF
    REPORT_FILES+=("$REPORT_FILE")
    log_event "Report: $REPORT_FILE"
}

append_iteration_report() {
    local iter="$1" dur_s="$2" tasks_before="$3" tasks_after="$4" completed="$5" exit_code="$6"
    [[ -z "$REPORT_FILE" || ! -f "$REPORT_FILE" ]] && return 0

    python3 << PYEOF
import json

with open("$REPORT_FILE") as f:
    report = json.load(f)

report["iterations"].append({
    "iteration": $iter,
    "duration_s": $dur_s,
    "input_tokens": $ITER_INPUT_TOKENS,
    "output_tokens": $ITER_OUTPUT_TOKENS,
    "cache_read_tokens": ${ITER_CACHE_READ:-0},
    "cache_create_tokens": ${ITER_CACHE_CREATE:-0},
    "cost_usd": ${ITER_COST_USD:-0},
    "num_turns": ${ITER_NUM_TURNS:-0},
    "tasks_before": $tasks_before,
    "tasks_after": $tasks_after,
    "task_completed": $( [[ "$completed" == "true" ]] && echo "True" || echo "False" ),
    "agent_exit": $exit_code
})

with open("$REPORT_FILE", "w") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)
PYEOF
}

finalize_report() {
    local total_iters="$1" total_dur_s="$2"
    [[ -z "$REPORT_FILE" || ! -f "$REPORT_FILE" ]] && return 0

    python3 << PYEOF
import json

with open("$REPORT_FILE") as f:
    report = json.load(f)

report["finished_at"] = "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
report["totals"] = {
    "iterations": $total_iters,
    "duration_s": $total_dur_s,
    "input_tokens": $TOTAL_INPUT_TOKENS,
    "output_tokens": $TOTAL_OUTPUT_TOKENS,
    "cache_read_tokens": $TOTAL_CACHE_READ,
    "cache_create_tokens": $TOTAL_CACHE_CREATE,
    "cost_usd": ${TOTAL_COST_USD},
    "tasks_initial": $INITIAL_TASK_COUNT,
    "tasks_final": $(task_count),
    "tasks_done": $(done_count)
}

with open("$REPORT_FILE", "w") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)
PYEOF
    log_event "Report finalized: $REPORT_FILE"
}

generate_summary_report() {
    [[ ${#REPORT_FILES[@]} -eq 0 ]] && return 0

    local summary_file
    summary_file=".planning/reports/ralph_summary_$(date +%Y%m%d_%H%M%S).md"
    mkdir -p "$(dirname "$summary_file")"

    python3 - "$summary_file" "${REPORT_FILES[@]}" << 'PYEOF2'
import json, sys
from datetime import datetime

summary_file = sys.argv[1]
files = sys.argv[2:]
reports = []
for f in files:
    try:
        with open(f) as fh:
            reports.append(json.load(fh))
    except:
        pass

if not reports:
    sys.exit(0)

def fmt_tok(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.1f}K"
    return str(n)

def fmt_dur(s):
    if s >= 3600: return f"{s//3600}h{(s%3600)//60:02d}m"
    if s >= 60: return f"{s//60}m{s%60:02d}s"
    return f"{s}s"

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

with open(summary_file, "w") as f:
    f.write(f"# Ralph Cost Report\n\n")
    f.write(f"**Generated:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}  \n")
    f.write(f"**Plans executed:** {len(reports)}  \n\n")

    f.write(f"## Summary\n\n")
    f.write(f"| Metric | Value |\n|--------|-------|\n")
    f.write(f"| Total iterations | {grand['iters']} |\n")
    f.write(f"| Total duration | {fmt_dur(grand['dur'])} |\n")
    f.write(f"| Tasks completed | {grand['done']} |\n")
    f.write(f"| Input tokens | {fmt_tok(grand['in'])} |\n")
    f.write(f"| Output tokens | {fmt_tok(grand['out'])} |\n")
    f.write(f"| Cache read | {fmt_tok(grand['cr'])} |\n")
    f.write(f"| Cache create | {fmt_tok(grand['cw'])} |\n")
    f.write(f"| **Total cost** | **${grand['cost']:.4f}** |\n\n")

    f.write(f"## Plans\n\n")
    f.write(f"| Plan | Project | Iters | Duration | Tasks | In | Out | Cost |\n")
    f.write(f"|------|---------|-------|----------|-------|----|-----|------|\n")
    for r in reports:
        t = r.get("totals", {})
        done = t.get("tasks_done", 0)
        total = t.get("tasks_final", t.get("tasks_initial", "?"))
        f.write(
            f"| `{r.get('plan_file','')}` "
            f"| {r.get('project','')} "
            f"| {t.get('iterations',0)} "
            f"| {fmt_dur(t.get('duration_s',0))} "
            f"| {done}/{total} "
            f"| {fmt_tok(t.get('input_tokens',0))} "
            f"| {fmt_tok(t.get('output_tokens',0))} "
            f"| ${t.get('cost_usd',0):.4f} |\n"
        )
    f.write(f"\n")

    for r in reports:
        iters = r.get("iterations", [])
        if not iters:
            continue
        f.write(f"### {r.get('project', r.get('plan_file',''))}\n\n")
        f.write(f"| # | Duration | In | Out | Cache R | Cache W | Cost | Tasks | Done? |\n")
        f.write(f"|---|----------|-----|-----|---------|---------|------|-------|-------|\n")
        for it in iters:
            done_flag = "yes" if it.get("task_completed") else ""
            td = f"{it.get('tasks_before','?')}->{it.get('tasks_after','?')}"
            f.write(
                f"| {it.get('iteration',0)} "
                f"| {fmt_dur(it.get('duration_s',0))} "
                f"| {fmt_tok(it.get('input_tokens',0))} "
                f"| {fmt_tok(it.get('output_tokens',0))} "
                f"| {fmt_tok(it.get('cache_read_tokens',0))} "
                f"| {fmt_tok(it.get('cache_create_tokens',0))} "
                f"| ${it.get('cost_usd',0):.4f} "
                f"| {td} "
                f"| {done_flag} |\n"
            )
        f.write(f"\n")

print(f"Report: {summary_file}")
PYEOF2

    echo -e " ${GR}${B}Report:${R} $summary_file"
    log_event "Summary report: $summary_file"
}

task_count() {
    python3 -c "import json; print(len(json.load(open('$TASKS_FILE')).get('tasks',[])))"
}

done_count() {
    python3 -c "import json; print(sum(1 for t in json.load(open('$TASKS_FILE')).get('tasks',[]) if t.get('status')=='done'))"
}

reset_in_progress_tasks() {
    local reset_count
    reset_count=$(TASKS_FILE="$TASKS_FILE" python3 << 'PYEOF'
import json, os

with open(os.environ["TASKS_FILE"]) as f:
    data = json.load(f)

count = 0
for t in data.get("tasks", []):
    if t.get("status") == "in_progress":
        t["status"] = "pending"
        count += 1

if count > 0:
    with open(os.environ["TASKS_FILE"], "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

print(count)
PYEOF
    )
    if (( reset_count > 0 )); then
        log_event "Reset $reset_count in_progress tasks to pending"
    fi
}

has_pending_tasks() {
    python3 -c "
import json
with open('$TASKS_FILE') as f:
    t = json.load(f).get('tasks', [])
exit(0 if any(x.get('status')=='pending' for x in t) else 1)
"
}

has_critical_task() {
    # Check if any of the given task IDs have priority "critical"
    TASKS_FILE="$TASKS_FILE" python3 - "$@" << 'PYEOF'
import json, sys, os
ids = set(sys.argv[1:])
with open(os.environ["TASKS_FILE"]) as f:
    tasks = json.load(f).get("tasks", [])
sys.exit(0 if any(t["id"] in ids and t.get("priority") == "critical" for t in tasks) else 1)
PYEOF
}

critical_done_count() {
    python3 -c "
import json
with open('$TASKS_FILE') as f:
    t = json.load(f).get('tasks', [])
print(sum(1 for x in t if x.get('status')=='done' and x.get('priority')=='critical'))
"
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Parse tasks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

parse_tasks() {
    TASKS_FILE="$TASKS_FILE" python3 << 'PYEOF'
import json, os

PRIO = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}

with open(os.environ["TASKS_FILE"]) as f:
    data = json.load(f)
tasks = data.get("tasks", [])
status_map = {t["id"]: t.get("status", "?") for t in tasks}

by = {}
for t in tasks:
    by.setdefault(t.get("status", "?"), []).append(t)

total = len(tasks)
done = len(by.get("done", []))
pending = len(by.get("pending", []))
ip = len(by.get("in_progress", []))
fail = len(by.get("failed", []))
skip = len(by.get("skipped", []))
print(f"C|{total}|{done}|{pending}|{ip}|{fail}|{skip}")

for t in by.get("done", [])[-5:]:
    print(f"D|{t['id']}|{t.get('priority','?')}|{t.get('category','?')}|{t['description'][:60]}")

for t in by.get("in_progress", []):
    print(f"I|{t['id']}|{t.get('priority','?')}|{t.get('category','?')}|{t['description'][:60]}")

for t in by.get("failed", []):
    print(f"F|{t['id']}|{t.get('priority','?')}|{t.get('category','?')}|{t['description'][:60]}")

pend = sorted(by.get("pending", []), key=lambda t: (PRIO.get(t.get("priority","low"),9), t.get("phase",99)))
for t in pend[:10]:
    deps = t.get("dependencies", [])
    blocked = any(status_map.get(d) != "done" for d in deps) if deps else False
    flag = "BLK" if blocked else "RDY"
    dep_str = ",".join(d.replace("TASK-","T") for d in deps[:3]) or "-"
    print(f"Q|{t['id']}|{t.get('priority','?')}|{t.get('category','?')}|P{t.get('phase','?')}|{flag}|{dep_str}|{t['description'][:50]}")
PYEOF
}

# Get full task detail as formatted text
task_detail() {
    local tid="$1"
    TASKS_FILE="$TASKS_FILE" python3 - "$tid" << 'PYEOF'
import json, sys, textwrap, os

tid = sys.argv[1]
with open(os.environ["TASKS_FILE"]) as f:
    tasks = json.load(f).get("tasks", [])

found = [t for t in tasks if t["id"] == tid]
if not found:
    # fuzzy: match partial
    found = [t for t in tasks if tid.lower() in t["id"].lower()]
if not found:
    print(f"Task '{tid}' not found")
    sys.exit(0)

t = found[0]
print(f"{'─'*70}")
print(f"  {t['id']}  [{t.get('status','?')}]  priority={t.get('priority','?')}  phase={t.get('phase','?')}")
print(f"  category: {t.get('category','?')}")
print(f"{'─'*70}")
print(f"\n  Description:")
for line in textwrap.wrap(t.get("description",""), 65):
    print(f"    {line}")

deps = t.get("dependencies", [])
if deps:
    print(f"\n  Dependencies: {', '.join(deps)}")

ac = t.get("acceptance_criteria", [])
if ac:
    print(f"\n  Acceptance Criteria:")
    for i, c in enumerate(ac, 1):
        for j, line in enumerate(textwrap.wrap(c, 62)):
            if j == 0:
                print(f"    {i}. {line}")
            else:
                print(f"       {line}")

ts = t.get("test_steps", [])
if ts:
    print(f"\n  Test Steps:")
    for i, s in enumerate(ts, 1):
        for j, line in enumerate(textwrap.wrap(s, 62)):
            if j == 0:
                print(f"    {i}. {line}")
            else:
                print(f"       {line}")
print(f"\n{'─'*70}")
PYEOF
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Dashboard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

INITIAL_TASK_COUNT=0
LAST_TASK_COUNT=0
TASK_DELTA_MSG=""
CURRENT_PHASE=""

show_dashboard() {
    local agent="$1" iter="$2" status_msg="${3:-}" heartbeat_line="${4:-}"

    local rows cols
    rows=$(tput lines 2>/dev/null || echo 40)
    cols=$(tput cols  2>/dev/null || echo 100)
    local W=$cols

    local lines=()
    local parsed
    parsed=$(parse_tasks)

    local counts_line
    counts_line=$(echo "$parsed" | grep '^C|' | head -1)
    local task_done task_total task_pending task_ip task_fail task_skip
    IFS='|' read -r _ task_total task_done task_pending task_ip task_fail task_skip <<< "$counts_line"

    local pct=0; (( task_total > 0 )) && pct=$(( task_done * 100 / task_total ))
    local bw=$(( W - 30 )); (( bw < 10 )) && bw=10; (( bw > 60 )) && bw=60
    local filled empty
    filled=$(( pct * bw / 100 )); empty=$(( bw - filled ))
    local bar=""
    for ((i=0; i<filled; i++)); do bar+="█"; done
    for ((i=0; i<empty; i++)); do bar+="░"; done

    # Header
    local phase_badge=""
    case "$CURRENT_PHASE" in
        plan) phase_badge="${MG}${B}PLAN${R}" ;;
        work) phase_badge="${CY}${B}WORK${R}" ;;
        *)    phase_badge="${D}——${R}" ;;
    esac
    local title
    title=" RALPH  ◆  $(elapsed)  ◆  $(echo "$agent" | tr '[:lower:]' '[:upper:]')  ◆  iter ${iter}/${MAX_ITERATIONS}"
    local title_pad=$(( W - ${#title} - 2 ))
    (( title_pad < 0 )) && title_pad=0
    lines+=("$(printf "${BGB}${WH}${B} %s%*s ${R}" "$title" "$title_pad" "")")

    local delta_indicator=""
    [[ -n "$TASK_DELTA_MSG" ]] && delta_indicator="  ${MG}${B}${TASK_DELTA_MSG}${R}"
    lines+=("$(printf " %b  ${GR}%s${R}  %s%%  ${B}%s${R}/${D}%s${R} done%b" "$phase_badge" "$bar" "$pct" "$task_done" "$task_total" "$delta_indicator")")

    local cnt=""
    cnt+="  ${GR}●${task_done} done${R}  ${YL}●${task_pending} pend${R}"
    (( task_ip > 0 ))   && cnt+="  ${CY}●${task_ip} wip${R}"
    (( task_fail > 0 )) && cnt+="  ${RD}●${task_fail} fail${R}"
    (( task_skip > 0 )) && cnt+="  ${D}●${task_skip} skip${R}"
    (( INITIAL_TASK_COUNT > 0 && task_total != INITIAL_TASK_COUNT )) && \
        cnt+="  ${D}│${R} ${MG}total ${INITIAL_TASK_COUNT}→${task_total}${R}"
    lines+=("$(printf " %b" "$cnt")")

    # Token usage line
    if (( TOTAL_INPUT_TOKENS + TOTAL_OUTPUT_TOKENS > 0 )); then
        local tok_line
        tok_line="  ${D}tokens:${R} ${CY}↓$(fmt_tokens "$TOTAL_INPUT_TOKENS")${R} ${GR}↑$(fmt_tokens "$TOTAL_OUTPUT_TOKENS")${R}"
        (( TOTAL_CACHE_READ > 0 )) && tok_line+="  ${D}cache-r:$(fmt_tokens $TOTAL_CACHE_READ)${R}"
        (( TOTAL_CACHE_CREATE > 0 )) && tok_line+="  ${D}cache-w:$(fmt_tokens $TOTAL_CACHE_CREATE)${R}"
        tok_line+="  ${YL}\$$(fmt_cost "$TOTAL_COST_USD")${R}"
        lines+=("$(printf " %b" "$tok_line")")
    fi
    lines+=("")

    # In Progress
    local ip_lines; ip_lines=$(echo "$parsed" | grep '^I|' || true)
    if [[ -n "$ip_lines" ]]; then
        lines+=("$(printf " %b" "${CY}${B}▶ IN PROGRESS${R}")")
        while IFS='|' read -r _ tid prio cat desc; do
            local pf="$prio"; [[ "$prio" == "critical" ]] && pf="${RD}${B}crit${R}"
            lines+=("$(printf "   ${CY}%-9s${R} ${D}%-8s${R} %b ${D}%s${R}" "$tid" "$cat" "$pf" "${desc:0:$((W-30))}")")
        done <<< "$ip_lines"
        lines+=("")
    fi

    # Queue
    local q_lines; q_lines=$(echo "$parsed" | grep '^Q|' || true)
    if [[ -n "$q_lines" ]]; then
        lines+=("$(printf " %b" "${YL}${B}◎ QUEUE${R} ${D}(next by priority)${R}")")
        lines+=("$(printf "   ${D}%-4s %-9s %-8s %-8s %-5s %-4s %-10s %s${R}" "#" "ID" "PRIO" "CAT" "PH" "ST" "DEPS" "DESCRIPTION")")
        local rank=1
        while IFS='|' read -r _ tid prio cat phase flag deps desc; do
            local c="$YL" fc="${GR}${flag}${R}"
            [[ "$flag" == "BLK" ]] && c="$D" && fc="${RD}BLK${R}"
            local pf="$prio"
            [[ "$prio" == "critical" ]] && pf="${RD}crit${R}${c}"
            [[ "$prio" == "high" ]] && pf="${MG}high${R}${c}"
            lines+=("$(printf "   ${c}%-4s %-9s %-8s %-8s %-5s${R} %b ${c}%-10s %s${R}" "$rank" "$tid" "$pf" "$cat" "$phase" "$fc" "$deps" "${desc:0:$((W-52))}")")
            ((rank++)) || true
        done <<< "$q_lines"
        lines+=("")
    fi

    # Done
    local d_lines; d_lines=$(echo "$parsed" | grep '^D|' || true)
    if [[ -n "$d_lines" ]]; then
        lines+=("$(printf " %b" "${GR}${B}✓ COMPLETED${R} ${D}(last 5)${R}")")
        while IFS='|' read -r _ tid prio cat desc; do
            lines+=("$(printf "   ${GR}${D}%-9s %-8s %s${R}" "$tid" "$cat" "${desc:0:$((W-30))}")")
        done <<< "$d_lines"
        lines+=("")
    fi

    # Failed
    local f_lines; f_lines=$(echo "$parsed" | grep '^F|' || true)
    if [[ -n "$f_lines" ]]; then
        lines+=("$(printf " %b" "${RD}${B}✗ FAILED${R}")")
        while IFS='|' read -r _ tid prio cat desc; do
            lines+=("$(printf "   ${RD}%-9s %-8s %s${R}" "$tid" "$cat" "${desc:0:$((W-30))}")")
        done <<< "$f_lines"
        lines+=("")
    fi

    # Separator
    local sepline=""; for ((i=0; i<W; i++)); do sepline+="─"; done
    lines+=("$(printf "${D}%s${R}" "$sepline")")

    # Status + heartbeat
    [[ -n "$status_msg" ]]     && lines+=("$(printf " %b" "$status_msg")")
    [[ -n "$heartbeat_line" ]] && lines+=("$(printf " %b" "$heartbeat_line")")

    # Hotkey bar (always last)
    lines+=("$(printf " %b" "${BGD}${WH} q${R}${D}=stop  ${R}${BGD}${WH} d${R}${D}=detail  ${R}${BGD}${WH} l${R}${D}=log  ${R}${BGD}${WH} t${R}${D}=tasks  ${R}${BGD}${WH} h${R}${D}=help ${R}")")

    # Render — truncate to terminal
    printf "\033[H\033[J"
    local max_lines=$(( rows - 1 ))
    local count=0
    for line in "${lines[@]}"; do
        (( count >= max_lines )) && break
        echo -e "$line"
        ((count++))
    done
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Interactive overlays
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Show overlay, wait for any key to dismiss
show_overlay() {
    local title="$1"
    shift
    local content=("$@")

    local rows cols
    rows=$(tput lines 2>/dev/null || echo 40)
    cols=$(tput cols  2>/dev/null || echo 100)

    printf "\033[H\033[J"
    echo -e "${BGB}${WH}${B} ${title} ${R}  ${D}(press any key to return)${R}"
    echo ""

    local max=$(( rows - 4 ))
    local i=0
    for line in "${content[@]}"; do
        (( i >= max )) && break
        echo -e " ${line:0:$((cols-2))}"
        ((i++))
    done

    echo ""
    echo -e " ${D}... press any key ...${R}"

    # Wait for keypress
    read -rsn1 _
}

# Detail view: ask for task ID, show full detail
show_task_detail() {
    local rows cols
    rows=$(tput lines 2>/dev/null || echo 40)
    cols=$(tput cols  2>/dev/null || echo 100)

    printf "\033[H\033[J"
    printf '%b' "${SH}"

    # Restore terminal for interactive input
    stty echo icanon 2>/dev/null || true

    echo -e "${BGB}${WH}${B} TASK DETAIL ${R}"
    echo ""

    # Show available IDs
    echo -e " ${D}Available tasks:${R}"
    python3 -c "
import json
with open('$TASKS_FILE') as f:
    tasks = json.load(f).get('tasks', [])
icons = {'done':'✓','pending':'○','in_progress':'▶','failed':'✗','skipped':'−'}
for t in tasks:
    ic = icons.get(t.get('status','?'), '?')
    print(f\"   {ic} {t['id']:<10} [{t.get('status','?'):<11}] {t.get('description','')[:50]}\")
" 2>/dev/null | head -$(( rows - 8 )) || true

    echo ""
    echo -ne " ${B}Enter task ID${R} (or part of it, empty=cancel): "
    read -r tid_input || tid_input=""

    # Restore raw mode
    stty -echo -icanon min 0 time 0 2>/dev/null || true
    printf '%b' "${HI}"

    if [[ -z "$tid_input" ]]; then
        return
    fi

    local detail
    detail=$(task_detail "$tid_input" 2>&1) || true

    local detail_lines=()
    while IFS= read -r line; do
        detail_lines+=("$line")
    done <<< "$detail"

    show_overlay "TASK: $tid_input" "${detail_lines[@]}"
}

# Log viewer: last N lines of ralph.log
show_log_viewer() {
    local rows
    rows=$(tput lines 2>/dev/null || echo 40)
    local max_lines=$(( rows - 5 ))

    local log_lines=()
    if [[ -f "$LOG_FILE" ]]; then
        while IFS= read -r line; do
            log_lines+=("$line")
        done < <(tail -"$max_lines" "$LOG_FILE")
    else
        log_lines+=("(no log file yet)")
    fi

    show_overlay "LOG — $LOG_FILE (last $max_lines lines)" "${log_lines[@]}"
}

# All tasks summary
show_all_tasks() {
    local rows
    rows=$(tput lines 2>/dev/null || echo 40)

    local task_lines=()
    while IFS= read -r line; do
        task_lines+=("$line")
    done < <(python3 -c "
import json
with open('$TASKS_FILE') as f:
    tasks = json.load(f).get('tasks', [])
icons = {'done':'\033[32m✓','pending':'\033[33m○','in_progress':'\033[36m▶','failed':'\033[31m✗','skipped':'\033[2m−'}
prios = {'critical':'\033[31m','high':'\033[35m','medium':'','low':'\033[2m'}
for t in tasks:
    ic = icons.get(t.get('status','?'), '?')
    pc = prios.get(t.get('priority',''), '')
    print(f\" {ic} {t['id']:<10} {t.get('status','?'):<11}\033[0m {pc}{t.get('priority','?'):<8}\033[0m {t.get('category','?'):<12} P{t.get('phase','?')} {t.get('description','')[:50]}\")
" 2>/dev/null | head -$(( rows - 5 )) || true)

    show_overlay "ALL TASKS ($TASKS_FILE)" "${task_lines[@]}"
}

# Help screen
show_help() {
    local help_lines=(
        ""
        "${B}Keyboard shortcuts:${R}"
        ""
        "  ${BGD}${WH} q ${R}  ${B}Quit${R} — gracefully stop agent and exit Ralph"
        "  ${BGD}${WH} d ${R}  ${B}Detail${R} — show full detail for a specific task (by ID)"
        "  ${BGD}${WH} l ${R}  ${B}Log${R} — show last lines of ralph.log (agent output)"
        "  ${BGD}${WH} t ${R}  ${B}Tasks${R} — show all tasks with status overview"
        "  ${BGD}${WH} h ${R}  ${B}Help${R} — this screen"
        ""
        "${B}Configuration:${R}"
        ""
        "  MAX_ITERATIONS=${MAX_ITERATIONS}     max work iterations"
        "  MAX_PARALLEL=${MAX_PARALLEL}          concurrent agents (1=seq, 2-3=parallel)"
        "  DECOMPOSE_EVERY=${DECOMPOSE_EVERY}      re-plan every N done tasks (0=off)"
        "  HEARTBEAT_INTERVAL=${HEARTBEAT_INTERVAL}s  dashboard refresh interval"
        "  RALPH_AGENT=...       force agent (claude/claude-1m/claude-1m-4.7/codex)"
        ""
        "${B}Files:${R}"
        ""
        "  ${TASKS_FILE}    task definitions (read/write by agent)"
        "  ${LOG_FILE}       full agent output log"
        "  progress.txt   agent progress notes"
        ""
        "${D}Tip: run 'tail -f ralph.log' in another terminal for full agent output${R}"
    )
    show_overlay "HELP" "${help_lines[@]}"
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Wait loop — replaces background heartbeat.
# Runs in main thread, polls agent + keypresses.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Wait for agent PID, refreshing dashboard and listening for keys.
# Sets: agent_exit
# Args: $1=agent_pid $2=result_file $3=agent_name $4=iteration $5=iter_start
wait_for_agent() {
    local apid="$1" rfile="$2" aname="$3" iter="$4" iter_start="$5"

    local spinner='⣾⣽⣻⢿⡿⣟⣯⣷'
    local si=0 prev_size=0
    local last_refresh=0

    agent_exit=""

    # Put terminal in raw mode for keypress detection
    local old_stty
    old_stty=$(stty -g 2>/dev/null || true)
    stty -echo -icanon min 0 time 0 2>/dev/null || true

    while true; do
        # Check if agent is still running
        if ! kill -0 "$apid" 2>/dev/null; then
            # Agent finished — get exit code
            wait "$apid" 2>/dev/null && agent_exit=0 || agent_exit=$?
            break
        fi

        # Read keypress (non-blocking)
        local key=""
        key=$(dd bs=1 count=1 2>/dev/null || true)

        if [[ -n "$key" ]]; then
            case "$key" in
                q|Q)
                    # Graceful stop
                    stty "$old_stty" 2>/dev/null || true
                    printf '%b' "${SH}"
                    printf "\033[H\033[J"
                    echo -e "${YL}${B}Stopping agent (pid $apid)...${R}"
                    kill "$apid" 2>/dev/null || true
                    wait "$apid" 2>/dev/null || true
                    agent_exit=130  # like Ctrl+C

                    echo -e "${YL}Agent stopped.${R}"
                    log_event "User pressed Q — agent killed"

                    echo ""
                    echo -e "${B}What would you like to do?${R}"
                    echo -e "  ${BGD}${WH} r ${R}  Resume from next iteration"
                    echo -e "  ${BGD}${WH} x ${R}  Exit Ralph completely"
                    echo ""
                    echo -ne " Choice: "

                    stty echo icanon 2>/dev/null || true
                    read -rsn1 choice
                    stty -echo -icanon min 0 time 0 2>/dev/null || true
                    printf '%b' "${HI}"

                    if [[ "$choice" == "x" || "$choice" == "X" ]]; then
                        stty "$old_stty" 2>/dev/null || true
                        show_dashboard "$aname" "$iter" "${YL}${B}Stopped by user${R}  ${D}$(elapsed)${R}"
                        log_event "User chose EXIT"
                        printf '%b' "${SH}"
                        exit 0
                    fi

                    # Resume — return with error code so main loop continues
                    log_event "User chose RESUME"
                    stty "$old_stty" 2>/dev/null || true
                    return 0
                    ;;
                d|D)
                    show_task_detail
                    ;;
                l|L)
                    show_log_viewer
                    ;;
                t|T)
                    show_all_tasks
                    ;;
                h|H|'?')
                    show_help
                    ;;
            esac
            # After overlay, redraw dashboard immediately
            last_refresh=0
        fi

        # Refresh dashboard every HEARTBEAT_INTERVAL seconds
        local now
        now=$(date +%s)
        if (( now - last_refresh >= HEARTBEAT_INTERVAL )); then
            last_refresh=$now

            local dur=$(( now - iter_start ))
            local dur_fmt; dur_fmt=$(fmt_dur $dur)

            local cur_size=0
            [[ -f "$rfile" ]] && cur_size=$(wc -c < "$rfile" 2>/dev/null || echo 0)
            local delta=$(( cur_size - prev_size ))
            prev_size=$cur_size

            local sc="${spinner:$si:1}"
            si=$(( (si + 1) % ${#spinner} ))

            local act="${GR}+${delta}B${R}"
            (( delta == 0 )) && act="${YL}thinking${R}"

            local last_line=""
            [[ -f "$rfile" ]] && last_line=$(tail -20 "$rfile" 2>/dev/null | grep -v '^$' | tail -1 | cut -c1-60)

            local hb="${CY}${sc}${R} ${B}${aname}${R} ${D}${dur_fmt}${R}  ${act} ${D}($(( cur_size/1024 ))KB)${R}"
            [[ -n "$last_line" ]] && hb+="  ${D}│ ${last_line}${R}"

            local task_label=""
            [[ -n "${CURRENT_TASK_ID:-}" ]] && task_label="  ${MG}task: ${CURRENT_TASK_ID}${R}"

            show_dashboard "$aname" "$iter" \
                "${CY}${B}▶ 1 агент работает${R}${task_label}  ${D}(лог: ${LOG_FILE})${R}" \
                "$hb"
        fi

        # Small sleep to avoid busy-wait (100ms)
        sleep 0.1
    done

    stty "$old_stty" 2>/dev/null || true
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Agent
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

resolve_agent() {
    if [[ -n "${RALPH_AGENT:-}" ]]; then echo "$RALPH_AGENT"; return 0; fi
    if command -v claude >/dev/null 2>&1; then echo "claude-1m"; return 0; fi
    if command -v codex  >/dev/null 2>&1; then echo "codex";  return 0; fi
    return 1
}

run_agent() {
    local agent="$1" prompt="$2"
    case "$agent" in
        claude) claude --permission-mode acceptEdits --output-format json -p "$prompt" ;;
        claude-1m) claude --permission-mode acceptEdits --output-format json --model "claude-opus-4-6[1m]" -p "$prompt" ;;
        claude-1m-4.7) claude --permission-mode acceptEdits --output-format json --model "claude-opus-4-7[1m]" -p "$prompt" ;;
        codex)
            local of; of="$(mktemp -t ralph_codex.XXXXXX)"
            codex exec --full-auto --color never -C "$PWD" --output-last-message "$of" "$prompt" >/dev/null
            cat "$of"; rm -f "$of" ;;
        *) echo "Unsupported agent: $agent" >&2; return 1 ;;
    esac
}

# Extract usage from JSON result and update token counters.
# Extracts .result text into result_file, sets ITER_* vars, updates TOTAL_* vars.
extract_usage() {
    local raw_file="$1"
    [[ -f "$raw_file" ]] || return 0

    # Check if output is valid JSON (claude agents)
    if python3 -c "import json; json.load(open('$raw_file'))" 2>/dev/null; then
        python3 << PYEOF
import json

with open("$raw_file") as f:
    data = json.load(f)

# Write result text
with open("$raw_file.text", "w") as f:
    f.write(data.get("result", ""))

# Write usage to separate file for shell parsing
usage = data.get("usage", {})
with open("$raw_file.usage", "w") as f:
    f.write(f"ITER_INPUT_TOKENS={usage.get('input_tokens', 0)}\n")
    f.write(f"ITER_OUTPUT_TOKENS={usage.get('output_tokens', 0)}\n")
    f.write(f"ITER_CACHE_READ={usage.get('cache_read_input_tokens', 0)}\n")
    f.write(f"ITER_CACHE_CREATE={usage.get('cache_creation_input_tokens', 0)}\n")
    f.write(f"ITER_COST_USD={data.get('total_cost_usd', 0)}\n")
    f.write(f"ITER_NUM_TURNS={data.get('num_turns', 0)}\n")
    f.write(f"ITER_DURATION_MS={data.get('duration_ms', 0)}\n")
PYEOF
        # Replace raw JSON with just the text result
        mv "$raw_file.text" "$raw_file"

        # Source usage vars
        if [[ -f "$raw_file.usage" ]]; then
            # shellcheck source=/dev/null
            source "$raw_file.usage"
            rm -f "$raw_file.usage"

            TOTAL_INPUT_TOKENS=$(( TOTAL_INPUT_TOKENS + ITER_INPUT_TOKENS ))
            TOTAL_OUTPUT_TOKENS=$(( TOTAL_OUTPUT_TOKENS + ITER_OUTPUT_TOKENS ))
            TOTAL_CACHE_READ=$(( TOTAL_CACHE_READ + ITER_CACHE_READ ))
            TOTAL_CACHE_CREATE=$(( TOTAL_CACHE_CREATE + ITER_CACHE_CREATE ))
            TOTAL_COST_USD=$(echo "$TOTAL_COST_USD + $ITER_COST_USD" | bc -l 2>/dev/null || echo "$TOTAL_COST_USD")

            log_event "Tokens: in=$(fmt_tokens "$ITER_INPUT_TOKENS") out=$(fmt_tokens "$ITER_OUTPUT_TOKENS") cache_r=$(fmt_tokens "$ITER_CACHE_READ") cache_w=$(fmt_tokens "$ITER_CACHE_CREATE") cost=\$$(fmt_cost "$ITER_COST_USD")"
        fi
    else
        # Not JSON (codex or error) — reset iter counters
        ITER_INPUT_TOKENS=0
        ITER_OUTPUT_TOKENS=0
        ITER_COST_USD="0"
    fi
}

# Run agent in background + interactive wait loop.
# Sets: result_file, agent_exit
run_agent_interactive() {
    local agent="$1" prompt="$2" iter="$3" iter_start="$4"
    result_file="$(mktemp -t ralph_result.XXXXXX)"

    run_agent "$agent" "$prompt" > "$result_file" 2>&1 &
    local apid=$!

    wait_for_agent "$apid" "$result_file" "$agent" "$iter" "$iter_start"

    # Extract usage from JSON output (claude agents)
    extract_usage "$result_file"
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Decomposition
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

build_decompose_prompt() {
    cat <<EOF
@${TASKS_FILE}

Ты — планировщик задач. Проанализируй ${TASKS_FILE} и определи, есть ли задачи со статусом "pending"
которые слишком крупные, нечёткие или содержат несколько независимых шагов.

Критерии для декомпозиции:
- Задача описывает 2+ независимых действия (например "создать X и настроить Y")
- Описание слишком высокоуровневое, нет конкретных шагов
- acceptance_criteria содержит 5+ пунктов из разных областей
- Задача охватывает несколько файлов/модулей из разных доменов

Если находишь такие задачи:
1. Разбей каждую на 2-5 подзадач
2. Новые подзадачи: ID формата TASK-XXXa, TASK-XXXb, ...
3. Наследуют phase, category, priority родителя
4. Добавь dependencies между подзадачами если нужно
5. Замени родителя на подзадачи (удали оригинал, вставь подзадачи)
6. Обнови total_tasks в корне JSON

ПРАВИЛА:
- НЕ трогай задачи со статусом "done", "in_progress", "failed"
- НЕ меняй ID на которые ссылаются dependencies
- Обнови dependencies если они ссылались на разбитую задачу
- Сохрани валидный JSON

После анализа:
- Если были изменения: <promise>DECOMPOSED N</promise> (N=новых подзадач)
- Если изменений нет: <promise>NO_DECOMPOSE</promise>
EOF
}

run_decompose() {
    local agent="$1" iter="$2"

    CURRENT_PHASE="plan"
    local before_count; before_count=$(task_count)
    local plan_start; plan_start=$(date +%s)

    CURRENT_TASK_ID="decompose"
    show_dashboard "$agent" "$iter" \
        "${MG}${B}⚙ Анализ задач — декомпозиция...${R}"
    log_event "Decompose phase started (tasks=$before_count)"

    run_agent_interactive "$agent" "$(build_decompose_prompt)" "$iter" "$plan_start"
    CURRENT_TASK_ID=""

    { echo "=== Decompose [$(fmt_dur $(( $(date +%s) - plan_start )))] ==="; cat "$result_file"; echo; } >> "$LOG_FILE"

    local after_count; after_count=$(task_count)
    local diff=$(( after_count - before_count ))

    if (( diff > 0 )); then
        TASK_DELTA_MSG="⚙ +${diff} tasks from decompose (${before_count}→${after_count})"
        LAST_TASK_COUNT=$after_count
        log_event "Decomposed: +$diff tasks ($before_count→$after_count)"
        show_dashboard "$agent" "$iter" \
            "${MG}${B}⚙ Декомпозиция: +${diff} задач${R} ${D}(${before_count}→${after_count})${R}"
        command -v say >/dev/null 2>&1 && say -v Milena "Декомпозиция: добавлено ${diff} задач."
        rm -f "$result_file"
        sleep 3
        return 0
    else
        log_event "Decompose: no changes"
        TASK_DELTA_MSG=""
        rm -f "$result_file"
        return 1
    fi
}

needs_decompose() {
    TASKS_FILE="$TASKS_FILE" python3 << 'PYEOF'
import json, sys, os
with open(os.environ["TASKS_FILE"]) as f:
    tasks = json.load(f).get("tasks", [])
for t in tasks:
    if t.get("status") != "pending": continue
    if len(t.get("acceptance_criteria", [])) >= 6: sys.exit(0)
    d = t.get("description", "")
    if d.count(" и ") >= 2 or d.count(" + ") >= 1: sys.exit(0)
sys.exit(1)
PYEOF
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Parallel orchestration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Get list of ready (unblocked, pending) task IDs
get_ready_tasks() {
    TASKS_FILE="$TASKS_FILE" python3 << 'PYEOF'
import json, os

with open(os.environ["TASKS_FILE"]) as f:
    data = json.load(f)
tasks = data.get("tasks", [])
status_map = {t["id"]: t.get("status", "?") for t in tasks}

PRIO = {"critical": 0, "high": 1, "medium": 2, "low": 3}
ready = []
for t in tasks:
    if t.get("status") != "pending":
        continue
    deps = t.get("dependencies", [])
    blocked = any(status_map.get(d) != "done" for d in deps) if deps else False
    if not blocked:
        ready.append(t)

ready.sort(key=lambda t: (PRIO.get(t.get("priority", "low"), 9), t.get("phase", 99)))
for t in ready:
    print(t["id"])
PYEOF
}

# Ask orchestrator agent which tasks can run in parallel.
# Input: list of ready task IDs. Output: JSON array of task ID groups.
# Falls back to sequential (one task per batch) if agent fails.
build_orchestrator_prompt() {
    local ready_ids="$1"
    cat <<EOF
@${TASKS_FILE}

Ты — архитектор-оркестратор. Твоя задача: определить, какие из готовых задач можно выполнять ПАРАЛЛЕЛЬНО.

Готовые задачи (не заблокированы, статус pending):
${ready_ids}

Максимум параллельных агентов: ${MAX_PARALLEL}

Правила параллелизации:
1. Задачи можно параллелить если они:
   - Работают с РАЗНЫМИ файлами/модулями
   - Не имеют общих зависимостей по данным
   - Имеют чётко типизированный интерфейс между модулями
   - Относятся к разным категориям (infrastructure, feature, test, docs)

2. Задачи НЕЛЬЗЯ параллелить если они:
   - Редактируют одни и те же файлы
   - Одна зависит от результата другой (даже неявно)
   - Обе меняют конфигурацию или схему БД
   - Обе модифицируют один API/интерфейс

3. При сомнениях — НЕ параллелить (sequential безопаснее)

Ответь ТОЛЬКО валидным JSON — массив массивов task ID:
- Первый вложенный массив = первый батч (запустятся параллельно)
- Если задача не может быть параллелизирована — одна в своём батче

Пример ответа:
[["TASK-001", "TASK-003"], ["TASK-002"]]

Это значит: TASK-001 и TASK-003 параллельно, затем TASK-002 отдельно.

ВАЖНО: верни ТОЛЬКО JSON, без пояснений, без markdown.
EOF
}

# Run orchestrator and parse batch plan.
# Sets: PARALLEL_BATCHES (array of space-separated task ID groups)
run_orchestrator() {
    local agent="$1" iter="$2"

    local ready_ids
    ready_ids=$(get_ready_tasks)
    local ready_count
    ready_count=$(echo "$ready_ids" | grep -c . || echo 0)

    # If 0 or 1 ready tasks, no need for orchestrator
    if (( ready_count <= 1 )); then
        PARALLEL_BATCHES=()
        if [[ -n "$ready_ids" ]]; then
            PARALLEL_BATCHES+=("$ready_ids")
        fi
        return 0
    fi

    # If MAX_PARALLEL is 1, just queue sequentially by priority
    if (( MAX_PARALLEL <= 1 )); then
        PARALLEL_BATCHES=()
        while IFS= read -r tid; do
            [[ -n "$tid" ]] && PARALLEL_BATCHES+=("$tid")
        done <<< "$ready_ids"
        return 0
    fi

    CURRENT_PHASE="plan"
    show_dashboard "$agent" "$iter" \
        "${MG}${B}⚙ Оркестратор: анализ параллелизации...${R} ${D}(${ready_count} ready tasks)${R}"
    log_event "Orchestrator: analyzing $ready_count ready tasks for parallelization"

    local orch_prompt
    orch_prompt=$(build_orchestrator_prompt "$ready_ids")

    local orch_result
    orch_result="$(mktemp -t ralph_orch.XXXXXX)"
    run_agent "$agent" "$orch_prompt" > "$orch_result" 2>&1 || true

    # Extract batches from orchestrator output
    PARALLEL_BATCHES=()
    local parsed
    parsed=$(TASKS_FILE="$TASKS_FILE" python3 - "$orch_result" << 'PYEOF'
import json, sys, os, re

result_file = sys.argv[1]
try:
    with open(result_file) as f:
        raw = f.read()

    # Try to parse as claude JSON output first
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "result" in data:
            raw = data["result"]
    except:
        pass

    # Extract JSON array from text (might have markdown fences)
    raw = raw.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)

    batches = json.loads(raw)
    assert isinstance(batches, list)

    # Validate: only include known ready task IDs
    with open(os.environ["TASKS_FILE"]) as f:
        tasks = json.load(f).get("tasks", [])
    valid_ids = {t["id"] for t in tasks if t.get("status") == "pending"}

    for batch in batches:
        assert isinstance(batch, list)
        valid_batch = [tid for tid in batch if tid in valid_ids]
        if valid_batch:
            print("|".join(valid_batch))

except Exception as e:
    print(f"ERROR:{e}", file=sys.stderr)
    sys.exit(1)
PYEOF
    ) || true

    rm -f "$orch_result"

    if [[ -n "$parsed" ]]; then
        while IFS= read -r batch_line; do
            [[ -n "$batch_line" ]] && PARALLEL_BATCHES+=("$batch_line")
        done <<< "$parsed"
        log_event "Orchestrator: ${#PARALLEL_BATCHES[@]} batches planned (LLM)"
    else
        # Fallback: smart grouping by key_files overlap
        log_event "Orchestrator: LLM failed, using file-based grouping"
        local grouped
        grouped=$(MAX_PARALLEL="$MAX_PARALLEL" TASKS_FILE="$TASKS_FILE" READY_IDS="$ready_ids" python3 << 'PYEOF'
import json, os

max_p = int(os.environ.get("MAX_PARALLEL", 3))
ready_ids = [l.strip() for l in os.environ["READY_IDS"].strip().splitlines() if l.strip()]

with open(os.environ["TASKS_FILE"]) as f:
    tasks = {t["id"]: t for t in json.load(f).get("tasks", [])}

# Group tasks into batches where no two tasks share key_files
batches = []
remaining = list(ready_ids)

while remaining:
    batch = []
    batch_files = set()
    skip = []
    for tid in remaining:
        t = tasks.get(tid, {})
        files = set(t.get("key_files", []))
        if not files or not batch_files & files:
            batch.append(tid)
            batch_files |= files
            if len(batch) >= max_p:
                skip.extend(remaining[remaining.index(tid)+1:])
                break
        else:
            skip.append(tid)
    if batch:
        batches.append("|".join(batch))
    remaining = skip

for b in batches:
    print(b)
PYEOF
        ) || true

        PARALLEL_BATCHES=()
        if [[ -n "$grouped" ]]; then
            while IFS= read -r batch_line; do
                [[ -n "$batch_line" ]] && PARALLEL_BATCHES+=("$batch_line")
            done <<< "$grouped"
        fi
        log_event "Orchestrator: ${#PARALLEL_BATCHES[@]} batches planned (file-based)"
    fi
}

# Mark tasks as in_progress in tasks.json before launching agents
mark_tasks_status() {
    local status="$1"
    shift
    local task_ids=("$@")
    TASKS_FILE="$TASKS_FILE" python3 - "$status" "${task_ids[@]}" << 'PYEOF'
import json, sys, os

status = sys.argv[1]
task_ids = set(sys.argv[2:])

with open(os.environ["TASKS_FILE"]) as f:
    data = json.load(f)

for t in data.get("tasks", []):
    if t["id"] in task_ids:
        t["status"] = status

with open(os.environ["TASKS_FILE"], "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
PYEOF
}

# Build work prompt for a specific task ID
build_task_prompt() {
    local task_id="$1"

    # Find interface contracts for this task's modules
    local iface_refs=""
    if [[ -d ".planning/interfaces" ]]; then
        local task_modules
        task_modules=$(TASKS_FILE="$TASKS_FILE" python3 -c "
import json, os
with open(os.environ['TASKS_FILE']) as f:
    tasks = {t['id']: t for t in json.load(f).get('tasks', [])}
t = tasks.get('$task_id', {})
seen = set()
for fpath in t.get('key_files', []):
    parts = fpath.split('/')
    mod = '_'.join(parts[:2]) if len(parts) >= 2 else parts[0] if parts else ''
    if mod and mod not in seen:
        seen.add(mod)
        print(mod)
" 2>/dev/null) || true

        while IFS= read -r mod; do
            [[ -n "$mod" ]] || continue
            local contract=".planning/interfaces/${mod}_contract.md"
            if [[ -f "$contract" ]]; then
                iface_refs+="@${contract}
"
            fi
        done <<< "$task_modules"
    fi

    local iface_instruction=""
    if [[ -n "$iface_refs" ]]; then
        iface_instruction="- СТРОГО следуй интерфейсному контракту (типы, сигнатуры, именование)"
    fi

    cat <<EOF
@${TASKS_FILE} @progress.txt
${iface_refs}Тебе назначена конкретная задача: **${task_id}**

1. Работай ТОЛЬКО над задачей ${task_id}. НЕ трогай другие задачи.
2. Проверь, что типы проходят через 'uv run ruff check .' и тесты через 'uv run pytest'.
3. Обнови статус задачи ${task_id} на "done" ТОЛЬКО после успешного прохождения тестов.
4. Добавь свой прогресс в файл progress.txt с пометкой [${task_id}].
5. Сделай git commit для этой задачи с ID в сообщении.

ВАЖНО:
- НЕ редактируй и НЕ меняй статус других задач
${iface_instruction}- Если задача полностью выполнена, выведи <promise>COMPLETE</promise>
- Если не можешь завершить — оставь статус "in_progress" и опиши проблему
EOF
}

# Parallel agent state arrays
declare -a AGENT_PIDS=()
declare -a AGENT_TASKS=()
declare -a AGENT_RESULTS=()
declare -a AGENT_STARTS=()

# Wait for multiple agent PIDs, refreshing dashboard.
# Sets: AGENT_EXITS (array of exit codes, indexed same as AGENT_PIDS)
wait_for_agents() {
    local aname="$1" iter="$2"
    local n_agents=${#AGENT_PIDS[@]}

    local spinner='⣾⣽⣻⢿⡿⣟⣯⣷'
    local si=0
    local last_refresh=0
    declare -a prev_sizes=()
    for ((i=0; i<n_agents; i++)); do prev_sizes+=(0); done

    AGENT_EXITS=()
    for ((i=0; i<n_agents; i++)); do AGENT_EXITS+=(""); done

    local old_stty
    old_stty=$(stty -g 2>/dev/null || true)
    stty -echo -icanon min 0 time 0 2>/dev/null || true

    local active=$n_agents

    while (( active > 0 )); do
        # Check each PID
        for ((i=0; i<n_agents; i++)); do
            [[ -n "${AGENT_EXITS[$i]}" ]] && continue
            if ! kill -0 "${AGENT_PIDS[$i]}" 2>/dev/null; then
                wait "${AGENT_PIDS[$i]}" 2>/dev/null && AGENT_EXITS[i]=0 || AGENT_EXITS[i]=$?
                ((active--)) || true
                log_event "Agent ${AGENT_TASKS[$i]} finished (exit=${AGENT_EXITS[$i]})"
            fi
        done

        # Read keypress
        local key=""
        key=$(dd bs=1 count=1 2>/dev/null || true)

        if [[ -n "$key" ]]; then
            case "$key" in
                q|Q)
                    stty "$old_stty" 2>/dev/null || true
                    printf '%b' "${SH}"
                    printf "\033[H\033[J"
                    echo -e "${YL}${B}Stopping all agents...${R}"
                    for ((i=0; i<n_agents; i++)); do
                        [[ -z "${AGENT_EXITS[$i]}" ]] && kill "${AGENT_PIDS[$i]}" 2>/dev/null || true
                    done
                    for ((i=0; i<n_agents; i++)); do
                        wait "${AGENT_PIDS[$i]}" 2>/dev/null || true
                        [[ -z "${AGENT_EXITS[$i]}" ]] && AGENT_EXITS[i]=130
                    done
                    log_event "User pressed Q — all agents killed"

                    echo -e "${YL}All agents stopped.${R}"
                    echo ""
                    echo -e "${B}What would you like to do?${R}"
                    echo -e "  ${BGD}${WH} r ${R}  Resume from next batch"
                    echo -e "  ${BGD}${WH} x ${R}  Exit Ralph completely"
                    echo ""
                    echo -ne " Choice: "
                    stty echo icanon 2>/dev/null || true
                    read -rsn1 choice
                    stty -echo -icanon min 0 time 0 2>/dev/null || true
                    printf '%b' "${HI}"
                    if [[ "$choice" == "x" || "$choice" == "X" ]]; then
                        stty "$old_stty" 2>/dev/null || true
                        log_event "User chose EXIT"
                        printf '%b' "${SH}"
                        exit 0
                    fi
                    log_event "User chose RESUME"
                    stty "$old_stty" 2>/dev/null || true
                    return 0
                    ;;
                d|D) show_task_detail ;;
                l|L) show_log_viewer ;;
                t|T) show_all_tasks ;;
                h|H|'?') show_help ;;
            esac
            last_refresh=0
        fi

        # Refresh dashboard
        local now
        now=$(date +%s)
        if (( now - last_refresh >= HEARTBEAT_INTERVAL )); then
            last_refresh=$now
            local sc="${spinner:$si:1}"
            si=$(( (si + 1) % ${#spinner} ))

            # Build multi-agent heartbeat
            local hb_lines=""
            for ((i=0; i<n_agents; i++)); do
                local tid="${AGENT_TASKS[$i]}"
                local dur=$(( now - AGENT_STARTS[i] ))
                local dur_fmt; dur_fmt=$(fmt_dur $dur)
                local rfile="${AGENT_RESULTS[$i]}"

                if [[ -n "${AGENT_EXITS[$i]}" ]]; then
                    local ec="${AGENT_EXITS[$i]}"
                    if (( ec == 0 )); then
                        hb_lines+="  ${GR}✓ ${tid}${R} ${D}${dur_fmt}${R} done"
                    else
                        hb_lines+="  ${RD}✗ ${tid}${R} ${D}${dur_fmt}${R} exit=$ec"
                    fi
                else
                    local cur_size=0
                    [[ -f "$rfile" ]] && cur_size=$(wc -c < "$rfile" 2>/dev/null || echo 0)
                    local delta=$(( cur_size - prev_sizes[i] ))
                    prev_sizes[i]=$cur_size

                    local act="${GR}+${delta}B${R}"
                    (( delta == 0 )) && act="${YL}thinking${R}"
                    hb_lines+="  ${CY}${sc} ${tid}${R} ${D}${dur_fmt}${R} ${act} ${D}($(( cur_size/1024 ))KB)${R}"
                fi
                hb_lines+="\n"
            done

            local par_label=""
            (( n_agents > 1 )) && par_label="  ${MG}${B}⫘ ${n_agents} parallel${R}"

            show_dashboard "$aname" "$iter" \
                "${CY}${B}▶ Агенты работают...${R}${par_label}  ${D}(active: ${active}/${n_agents})${R}" \
                "$(echo -e "$hb_lines")"
        fi

        sleep 0.1
    done

    stty "$old_stty" 2>/dev/null || true
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Interface Agreement for parallel tasks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Detect tasks in a batch that share the same module (first 2 path components of key_files).
# Output: lines of "module\ttask1,task2,..." for modules with >1 task
detect_module_overlap() {
    local batch="$1"
    TASKS_FILE="$TASKS_FILE" python3 - "$batch" << 'PYEOF'
import json, sys, os
from collections import defaultdict

batch_ids = sys.argv[1].split("|")
with open(os.environ["TASKS_FILE"]) as f:
    tasks = {t["id"]: t for t in json.load(f).get("tasks", [])}

module_tasks = defaultdict(list)
for tid in batch_ids:
    t = tasks.get(tid, {})
    for fpath in t.get("key_files", []):
        parts = fpath.split("/")
        mod = "/".join(parts[:2]) if len(parts) >= 2 else parts[0] if parts else ""
        if mod:
            module_tasks[mod].append(tid)

# Only output modules with >1 task (actual overlap)
for mod, tids in module_tasks.items():
    if len(tids) > 1:
        unique = list(dict.fromkeys(tids))  # deduplicate preserving order
        print(f"{mod}\t{','.join(unique)}")
PYEOF
}

build_interface_agreement_prompt() {
    local module="$1"
    shift
    local task_ids=("$@")
    cat <<EOF
@${TASKS_FILE}

Ты — архитектор. Несколько задач будут выполняться ПАРАЛЛЕЛЬНО в модуле "${module}".
Задачи: ${task_ids[*]}

Твоя задача — определить и зафиксировать ИНТЕРФЕЙСНЫЙ КОНТРАКТ между этими задачами:

1. Прочти описание и acceptance_criteria каждой задачи
2. Определи общие точки соприкосновения (shared types, function signatures, imports, API endpoints)
3. Создай файл .planning/interfaces/${module//\//_}_contract.md с:
   - Список общих типов/интерфейсов с точными сигнатурами
   - Именование: какие имена функций/классов/переменных использовать
   - Import paths: откуда что импортировать
   - Любые shared constants или enums
   - Структуры данных, которые передаются между модулями

ВАЖНО: НЕ реализуй задачи. Только определи интерфейсы.
Выведи <promise>COMPLETE</promise> когда контракт создан.
EOF
}

run_interface_agreement() {
    local agent="$1" iter="$2" module="$3"
    shift 3
    local task_ids=("$@")

    log_event "Interface agreement: module=$module tasks=${task_ids[*]}"
    show_dashboard "$agent" "$iter" \
        "${MG}${B}⚙ Interface agreement: ${module}${R} ${D}(${task_ids[*]})${R}"

    mkdir -p .planning/interfaces

    local prompt
    prompt=$(build_interface_agreement_prompt "$module" "${task_ids[@]}")

    local rfile
    rfile="$(mktemp -t ralph_iface.XXXXXX)"
    run_agent "$agent" "$prompt" > "$rfile" 2>&1 || true

    extract_usage "$rfile"

    if grep -q '<promise>COMPLETE</promise>' "$rfile" 2>/dev/null; then
        log_event "Interface agreement: COMPLETE for $module"
    else
        log_event "Interface agreement: no COMPLETE signal for $module"
    fi

    { echo "=== Interface Agreement [$module] ==="; cat "$rfile" 2>/dev/null; echo; } >> "$LOG_FILE"
    rm -f "$rfile"
}

# Run a batch of tasks in parallel. Handles launch, wait, extract, report.
# Args: $1=agent $2=iteration $3=batch (pipe-separated task IDs, e.g. "TASK-001|TASK-003")
# Returns: number of completed tasks
run_parallel_batch() {
    local agent="$1" iter="$2" batch="$3"
    local iter_start; iter_start=$(date +%s)

    # Split batch into task IDs
    IFS='|' read -ra task_ids <<< "$batch"
    local n_tasks=${#task_ids[@]}

    AGENT_PIDS=()
    AGENT_TASKS=()
    AGENT_RESULTS=()
    AGENT_STARTS=()

    log_event "Batch start: ${task_ids[*]} (${n_tasks} tasks, parallel)"

    # Interface agreement phase: if tasks share a module, agree on interfaces first
    if (( n_tasks > 1 )); then
        local overlaps
        overlaps=$(detect_module_overlap "$batch") || true
        if [[ -n "$overlaps" ]]; then
            while IFS=$'\t' read -r module overlap_tids; do
                [[ -n "$module" ]] || continue
                IFS=',' read -ra overlap_arr <<< "$overlap_tids"
                run_interface_agreement "$agent" "$iter" "$module" "${overlap_arr[@]}"
            done <<< "$overlaps"
        fi
    fi

    # Mark tasks as in_progress
    mark_tasks_status "in_progress" "${task_ids[@]}"

    # Launch agents
    for tid in "${task_ids[@]}"; do
        local prompt
        prompt=$(build_task_prompt "$tid")
        local rfile
        rfile="$(mktemp -t "ralph_${tid}.XXXXXX")"

        run_agent "$agent" "$prompt" > "$rfile" 2>&1 &
        local apid=$!

        AGENT_PIDS+=("$apid")
        AGENT_TASKS+=("$tid")
        AGENT_RESULTS+=("$rfile")
        AGENT_STARTS+=("$(date +%s)")

        log_event "Launched agent for $tid (pid=$apid)"
    done

    # Wait for all agents
    wait_for_agents "$agent" "$iter"

    local iter_end; iter_end=$(date +%s)
    local iter_dur=$(( iter_end - iter_start ))
    local completed=0
    local completed_ids=()

    # Process results for each agent
    for ((i=0; i<n_tasks; i++)); do
        local tid="${AGENT_TASKS[$i]}"
        local rfile="${AGENT_RESULTS[$i]}"
        local exit_code="${AGENT_EXITS[$i]}"

        # Extract usage from JSON
        extract_usage "$rfile"

        local task_completed="false"

        if (( exit_code == 0 )) && [[ -f "$rfile" ]] && grep -q '<promise>COMPLETE</promise>' "$rfile"; then
            task_completed="true"
            ((completed++)) || true
            completed_ids+=("$tid")
            log_event "Task $tid: COMPLETE ($(fmt_dur $((iter_end - AGENT_STARTS[i]))))"
        elif (( exit_code == 130 )); then
            # User stopped — revert to pending
            mark_tasks_status "pending" "$tid"
            log_event "Task $tid: stopped by user"
        elif (( exit_code != 0 )); then
            # Error — revert to pending
            mark_tasks_status "pending" "$tid"
            log_event "Task $tid: ERROR exit=$exit_code"
        else
            log_event "Task $tid: no COMPLETE signal"
        fi

        # Append to log
        { echo "=== Task $tid [$(fmt_dur $((iter_end - AGENT_STARTS[i])))] exit=$exit_code ==="; cat "$rfile" 2>/dev/null; echo; } >> "$LOG_FILE"

        # Append iteration report
        local count_now; count_now=$(task_count)
        append_iteration_report "$iter" "$((iter_end - AGENT_STARTS[i]))" "$count_before" "$count_now" "$task_completed" "$exit_code"

        rm -f "$rfile"
    done

    log_event "Batch done: $completed/$n_tasks completed ($(fmt_dur $iter_dur))"

    # Export completed task IDs for smart decompose trigger
    LAST_COMPLETED_TASKS="${completed_ids[*]}"
    return 0
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Cleanup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

cleanup() {
    local exit_code=$?
    stty sane 2>/dev/null || true
    printf '%b' "${SH}"
    if (( exit_code != 0 && exit_code != 130 )); then
        local msg="CRASH exit=$exit_code at line ${BASH_LINENO[0]:-?} cmd='${BASH_COMMAND:-?}'"
        echo "[$(date '+%H:%M:%S')] $msg" >> "${LOG_FILE:-ralph_crash.log}"
        echo -e "\n${RD}${B}Ralph crashed:${R} $msg" >&2
        echo -e "${D}Log: ${LOG_FILE:-ralph_crash.log}${R}" >&2
    fi
}
on_error() {
    local exit_code=$? line="${BASH_LINENO[0]}" cmd="$BASH_COMMAND"
    # Skip harmless failures
    [[ "$cmd" == \(\(* ]] && return 0          # arithmetic false
    [[ "$cmd" == *tput* ]] && return 0          # terminal queries
    [[ "$cmd" == *"wc -c"* ]] && return 0      # file size checks
    [[ "$cmd" == *"kill -0"* ]] && return 0    # process alive checks
    [[ "$cmd" == *"dd bs="* ]] && return 0     # keypress reads
    echo "[$(date '+%H:%M:%S')] ERROR exit=$exit_code line=$line cmd='$cmd'" >> "${LOG_FILE:-ralph_crash.log}"
}
trap on_error ERR
trap cleanup EXIT

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

agent=$(resolve_agent) || {
    echo -e "${RD}No agent found. Install claude or codex, or set RALPH_AGENT.${R}" >&2
    exit 1
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Multi-plan loop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PLAN_INDEX=0
PLAN_TOTAL=${#PLAN_FILES[@]}
CURRENT_TASK_ID=""
LAST_COMPLETED_TASKS=""

for TASKS_FILE in "${PLAN_FILES[@]}"; do
    ((PLAN_INDEX++)) || true
    LOG_FILE="ralph_$(basename "$TASKS_FILE" .json).log"
    START_TIME=$(date +%s)
    iteration=1
    INITIAL_TASK_COUNT=0
    LAST_TASK_COUNT=0
    TASK_DELTA_MSG=""
    CURRENT_PHASE=""
    TOTAL_INPUT_TOKENS=0
    TOTAL_OUTPUT_TOKENS=0
    TOTAL_CACHE_READ=0
    TOTAL_CACHE_CREATE=0
    TOTAL_COST_USD="0.00"

    printf '%b' "${HI}"

    if (( PLAN_TOTAL > 1 )); then
        log_event "Plan ${PLAN_INDEX}/${PLAN_TOTAL}: $TASKS_FILE (max=$MAX_ITERATIONS)"
    else
        log_event "Ralph started: $TASKS_FILE (max=$MAX_ITERATIONS)"
    fi
    log_event "Agent: $agent"
    init_report "$TASKS_FILE" "$agent"

    # Reset stale in_progress tasks from previous runs
    reset_in_progress_tasks

    INITIAL_TASK_COUNT=$(task_count)
    LAST_TASK_COUNT=$INITIAL_TASK_COUNT
    DONE_AT_LAST_DECOMPOSE=$(done_count)

    # Initial planning
    if needs_decompose; then
        run_decompose "$agent" "0" || true
    fi
    INITIAL_TASK_COUNT=$(task_count)
    LAST_TASK_COUNT=$INITIAL_TASK_COUNT

    # Main work loop
    while true; do
        # Wait for pending tasks (they may appear from decompose or external edits)
        if ! has_pending_tasks; then
            show_dashboard "$agent" "$iteration" \
                "${YL}${B}⏳ Ожидание задач...${R}  ${D}(pending=0, проверка каждые 5с)${R}"
            sleep 5
            continue
        fi

        if (( MAX_ITERATIONS > 0 && iteration > MAX_ITERATIONS )); then
            show_dashboard "$agent" "$iteration" "${RD}${B}MAX ITERATIONS ($MAX_ITERATIONS) — stopped${R}"
            log_event "STOPPED: max iterations"
            break
        fi

        # Smart re-planning: always after critical tasks, periodic for others
        decompose_needed=0

        if [[ -n "${LAST_COMPLETED_TASKS:-}" ]]; then
            if [[ "$LAST_COMPLETED_TASKS" == critical-seq ]]; then
                # Sequential mode detected a new critical task done
                decompose_needed=1
                log_event "Critical task completed (sequential) — triggering decompose"
            else
                # Parallel mode — check actual task IDs
                # shellcheck disable=SC2086
                if has_critical_task $LAST_COMPLETED_TASKS; then
                    decompose_needed=1
                    log_event "Critical task completed (${LAST_COMPLETED_TASKS}) — triggering decompose"
                fi
            fi
            LAST_COMPLETED_TASKS=""
        fi

        if (( !decompose_needed && DECOMPOSE_EVERY > 0 )); then
            current_done=$(done_count)
            done_since=$(( current_done - DONE_AT_LAST_DECOMPOSE ))
            if (( done_since >= DECOMPOSE_EVERY )); then
                decompose_needed=1
            fi
        fi

        if (( decompose_needed )); then
            run_decompose "$agent" "$iteration" || true
            DONE_AT_LAST_DECOMPOSE=$(done_count)
        fi

        CURRENT_PHASE="work"
        count_before=$(task_count)

        # ── Parallel mode (MAX_PARALLEL > 1) ──────────────────
        if (( MAX_PARALLEL > 1 )); then
            # Re-orchestrate every iteration (fresh dependency state)
            run_orchestrator "$agent" "$iteration"

            if [[ ${#PARALLEL_BATCHES[@]} -eq 0 ]]; then
                log_event "No ready tasks for orchestrator"
                continue  # while loop will wait for new tasks
            fi

            # Execute only the first batch, then re-orchestrate
            batch="${PARALLEL_BATCHES[0]}"

            show_dashboard "$agent" "$iteration" \
                "${CY}${B}▶ Batch: ${batch//|/, }${R}  ${D}(${#PARALLEL_BATCHES[@]} batches queued)${R}"
            log_event "Iter $iteration: batch [${batch//|/, }] (${#PARALLEL_BATCHES[@]} total)"

            run_parallel_batch "$agent" "$iteration" "$batch"

            # Track task count changes
            count_after=$(task_count)
            if (( count_after != count_before )); then
                local_diff=$(( count_after - count_before ))
                TASK_DELTA_MSG="Δ${local_diff} tasks (${count_before}→${count_after})"
                # shellcheck disable=SC2034
                LAST_TASK_COUNT=$count_after
            fi

            show_dashboard "$agent" "$iteration" \
                "${GR}${B}✓ Batch done${R} ${D}— re-orchestrating...${R}"
            sleep 1

            TASK_DELTA_MSG=""
            ((iteration++)) || true
            continue
        fi

        # ── Sequential mode (MAX_PARALLEL = 1) ────────────────
        iter_start=$(date +%s)

        CURRENT_TASK_ID="auto-pick"
        show_dashboard "$agent" "$iteration" \
            "${CY}${B}▶ Запуск агента...${R}"
        log_event "Iter $iteration start"

        prompt="@${TASKS_FILE} @progress.txt
1. Найди фичу с наивысшим приоритетом и работай ТОЛЬКО над ней.
Это должна быть фича, которую ТЫ считаешь наиболее приоритетной — не обязательно первая в списке.
2. Проверь, что типы проходят через 'uv run ruff check .' и тесты через 'uv run pytest'.
3. Обнови TASK с информацией о выполненной работе.
4. Добавь свой прогресс в файл progress.txt.
Используй это, чтобы оставить заметку для следующей итерации работы над кодом.
5. Сделай git commit для этой фичи.
РАБОТАЙ ТОЛЬКО НАД ОДНОЙ ФИЧЕЙ.
Если при реализации фичи ты заметишь, что TASK полностью выполнен, выведи <promise>COMPLETE</promise>."

        critical_before=$(critical_done_count)
        run_agent_interactive "$agent" "$prompt" "$iteration" "$iter_start"

        iter_end=$(date +%s)
        iter_dur=$(( iter_end - iter_start ))

        { echo "=== Iter $iteration [$(fmt_dur $iter_dur)] ==="; cat "$result_file"; echo; } >> "$LOG_FILE"

        # Detect task count changes
        count_after=$(task_count)
        if (( count_after != count_before )); then
            local_diff=$(( count_after - count_before ))
            if (( local_diff > 0 )); then
                TASK_DELTA_MSG="⊕ +${local_diff} tasks this iter (${count_before}→${count_after})"
            else
                TASK_DELTA_MSG="⊖ ${local_diff} tasks this iter (${count_before}→${count_after})"
            fi
            # shellcheck disable=SC2034
            LAST_TASK_COUNT=$count_after
            log_event "Iter $iteration: tasks ${count_before}→${count_after}"
        fi

        # Determine completion status for reporting
        iter_completed="false"
        if (( agent_exit == 0 )) && [[ -f "$result_file" ]] && grep -q '<promise>COMPLETE</promise>' "$result_file"; then
            iter_completed="true"
            # Check if a critical task was completed (for smart decompose)
            critical_after=$(critical_done_count)
            if (( critical_after > critical_before )); then
                LAST_COMPLETED_TASKS="critical-seq"
            fi
        fi

        # Append iteration to cost report
        append_iteration_report "$iteration" "$iter_dur" "$count_before" "$count_after" "$iter_completed" "${agent_exit:-1}"

        if (( agent_exit == 130 )); then
            rm -f "$result_file"
            TASK_DELTA_MSG=""
            ((iteration++))
            continue
        fi

        if (( agent_exit != 0 )); then
            log_event "Iter $iteration: ERROR exit=$agent_exit ($(fmt_dur $iter_dur))"
            show_dashboard "$agent" "$iteration" \
                "${RD}${B}✗ Agent error (exit $agent_exit)${R} ${D}— see ralph.log${R}"
            rm -f "$result_file"
            ((iteration++))
            sleep 3
            continue
        fi

        if [[ "$iter_completed" == "true" ]]; then
            rm -f "$result_file"
            log_event "Iter $iteration: COMPLETE ($(fmt_dur $iter_dur))"

            if ! has_pending_tasks; then
                show_dashboard "$agent" "$iteration" \
                    "${GR}${B}✓ ALL DONE${R}  ${D}$(elapsed) — ожидание новых задач...${R}"
                log_event "ALL DONE after $iteration iters — waiting for new tasks"
                command -v say >/dev/null 2>&1 && say -v Milena "План завершён!"
                continue  # back to while — will wait for new tasks
            fi

            show_dashboard "$agent" "$iteration" \
                "${GR}${B}✓ Task done${R} ${D}($(fmt_dur $iter_dur)) — next...${R}"
            command -v say >/dev/null 2>&1 && say -v Milena "Задача готова. Продолжаю работу."
            sleep 2
        else
            rm -f "$result_file"
            log_event "Iter $iteration: no COMPLETE ($(fmt_dur $iter_dur))"
            show_dashboard "$agent" "$iteration" \
                "${YL}${B}⚠ No COMPLETE signal${R} ${D}($(fmt_dur $iter_dur)) — continuing...${R}"
            sleep 2
        fi

        TASK_DELTA_MSG=""
        ((iteration++))
    done

    show_dashboard "$agent" "$iteration" \
        "${GR}${B}✓ PLAN DONE${R} ${D}${TASKS_FILE} — $((iteration-1)) iterations, $(elapsed)${R}"
    log_event "PLAN DONE: $TASKS_FILE after $((iteration-1)) iters — tokens: in=$(fmt_tokens $TOTAL_INPUT_TOKENS) out=$(fmt_tokens $TOTAL_OUTPUT_TOKENS) cost=\$$(fmt_cost "$TOTAL_COST_USD")"

    # Finalize cost report for this plan
    plan_dur=$(( $(date +%s) - START_TIME ))
    finalize_report "$((iteration-1))" "$plan_dur"

    # Pause between plans if running multiple
    if (( PLAN_INDEX < PLAN_TOTAL )); then
        sleep 3
    fi
done

# Generate summary markdown report across all plans
generate_summary_report

command -v say >/dev/null 2>&1 && say -v Milena "Хозяин, я всё сделалъ!"
printf '%b' "${SH}"
