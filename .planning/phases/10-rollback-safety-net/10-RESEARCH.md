# Phase 10: Rollback safety net - Research

**Researched:** 2026-05-08
**Domain:** Git rollback safety, Whilly CLI adapters, auditable operator preflight
**Confidence:** HIGH for local architecture and tests; MEDIUM for optional GitHub protection probing

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

## Implementation Decisions

### Operator Safety
- Rollback commands must be explicit operator actions, not hidden automatic cleanup.
- Restore operations must be confirmation-gated and must not silently destroy unrelated working-tree changes.
- If the worktree is dirty, rollback restore should stop with a clear diagnostic unless the operator provides an explicit force/confirm path defined by the CLI contract.
- The restore contract should prioritize exact prior state for the requested artifact or branch and avoid collateral edits.

### Backup Points And Preflight
- Operators should be able to create rollback points before risky branch mutation.
- Rollback point names should be deterministic and discoverable, using a clear Whilly-specific prefix instead of ad hoc tag names.
- Push, merge, and restore preflight checks should report branch, HEAD SHA, dirty worktree state, upstream/protection signals available locally, and whether a backup point exists.
- Preflight should be auditable through machine-readable output or structured data that tests can inspect.

### CLI Shape
- Add rollback behavior as a first-class Whilly CLI surface, for example `whilly rollback ...`.
- Prefer dry-run and list/status commands that are safe by default.
- Confirmation should be explicit for destructive restore behavior; no default `git reset --hard` style behavior.
- Existing v3/v4 CLI compatibility should not regress.

### Compliance And Documentation
- Compliance should distinguish general rollback safety-net support from older verifier-helper rollback behavior.
- Wording must not claim full autonomous rollback or automatic production recovery.
- Documentation updates should stay scoped to current-vs-target and command evidence if needed.

### Claude's Discretion
- The exact internal module layout is at Claude's discretion, but a small dedicated rollback module is preferred over burying Git safety logic in the top-level CLI dispatcher.

### Deferred Ideas (OUT OF SCOPE)

## Deferred Ideas

- CI polling and bounded repair loops belong to Phase 11.
- Governance policy and semantic-memory scope belong to Phase 12.
- Full automatic production recovery remains out of current scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| ROLL-01 | Operators can create backup tags before risky branch mutation. | Use annotated Git tags with `whilly/rollback/` prefix, deterministic names, `git check-ref-format` validation, and list/query helpers. |
| ROLL-02 | Branch protection/preflight checks run before push, merge, or restore operations. | Add structured preflight service and CLI; wire push preflight into `whilly.sinks.github_pr.open_pr_for_task` before `git push`; expose merge/restore operation preflights even though Whilly currently has no merge executor. |
| ROLL-03 | Rollback restore is explicit, auditable, and confirmation-gated. | Implement clean-worktree-only restore by default, exact confirmation phrase, JSON audit output, and no hidden `git clean` or stash behavior. |
</phase_requirements>

## Summary

Phase 10 should be implemented as an operator-facing Git safety package, not as an extension of the old verifier helper. The existing verifier path in `whilly/verifier.py` can soft-reset one recent commit after verification failure, but compliance correctly reports that as only partial Git rollback support. The new feature should live under `whilly/rollback/` with `whilly/cli/rollback.py`, then be lazily dispatched from `whilly/cli/__init__.py` so top-level help remains dependency-light.

The central design choice is to make preflight a data contract. Every rollback operation should produce a structured report containing operation, repo root, branch, HEAD SHA, dirty status, upstream details, protection status, backup-point status, blockers, and warnings. CLI text can be human-friendly, but tests and compliance should assert against the JSON/dict contract.

