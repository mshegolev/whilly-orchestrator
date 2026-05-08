# Phase 08: Sandbox and Secrets Hardening - Research

**Researched:** 2026-05-08
**Domain:** Python subprocess security, secret linting, runner environment controls, audit evidence
**Confidence:** HIGH

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

#### Scope

- Treat Phase 8 as a guard-and-evidence hardening slice, not a full sandbox backend.
- Create a reusable pure secret-lint module if needed instead of duplicating regexes across prompt,
  PR, config, and command guard code.
- Preserve existing prompt-injection guard and shell deny-list behavior; extend coverage and tests
  rather than replacing the current path.
- Keep full VM/container isolation as a documented residual risk. Do not claim isolation unless an
  actual per-task isolation backend is added in a later phase.

#### Secret linting

- Existing `whilly/security/prompt_sanitizer.py` already redacts common AWS, GitHub, Slack, and
  OpenAI token shapes in sanitized external text and title slots.
- Existing prompt deny scanning blocks only the task description. Acceptance criteria, test steps,
  PRD requirements, PR review comments, and diffs are fenced/redacted but are not currently treated
  as blocking prompt-injection attempts.
- Phase 8 should make that secret-pattern contract reusable for:
  - task descriptions, acceptance criteria, test steps, and PRD requirement text,
  - external issue/PR comments and feedback,
  - runner prompt text before subprocess invocation,
  - config-like string values that may accidentally contain plaintext secrets.
- Guard outputs must use redacted excerpts and stable reason/pattern identifiers. Do not persist raw
  secrets in events, logs, or test fixtures beyond synthetic fake tokens.

#### Runner environment allowlist

- `whilly/pipeline/verification.py` already runs verification commands with an explicit
  `env_allowlist`.
- The Claude runner environment is separate from verification and still needs direct coverage:
  `whilly/adapters/runner/proxy.py` and `whilly/adapters/runner/claude_cli.py` should be inspected
  before implementation because proxy/model-provider credential forwarding can otherwise inherit too
  much from the parent process.
- Phase 8 should define the agent runner environment contract separately from verification: an
  explicit base allowlist plus configured required credential names that may be forwarded.
- The allowlist contract should be pure and testable; worker tests can assert injected hidden
  variables are not passed to the runner surface where the runner environment is controlled by
  Whilly.
- Preserve required credentials for configured model/provider execution; do not silently strip the
  keys the selected runner actually needs.

#### Auditable guard failures

- Local and remote workers already emit prompt guard and shell guard failure paths before runner
  invocation.
- `ShellScanResult` supports warning results, but current worker and verification paths mostly treat
  blocking paths as the auditable surface. If warnings are preserved, tests should pin whether they
  are reported or intentionally non-blocking.
- Phase 8 should make secret-lint blocked work produce the same quality of audit evidence:
  deterministic event type, reason, pattern id, task id, plan id, and redacted excerpt.
- Blocked guard paths must fail or block before agent execution. The runner must not be called after
  a blocking secret/prompt/shell finding.

#### Compliance and docs

- Update compliance evidence so sandbox/security rows describe concrete guards and residual risk.
- Do not turn the `Sandbox/VM isolation` row into a full `PASS` unless per-task VM/container
  isolation is actually implemented.
- It is acceptable for compliance to say command/prompt/secret guards improved while full isolation
  remains partial/future.

### Claude's Discretion

Not specified in CONTEXT.md.

### Deferred Ideas (OUT OF SCOPE)

- Full per-task VM/container isolation backend is explicitly deferred.
- A rich policy engine for governance risk scoring belongs to Phase 12.
- Profile-native verification wiring belongs to Phase 9 and should build on Phase 8 env/guard
  contracts instead of being bundled here.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| SEC-01 | Secret linting covers task descriptions, comments, config values, runner prompts, and external feedback. | Use a new pure `whilly/security/secret_lint.py` pattern registry and wire it into prompt building, ingestion/feedback paths, verification details, and config-like string checks. |
