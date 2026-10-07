# Whilly Product Swarm: first bounded use

This runbook is for one documentation-only feature in a disposable project. It
uses the Product Swarm cockpit and normal server composition. It is not a
production rollout or a general-purpose autonomous mode. Keep the database,
registry, state directory, candidate repository, and logs outside the source
checkout and outside shared or production services.

## Before starting

Use a fresh PostgreSQL database with no valuable data, a new local Git
repository with no remote, and a private registry that points only at that
repository. Store the registry and provider credentials outside tracked files;
restrict the registry to the operator (`chmod 600`). Do not add a `publication`
entry, remote URL, push credential, or deployment-capable integration. The
normal ProductWorkflow composition has no publication backend, but execution
still attempts publication after accepted tasks; confirm the running server
uses that normal composition and that publication fails closed before making
the model call.

Use a configured planner and explicit `worker-cheap` and `reviewer-cheap`
profiles. Check every profile's engine, exact model, auth source, timeout, and
turn limit. Codex does not enforce profile `budget_usd` or `max_turns` as hard
per-process caps. If the chosen backend cannot enforce a monetary cap, report
cost as unknown and rely only on the configured feature-call/time bounds and
any provider-side limit you verified. Never copy credentials into a profile,
prompt, shell transcript, receipt, or Git file.

Copy [`../../examples/product-swarm-first-use.template.json`](../../examples/product-swarm-first-use.template.json)
to the private registry path and replace every `__...__` placeholder. The
template is intentionally invalid until those host-specific private paths are
set. A Product Swarm entry needs its complete
`product_policy`, `verification`, `verification_policy`, profiles, toolchains,
and phase mapping; registry validation is the schema gate. Use absolute paths
for the disposable repository and private state directory. Set `allowed_paths`
to `READY.md`, protect the seed `README.md`, use `/bin/test -f READY.md` for the
task/fast/full checks, and make the test verification compare the exact bytes
`b"- Canary completed.\n"`. Configure planner, worker, and reviewer profiles
explicitly; never rely on an implicit provider fallback.

The first-use evidence came from a disposable repository and database. The
actual run used three calls (planner, worker, reviewer), one worker, a 900
second feature elapsed limit, and no publication transport. Its receipt records
unknown cost. The independent reviewer model returned `approve` after receiving
the exact coordinator-produced diff and byte-for-byte host verification
evidence. Its explanation says direct shell inspection was blocked by the
execution sandbox. Treat this as evidence of an independent evidence review,
not evidence of independent workspace inspection or human-quality review.

## Preflight commands

Run these in the Whilly checkout using Python 3.12 and `uv`. Set the variables
in the current shell to the disposable resources only, replacing every
angle-bracket placeholder with the actual local value before executing. Do not
paste the DSN or registry contents into shared logs.

```bash
export WHILLY_DATABASE_URL='postgresql://<disposable-user>:<local-only-password>@127.0.0.1:<port>/<disposable-db>'
export WHILLY_SWARM_REGISTRY='<private-absolute-path>/registry.json'
export WHILLY_PRODUCT_SWARM=1
export WHILLY_CANARY_REPO='<private-absolute-path>/repo'

pg_isready -d "$WHILLY_DATABASE_URL"
uv run --extra server alembic current
uv run --extra server alembic upgrade head
uv run whilly swarm registry validate --registry "$WHILLY_SWARM_REGISTRY"
```

The DSN must point to the disposable database. `alembic upgrade head` changes
that database, so do not run it until the target has been checked. Registry
validation output may include local project identifiers; keep it local.

Then confirm that the registry resolves the required profiles and that the
host can run its actual sandbox deny probes. This command prints readiness
evidence but never prints credential values:

```bash
uv run --extra server python -c 'import json, os; from whilly.swarm.registry import load_registry; from whilly.swarm.execution import GuardedExecutor; result = GuardedExecutor.from_registry(load_registry(os.environ["WHILLY_SWARM_REGISTRY"])).ready(); print(json.dumps(result, sort_keys=True)); raise SystemExit(0 if result.get("ready") is True else 1)'
```

Stop if the command exits non-zero, reports missing auth/toolchain, or either
outside-read or network denial probe fails. Do not switch provider, disable
isolation, or widen access to continue.

Check the disposable repository boundary before starting the server:

```bash
git -C "$WHILLY_CANARY_REPO" status --short --branch
git -C "$WHILLY_CANARY_REPO" remote -v
```

The second command must print no remotes. The repository must contain only the
throwaway documentation fixture. Use a separate state directory in the private
registry; do not point it at the source repository or an existing Swarm state
directory.

Start the local control plane bound to loopback:

```bash
uv run --extra server whilly server --host 127.0.0.1 --port 8000
```

In another terminal, check database-backed health and the authenticated
cockpit route:

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/health
```

Open `http://127.0.0.1:8000/swarm/product` in the browser and sign in as an
administrator. The browser path preserves the app's normal session and CSRF
checks. The page must show the expected planner profile; a missing planner is
`planner_setup_required` and is a stop condition.

## One-feature workflow

1. Create a throwaway feature titled `First bounded use` with this intent:
   `Create READY.md containing exactly one line: - Canary completed. Do not
   change any other file.` The plan must contain exactly one task in the
   disposable repository. Do not use the chief Discuss action for the
   three-call acceptance receipt: discussion is optional discovery and consumes
   a separate call reservation before planning.