**Primary recommendation:** implement `whilly rollback create|list|preflight|restore` with local annotated tags, JSON-first preflight reports, clean-worktree restore by default, and a narrow PR-push integration that blocks only on concrete safety blockers.

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python stdlib `argparse`, `dataclasses`, `json`, `subprocess`, `pathlib` | Python 3.12.1 local; project requires `>=3.12` | CLI parsing, typed contracts, Git command adapter | Matches existing Whilly CLI modules and avoids adding dependencies. |
| Git CLI | 2.39.3 local | Tags, status, refs, restore/reset operations | Existing repo already shells out to `git`; official docs provide stable machine formats. |
| GitHub CLI `gh` through `whilly.gh_utils.gh_subprocess_env` | Optional, no new dependency | Optional GitHub branch protection probe | Existing GitHub shellouts centralize auth env through this helper. |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pytest | 9.0.3 local; pyproject floor `>=8.0` | Unit and isolated Git integration tests | All rollback behavior should be testable with temp Git repos and monkeypatched subprocesses. |
| Ruff | 0.11.5 pinned in pyproject | Formatting and linting | Run for new modules and tests. |
| import-linter | pyproject floor `>=2.0` | Preserve `whilly.core` purity | Rollback code must stay outside `whilly.core` because it uses subprocess/Git. |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Dedicated `whilly/rollback/` package | Put helpers in `whilly/workspaces.py` or `whilly/verifier.py` | Burying operator rollback into workspace/verifier code would blur safety contracts and compliance evidence. |
| Git CLI commands | GitPython/dulwich | Additional dependencies are not needed; Whilly already uses subprocess Git with list argv. |
| JSON preflight contract | Text-only CLI output | Text is hard to audit and test; JSON/dicts satisfy the phase requirement directly. |
| Local-only protection status | Mandatory GitHub API lookup | Mandatory network/API calls would make unit tests brittle and fail in non-GitHub repos. Make GitHub probing optional and report `unknown` when unavailable. |

**Installation:** no new runtime packages.

**Version verification performed:**

```bash
git --version                 # git version 2.39.3 (Apple Git-146)
.venv/bin/python --version    # Python 3.12.1
.venv/bin/python -m pytest --version  # pytest 9.0.3
.venv/bin/python -m ruff --version    # ruff 0.11.5
```

## Architecture Patterns

### Recommended Project Structure

```text
whilly/
├── rollback/
│   ├── __init__.py       # public service/model exports
│   ├── git_ops.py        # GitClient, no shell=True, list argv only
│   ├── models.py         # RollbackPoint, WorktreeState, PreflightReport
│   └── service.py        # create/list/preflight/restore orchestration
├── cli/
│   └── rollback.py       # argparse adapter, text/json rendering, confirmation
└── sinks/
    └── github_pr.py      # call preflight before git push

tests/
├── unit/test_rollback.py
├── unit/test_compliance_report.py
└── integration/test_rollback_cli.py
```

Keep `whilly.core` untouched. The import-linter contract forbids subprocess in `whilly.core`, and rollback is inherently an adapter/service concern.

### Pattern 1: Lazy CLI Subcommand

**What:** Add `rollback` to the help text and dispatch block in `whilly/cli/__init__.py`, importing `whilly.cli.rollback` only when invoked.

**When to use:** Always for new top-level Whilly commands. Existing command dispatch imports subcommands lazily to keep `whilly --help` fast and free of server/worker dependencies.

**Implementation note:** Add `whilly rollback ...` without changing legacy flag shim behavior. Existing `--reset` must keep routing to `plan reset --keep-tasks --yes`.

### Pattern 2: Git Adapter Boundary

**What:** Wrap Git subprocess calls in one small adapter (`GitClient.run`, `GitClient.ok`) that accepts `cwd`, list argv, timeout, and returns parsed structured results or typed errors.

**When to use:** All rollback code paths: `status`, `rev-parse`, `tag`, `for-each-ref`, `check-ref-format`, and `reset`.

**Existing precedent:** `whilly/workspaces.py` has `_git()` and `_git_ok()` wrappers plus a clean-worktree guard. Reuse the pattern, not the private functions.

### Pattern 3: Structured Preflight Contract

**What:** Preflight returns a `PreflightReport`, not printed text.

Recommended fields:

```python
@dataclass(frozen=True, slots=True)
class ProtectionSignal:
    provider: str = ""
    status: str = "unknown"  # "protected" | "unprotected" | "unknown"
    reason: str = ""

@dataclass(frozen=True, slots=True)
class PreflightReport:
    operation: str
    repo_root: str
    branch: str
    head_sha: str
    upstream: str | None
    dirty: bool
    dirty_entries: tuple[str, ...]
    backup_points: tuple[str, ...]
    protection: ProtectionSignal
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.blockers
```