| SEC-02 | Runner environments are scrubbed to an explicit allowlist plus configured required tokens. | Reuse the verification allowlist precedent, but add an agent-runner-specific env builder used by async Claude, sync Claude, OpenCode, and handoff paths where Whilly controls `env=`. |
| SEC-03 | Command and prompt guard failures emit auditable reasons. | Preserve existing prompt/shell prelude events and add `secret_lint_blocked` with the same event quality: reason, pattern id, field path, task id, plan id, redacted excerpt, and no runner invocation. |
</phase_requirements>

## Summary

Phase 8 should be planned as a repository-local security hardening slice, not as a new isolation
platform. The existing code already has the right seams: pure prompt and shell scanners in
`whilly.core`/`whilly.security`, worker pre-run guard branches in local and remote workers, a
verification runner with explicit env allowlisting, and compliance rows that already distinguish
partial sandbox posture from full VM isolation.

The main implementation risk is duplicated or misleading security behavior. Secret patterns are
currently embedded in `prompt_sanitizer.py` and partially duplicated in `sources/github_issues.py`.
Runner subprocesses also still inherit broad parent environments through `proxy.spawn_env_for_claude`
and the synchronous backend `subprocess.run`/`Popen` calls. The plan should centralize both contracts:
one pure secret-lint/redaction module, and one explicit runner-env builder that receives a parent
mapping and returns only allowed names plus required provider credentials.

**Primary recommendation:** Add reusable pure `secret_lint` and `runner_env` contracts first, then
wire them into worker guard paths, runner subprocess calls, verification detail redaction, and
compliance wording without changing the roadmap boundary: sandbox/VM isolation remains partial.

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python stdlib `re`, `dataclasses`, `os`, `subprocess`, `asyncio.subprocess` | Python 3.12.1 local runtime, 3.12 docs current | Pattern registry, immutable finding values, environment maps, subprocess env control | No new dependency needed; current project already uses these APIs for prompt/shell guards and verification. |
| `whilly.security.prompt_sanitizer` | whilly-orchestrator 4.6.3 | Existing secret redaction and untrusted-text fencing | Reuse its patterns through a shared module rather than keeping private regex copies. |
| `whilly.core.agent_runner` | whilly-orchestrator 4.6.3 | Existing shell-command deny-list and structured guard result | Preserve current fail-before-runner behavior and event payload shape. |
| `whilly.pipeline.verification` | whilly-orchestrator 4.6.3 | Existing env allowlist, command scan, output capture, verification events | This is the local precedent for explicit environment inheritance. |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `pytest` / `pytest-asyncio` | pytest 9.0.3 local, pytest-asyncio >=0.23 from project deps | Unit and async worker tests | Use for focused guard/env/audit tests before broader worker suites. |
| `ruff` | 0.11.5 local and pinned in `pyproject.toml` | Formatting and linting | Run after implementation touches security/worker modules. |
| `import-linter` | 2.11 local | Enforce `whilly.core` purity | Run because new guard helpers must not accidentally import adapters/network/subprocess into `whilly.core`. |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Pure `whilly.security.secret_lint` | `detect-secrets`, TruffleHog, GitHub push protection | Good for repository/history scanning, but too heavy and out of scope for deterministic in-process task/config/prompt guards. |
| Explicit runner env builder | Continue copying `os.environ` | Preserves compatibility but violates SEC-02 and forwards unrelated secrets to child agents. |
| Residual-risk compliance wording | Claim sandbox PASS because tool flags are restricted | Misleading. Tool flags and env scrubbing are guards, not VM/container isolation. |

**Installation:**

No new package should be installed for Phase 8. Use the existing dev install:

```bash
pip install -e '.[dev]'
```

**Version verification:** Verified locally on 2026-05-08:

```bash
.venv/bin/python --version        # Python 3.12.1
.venv/bin/python -c "import pytest; print(pytest.__version__)"  # 9.0.3
.venv/bin/ruff --version          # ruff 0.11.5
.venv/bin/lint-imports --version  # import-linter 2.11
```

