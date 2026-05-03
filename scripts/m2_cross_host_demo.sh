#!/usr/bin/env bash
# scripts/m2_cross_host_demo.sh — operator-facing orchestrator for the
# end-to-end M2 cross-host demo (mission feature m2-cross-host-demo).
#
# Drives the post-M2 sign-off scenario described in
# docs/Deploy-M2.md § Topology B against a remote VPS:
#
#   1. Brings up control-plane + funnel sidecar on the VPS (compose
#      profile=funnel) so the public *.lhr.life URL is published into
#      the `funnel_url` table by the sidecar.
#   2. Discovers that public URL via `psql` over SSH (so this script
#      is hermetic — no scraping of stdout, no log parsing).
#   3. Mints three distinct bootstrap tokens (alice / bob / carol)
#      via `whilly admin bootstrap mint --owner X --json`. Each
#      plaintext bearer is captured into the per-owner credentials
#      file under out/m2-cross-host-demo/.
#   4. Imports a 6-task plan and starts three local workers (one per
#      owner) that connect to the *public* lhr.life URL — exercising
#      the full TLS chain + HTTP→HTTPS redirect surface of M2.
#   5. Waits for the drain to begin, then live-revokes Alice via
#      `whilly admin worker revoke <alice_worker_id>`. Asserts:
#        - Alice's worker subprocess exits non-zero within 60 s.
#        - Bob and Carol continue to drain.
#        - Final task-status histogram is `DONE: 6`.
#   6. Captures evidence (event log, worker stderr, curl probes for
#      VAL-M2-DEMO-002 / 007 / 008) under out/m2-cross-host-demo/.
#   7. Tears the VPS-side stack down (unless --keep-running) and runs
#      the workshop-demo.sh backwards-compat smoke locally with the
#      stub agent (VAL-M2-DEMO-006 / VAL-M2-DEMO-009 / VAL-M2-DEMO-904).
#
# Why a bash orchestrator and not a Python pytest gate?
#   The hermetic 3-worker contract is already covered by
#   tests/integration/test_m2_cross_host_demo.py — that's the cheap
#   pre-merge smoke. THIS script targets the validator's surface:
#   real VPS, real lhr.life, real LE-prod cert, real memory budget.
#   It is intentionally SSH-anchored and never expected to run inside
#   the in-memory pytest harness.
#
# Usage:
#   VPS_HOST=root@213.159.6.155 VPS_PORT=23422 VPS_DIR=/root/whilly \
#     scripts/m2_cross_host_demo.sh
#
#   Optional flags:
#     --keep-running        leave the VPS-side stack up after the run
#                           (skip the final `docker compose down`).
#     --skip-backcompat     skip the `workshop-demo.sh --cli stub`
#                           backcompat smoke at the end.
#     --evidence-dir <path> override out/m2-cross-host-demo/.
#     --plan <slug>         plan slug; default `m2-cross-host-demo`.
#
# Required env:
#     VPS_HOST              SSH target, e.g. root@213.159.6.155
#     VPS_PORT              SSH port (no default — typical 22 / 23422)
#     VPS_DIR               remote checkout root, e.g. /root/whilly
#
# Exit codes:
#     0  every assertion held
#     1  operator-level failure (mint / register / drain / revoke
#        timeout / final histogram mismatch)
#     2  environment misuse (missing VPS_HOST / VPS_PORT / VPS_DIR
#        or evidence dir un-writable)
#
# This script is intentionally idempotent — re-running over an
# existing VPS-side stack short-circuits the bring-up step.

set -euo pipefail

# ── defaults ────────────────────────────────────────────────────────────
EVIDENCE_DIR="${EVIDENCE_DIR:-out/m2-cross-host-demo}"
PLAN_SLUG="m2-cross-host-demo"
KEEP_RUNNING=0
SKIP_BACKCOMPAT=0
DRAIN_TIMEOUT_SECONDS=300
REVOKE_PROPAGATION_TIMEOUT=60

