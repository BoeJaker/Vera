#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
#  Garage single-node bootstrap for the Vera data-fabric object store.
#
#  Runs once (one-shot sidecar) after the garage daemon is healthy:
#    1. assigns + applies a cluster layout for the single node
#    2. imports a deterministic S3 access key (so vera's creds stay stable)
#    3. creates the data-fabric bucket and grants the key read/write/owner
#
#  Everything goes through Garage's admin API (v1) over HTTP — the distroless
#  garage image has no shell, so we drive it from this alpine sidecar instead
#  of the `garage` CLI. If bootstrap fails, see: docker compose logs garage-init
# ─────────────────────────────────────────────────────────────────────────────
set -eu

ADMIN="http://garage:3903/v1"
AUTH="Authorization: Bearer ${GARAGE_ADMIN_TOKEN}"
BUCKET="${S3_BUCKET}"
ACCESS="${S3_ACCESS_KEY}"
SECRET="${S3_SECRET_KEY}"  # pragma: allowlist secret

say() { echo ">> garage-init: $*"; }

# ── wait for the admin API to answer ────────────────────────────────────────
say "waiting for garage admin API ..."
until curl -fsS -H "$AUTH" "$ADMIN/status" >/dev/null 2>&1; do
  sleep 2
done

# ── 1. cluster layout ───────────────────────────────────────────────────────
NODE=$(curl -fsS -H "$AUTH" "$ADMIN/status" | jq -r '.node')
say "node id: $NODE"

ASSIGNED=$(curl -fsS -H "$AUTH" "$ADMIN/layout" | jq -r --arg n "$NODE" '.roles[]?.id | select(. == $n)')
if [ -z "$ASSIGNED" ]; then
  say "staging layout for node"
  curl -fsS -H "$AUTH" -X POST "$ADMIN/layout" \
    -d "{\"$NODE\":{\"zone\":\"dc1\",\"capacity\":1000000000,\"tags\":[]}}" >/dev/null
  VER=$(curl -fsS -H "$AUTH" "$ADMIN/layout" | jq -r '.version')
  NEXT=$((VER + 1))
  say "applying layout version $NEXT"
  curl -fsS -H "$AUTH" -X POST "$ADMIN/layout/apply" -d "{\"version\":$NEXT}" >/dev/null
else
  say "layout already assigned — skipping"
fi

# ── 2. deterministic S3 access key (idempotent import) ──────────────────────
if curl -fsS -H "$AUTH" "$ADMIN/key?id=$ACCESS" >/dev/null 2>&1; then
  say "key already present — skipping import"
else
  say "importing access key $ACCESS"
  curl -fsS -H "$AUTH" -X POST "$ADMIN/key/import" \
    -d "{\"accessKeyId\":\"$ACCESS\",\"secretAccessKey\":\"$SECRET\",\"name\":\"vera-fabric\"}" >/dev/null
fi

# ── 3. bucket + grant ───────────────────────────────────────────────────────
BID=$(curl -fsS -H "$AUTH" "$ADMIN/bucket?globalAlias=$BUCKET" 2>/dev/null | jq -r '.id // empty')
if [ -z "$BID" ]; then
  say "creating bucket $BUCKET"
  BID=$(curl -fsS -H "$AUTH" -X POST "$ADMIN/bucket" -d "{\"globalAlias\":\"$BUCKET\"}" | jq -r '.id')
else
  say "bucket $BUCKET already exists ($BID)"
fi

say "granting key read/write/owner on $BUCKET"
curl -fsS -H "$AUTH" -X POST "$ADMIN/bucket/allow" \
  -d "{\"bucketId\":\"$BID\",\"accessKeyId\":\"$ACCESS\",\"permissions\":{\"read\":true,\"write\":true,\"owner\":true}}" >/dev/null

say "✓ bootstrap complete — bucket=$BUCKET key=$ACCESS"