## Architecture Patterns

### Recommended Project Structure

```text
whilly/
+-- security/
|   +-- secret_lint.py           # pure pattern registry, scan/redact helpers, finding dataclasses
|   +-- prompt_sanitizer.py      # delegates redaction to secret_lint
+-- core/
|   +-- agent_runner.py          # shell scan stays pure; optionally redacts excerpts via secret_lint
|   +-- prompts.py               # prompt guard + secret guard before prompt reaches runner
+-- adapters/
|   +-- runner/
|       +-- env.py               # pure env builder over a supplied parent mapping
|       +-- proxy.py             # proxy resolution feeds env builder, no full parent copy
|       +-- claude_cli.py        # async subprocess uses scrubbed env
+-- agents/
|   +-- claude.py                # sync subprocess uses scrubbed env
|   +-- opencode.py              # sync subprocess uses scrubbed env
|   +-- claude_handoff.py        # handoff subprocess passes only handoff vars plus base env
+-- compliance/
    +-- __init__.py              # reports concrete guards plus residual isolation risk

tests/unit/
+-- test_secret_lint.py
+-- test_runner_env.py
+-- test_prompt_sanitizer.py
+-- test_prompt_sanitizer_wiring.py
+-- test_claude_subprocess_env.py
+-- test_local_worker.py
+-- test_remote_worker.py
+-- test_compliance_report.py
```

### Pattern 1: Pure Secret Lint Contract

**What:** Centralize regex metadata, scanning, redaction, and audit-safe excerpts in
`whilly/security/secret_lint.py`.

**When to use:** Any string that can reach prompts, events, verification details, config values,
issue/PR feedback, PR bodies, task descriptions, acceptance criteria, or test steps.

**Example:**

```python
# Source: repo pattern based on whilly/security/prompt_sanitizer.py and OWASP logging guidance.
from dataclasses import dataclass
import re

SECRET_LINT_BLOCKED_EVENT_TYPE = "secret_lint_blocked"
SECRET_LINT_FAIL_REASON = "secret_lint_blocked"

@dataclass(frozen=True)
class SecretPattern:
    pattern_id: str
    regex: re.Pattern[str]
    replacement: str

@dataclass(frozen=True)
class SecretFinding:
    pattern_id: str
    field_path: str
    redacted_excerpt: str

    def event_payload(self, *, task_id: str, plan_id: str) -> dict[str, str]:
        return {
            "event_type": SECRET_LINT_BLOCKED_EVENT_TYPE,
            "pattern_id": self.pattern_id,
            "field_path": self.field_path,
            "task_id": task_id,
            "plan_id": plan_id,
            "redacted_excerpt": self.redacted_excerpt,
        }
```

Implementation should include current sanitizer patterns plus repo-relevant provider credentials:
AWS access keys, GitHub PAT prefixes, Slack `xox...`, OpenAI `sk-...` and `sk-proj-...`, Anthropic
`sk-ant-...`, Groq `gsk_...`, private-key headers, bearer/basic auth headers, and database URLs
with embedded credentials. For config-like mappings, also flag non-empty values whose key name
contains `TOKEN`, `SECRET`, `PASSWORD`, `API_KEY`, `DATABASE_URL`, or `DSN`.

### Pattern 2: Scan Structured Surfaces Before Runner Invocation

**What:** Add one helper that scans task-owned instruction fields and returns a first blocking
secret finding. Keep raw external PR diff/comment prompt-injection text fenced; do not turn PR
comment instructions into prompt-injection false positives unless they contain secrets.

**When to use:** In both `run_local_worker` and `run_remote_worker` after prompt construction
context is known and before the runner coroutine is called.

**Example:**

```python
# Source: repo pattern based on whilly/core/agent_runner.py::scan_task_command_surface.
def scan_task_secret_surface(task: Task, *, prompt: str = "") -> SecretFinding | None:
    surfaces = {
        "task.description": task.description,
        "task.prd_requirement": task.prd_requirement,
        **{f"task.acceptance_criteria[{i}]": v for i, v in enumerate(task.acceptance_criteria)},
        **{f"task.test_steps[{i}]": v for i, v in enumerate(task.test_steps)},
        "runner.prompt": prompt,
    }
    return first_secret_finding(surfaces)
```