**When to use:** `rollback preflight push|merge|restore`, `rollback restore --dry-run`, and PR sink push preflight.

### Pattern 4: Safe Restore Flow

**What:** Restore uses a two-step process: preflight, then exact confirmation, then `git reset --hard <target>` only when clean.

**Recommended CLI contract:**

```text
whilly rollback restore <tag-or-ref> [--repo PATH] [--dry-run] [--json]
whilly rollback restore <tag-or-ref> --confirm "restore <short-target-sha> to <branch>"
```

TTY mode may prompt. Non-TTY mode must require `--confirm` with the exact phrase. Do not add a broad `--yes` for restore in this phase; the existing `plan reset --yes` pattern is acceptable for database reset, but branch restore is a higher-risk Git mutation.

### Pattern 5: Compliance Probe Upgrade

**What:** Update the `Git rollback` row in `whilly/compliance/__init__.py` from verifier-helper partial to safety-net support when all concrete artifacts exist.

**Recommended status after Phase 10:** `PASS` only if these are present:

- `whilly/rollback/service.py` includes backup tag creation and restore preflight.
- `whilly/cli/rollback.py` exposes create/list/preflight/restore.
- `whilly/sinks/github_pr.py` or equivalent push path runs rollback preflight before `git push`.
- Tests cover dirty restore refusal and JSON preflight.

If push integration is not completed, keep status `PARTIAL`.

### Anti-Patterns to Avoid

- **Do not hide rollback behind failed verification.** The old verifier helper remains evidence, not the new safety net.
- **Do not parse human `git status` output.** Use porcelain format.
- **Do not mutate dirty worktrees by default.** Dirty restore must stop before `git reset --hard`.
- **Do not call `git clean`, `git stash`, or `git checkout .` implicitly.** These are unrelated-worktree mutation traps.
- **Do not treat missing GitHub protection data as unprotected.** Report `unknown` with a reason.
- **Do not add network/API requirements to all preflights.** Local Git repos and offline operators must still get useful branch/dirty/backup evidence.

## Data Contracts

### Rollback Tag Naming

Use a fixed prefix:

```text
whilly/rollback/<sanitized-branch>/<YYYYmmddTHHMMSSZ>-<short-head-sha>
```

Rules:

- UTC timestamp, no randomness.
- Sanitize branch path components for display, then validate the final ref with `git check-ref-format refs/tags/<name>`.
- Create annotated tags with `git tag -a <name> -m <message> <head_sha>`.
- Do not pass `-f`; existing names should fail instead of replacing rollback evidence.

### Rollback Point JSON

```json
{
  "name": "whilly/rollback/main/20260508T170000Z-3be26db",
  "target_sha": "3be26db...",
  "branch": "main",
  "created_at": "2026-05-08T17:00:00Z",
  "message": "Whilly rollback point before push"
}
```

### Preflight JSON

```json
{
  "operation": "push",
  "ok": true,
  "repo_root": "/repo",
  "branch": "feature/x",
  "head_sha": "abc123...",
  "upstream": "origin/feature/x",
  "dirty": false,
  "dirty_entries": [],
  "backup_points": ["whilly/rollback/feature-x/20260508T170000Z-abc1234"],
  "protection": {"provider": "github", "status": "unknown", "reason": "not requested"},
  "blockers": [],
  "warnings": []
}
```

Recommended blocker policy:

| Condition | push | merge | restore |
|-----------|------|-------|---------|
| Not a Git repo | blocker | blocker | blocker |
| Detached HEAD | warning/blocker depending target | blocker | blocker |
| Dirty worktree | blocker for PR sink push | blocker | blocker |
| No backup point at current HEAD | warning | warning | warning |
| Protected target branch confirmed | blocker unless explicit allow flag | blocker | blocker unless explicit allow flag |
| Protection unknown | warning | warning | warning |

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Dirty-worktree detection | Human `git status` parser | `git status --porcelain=v1` or v2 | Git documents porcelain v1 as stable for scripts. |
| Valid tag/ref names | Regex-only validator | `git check-ref-format refs/tags/<name>` | Git ref rules are subtle and already implemented by Git. |
| Rollback point storage | JSON file under `.whilly` | Annotated Git tag | Tags are native refs, discoverable, and point at immutable commits. |
| Restore cleanup | Custom deletion/stash routines | Refuse dirty tree by default | Hidden cleanup is the main collateral-damage risk. |
| GitHub auth env | Direct `os.environ` inheritance | `whilly.gh_utils.gh_subprocess_env()` | Existing helper handles `WHILLY_GH_TOKEN`, keyring preference, and TOML tokens. |
| Branch protection certainty | Guess from branch name | Optional `gh api repos/{owner}/{repo}/branches/{branch}` probe | Protection can come from GitHub settings/rulesets, not local Git config. |

