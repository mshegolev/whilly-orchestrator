# Architecture Decision Records — Whilly

> **EN:** Lightweight MADR (Markdown ADR) records documenting the **why** behind whilly's architecture. Each ADR captures a decision, its context, considered alternatives, and consequences. Read in any order; cross-references are explicit.
>
> **RU:** Облегчённые MADR-записи, фиксирующие **почему** в архитектуре whilly. Каждый ADR описывает решение, контекст, альтернативы и последствия. Читать можно в любом порядке.

---

## Index

| # | Title | Status | Domain |
|---|---|---|---|
| [001](ADR-001-python-over-go-and-bash.md) | Python over Go and Bash | accepted | Stack choice |
| [002](ADR-002-tasks-json-source-of-truth.md) | tasks.json as source of truth | accepted | State |
| [003](ADR-003-tmux-and-worktree-parallelism.md) | tmux + git worktree for parallelism | accepted | Concurrency |
| [004](ADR-004-claude-cli-subprocess-vs-sdk.md) | Claude CLI subprocess vs Anthropic SDK | accepted | Backend |
| [005](ADR-005-jsonl-events.md) | JSONL events as observability layer | accepted | Observability |
| [006](ADR-006-github-issues-source-adapter.md) | GitHub Issues source adapter | accepted (gap pack) | Source |
| [007](ADR-007-pr-creation-sink.md) | PR creation sink via `gh` CLI | accepted (gap pack) | Sink |
| [008](ADR-008-decision-gate.md) | Decision Gate before agent dispatch | accepted (gap pack) | Quality |
| [009](ADR-009-triz-analyzer.md) | TRIZ analyzer for ambiguous tasks | accepted | Quality |
| [010](ADR-010-prd-wizard.md) | PRD wizard pipeline | accepted | Workflow |
| [011](ADR-011-task-decomposer.md) | Mid-run task decomposer | accepted | Workflow |
| [012](ADR-012-self-hosting-bootstrap-demo.md) | Self-hosting bootstrap demo | accepted (gap pack) | Demo |
| [021](ADR-021-draft-pr-vs-auto-merge.md) | Draft PR vs auto-merge: clarifying the human gate | accepted | Sink / safety policy |

---

## Status meanings

- **proposed** — under discussion, not yet implemented
- **accepted** — decision is in effect, code follows it
- **deprecated** — superseded but kept for history
- **superseded by ADR-XXX** — replaced explicitly

---

## How to add a new ADR

1. Copy the most recent ADR as a template.
2. Number sequentially (`ADR-013-...md`).
3. Status starts `proposed`.
4. Add a row to the index above.
5. Reference relevant code (`whilly/X.py`) and PRD sections.
6. Open a PR; merge only after team review.

ADRs **never** get rewritten after `accepted`. To change a decision, write a new ADR that supersedes the old one.