Worker branches should mirror prompt and shell guard branches: record `pipeline.stage.failed`,
fail the task with reason `secret_lint_blocked`, emit a security prelude event, increment failed
stats, and never invoke the runner.

### Pattern 3: Explicit Runner Environment Builder

**What:** Build child subprocess environments from a parent mapping and explicit names, never by
copying the whole parent environment.

**When to use:** Any agent subprocess path Whilly owns: async `adapters/runner/claude_cli.py`,
sync `agents/claude.py`, sync `agents/opencode.py`, and `agents/claude_handoff.py`.

**Example:**

```python
# Source: repo pattern based on whilly/pipeline/verification.py::_allowed_env and Python subprocess docs.
BASE_RUNNER_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "WHILLY_MODEL",
    "CLAUDE_BIN",
    "WHILLY_OPENCODE_BIN",
)

def build_runner_env(parent: Mapping[str, str], *, required_env: tuple[str, ...]) -> dict[str, str]:
    allowed = {*BASE_RUNNER_ENV_ALLOWLIST, *required_env}
    return {name: parent[name] for name in sorted(allowed) if name in parent}
```

Required provider env names should be inferred from the selected backend/model and overridable by
an explicit, testable parameter. Baseline provider names observed in this repo are:
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`,
`OPENCODE_API_KEY`, and `OPENCODE_ZEN_API_KEY`. Do not forward unrelated operational secrets such
as `WHILLY_WORKER_TOKEN`, `WHILLY_WORKER_BOOTSTRAP_TOKEN`, `WHILLY_ADMIN_TOKEN`,
`WHILLY_DATABASE_URL`, Slack tokens, or GitHub tokens to coding-agent child processes.

### Pattern 4: Redact Audit Details At Event Boundary

**What:** Redact output strings before constructing event payload/detail objects.

**When to use:** `make_verification_result_event`, worker fail reasons, LLM/session detail if it
stores prompt/output excerpts, and PR feedback follow-up events.

**Example:**

```python
# Source: repo pattern based on whilly/pipeline/verification.py::make_verification_result_event.
def make_verification_result_event(task_id: str, result: VerificationCommandResult, *, plan_id: str = ""):
    safe_stdout = redact_secrets(result.stdout)
    safe_stderr = redact_secrets(result.stderr)
    return PipelineTaskEvent(
        task_id=task_id,
        event_type=result.event_name,
        payload={...},
        detail={"stdout": safe_stdout, "stderr": safe_stderr},
    )