**Key insight:** The safety net is mostly about explicit evidence and refusal behavior. The dangerous work is not creating tags; it is avoiding any restore/push path that silently discards local state or overclaims branch-protection certainty.

## Common Pitfalls

### Pitfall 1: Extending `whilly.verifier` Instead Of Building Operator Rollback

**What goes wrong:** The phase ships another automatic reset helper but no operator CLI or preflight report.
**Why it happens:** `whilly/verifier.py` already has `revert_on_fail`.
**How to avoid:** Keep verifier behavior as legacy evidence; implement new rollback commands in `whilly/rollback/` and `whilly/cli/rollback.py`.
**Warning signs:** Compliance still says rollback is tied to verifier helper behavior.

### Pitfall 2: Restore Destroys Dirty Tracked Work

**What goes wrong:** `git reset --hard <tag>` discards tracked working-tree changes.
**Why it happens:** Restore code treats "confirmation" as enough even when local changes exist.
**How to avoid:** Dirty worktree is a blocker by default; confirmation only applies after a clean preflight. A future force path must require a separate explicit dirty-discard contract.
**Warning signs:** Tests create dirty files and restore still invokes `git reset`.

### Pitfall 3: Untracked Files Are Ignored

**What goes wrong:** Restore appears safe but untracked files can obstruct checkout/reset or mask collateral risk.
**Why it happens:** Code checks only `git diff --quiet`.
**How to avoid:** Use status porcelain and include `??` entries in `dirty_entries`.
**Warning signs:** Dirty tests only cover modified tracked files.

### Pitfall 4: Protection Unknown Becomes Protection False

**What goes wrong:** CLI says "unprotected" when `gh` is missing, offline, unauthorized, or the remote is not GitHub.
**Why it happens:** Exception path defaults to boolean false.
**How to avoid:** Model `protected`, `unprotected`, and `unknown`; include reason and source.
**Warning signs:** JSON has `"protected": false` without `"source"` or `"reason"`.

### Pitfall 5: Remote URL Leaks Tokens

**What goes wrong:** Preflight JSON prints `https://token@github.com/owner/repo.git`.
**Why it happens:** Code reports raw `git remote get-url`.
**How to avoid:** Redact userinfo before output. Prefer remote name plus parsed host/owner/repo.
**Warning signs:** Tests do not include credential-bearing remote URLs.

### Pitfall 6: PR Sink Tests Regress Because Fake Worktree Is Not Git

**What goes wrong:** Existing `test_post_complete_pr_hook` creates a plain directory because current `open_pr_for_task` only checks path existence.
**Why it happens:** Adding push preflight requires a Git repository.
**How to avoid:** Add injection or adapt test fixtures to initialize a temp Git repo. Keep preflight failure converted into `PRResult`, never an exception.
**Warning signs:** Integration tests fail before mocked `git push`/`gh pr create` calls.

## Code Examples

Verified patterns from local code and official docs.

### Git Adapter Pattern

```python
@dataclass(frozen=True, slots=True)
class GitCommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class GitClient:
    def __init__(self, cwd: Path, *, git_bin: str = "git") -> None:
        self.cwd = cwd
        self.git_bin = git_bin

    def run(self, *args: str, timeout: float = 30.0) -> GitCommandResult:
        cmd = [self.git_bin, *args]
        proc = subprocess.run(
            cmd,
            cwd=str(self.cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return GitCommandResult(tuple(cmd), proc.returncode, proc.stdout, proc.stderr)
```

Source pattern: `whilly/workspaces.py` uses list argv, `cwd`, capture, timeout, and `check=False` for Git shellouts.

### Backup Tag Creation

