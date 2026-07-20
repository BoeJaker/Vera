#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
#  Garage single-node bootstrap for the Vera data-fabric object store.
#
#  Runs once (one-shot sidecar) after the garage daemon is healthy. Drives the
#  garage ADMIN API (:3903) with plain curl — no docker.sock, no `docker exec`.
#  The previous docker-exec approach needed the host docker socket mounted into
#  the init container, which silently fails on some hosts (Docker Desktop /
#  hardened daemons) and left the store with NO key and NO bucket — the
#  orchestrator then saw AccessDenied ("No such key: …") on every S3 call.
#
#  Admin API payloads used are the v1 shapes (garage ≥ 1.0):
#    GET  /v1/status                       → {"node": "<id>", ...}
#    GET  /v1/layout                       → {"version": N, "roles": [...]}
#    POST /v1/layout                       ← [{"id","zone","capacity","tags"}]
#    POST /v1/layout/apply                 ← {"version": N+1}
#    GET  /v1/key?id=<access-key>          → 200 if present
#    POST /v1/key/import                   ← {"accessKeyId","secretAccessKey","name"}
#    GET  /v1/bucket?globalAlias=<name>    → 200 + {"id": ...} if present
#    POST /v1/bucket                       ← {"globalAlias": <name>}
#    POST /v1/bucket/allow                 ← {"bucketId","accessKeyId","permissions"}
#
#  Steps (all idempotent — safe to re-run):
#    1. assign + apply a single-node cluster layout (zone dc1)
#    2. import the deterministic S3 access key vera expects (stable creds)
#    3. create the data-fabric bucket and grant the key read/write/owner
#
#  Env (see docker-compose.yml):
#    GARAGE_ADMIN_URL    default http://garage:3903
#    GARAGE_ADMIN_TOKEN  must match [admin].admin_token in garage.toml
#    GARAGE_CAPACITY     layout capacity in bytes (default 1G)
#    S3_ACCESS_KEY / S3_SECRET_KEY / S3_BUCKET
#
#  If bootstrap fails, see:  docker compose logs garage-init
# ─────────────────────────────────────────────────────────────────────────────
set -eu

ADMIN="${GARAGE_ADMIN_URL:-http://garage:3903}"
TOKEN="${GARAGE_ADMIN_TOKEN:?GARAGE_ADMIN_TOKEN is required (matches garage.toml admin_token)}"
BUCKET="${S3_BUCKET}"
ACCESS="${S3_ACCESS_KEY}"
SECRET="${S3_SECRET_KEY}"  # pragma: allowlist secret
CAPACITY="${GARAGE_CAPACITY:-1000000000}"

say() { echo ">> garage-init: $*"; }

# api METHOD PATH [JSON_BODY]  → response body on stdout (fails on HTTP >= 400)
api() {
  _m="$1"; _p="$2"; _b="${3:-}"
  if [ -n "$_b" ]; then
    curl -fsS -m 20 -X "$_m" -H "Authorization: Bearer $TOKEN" \
         -H "Content-Type: application/json" -d "$_b" "$ADMIN$_p"
  else
    curl -fsS -m 20 -X "$_m" -H "Authorization: Bearer $TOKEN" "$ADMIN$_p"
  fi
}

# json_field KEY  ← reads stdin, extracts the first "KEY":"value" or "KEY":number
json_str()  { sed -n 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1; }
json_num()  { sed -n 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -n1; }

# ── wait for the admin API to answer ─────────────────────────────────────────
say "waiting for garage admin API at $ADMIN ..."
i=0
until STATUS=$(api GET /v1/status 2>/dev/null); do
  i=$((i+1)); [ "$i" -gt 60 ] && { say "FATAL: admin API not reachable"; exit 1; }
  sleep 2
done

NODE=$(echo "$STATUS" | json_str node)
[ -n "$NODE" ] || { say "FATAL: could not parse node id from /v1/status"; exit 1; }
say "node id: $NODE"

# ── 1. cluster layout (assign + apply only if this node has no role yet) ─────
LAYOUT=$(api GET /v1/layout)
if echo "$LAYOUT" | grep -q "\"id\"[[:space:]]*:[[:space:]]*\"$NODE\""; then
  say "layout already assigned — skipping"
else
  say "staging layout (zone dc1, capacity ${CAPACITY}B)"
  api POST /v1/layout \
    "[{\"id\":\"$NODE\",\"zone\":\"dc1\",\"capacity\":$CAPACITY,\"tags\":[\"vera\"]}]" >/dev/null
  VER=$(echo "$LAYOUT" | json_num version); VER="${VER:-0}"
  say "applying layout version $((VER+1))"
  api POST /v1/layout/apply "{\"version\":$((VER+1))}" >/dev/null
fi

# ── 2. deterministic S3 access key (idempotent import) ───────────────────────
if api GET "/v1/key?id=$ACCESS" >/dev/null 2>&1; then
  say "key already present — skipping import"
else
  say "importing access key $ACCESS"
  api POST /v1/key/import \
    "{\"accessKeyId\":\"$ACCESS\",\"secretAccessKey\":\"$SECRET\",\"name\":\"vera-fabric\"}" >/dev/null
fi

# ── 3. bucket + grant ─────────────────────────────────────────────────────────
if BINFO=$(api GET "/v1/bucket?globalAlias=$BUCKET" 2>/dev/null); then
  say "bucket $BUCKET already exists"
else
  say "creating bucket $BUCKET"
  BINFO=$(api POST /v1/bucket "{\"globalAlias\":\"$BUCKET\"}")
fi
BID=$(echo "$BINFO" | json_str id)
[ -n "$BID" ] || { say "FATAL: could not resolve bucket id for $BUCKET"; exit 1; }

say "granting key read/write/owner on $BUCKET ($BID)"
api POST /v1/bucket/allow \
  "{\"bucketId\":\"$BID\",\"accessKeyId\":\"$ACCESS\",\"permissions\":{\"read\":true,\"write\":true,\"owner\":true}}" >/dev/null

say "✓ bootstrap complete — bucket=$BUCKET key=$ACCESS"