```

### Anti-Patterns to Avoid

- **Copying `os.environ` in runner paths:** `proxy.build_subprocess_env` currently returns
  `dict(parent_env)` when inactive. That is exactly the SEC-02 failure mode for agent subprocesses.
- **Leaving regexes private to one module:** `_SECRET_REPLACEMENTS` and GitHub issue
  `_SECRET_PATTERNS` already drift. Replace both with shared `secret_lint` helpers.
- **Raw secret in event detail because the task is already failed:** Failed/audited paths are the
  highest-risk place to leak secrets. Findings must include redacted excerpts only.
- **Claiming sandbox PASS after env/tool hardening:** CLI permission flags, deny lists, and env
  scrubbers are guardrails, not isolation.
- **Expanding Phase 8 into profile-native verification:** Use the env/guard contracts so Phase 9 can
  reuse them; do not wire `ProjectConfig.verification_commands` into runtime here.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Full repo history secret scanner | New entropy engine or git-history crawler | Small `secret_lint` runtime scanner now; GitHub/TruffleHog/detect-secrets can be a future repo-scan phase | Phase 8 protects runtime task/config/prompt surfaces, not full repository forensics. |
| VM/container sandbox | Fake "sandbox" wrapper around subprocess flags | Existing CLI tool restrictions, command/prompt/secret guards, env scrub, and residual-risk docs | Full per-task isolation is ISO-01 and explicitly deferred. |
| Shell parser | A custom shell grammar | Existing `scan_command` deny-list plus Python `create_subprocess_exec` argv lists for agent prompts | Shell parsing is brittle; scan known risky surfaces and avoid shell for prompt-bearing agent subprocesses. |
| Audit taxonomy | One-off event shapes per guard | Existing `PipelineTaskEvent`, `fail_task` detail, and prelude event pattern | Operators need deterministic event type/reason/pattern fields across guards. |
| Config loader rewrite | New settings framework | Existing dataclass/TOML loader plus `secret_lint.scan_mapping` | Project already uses strict dataclass loaders and `whilly.secrets` reference schemes. |

**Key insight:** This phase is about removing ambient trust from existing surfaces. The safest plan is
to centralize small deterministic contracts and wire them through known seams, not to add a broad new
security subsystem.

## Common Pitfalls

### Pitfall 1: Inactive Proxy Still Copies Secrets

**What goes wrong:** `proxy.build_subprocess_env(parent_env, inactive_settings)` returns a full copy
of the parent environment, so secrets unrelated to the selected agent model reach the child process.

**Why it happens:** Proxy code was built to avoid mutating parent env, not to scrub child env.

**How to avoid:** Build a scrubbed base env first, then layer proxy keys only if active. Tests should
plant `WHILLY_DATABASE_URL`, `WHILLY_WORKER_TOKEN`, `GH_TOKEN`, and `SLACK_ACCESS_TOKEN` and assert
they are absent from subprocess `env=`.

**Warning signs:** A test asserts only that `HTTPS_PROXY` is present/absent but not that unrelated
secrets are absent.

### Pitfall 2: Stripping Required Agent Credentials

**What goes wrong:** Env scrub is too strict and breaks legitimate `WHILLY_MODEL=groq/...` or
Anthropic/OpenAI model runs.

**Why it happens:** Provider credentials are currently ambient; there is no explicit runner-env
contract.

**How to avoid:** Infer required provider names from backend/model and support explicit required env
names as a function parameter. Preserve zero-key `opencode/big-pickle` by not requiring a provider
key for that model.

**Warning signs:** `test_zero_key_default_works.py` or Groq credential diagnostics fail after env
scrub work.

### Pitfall 3: Secret Redaction Still Leaves Raw Match In Excerpt

**What goes wrong:** Findings include `matched_secret` or an excerpt around the secret that still
contains the raw value.

**Why it happens:** Prompt guard excerpts redact the matched marker; secret lint must be at least as
strict.

**How to avoid:** Store only `pattern_id`, `field_path`, and `redacted_excerpt`. Unit tests should
assert fake token strings are absent from payload, detail, stdout/stderr, PR bodies, and task
descriptions.

**Warning signs:** Tests compare against the literal fake secret inside an expected event payload.

### Pitfall 4: Prompt Guard Expansion Creates External Feedback False Positives

**What goes wrong:** PR review comments like "ignore previous behavior" block a repair task even
though comments are already fenced as untrusted data.

**Why it happens:** Prompt injection guard and secret lint solve different problems. External
feedback should be secret-linted and fenced; task-owned instruction fields are the safer blocking
surface for prompt-injection markers.

**How to avoid:** Keep prompt-injection blocking focused on task-owned instruction surfaces unless a
test explicitly documents a new external blocking rule. Apply secret lint to external feedback
regardless.

**Warning signs:** A review comment containing a prompt-injection marker blocks before the planner
has decided that false-positive tradeoff.

### Pitfall 5: Remote Worker Loses Security Prelude Events

**What goes wrong:** Local worker emits `prompt_injection_blocked`/`shell_command_blocked` prelude
events, but remote worker only sends generic failure detail.

**Why it happens:** Remote `fail` routes through `transport/server.py`; the server has a
`security_prelude_events` allowlist that currently names prompt and shell events only.

**How to avoid:** Add `secret_lint_blocked` to the server allowlist and pin remote worker tests plus
transport integration coverage.

**Warning signs:** Local worker tests pass but `tests/unit/test_remote_worker.py` has no assertion
for the new event type.

### Pitfall 6: Compliance Row Overclaims Isolation

**What goes wrong:** The `Sandbox/VM isolation` row changes to PASS because guards improved.

**Why it happens:** Compliance currently uses simple deterministic source probes.

**How to avoid:** Keep status PARTIAL unless a per-task VM/container backend exists. Update evidence
and risk text to name prompt, shell, secret, env, and audit improvements plus residual risk.

**Warning signs:** `test_compliance_report.py` no longer expects sandbox/VM isolation to be partial.

## Code Examples

Verified patterns from current repo and official sources:

### Env Allowlist Pattern

```python
# Source: whilly/pipeline/verification.py and Python subprocess docs.
def _allowed_env(env_allowlist: tuple[str, ...]) -> dict[str, str]:
    return {name: os.environ[name] for name in env_allowlist if name in os.environ}
