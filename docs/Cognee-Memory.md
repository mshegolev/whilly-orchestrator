# Local Cognee memory retrieval

This adapter is an optional retrieval layer, not an autonomous knowledge authority.
PostgreSQL remains the canonical store. Manual submissions remain candidates;
connecting Cognee does not verify them, import repositories, or start research jobs.

## Intended configuration

Keep installation-specific paths and credentials outside the repository:

```text
WHILLY_SWARM_MEMORY=1
WHILLY_MEMORY_BACKEND=cognee
WHILLY_COGNEE_PYTHON=/private-install/cognee-venv/bin/python
WHILLY_COGNEE_MODELS_DIR=/private-install/cognee-models
WHILLY_COGNEE_STATE_DIR=/private-install/cognee-requests
```

The default backend is `l1`. Cognee dependencies belong in a separate Python
environment, not Whilly's main venv. Models must be provisioned separately:
the model root contains `hub/` for the GLiNER Hugging Face cache and `fastembed/`
for the local embedding cache. Worker operation must not download models.

The first platform adapter targets macOS `sandbox-exec`. Other platforms must
report unavailable until an equivalent enforced sandbox has been implemented
and tested; environment filtering alone is not a sandbox.

## Safety contract

- Authorize product, project and classification before sending any body to the
  worker. Only eligible verified canonical revisions may enter an index.
- Build a private temporary index for each request. Read models/runtime only;
  deny networking and unrelated filesystem reads at the OS boundary.
- Bound the whole JSON input to 128 KiB, 32 records and a 4096-character query.
  Allow one active index and four queued requests per local installation.
- Apply a 90-second operation deadline. Cleanup must not report success until
  the child has exited and accessible temporary copies have been removed.
- Accept only ranked IDs from the child, then reread and revalidate canonical
  revisions. Backend text is never a source of response bodies or permissions.
- Bind selected revisions to approval. Worker prompts contain only approved
  knowledge for that project and declared dependencies, labelled as untrusted
  evidence rather than instructions.

## Status and failure

The administrator knowledge panel distinguishes an empty canonical registry
from backend unavailability. Backend status must not disclose queries, bodies,
source paths or secrets. Selecting Cognee and encountering an error blocks
dependent planning; it must not silently return an empty successful result or
fall back to another backend.

Redaction removes the canonical payload from new retrieval immediately. It
also cancels affected indexing requests. `purge_pending` indicates that removal
of temporary copies has not yet been confirmed. This is not a guarantee of
physical SSD erasure, deletion of OS backups, or recall of context already sent
to a model. Do not manually delete the lease root to bypass pending cleanup.

## Acceptance and rollback

Before local activation, run protocol/lifecycle tests, cross-process queue and
redaction tests, real PostgreSQL tests, and real Cognee retrieval with child-side
negative network/file probes. Fake Claude/Codex processes prove prompt transport,
not successful real model sessions. Record real CLI acceptance separately;
do not trigger external model calls merely to turn a test green.

Confirm all swarm sessions are idle, back up private configuration and the
local database, apply the reviewed migration, configure the adapter, then restart
and verify health, authenticated backend status, empty state and a synthetic
canary in a separate scope. Do not seed real project knowledge as a side effect.

For rollback, select `WHILLY_MEMORY_BACKEND=l1` and restart during an idle window.
Already approved plans still validate canonical bindings, regardless of backend
selection. Preserve pending cleanup artifacts for recovery; do not downgrade or
delete canonical database tables as a routine backend rollback.

## Implementation evidence

Local macOS activation was verified on 2026-09-29 after a private configuration
backup and a validated PostgreSQL archive, idle-session checks, migration 036,
and restart of the existing service. The implementation plan is
`docs/superpowers/plans/2026-09-29-cognee-adapter.md`.

Verified evidence:

- 140 focused Python tests, including real offline Cognee ingestion/recall,
  negative sandbox probes, process-group cleanup, cross-process leases,
  source/ACL revalidation, approval bindings and both fake-child transports.
- Two JavaScript tests and three import-layer contracts passed. Disposable
  PostgreSQL memory/migration checks passed (seven tests plus one roundtrip).
- A synthetic isolated product scope exercised real PostgreSQL, a temporary
  Git source, offline retrieval, canonical body return, redaction and cleanup.
  Its rows were removed; real product knowledge was not seeded.
- Authenticated browser checks after restart confirmed `cognee: ready`, empty
  canonical context, anonymous denial, desktop/mobile layout, and no page errors.

These are scoped acceptance results, not a whole-repository green claim. A
broader run encountered legacy migration tests hardcoding older head revisions;
the full suite was not completed. Real paid Claude/Codex model sessions were not
run: local fake children verify actual transport paths, not model quality.

The sandbox assumes controller-owned commands, runtime/model paths and private
lease directories. It is not a general-purpose runner for arbitrary hostile
executables or an OS-wide IPC isolation guarantee. Structural readiness must
remain paired with the real offline SDK probe when dependencies change.

No daily schedules, bulk session ingestion, global Claude/Codex configuration,
MCP service or external LLM calls are enabled by this adapter.
