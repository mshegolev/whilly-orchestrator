# Session Handoff — 2026-05-22

PRD scope: [`docs/PRD-post-auth-hardening.md`](../docs/PRD-post-auth-hardening.md)
Plan file: [`.planning/post-auth-hardening-tasks.json`](post-auth-hardening-tasks.json)
ADR: [`docs/adr/ADR-001-auth-hardening-p1.md`](../docs/adr/ADR-001-auth-hardening-p1.md)
Prev handoff: [`SESSION-HANDOFF-2026-05-21.md`](SESSION-HANDOFF-2026-05-21.md)

## TL;DR

Continued the rolling **security-review loop** (the same loop that produced
Findings 2/3/5/6/7/8 and PR #318). Audited the *whole* "agent-controlled
identifier → filesystem path / tmux target / shell" class that PR #318 opened.
PR #318 had fixed the **write** sinks (`tmux_runner`, `verifier`) but left the
**symmetric read sink** in the dashboard, and shipped with **no ADR section**.
This session: fixed the read sink, swept the rest of the class (clean), and
backfilled the documentation as ADR-001 **§P1.13** (covering both #318 and this
fix). `post-auth-hardening` plan unchanged at `done=27, skipped=2`.

## What shipped this session

| Area | What |
|---|---|
| Fix | [`whilly/dashboard.py::_resolve_task_log_path`](../whilly/dashboard.py) now flattens the task id with [`safe_task_id_filename`](../whilly/core/task_id.py) for its fallback log-path candidates, so the log **reader** matches the #318 **writer**. Hierarchical/namespaced ids (`/`, `:`) were previously unviewable; a leading-slash id resolved to an absolute path (read-side twin of the #318 write escape). Latent today — all 208 plan ids are slash-free. |
| Tests | `tests/test_whilly_dashboard.py`: `test_resolve_task_log_path_flattens_hierarchical_id_to_match_writer` (reader == writer for `epic.subepic/leaf` → `epic.subepic_leaf.log`) and `…_leading_slash_id_cannot_escape_log_dir` (`/etc/cron.d/x` stays under `log_dir`). |
| Docs | [ADR-001 §P1.13](../docs/adr/ADR-001-auth-hardening-p1.md) — documents the agent-exec path-sink class: Finding A (#318 write sinks, previously undocumented), Finding B (this dashboard read sink), the swept-and-clean sinks (`workspaces`, `worktree_runner`, `prd_wizard`, `llm_ops`), and the `history.load_session` defense-in-depth residual note. |

## The class, as audited (ADR §P1.13)

- ✅ `tmux_runner` / `verifier` — flattened by #318.
- ✅ `dashboard` reader — flattened this session (was the gap).
- ✅ `workspaces.prepare_git_workspace` — `_safe_path_part` on repo/plan/task slugs (path + branch).
- ✅ `worktree_runner` — slug from `plan_slug` (`re.sub(r"[^a-z0-9]+", "-")`, capped 48).
- ✅ `prd_wizard` — slug filtered to `isalnum() or in "-_"` (no `.`/`/`, so no `..`).
- ✅ `llm_ops` artifact dirs — `_safe_part` (byte-equivalent regex to `safe_task_id_filename`).
- ⚠️ `history.load_session` globs a caller string — internally a timestamp, not
  attacker-reachable today. Defense-in-depth opportunity, **not** a finding.
- Prompt-injection surface already covered: `prompt_sanitizer` (12 sites),
  prompt passed via file + `"$(cat …)"` (never the shell source), no `shell=True`.

## What's left / next candidate review threads

- **No open finding.** The agent-exec path-sink class is closed.
- Optional defense-in-depth: flatten/validate `history.load_session`'s glob input.
- Untouched-by-recent-reviews surfaces, if the loop continues: the E17 XFF chain
  parser edge cases beyond `num_trusted_hops`, session fixation/rotation on
  privilege change, and whether the `auth_audit` ledger ever records a secret.

## Verification commands

```bash
cd /opt/develop/whilly-orchestrator
PYBIN=.venv/bin/python   # or python3
$PYBIN -m pytest tests/test_whilly_dashboard.py -q                          # 10 passed
$PYBIN -m pytest tests/unit/test_task_id_validation.py tests/unit/test_tmux_runner_quoting.py -q  # 148 passed
$PYBIN -m ruff check whilly/ tests/
$PYBIN -m ruff format --check whilly/ tests/
```

## Sharp edges

- **`safe_task_id_filename` (task_id.py) and `_safe_part` (llm_ops.py) share the
  regex `[^A-Za-z0-9_.-]+`** — only the empty-result fallback differs (`"task"`
  vs `"unknown"`), irrelevant for real ids. If you add a third flattener, reuse
  `safe_task_id_filename` instead of minting a fourth.
- **The dashboard's active-agent branch (#1) still keys on the raw `task_id`** —
  that's correct: it matches the registration dict key and returns the absolute
  path recorded at spawn. Only the standard-locations fallback (#2) flattens.