```

Adapt this to accept a parent mapping so tests do not depend on global `os.environ`.

### Worker Guard Branch Pattern

```python
# Source: whilly/worker/local.py prompt/shell guard branches.
finding = scan_task_secret_surface(running, prompt=prompt)
if finding is not None:
    payload = finding.event_payload(task_id=running.id, plan_id=plan.id)
    await _record_pipeline_event(
        repo,
        make_stage_failed_event(stage_context, reason=SECRET_LINT_FAIL_REASON, detail=payload),
    )
    await repo.fail_task(
        running.id,
        running.version,
        SECRET_LINT_FAIL_REASON,
        detail=payload,
        prelude_event_type=SECRET_LINT_BLOCKED_EVENT_TYPE,
        prelude_payload=payload,
    )
    failed += 1
    continue
```

### Remote Prelude Allowlist Pattern

```python
# Source: whilly/adapters/transport/server.py fail_task route.
security_prelude_events = {
    PROMPT_INJECTION_BLOCKED_EVENT_TYPE,
    SHELL_COMMAND_BLOCKED_EVENT_TYPE,
    SECRET_LINT_BLOCKED_EVENT_TYPE,
}
```

### Subprocess Safety Pattern

```python
# Source: whilly/adapters/runner/claude_cli.py and Python subprocess docs.
proc = await asyncio.create_subprocess_exec(
    *cmd,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    env=build_runner_env(os.environ, required_env=required_provider_env),
)
```

Use argv-list subprocesses for prompts. Python docs state that env mappings replace inherited
environment behavior, and warn that shell command strings require proper quoting to avoid injection.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Sanitizer owns private secret regexes | Shared secret lint/redaction registry | Phase 8 plan | Prevents drift between prompt, PR, config, and feedback paths. |
| Agent subprocess inherits parent env | Explicit base allowlist plus provider-required env names | Phase 8 plan | Reduces accidental token exposure to agent tools. |
| Prompt/shell guards only | Prompt, shell, secret, env, and audit guard evidence | Phase 8 plan | Compliance can report concrete hardening while full isolation remains future. |
| Verification output persisted raw | Verification stdout/stderr redacted before event detail | Phase 8 plan | Aligns audit detail with OWASP logging guidance to mask/remove tokens and database URLs. |

**Deprecated/outdated:**

- Treating `--disallowedTools` as sandbox isolation: it is a useful runner guard, not VM/container
  isolation.
- Copying all of `os.environ` into child agents: this is no longer acceptable for SEC-02.
- Keeping best-effort GitHub issue secret warnings separate from prompt sanitizer patterns: centralize
  through `secret_lint`.

## Open Questions

1. **Where should explicit required runner token names be configured?**
   - What we know: The repo currently infers provider needs from `WHILLY_MODEL` in some paths and
     docs name `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GROQ_API_KEY`, `GEMINI_API_KEY`,
     `OPENROUTER_API_KEY`, `OPENCODE_API_KEY`, and `OPENCODE_ZEN_API_KEY`.
   - What's unclear: There is no existing `ProjectConfig` field for runner env names, and adding one
     would widen the phase.
   - Recommendation: In Phase 8, implement inference plus an explicit function parameter/test seam.
     Defer a public profile config field unless implementation proves it is necessary.

2. **Should prompt-injection blocking expand to PR review comments and diffs?**
   - What we know: Those surfaces are intentionally fenced as untrusted data and can naturally contain
     adversarial text.
   - What's unclear: Blocking them may create false positives in legitimate review-repair workflows.
   - Recommendation: Secret-lint external feedback now; keep prompt-injection blocking to task-owned
     instruction fields unless a specific acceptance test requires broader blocking.

3. **Should Slack/GitHub tokens ever be forwarded to agent subprocesses?**
   - What we know: `gh` subprocesses use `gh_subprocess_env`; agent subprocesses should not need
     `WHILLY_GH_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, Slack tokens, worker tokens, or database URLs.
   - What's unclear: Legacy workflows may rely on ambient `gh` auth from within an agent-run command.
   - Recommendation: Do not forward those tokens to coding-agent child processes in Phase 8. If a
     later phase needs agent-mediated GitHub/Slack access, model it as an explicit sink/credential
     grant with audit evidence.

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 9.0.3, pytest-asyncio >=0.23 |
| Config file | `pyproject.toml` (`[tool.pytest.ini_options]`, testpaths=`tests`, asyncio_mode=`auto`) |
| Quick run command | `.venv/bin/python -m pytest -q tests/unit/test_secret_lint.py tests/unit/test_runner_env.py tests/unit/test_prompt_sanitizer.py tests/unit/test_claude_subprocess_env.py --maxfail=1` |
| Full suite command | `make test` |

