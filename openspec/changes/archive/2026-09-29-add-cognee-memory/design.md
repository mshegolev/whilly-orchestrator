# Design

The full adapter design is recorded in
`docs/superpowers/specs/2026-09-29-cognee-adapter-design.md` and activation evidence
in `docs/Cognee-Memory.md`. PostgreSQL is canonical; offline Cognee only ranks
host-generated IDs in per-request authorized ephemeral indexes. A process-wide
launch fence serializes actual spawn with redaction, inherited locks retain
ownership after parent crashes, and cleanup preserves manifests on failure.

`build_context` accepts optional omission IDs and constructs a `ContextPackage`.
It repeatedly removes deterministic metadata/items until the serialized JSON is
within the requested character budget; large metadata collections are compacted
to count markers before the loop to keep the operation cheap and terminating.

Binding validation operates on one store snapshot. It rejects expired,
conflicted, retracted/superseded/stale, changed, or unavailable-source records.
`bound_worker_prompt` validates and renders from that same snapshot, preventing
a second read from bypassing the check.