```python
def create_backup_tag(client: GitClient, name: str, head_sha: str, message: str) -> RollbackPoint:
    check = client.run("check-ref-format", f"refs/tags/{name}")
    if check.returncode != 0:
        raise RollbackError(f"invalid rollback tag name: {name}")
    created = client.run("tag", "-a", name, "-m", message, head_sha, timeout=60)
    if created.returncode != 0:
        raise RollbackError(created.stderr.strip() or created.stdout.strip() or "git tag failed")
    return RollbackPoint(name=name, target_sha=head_sha, message=message)
```

Source: Git official docs state `git tag -m` creates an annotated tag when `-a`, `-s`, and `-u` are absent; use explicit `-a` for clarity.

### Restore Confirmation Pattern

```python
def confirmation_phrase(branch: str, target_sha: str) -> str:
    return f"restore {target_sha[:12]} to {branch}"


def confirmed_restore(args: argparse.Namespace, report: PreflightReport) -> bool:
    phrase = confirmation_phrase(report.branch, args.target_sha)
    if args.confirm == phrase:
        return True
    if not sys.stdin.isatty():
        return False
    answer = input(f'Type "{phrase}" to continue: ').strip()
    return answer == phrase
```

Source pattern: `whilly/cli/plan.py::_confirm_reset` treats non-TTY as no answer and requires explicit operator intent.

### PR Sink Preflight Shape

```python
report = build_preflight_report(worktree_path, operation="push", target_branch=branch)
if not report.ok:
    return PRResult(
        ok=False,
        branch=branch,
        reason="rollback preflight failed: " + "; ".join(report.blockers),
        failure_mode="rollback_preflight_failed",
    )
```

Source pattern: `whilly/sinks/github_pr.py::open_pr_for_task` already converts push/PR failures into `PRResult` instead of raising.

## State Of The Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Verifier helper can `git reset --soft HEAD~1` after failed verification | Operator-visible rollback CLI with backup tags, preflight, and confirmation | Phase 10 target | Compliance can distinguish general rollback safety from verifier-only behavior. |
| Text-only diagnostics | JSON/dict preflight reports plus text rendering | Phase 10 target | Tests and operators can audit why a mutation was allowed or blocked. |
| Human-readable Git status parsing | Porcelain status formats | Git documented stable format | Safer dirty checks across user config/color settings. |
| Assuming branch protection locally | Optional GitHub branch API probe with `unknown` fallback | Current GitHub REST docs | Avoids false claims when offline or non-GitHub. |

**Deprecated/outdated:**

- Treating `revert_on_fail` as robust rollback is outdated for compliance. It should remain a partial legacy helper.
- Using broad `--yes` for restore is too weak for this phase. Use exact confirmation for branch reset.
- Automatic production recovery is out of scope.

## Integration Points

| File | Current Role | Phase 10 Action |
|------|--------------|-----------------|
| `whilly/cli/__init__.py` | Lazy top-level dispatcher | Add help text and `rollback` branch importing `whilly.cli.rollback`. |
| `whilly/cli/plan.py` | Existing confirmation/preflight precedent | Reuse non-TTY refusal and explicit operator confirmation pattern. |
| `whilly/workspaces.py` | Git workspace prep and clean-worktree check | Mirror `_git`/`_git_ok` style; do not import private helpers directly. |
| `whilly/verifier.py` | Legacy verification rollback helper | Leave behavior intact; maybe reference in docs/compliance only. |
| `whilly/sinks/github_pr.py` | Runs `git push origin HEAD:<branch> --force-with-lease` | Add push preflight before the push command and return `PRResult` on blockers. |
| `whilly/sinks/post_complete_pr_hook.py` | Converts PR sink results into audit events | Existing `pr.open_failed` can carry preflight failure mode/detail. |
| `whilly/compliance/__init__.py` | Reports `Git rollback` as partial | Upgrade row and tests once safety-net artifacts exist. |
| `tests/unit/test_compliance_report.py` | Compliance row assertions | Add Git rollback tests for PASS/PARTIAL wording. |
| `tests/unit/test_workspaces.py` | Temp Git repo fixture precedent | Reuse fixture style for rollback unit/integration tests. |
| `tests/integration/test_post_complete_pr_hook.py` | PR sink integration with mocked subprocess | Update fake worktree or inject preflight to keep test deterministic. |