### Phase Requirements -> Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|--------------|
| SEC-01 | Secret lint detects and redacts configured token patterns across task fields, prompts, config-like mappings, feedback, verification details, and PR bodies. | unit/integration | `.venv/bin/python -m pytest -q tests/unit/test_secret_lint.py tests/unit/test_prompt_sanitizer.py tests/unit/test_prompt_sanitizer_wiring.py tests/unit/test_verification_runner.py tests/integration/test_pr_feedback_e2e.py --maxfail=1` | Partial; `test_secret_lint.py` needed |
| SEC-02 | Agent subprocess env contains only base allowlist plus inferred/required provider credentials; hidden tokens are absent. | unit | `.venv/bin/python -m pytest -q tests/unit/test_runner_env.py tests/unit/test_claude_subprocess_env.py tests/unit/test_claude_cli.py tests/unit/test_worker_default_deny.py --maxfail=1` | Partial; `test_runner_env.py` needed |
| SEC-03 | Secret/prompt/shell guard blocks happen before runner calls and emit deterministic auditable reason/pattern payloads on local and remote paths. | unit/integration | `.venv/bin/python -m pytest -q tests/unit/test_local_worker.py tests/unit/test_remote_worker.py tests/integration/test_transport_tasks.py tests/unit/test_compliance_report.py --maxfail=1` | Yes, extend existing files |

### Sampling Rate

- **Per task commit:** `.venv/bin/python -m pytest -q tests/unit/test_secret_lint.py tests/unit/test_runner_env.py tests/unit/test_local_worker.py tests/unit/test_remote_worker.py --maxfail=1`
- **Per wave merge:** `.venv/bin/python -m pytest -q tests/unit/test_prompt_sanitizer.py tests/unit/test_prompt_sanitizer_wiring.py tests/unit/test_verification_runner.py tests/unit/test_claude_subprocess_env.py tests/unit/test_compliance_report.py --maxfail=1`
- **Phase gate:** `.venv/bin/python -m ruff check whilly/ tests/`; `.venv/bin/python -m ruff format --check whilly/ tests/`; `.venv/bin/lint-imports --config .importlinter`; `make test` when practical.

### Wave 0 Gaps