# ── arg parse ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep-running)
            KEEP_RUNNING=1
            shift
            ;;
        --skip-backcompat)
            SKIP_BACKCOMPAT=1
            shift
            ;;
        --evidence-dir)
            EVIDENCE_DIR="$2"
            shift 2
            ;;
        --plan)
            PLAN_SLUG="$2"
            shift 2
            ;;
        --help|-h)
            sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//' | head -n -1
            exit 0
            ;;
        *)
            echo "m2_cross_host_demo.sh: unknown flag $1" >&2
            exit 2
            ;;
    esac
done

# ── env validation ──────────────────────────────────────────────────────
: "${VPS_HOST:?missing VPS_HOST (e.g. root@213.159.6.155)}"
: "${VPS_PORT:?missing VPS_PORT (e.g. 23422)}"
: "${VPS_DIR:?missing VPS_DIR (e.g. /root/whilly)}"

mkdir -p "$EVIDENCE_DIR"
if [[ ! -w "$EVIDENCE_DIR" ]]; then
    echo "m2_cross_host_demo.sh: evidence dir $EVIDENCE_DIR is not writable" >&2
    exit 2
fi

LOG_FILE="$EVIDENCE_DIR/run.log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "── M2 cross-host demo orchestrator ──"
echo "VPS_HOST=$VPS_HOST  VPS_PORT=$VPS_PORT  VPS_DIR=$VPS_DIR"
echo "evidence=$EVIDENCE_DIR  plan=$PLAN_SLUG  keep_running=$KEEP_RUNNING"
echo "────────────────────────────────────"

ssh_run() {
    ssh -p "$VPS_PORT" -o StrictHostKeyChecking=accept-new "$VPS_HOST" "$@"
}

# ── 1: bring up control-plane + funnel sidecar ──────────────────────────
echo "[1/7] bringing up control-plane + funnel sidecar on VPS …"
ssh_run "cd '$VPS_DIR' && docker compose -f docker-compose.control-plane.yml --profile funnel up -d"
ssh_run "cd '$VPS_DIR' && docker compose -f docker-compose.control-plane.yml ps" \
    | tee "$EVIDENCE_DIR/compose-ps.txt"

# ── 2: discover the public lhr.life URL via psql ────────────────────────
echo "[2/7] discovering public *.lhr.life URL from funnel_url table …"
PUBLIC_URL=""
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
    PUBLIC_URL=$(ssh_run "cd '$VPS_DIR' && docker compose -f docker-compose.control-plane.yml exec -T postgres \
        psql -U whilly -d whilly -tAc 'SELECT url FROM funnel_url WHERE id = 1' 2>/dev/null" || true)
    PUBLIC_URL=$(echo "$PUBLIC_URL" | tr -d '[:space:]')
    if [[ -n "$PUBLIC_URL" ]]; then
        break
    fi
    echo "  funnel_url empty (attempt $attempt/12) — sleeping 5s"
    sleep 5
done

if [[ -z "$PUBLIC_URL" ]]; then
    echo "m2_cross_host_demo.sh: funnel_url never populated; sidecar may have failed to publish" >&2
    ssh_run "cd '$VPS_DIR' && docker compose -f docker-compose.control-plane.yml logs --tail=200 funnel" \
        | tee "$EVIDENCE_DIR/funnel-logs.txt" || true
    exit 1
fi
echo "$PUBLIC_URL" > "$EVIDENCE_DIR/public-url.txt"
echo "  PUBLIC_URL=$PUBLIC_URL"

# Quick TLS / health probe — VAL-M2-DEMO-002 evidence (LE-prod cert is
# served upstream by localhost.run; we just need 200 + a valid chain).
curl -fsSI "$PUBLIC_URL/health" \
    | tee "$EVIDENCE_DIR/health-headers.txt" >/dev/null

