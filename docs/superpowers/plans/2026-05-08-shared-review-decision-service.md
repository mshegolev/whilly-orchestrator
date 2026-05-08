# Shared Review Decision Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make TUI and WUI human-review decisions use one shared backend command so approval, rejection, requested changes, comments, evidence, and audit payloads stay aligned.

**Architecture:** Add a small pipeline-layer review-decision service that accepts a typed command and records the mapped `human_review.*` event through a repository-like object. The admin API and browserless TUI will both call this service, passing only their surface-specific `source` and optional operator metadata.

**Tech Stack:** Python 3.12, dataclasses, async protocols, FastAPI, Rich TUI, pytest/pytest-asyncio.

---

## Contract

| Field | API / WUI | TUI | Shared behavior |
| --- | --- | --- | --- |
| `task_id` | URL path | selected review gap | Always written into payload. |
| `decision` | request body | hotkey action | Maps to `human_review.approved`, `.rejected`, or `.changes_requested`. |
| `reviewer` | request body | `--reviewer` / `WHILLY_OPERATOR_EMAIL` | Required before calling service. |
| `stage_id` | request body | selected review gap | Omitted when empty. |
| `comment` | request body | empty for now | Omitted when empty. |
| `evidence` | request body | none for now | Omitted when empty. |
| `requested_changes` | request body | default TUI message for `c` | Omitted when empty. |
| `operator` | admin token owner | none for now | Omitted when empty. |
| `source` | `admin_api` | `tui` | Explicitly identifies the surface. |

## Tasks

Status: **implemented and verified on 2026-05-08**.

### Task 1: Shared Service

**Files:**
- Create: `whilly/pipeline/human_review_decisions.py`
- Test: `tests/unit/test_human_review_decisions.py`

- [x] Write failing tests for approved and changes-requested commands.
- [x] Implement `HumanReviewDecisionCommand` and `record_human_review_decision`.
- [x] Verify service tests pass.
- [x] Commit: included in `feat(operator): share review decision path`.

### Task 2: API And TUI Wiring

**Files:**
- Modify: `whilly/adapters/transport/server.py`
- Modify: `whilly/cli/tui.py`
- Test: `tests/integration/test_transport_tasks.py`
- Test: `tests/unit/test_tui.py`

- [x] Add failing assertions that API and TUI payloads include shared-service fields.
- [x] Replace duplicated payload construction with `record_human_review_decision`.
- [x] Keep API auth and TUI reviewer validation unchanged.
- [x] Verify focused API/TUI tests pass.
- [x] Commit: `feat(operator): share review decision path`.

### Task 3: Compliance And Review Evidence

**Files:**
- Modify: `docs/superpowers/reviews/2026-05-08-whilly-operator-ui-review.md`

- [x] Mark the first UI audit finding as resolved.
- [x] Keep remaining UI findings unchanged.
- [x] Run focused verification and commit docs.
