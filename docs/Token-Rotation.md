# Token Rotation Runbook (v4.5)

> **Scope.** Two separate playbooks: one for a **per-user
> bootstrap-token leak** (limited blast radius — one operator's
> minted token got out), and one for an **admin / shared
> legacy-token leak** (cluster-wide secret out — everyone's joining
> token must be rotated). Pairs with
> [`docs/Deploy-M2.md`](Deploy-M2.md) and
> [`docs/Cert-Renewal.md`](Cert-Renewal.md).

The two leaks have very different operational shapes, so they get
two different runbooks. **Do not mix them up** — running the admin
playbook for a per-user leak is overkill (and will boot every other
operator's workers); running the per-user playbook for an admin
leak leaves the cluster wide open until you escalate.

---

## Contents

1. [Token taxonomy — what can leak, and how bad](#token-taxonomy--what-can-leak-and-how-bad)
2. [Playbook A — per-user bootstrap-token leak](#playbook-a--per-user-bootstrap-token-leak)
3. [Playbook B — admin / shared legacy-token leak](#playbook-b--admin--shared-legacy-token-leak)
4. [Per-worker bearer leak (single worker)](#per-worker-bearer-leak-single-worker)
5. [Forensic checklist (post-rotation)](#forensic-checklist-post-rotation)
6. [Reference: `whilly admin` commands used by these playbooks](#reference-whilly-admin-commands-used-by-these-playbooks)

---

## Token taxonomy — what can leak, and how bad

| Token | Where it lives | Who has it | Blast radius if leaked | Playbook |
|---|---|---|---|---|
| **Per-operator bootstrap token** (`bootstrap_tokens` row, `is_admin=false`) | Postgres `bootstrap_tokens` table; plaintext shown once at mint, then forgotten by the server | The single operator it was minted for (e.g. `alice@example.com`) | The leaker can register *new* workers under that operator's identity until you revoke. Existing workers are untouched. | [Playbook A](#playbook-a--per-user-bootstrap-token-leak) |
| **Per-worker bearer** (`workers.token_hash`) | Postgres + worker's OS keychain | One specific worker process | The leaker can act as that worker until you revoke. State-mutating routes are gated by `_require_token_owner` so the bearer cannot impersonate a *different* `worker_id`. | [Per-worker bearer leak](#per-worker-bearer-leak-single-worker) |
| **Admin bootstrap token** (`bootstrap_tokens` row, `is_admin=true`) | Same table, `is_admin=true` | The cluster operator(s) | The leaker can mint and revoke other operators' tokens, evict workers, and impersonate the admin role across `/api/v1/admin/*`. **No worker bearers** are derivable from this. | [Playbook B](#playbook-b--admin--shared-legacy-token-leak) |
| **Legacy shared `WHILLY_WORKER_BOOTSTRAP_TOKEN` env var** | The control-plane host's environment | Anyone with shell on the control-plane | The leaker can register *any number of* workers (no per-operator attribution). One-minor-version compat fallback only. | [Playbook B](#playbook-b--admin--shared-legacy-token-leak) — same drill as admin leak. |

---

## Playbook A — per-user bootstrap-token leak

**Trigger:** Alice messages you in Slack, "I pasted my bootstrap
token in the wrong channel — can you nuke it?"

**Blast radius:** Limited. The leaker can register *new* workers
that look like Alice's (and so will be allowed to claim tasks) until
you revoke. Already-registered workers (Alice's or anyone else's)
are not affected.

**SLA target:** Inside 5 minutes.

### Steps (Playbook A)

#### 1. Identify the token to revoke

```bash
# WHILLY_DATABASE_URL must point at the control-plane Postgres.

whilly admin bootstrap list
# TOKEN_HASH    OWNER                CREATED_AT  EXPIRES_AT  ADMIN
# 9f3c0a4e1b2d  alice@example.com    2026-04-…   2026-06-…   no
# 7b2e1f9a0c8d  bob@example.com      2026-05-…   <never>     no
# 5d6a7e3f4b1c  ops@example.com      2026-03-…   <never>     yes
```

The first column is the truncated `token_hash`. The plaintext bearer
is **not** in the table — Alice is the only person who can confirm
which row is hers (by comparing the prefix to what she still has).

#### 2. Revoke it

```bash
whilly admin bootstrap revoke 9f3c0a4e1b2d
# revoked: true
# token_hash: 9f3c0a4e1b2d…   (full hash echoed)
# owner: alice@example.com
# is_admin: false
```

`bootstrap revoke` requires a minimum of **8 hex characters** and
must uniquely match exactly **one ACTIVE** token; ambiguous and
missing prefixes both exit non-zero with a clear stderr line.
Already-revoked rows do not match.

#### 3. Mint a fresh token for Alice

```bash
whilly admin bootstrap mint --owner alice@example.com --expires-in 30d
# token: <new plaintext>     ← capture once, hand to Alice securely
# owner: alice@example.com
# token_hash: a1b2c3d4e5f6…
# is_admin: false
# expires_at: 2026-06-02T...
```

Hand the new plaintext to Alice via your usual secret channel (1Password,
encrypted DM, signed PR, …) — **not** the same channel the leak
happened in.

#### 4. Confirm Alice's existing workers are unaffected

Per-worker bearers are independent of the bootstrap token they were
minted with. Alice's already-registered workers continue to claim,
heartbeat, complete, and fail tasks against their own per-worker
bearers (in the OS keychain) — no restart needed.

```bash
psql "$WHILLY_DATABASE_URL" -c \
    "SELECT worker_id, owner_email, last_heartbeat
     FROM workers WHERE owner_email='alice@example.com';"
```

You should see the same set of `worker_id`s with recent
`last_heartbeat` values.

#### 5. Audit-log scan for impostor registrations

```bash
psql "$WHILLY_DATABASE_URL" -c \
    "SELECT event_type, payload, created_at
     FROM events
     WHERE event_type='WORKER_REGISTERED'
       AND payload->>'owner_email'='alice@example.com'
       AND created_at >= now() - interval '24 hours'
     ORDER BY created_at DESC;"
```

Cross-reference the rows with Alice's known `hostname` set. Any
unexpected hostname is an impostor registration that happened *before*
your revoke landed — `whilly admin worker revoke <id>` it (see
[Per-worker bearer leak](#per-worker-bearer-leak-single-worker)).

#### 6. Update your operator runbook so this is in muscle memory

Append a one-line note to your team's incident log:
`<date>: alice@example.com bootstrap token revoked + reissued. No
impostor registrations observed.`

> ⚠️ **Do NOT skip step 5.** The window between leak and revoke is
> usually short, but the cost of an undetected impostor registration
> is high (silent task corruption). Always do the audit-log scan.

---

## Playbook B — admin / shared legacy-token leak

**Trigger:** The shared `WHILLY_WORKER_BOOTSTRAP_TOKEN` value showed
up in a public log dump, OR an admin-scoped `bootstrap_tokens` row's
plaintext got loose (e.g. it was pasted in a screenshot).

**Blast radius:** Full cluster.

* Legacy shared token leak → anyone can register *new* workers
  (they will not have `owner_email` because the legacy path cannot
  attribute) until you rotate.
* Admin bootstrap-token leak → the leaker can mint and revoke other
  operators' tokens via `/api/v1/admin/*`, including elevating their
  own. They can also evict any worker.

Either way: **rotate hard, rotate now**.

**SLA target:** Inside 30 minutes (longer than Playbook A because
this one disrupts other operators).

### Steps (Playbook B)

#### 1. Stop the bleeding — disable the leaked secret first

**If the legacy shared token leaked:**

```bash
# On the control-plane host:
ssh root@$CONTROL_PLANE_HOST
unset WHILLY_WORKER_BOOTSTRAP_TOKEN
# OR comment it out in /root/whilly/.env
sed -i 's/^WHILLY_WORKER_BOOTSTRAP_TOKEN=/#&/' /root/whilly/.env

# Restart the control-plane to pick up the absence:
docker compose -f docker-compose.control-plane.yml restart control-plane
curl -fsS http://127.0.0.1:8000/health
```

Setting the env var to empty disables the legacy fallback; the
DB-backed `bootstrap_tokens` path remains, so operators with
per-operator tokens are unaffected.

**If an admin bootstrap-token leaked:**

```bash
# Find the leaked admin row:
whilly admin bootstrap list
# Note the token_hash prefix of the leaked admin row.

# Revoke it:
whilly admin bootstrap revoke <leaked-prefix>
```

> If you don't know the exact token prefix (e.g. you only have the
> plaintext from the screenshot), `sha256sum <<<"<plaintext>"`
> reproduces the hash; the first 12 hex chars are the prefix.

#### 2. Mint a fresh admin token for yourself

```bash
whilly admin bootstrap mint --owner ops@example.com --admin --expires-in 90d
# token: <new admin plaintext>     ← capture once
# is_admin: true
```

Without an active admin token you cannot rotate other operators'
tokens — do this **before** the next step.

#### 3. Mass-revoke all per-operator bootstrap tokens

If you have any reason to believe the admin token was used to mint
new bootstrap rows under attacker control, mass-revoke all
non-yours tokens and have each legitimate operator re-mint:

```bash
whilly admin bootstrap list --include-revoked > /tmp/before.txt

# Revoke each non-admin row (skip your own fresh admin token):
whilly admin bootstrap list \
    | tail -n +3 \
    | awk '$5=="no"{print $1}' \
    | while read prefix; do
        whilly admin bootstrap revoke "$prefix" || true
      done

whilly admin bootstrap list --include-revoked > /tmp/after.txt
diff /tmp/before.txt /tmp/after.txt
```

Then page each operator (alice, bob, …) to re-run the per-user
playbook (Playbook A, steps 3-4) for themselves — the new admin
mints them fresh tokens.

> If you only need to evict suspicious *workers* (not all
> operators), skip the mass-revoke and use `whilly admin worker
> revoke <id>` per worker — see the next section.

#### 4. Evict suspicious workers

Cross-reference `workers.last_heartbeat` and `workers.host_url`
against your trusted set. Anything unexpected:

```bash
whilly admin worker revoke w-IMPOSTOR1
# revoked: true
# worker_id: w-IMPOSTOR1
# released_tasks: 2
```

Each revoked worker emits one `RELEASE` audit event per in-flight
task with `payload.reason='admin_revoked'` so you can grep the
events table later.

#### 5. (If legacy token leaked) Mint a new shared token for the back-compat consumers that still need it

Some legacy automation may still want a shared bootstrap token
because they predate the per-operator flow. Generate a fresh value
and set it in only the place that needs it:

```bash
NEW_LEGACY=$(openssl rand -hex 32)
echo "WHILLY_WORKER_BOOTSTRAP_TOKEN=$NEW_LEGACY" >> /root/whilly/.env
docker compose -f docker-compose.control-plane.yml restart control-plane

# Push the new value to the one consumer that needs it:
ssh root@legacy-ci-host \
    "echo 'WHILLY_WORKER_BOOTSTRAP_TOKEN=$NEW_LEGACY' >> /etc/whilly-ci.env"
```

Resist the temptation to skip this and have everyone migrate to
per-operator tokens *right now* — that's the right end-state, but
wedge it in a controlled deprecation window, not in the middle of
an incident response.

#### 6. Forensic scan (mandatory)

See [Forensic checklist](#forensic-checklist-post-rotation) below.
For an admin-token leak this is **not** optional — assume the
leaker minted at least one impostor token and exfiltrated at least
one worker bearer.

---

## Per-worker bearer leak (single worker)

**Trigger:** A laptop with a `whilly-worker` keychain entry was
lost / stolen / handed to a contractor.

**Blast radius:** That one `worker_id`. The bearer is identity-bound
to its `worker_id` server-side (`_require_token_owner`), so the
leaker cannot pretend to be a different worker.

**Steps:**

```bash
whilly admin worker revoke w-XXXXXXXX
# revoked: true
# worker_id: w-XXXXXXXX
# released_tasks: 1
```

That sets `workers.token_hash=NULL` and releases any in-flight
`CLAIMED`/`IN_PROGRESS` tasks back to `PENDING` (with a `RELEASE`
event per task carrying `payload.reason='admin_revoked'`).
Subsequent RPCs from the old bearer return 401.

If the operator behind that worker still wants to participate, they
re-run `whilly worker connect <url>` (their bootstrap token is
still valid because nothing about the revoke touched the
`bootstrap_tokens` row), get a fresh `worker_id`, and the freshly
minted per-worker bearer goes into the OS keychain.

---

## Forensic checklist (post-rotation)

After **either** playbook, before you close the incident, run this
checklist:

* [ ] **Audit-log scan.** Look for `WORKER_REGISTERED` events in the
  exposure window with unexpected `payload.owner_email` /
  `payload.hostname`.

  ```bash
  psql "$WHILLY_DATABASE_URL" -c \
      "SELECT created_at, payload->>'worker_id' AS worker_id,
              payload->>'owner_email' AS owner,
              payload->>'hostname' AS hostname
       FROM events WHERE event_type='WORKER_REGISTERED'
         AND created_at >= '<exposure-start>'
       ORDER BY created_at DESC;"
  ```

* [ ] **Claim attribution scan.** For each impostor `worker_id`
  found above, check what tasks it claimed.

  ```bash
  psql "$WHILLY_DATABASE_URL" -c \
      "SELECT t.id, t.status, t.plan_id, t.claimed_at
       FROM tasks t WHERE t.claimed_by='<impostor>'
       ORDER BY t.claimed_at DESC;"
  ```

  Anything `DONE` may need a manual revert — you decide based on
  what the task did. Anything `CLAIMED` or `IN_PROGRESS` is already
  released by the revoke.

* [ ] **Remote-bearer revocation.** For each impostor `worker_id`,
  `whilly admin worker revoke <id>`. Captured one revocation per row
  in the events log.

* [ ] **Remove the leaked secret from wherever it leaked.** Slack
  channel cleanup, log redaction, GitHub Actions secret rotation,
  etc. The `whilly admin bootstrap revoke` step neutralises the
  token in the database, but the original leak vector still needs
  shutting down.

* [ ] **Per-user vs admin classification.** Note in your incident
  log which playbook (A vs B) ran, with timestamps. This helps
  pattern-spot future incidents.

* [ ] **Verify `workshop-demo.sh` still passes** after the rotation:

  ```bash
  bash workshop-demo.sh --cli stub
  ```

  The single-host demo should remain green — token rotations don't
  touch that path.

---

## Reference: `whilly admin` commands used by these playbooks

All `whilly admin` commands need `WHILLY_DATABASE_URL` pointing at
the control-plane Postgres. Output is line-oriented `key: value`
pairs by default; pass `--json` for JSON.

| Command | Purpose | Exit codes |
|---|---|---|
| `whilly admin bootstrap mint --owner <email> [--expires-in 30d] [--admin] [--json]` | Mint a fresh bootstrap token row, print plaintext **once**. | `0` ok / `1` op error / `2` env error |
| `whilly admin bootstrap revoke <prefix> [--json]` | Mark the unique active row matching `<prefix>` (≥ 8 hex) as revoked. | `0` ok / `1` op error (missing/ambiguous) / `2` env error |
| `whilly admin bootstrap list [--include-revoked] [--json]` | Tabular listing of bootstrap rows; plaintext is **never** displayed. | `0` ok / `1` op error / `2` env error |
| `whilly admin worker revoke <worker_id> [--json]` | Set `workers.token_hash=NULL`, release in-flight tasks, emit one `RELEASE` event per release with `payload.reason='admin_revoked'`. | `0` ok / `1` worker not found / `2` env error |

The `--admin` flag on `bootstrap mint` controls whether the new
token row is gated by `make_admin_auth` (`is_admin=true`). Without
`--admin`, the token can register workers but cannot hit
`/api/v1/admin/*` routes.

---

## See also

* [`docs/Deploy-M2.md`](Deploy-M2.md) — full M2 deploy walk-through.
* [`docs/Cert-Renewal.md`](Cert-Renewal.md) — TLS / cert lifecycle
  runbook for the localhost.run sidecar.
* [`docs/Distributed-Setup.md`](Distributed-Setup.md) — M1 multi-host
  foundation.
* [`whilly/cli/admin.py`](../whilly/cli/admin.py) — source of truth
  for the `whilly admin` CLI surface.
* [`whilly/adapters/transport/auth.py`](../whilly/adapters/transport/auth.py)
  — `make_admin_auth` / `make_db_bootstrap_auth` factories.