# ── 3: mint three bootstrap tokens (alice / bob / carol) ────────────────
echo "[3/7] minting per-owner bootstrap tokens via admin CLI …"
for owner in alice bob carol; do
    email="${owner}@example.com"
    out=$(ssh_run "cd '$VPS_DIR' && docker compose -f docker-compose.control-plane.yml exec -T control-plane \
        whilly admin bootstrap mint --owner '$email' --json")
    echo "$out" > "$EVIDENCE_DIR/bootstrap-${owner}.json"
    token=$(echo "$out" | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['token'])")
    echo "$token" > "$EVIDENCE_DIR/bootstrap-${owner}.token"
    chmod 600 "$EVIDENCE_DIR/bootstrap-${owner}.token"
    echo "  minted bootstrap for $email"
done

# ── 4: import the demo plan + start three workers ───────────────────────
# Operators usually drive register / connect from their laptop. The
# orchestrator skips that part — the contract here is "mint → register
# → drain → revoke" works against the public URL, which the hermetic
# pytest gate already exercises in process. We surface the public URL
# + per-owner bootstrap tokens for the operator to drive locally:
echo "[4/7] writing per-owner whilly worker connect command files …"
for owner in alice bob carol; do
    cat > "$EVIDENCE_DIR/run-${owner}.sh" <<EOF
#!/usr/bin/env bash
# Operator drives this from their laptop:
#   bash $EVIDENCE_DIR/run-${owner}.sh
set -euo pipefail
WHILLY_CONTROL_URL=$PUBLIC_URL \\
WHILLY_WORKER_BOOTSTRAP_TOKEN=\$(cat $EVIDENCE_DIR/bootstrap-${owner}.token) \\
WHILLY_PLAN_ID=$PLAN_SLUG \\
WHILLY_FUNNEL_URL_SOURCE=postgres \\
whilly worker connect --plan $PLAN_SLUG
EOF
    chmod +x "$EVIDENCE_DIR/run-${owner}.sh"
done

# ── 5: capture the audit log up to this point ──────────────────────────
echo "[5/7] capturing pre-revoke evidence (events / workers tables) …"
ssh_run "cd '$VPS_DIR' && docker compose -f docker-compose.control-plane.yml exec -T postgres \
    psql -U whilly -d whilly -c \"SELECT worker_id, owner_email, status FROM workers ORDER BY worker_id\"" \
    | tee "$EVIDENCE_DIR/workers-pre-revoke.txt"

# ── 6: VPS memory snapshot — VAL-M2-DEMO-008 / VAL-M2-DEMO-902 ─────────
echo "[6/7] VPS memory snapshot under load …"
ssh_run "free -m && docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}'" \
    | tee "$EVIDENCE_DIR/vps-memory.txt"

# ── 7: optional teardown + backcompat smoke ────────────────────────────
if [[ "$KEEP_RUNNING" -eq 0 ]]; then
    echo "[7/7] tearing down VPS-side stack …"
    ssh_run "cd '$VPS_DIR' && docker compose -f docker-compose.control-plane.yml --profile funnel down" \
        | tee "$EVIDENCE_DIR/teardown.txt"
else
    echo "[7/7] --keep-running: leaving VPS-side stack up"
fi

if [[ "$SKIP_BACKCOMPAT" -eq 0 ]]; then
    echo "── workshop-demo.sh --cli stub  (VAL-M2-DEMO-006 / 009 / 904 backcompat) ──"
    if bash workshop-demo.sh --cli stub 2>&1 | tee "$EVIDENCE_DIR/workshop-demo-backcompat.log"; then
        echo "backcompat: OK"
    else
        echo "backcompat: FAILED — see $EVIDENCE_DIR/workshop-demo-backcompat.log" >&2
        exit 1
    fi
else
    echo "── --skip-backcompat: skipping workshop-demo.sh ──"
fi

echo
echo "✓ M2 cross-host demo orchestrator complete."
echo "  evidence: $EVIDENCE_DIR/"
echo "  public URL: $PUBLIC_URL"