- [ ] `tests/unit/test_secret_lint.py` - covers SEC-01 secret pattern metadata, redaction, mapping scans, excerpts, and no raw secret payloads.
- [ ] `tests/unit/test_runner_env.py` - covers SEC-02 base allowlist, provider-required credentials, proxy layering, and hidden env exclusion.
- [ ] Extend `tests/unit/test_claude_subprocess_env.py` - assert unrelated secrets are absent, not only proxy keys present.
- [ ] Extend `tests/unit/test_local_worker.py` and `tests/unit/test_remote_worker.py` - cover `secret_lint_blocked` pre-run failure and runner-not-called behavior.
- [ ] Extend `tests/integration/test_transport_tasks.py` - cover remote fail prelude acceptance for `secret_lint_blocked`.
- [ ] Extend `tests/unit/test_compliance_report.py` - keep sandbox/VM isolation PARTIAL while evidence mentions env and secret guards.

## Sources

### Primary (HIGH confidence)

- `.planning/phases/08-sandbox-and-secrets-hardening/08-CONTEXT.md` - locked phase decisions, scope, success criteria, code insights.
- `.planning/REQUIREMENTS.md` - SEC-01, SEC-02, SEC-03 requirement definitions.
- `.planning/ROADMAP.md` and `.planning/ROADMAP-ANALYSIS.md` - phase order and residual-risk boundary.
- `docs/CODEX-MISSION.md` - v6 hardening scope and validation gates.
- `docs/superpowers/plans/2026-05-07-doc-pack-alignment-roadmap.md` - source backlog task 5.
- `whilly/security/prompt_sanitizer.py` - current secret redaction/fencing contract.
- `whilly/core/agent_runner.py` and `whilly/core/prompts.py` - shell and prompt guard contracts.
- `whilly/pipeline/verification.py` - existing env allowlist and verification event pattern.
- `whilly/adapters/runner/proxy.py`, `whilly/adapters/runner/claude_cli.py`, `whilly/agents/claude.py`, `whilly/agents/opencode.py`, `whilly/agents/claude_handoff.py` - runner env inheritance surfaces.
- `whilly/worker/local.py`, `whilly/worker/remote.py`, `whilly/adapters/transport/server.py` - guard failure and prelude event paths.
- `whilly/compliance/__init__.py` - compliance row status/evidence/risk wording.
- Python 3.12 subprocess docs - `env` mapping replaces default environment inheritance; argv sequences are recommended; shell strings require quoting care. https://docs.python.org/3.12/library/subprocess.html
- Python 3.12 asyncio subprocess docs - `create_subprocess_exec` and `create_subprocess_shell` APIs; shell injection warning for command strings. https://docs.python.org/3.12/library/asyncio-subprocess.html

### Secondary (MEDIUM confidence)

- OWASP Logging Cheat Sheet - event attributes should include type/status/reason; access tokens, passwords, database connection strings, encryption keys, and primary secrets should be removed/masked/sanitized/hashed/encrypted before logging. https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html
- GitHub supported secret scanning patterns - confirms OpenAI keys and generic secret categories such as private keys, bearer/basic auth headers, and database connection strings are recognized secret-scanning categories. https://docs.github.com/en/code-security/reference/secret-security/supported-secret-scanning-patterns

### Tertiary (LOW confidence)

- None. External findings used here were cross-checked against official docs or repository source.

## Metadata

**Confidence breakdown:**

- Standard stack: HIGH - derived from `pyproject.toml`, local version commands, and existing repo modules.
- Architecture: HIGH - direct source inspection found stable seams for secret linting, env building, worker guards, transport prelude events, and compliance.
- Pitfalls: HIGH - each pitfall maps to an observed current code path or documented phase constraint.
- External docs: MEDIUM - official sources verify subprocess/env/logging principles, but exact provider token regexes should be pinned by local tests.

**Research date:** 2026-05-08
**Valid until:** 2026-06-07 for repo-local architecture; re-check provider token patterns before expanding beyond the minimum set.