## Open Questions

1. **Should missing backup points block push/merge?**
   - What we know: requirements say preflight must report whether a backup point exists.
   - What's unclear: context does not explicitly say missing backup must block every mutation.
   - Recommendation: warning in Phase 10, blocker only if the operator passes a future strict policy flag. This preserves existing opt-in PR behavior while making evidence visible.

2. **Should rollback tags be pushed to remote?**
   - What we know: local tags are enough to restore a local branch and avoid extra external mutation.
   - What's unclear: remote branch recovery may benefit from remote backup refs.
   - Recommendation: keep Phase 10 local by default. If implemented, make `rollback create --push` explicit and preflight it as a separate external mutation.

3. **How deep should GitHub branch protection probing go?**
   - What we know: the GitHub branch endpoint exposes a `protected` field with Contents read access; the detailed branch protection endpoint requires Administration read.
   - What's unclear: Whilly does not currently centralize a generic `gh api` helper for branch protection.
   - Recommendation: implement local evidence first and optional best-effort GitHub branch probe. Never require it for non-GitHub repos.

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 9.0.3, pytest-asyncio configured, pytest-xdist via Makefile |
| Config file | `pyproject.toml` |
| Quick run command | `.venv/bin/python -m pytest -q tests/unit/test_rollback.py tests/unit/test_compliance_report.py --maxfail=1` |
| Integration command | `.venv/bin/python -m pytest -q tests/integration/test_rollback_cli.py --maxfail=1` |
| Full suite command | `make test` |

### Phase Requirements To Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|--------------|
| ROLL-01 | `rollback create` creates deterministic annotated `whilly/rollback/...` tag and `rollback list` returns it as JSON | unit + integration | `.venv/bin/python -m pytest -q tests/unit/test_rollback.py::test_create_backup_tag_uses_whilly_prefix tests/integration/test_rollback_cli.py::test_create_and_list_rollback_points -x` | No - Wave 0 |
| ROLL-02 | Preflight reports branch, HEAD, dirty entries, upstream/protection status, backup status, blockers/warnings before push/merge/restore | unit | `.venv/bin/python -m pytest -q tests/unit/test_rollback.py::test_preflight_report_contains_auditable_git_state -x` | No - Wave 0 |
| ROLL-02 | PR sink push path runs preflight before `git push` and records failure as `PRResult`/`pr.open_failed` instead of raising | unit/integration | `.venv/bin/python -m pytest -q tests/test_github_pr_sink.py tests/integration/test_post_complete_pr_hook.py --maxfail=1` | Existing files to extend |
| ROLL-03 | Restore refuses dirty worktree, requires exact confirmation, and supports dry-run/JSON report | unit + integration | `.venv/bin/python -m pytest -q tests/unit/test_rollback.py::test_restore_refuses_dirty_worktree tests/integration/test_rollback_cli.py::test_restore_requires_exact_confirmation -x` | No - Wave 0 |

### Sampling Rate

- **Per task commit:** `.venv/bin/python -m pytest -q tests/unit/test_rollback.py tests/unit/test_compliance_report.py --maxfail=1`
- **Per wave merge:** `.venv/bin/python -m pytest -q tests/integration/test_rollback_cli.py tests/test_github_pr_sink.py tests/integration/test_post_complete_pr_hook.py --maxfail=1`
- **Phase gate:** `make lint`, `.venv/bin/lint-imports --config .importlinter`, targeted rollback/PR tests, and `make test` when practical.

### Wave 0 Gaps

- [ ] `whilly/rollback/__init__.py` - public exports for rollback models/service
- [ ] `whilly/rollback/models.py` - structured contracts
- [ ] `whilly/rollback/git_ops.py` - subprocess adapter
- [ ] `whilly/rollback/service.py` - create/list/preflight/restore logic
- [ ] `whilly/cli/rollback.py` - CLI parser and rendering
- [ ] `tests/unit/test_rollback.py` - unit coverage for model/service behavior
- [ ] `tests/integration/test_rollback_cli.py` - temp Git repo CLI coverage
- [ ] Extend `tests/unit/test_compliance_report.py` - Git rollback wording/status
- [ ] Extend `tests/test_github_pr_sink.py` or `tests/integration/test_post_complete_pr_hook.py` - push preflight behavior

