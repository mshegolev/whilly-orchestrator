# Deferred scope — `whilly-v6.0-hardening` (post-v5.0 mission)

User decision (2026-05-04): the gaps surfaced by `out/whilly-analysis.html` are **explicitly out of scope** for v5.0 / M1-M3. They will become a separate mission `whilly-v6.0-hardening` after v5.0 is sealed.

**Selected scope for v6.0** (user's choice — block A + D only, minimal):

## Block A — Security & Isolation

Source: report Q4 (Безопасность и изоляция), gaps flagged.

- Prompt-injection guard on `task.description` when assembling agent prompt (`whilly/core/prompts.py`)
- Deny-list for dangerous shell commands (`rm -rf /`, `git push --force`, `dd of=/dev/...`, fork-bomb patterns) at agent_runner subprocess layer
- Optional per-task seccomp / firejail / docker isolation (opt-in env switch, default OFF)
- Preflight gate against `git push --force` to default branch (read remote branch-protection rules; client-side fast-fail before push)
- Secrets discipline lint: assert no `gsk_*`/`sk-*`/`AKIA*`/`ghp_*` tokens leak into worker logs or events table

## Block D — Rollback & Safety Net

Source: report Q10 (Откат изменений), gaps flagged.

- Auto-restore worktree on FAILED (currently left in place per AGENTS.md — give operator opt-in cleanup with backup tag)
- `whilly-backup/<task_id>-<sha>` git tag created BEFORE every cherry-pick / push (allows recovery from bad merge)
- Protection-rule check before force-push: query GitHub API for branch-protection on the target branch; refuse push if force-push is rejected by the rule
- Smart rollback: distinguish "abort whole task" vs "abort changed files only" (use `git diff --name-only HEAD~1` to scope the reset)

## Explicitly NOT in scope for v6.0 (rejected by user)

- Block B: Extended PR feedback loop (review-comment threading)
- Block C: Long-term memory / pgvector / repo-memory
- Block E: Deployment-verifier / schema-migration gate / security-scan gate / cross-repo
- Block F: Auto-comment in issue with clarifying question
- Block G: Slack-webhook + confirm-prompts + human approve-gate
- Block H: OOM/SIGKILL handler + tracker circuit-breaker

These remain potential candidates for a future v7.0+ mission but should not be silently absorbed into v6.0.

## Source artifact

Full analytical report archived at `out/whilly-analysis.html` (HTML, 13 Q&A blocks, lists Russian-language gap analysis). Mission planner for v6.0 should read it BEFORE drafting validation-contract.md.