2. Set the feature budget to `max_calls=3` and
   `max_elapsed_seconds=900` before planning. Three calls cover one planner,
   one worker, and one reviewer; a retry/escalation must fail closed. A one
   worker concurrency setting does not constrain the plan to one task, so
   inspect and require exactly one task yourself.
3. Choose an explicit planner profile and request Plan. Inspect the complete
   specification, task, acceptance criteria, verification argv, project path,
   base SHA, registry binding, and displayed digest. Reject any extra task,
   source-code change, remote/publish action, or unexpected command.
4. Approve the displayed revision and server-computed digest only after
   confirming all bindings. Any change to the feature, budget, registry, base
   SHA, or verification policy requires a new plan review and approval.
5. Run with `workers=1`. Observe task state, attempt, verification, reviewer
   verdict, feature status, and call reservations. Do not click Publish.
6. On a successful docs task, expect the feature to reach `review`; the normal
   workflow then records `publication_unavailable` because no publication
   backend is provisioned. That named blocker is expected for this exercise.
   Save a redacted receipt and stop the feature so the session is drained.

The corresponding authenticated API actions are `POST
/api/v1/swarm/products/default/features`, then `POST
/api/v1/swarm/features/{feature_id}/budget`, `/plan`, `/approve`, `/run` with
`{"workers":1}`, and `/stop` when needed. They require the normal admin session
and same-origin CSRF checks. Use the cockpit unless you already have a safe
session-aware API client; do not put cookies, CSRF values, or bearer tokens in
the runbook or receipt.

## Stop and retain evidence

For a Product Swarm feature, use its **Stop** action in the cockpit, or send
the authenticated same-origin request below from a session-aware client. For
curl, point `WHILLY_ADMIN_COOKIE_JAR` at a private Netscape-format cookie jar
containing the administrator session (`chmod 600`); keep it outside the
checkout and do not print or commit it. Replace the example feature ID locally.

```bash
export WHILLY_BASE_URL='http://127.0.0.1:8000'
export WHILLY_ADMIN_COOKIE_JAR='<private-absolute-path>/admin-cookies.txt'
export WHILLY_FEATURE_ID='<feature-id-from-cockpit>'
curl --fail-with-body --silent --show-error \
  --cookie "$WHILLY_ADMIN_COOKIE_JAR" \
  --header "Origin: $WHILLY_BASE_URL" \
  --request POST \
  "$WHILLY_BASE_URL/api/v1/swarm/features/$WHILLY_FEATURE_ID/stop"
```

Require the response to contain `status: "stop_requested"` and `drained: true`.
If `drained` is false, shutdown is pending: keep the candidate worktree and
logs, inspect the running coordinator/process identity, and do not claim that
all work stopped. Do not kill by PID alone. Stop at the first auth, sandbox,
binding, policy, or budget blocker; do not retry by changing providers or
limits. Stop does not undo local candidate commits and cannot reverse a remote
publication already in flight, so the no-publication condition is a preflight
gate, not a recovery action.

For a local CLI Swarm session (the legacy session workflow, not the Product
feature API), graceful stop is:

```bash
uv run whilly swarm stop --session "$SESSION_ID"
uv run whilly swarm report --session "$SESSION_ID" --json
```

Do not use `--kill` as a routine stop. Preserve the disposable database,
registry, repository, state, logs, and receipt until the result is reviewed.
Cleanup is a separate operator action and must target only those confirmed
disposable paths.

## Redacted successful canary receipt

This receipt is from the disposable first-use run. Paths and host-specific
identifiers are removed. Cost remains unknown where the backend did not report
it.

```json
{
  "feature_id": "<redacted-feature>",
  "session_id": "<redacted-session>",
  "revision": 2,
  "plan_revision": 1,
  "approval_digest": "<redacted-sha256>",
  "base_sha": "<redacted-sha>",
  "candidate_sha": "<redacted-sha>",
  "profiles": {
    "planner": {"engine": "codex", "model": "gpt-6-luna", "reasoning_effort": "medium"},
    "worker-cheap": {"engine": "codex", "model": "gpt-6-luna", "reasoning_effort": "medium"},
    "reviewer-cheap": {"engine": "codex", "model": "gpt-6.1-sol", "reasoning_effort": "low"}
  },
  "budget": {"max_calls": 3, "max_elapsed_seconds": 900},
  "task": {"count": 1, "attempts": 1, "status": "DONE", "outcome": "accepted"},
  "verification": {"task": "passed", "test": "passed", "lint": "passed", "architecture": "passed"},
  "review": {
    "verdict": "approve",
    "basis": "exact coordinator diff plus byte-for-byte host verification evidence",
    "limitation": "direct workspace inspection was blocked by the execution sandbox"
  },
  "calls": {"planner": 1, "worker": 1, "reviewer": 1, "total": 3},
  "elapsed_seconds": 60.386,
  "cost_usd": "unknown",
  "publication": "blocked: publication_unavailable; transport not provisioned",
  "stop": {"status": "stop_requested", "drained": true}
}
```

This is evidence of one bounded orchestration run in disposable resources. It
does not prove production readiness, independent review quality, cost
enforcement for Codex, or permission to publish, push, merge, or deploy.