## Sources

### Primary (HIGH confidence)

- Local: `.planning/phases/10-rollback-safety-net/10-CONTEXT.md` - locked phase decisions and deferred scope.
- Local: `.planning/REQUIREMENTS.md` - ROLL-01, ROLL-02, ROLL-03.
- Local: `.planning/STATE.md` and `.planning/ROADMAP.md` - current phase position and success criteria.
- Local: `docs/CODEX-MISSION.md` - v6 hardening order and validation gates.
- Local: `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md` - Task 6 decomposition.
- Local: `AGENTS.md` - repo conventions, Python 3.12, Ruff, pytest, import purity, Codex mission.
- Local: `whilly/cli/__init__.py` - lazy dispatcher and legacy shim.
- Local: `whilly/cli/plan.py` - `plan reset` preflight and confirmation pattern.
- Local: `whilly/workspaces.py` - Git subprocess wrapper and clean workspace checks.
- Local: `whilly/verifier.py` - legacy verifier rollback helper.
- Local: `whilly/sinks/github_pr.py` and `whilly/sinks/post_complete_pr_hook.py` - current push mutation and audit failure pattern.
- Local: `whilly/compliance/__init__.py` and `tests/unit/test_compliance_report.py` - current rollback compliance row.
- Official Git docs: `https://git-scm.com/docs/git-tag.html` - annotated tags, listing, `--points-at`, message behavior.
- Official Git docs: `https://git-scm.com/docs/git-status` - porcelain status stability for scripts.
- Official Git docs: `https://git-scm.com/docs/git-reset` - reset modes and working tree effects.
- Official Git docs: `https://git-scm.com/docs/git-check-ref-format` - ref validation rules.

### Secondary (MEDIUM confidence)

- GitHub REST docs: `https://docs.github.com/en/rest/branches/branches` - branch endpoint exposes protected status and branch parameters.
- GitHub REST docs: `https://docs.github.com/en/rest/branches/branch-protection?apiVersion=2022-11-28` - detailed protection endpoint permissions and response shape.

### Tertiary (LOW confidence)

- None. Recommendations above do not rely on unverified community sources.

## Files Inspected

- `.planning/phases/10-rollback-safety-net/10-CONTEXT.md`
- `.planning/REQUIREMENTS.md`
- `.planning/STATE.md`
- `.planning/ROADMAP.md`
- `.planning/config.json`
- `docs/CODEX-MISSION.md`
- `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md`
- `AGENTS.md`
- `CLAUDE.md` (treated as stale where it conflicts with AGENTS/current code)
- `.importlinter`
- `Makefile`
- `pyproject.toml`
- `whilly/cli/__init__.py`
- `whilly/cli/plan.py`
- `whilly/cli/run.py`
- `whilly/workspaces.py`
- `whilly/verifier.py`
- `whilly/compliance/__init__.py`
- `whilly/cli/compliance.py`
- `whilly/sinks/github_pr.py`
- `whilly/sinks/post_complete_pr_hook.py`
- `whilly/pipeline/sinks.py`
- `whilly/gh_utils.py`
- `whilly/forge/_gh.py`
- `whilly/external_integrations.py`
- `tests/unit/test_compliance_report.py`
- `tests/unit/test_cli_legacy_flag_shim.py`
- `tests/unit/test_workspaces.py`
- `tests/unit/test_verifier_hardening.py`
- `tests/integration/test_plan_reset.py`
- `tests/integration/test_post_complete_pr_hook.py`
- `tests/test_github_pr_sink.py`

## Metadata

**Confidence breakdown:**

- Standard stack: HIGH - existing repo uses Python stdlib, Git CLI, pytest/Ruff; local versions verified.
- Architecture: HIGH - based on concrete CLI dispatcher, workspace, verifier, PR sink, and compliance code.
- Pitfalls: HIGH for dirty-worktree/restore risks; MEDIUM for GitHub protection details because API probing depends on host, auth, and permissions.
- Validation: HIGH - existing pytest/Makefile/import-linter infrastructure is clear.

**Research date:** 2026-05-08
**Valid until:** 2026-06-07 for local architecture; 2026-05-15 for GitHub protection API details.
