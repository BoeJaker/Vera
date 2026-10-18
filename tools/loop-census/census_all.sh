#!/bin/sh
# census_all.sh - restart prod (optional), then run EVERY census template with
# the standalone runner (run_census.py), strictly one after another, archiving
# each finished run under its own name so the series stay separate.
#
# by claude, session c4cceff0-829e-4853-b114-5abce77afbad, 2026-09-10.
# Detached with nohup: the prod restart it issues kills the MCP call that
# launched it, and thirteen censuses are the better part of a day - nothing
# that lives inside a client connection can drive this.
#
# usage:  nohup sh census_all.sh restart > /dev/null 2>&1 &
#         nohup sh census_all.sh         > /dev/null 2>&1 &     (no restart)
#         nohup sh census_all.sh resume  > /dev/null 2>&1 &     (no restart; the
#             first template RESUMES from the rows already in census.jsonl -
#             run_census.py skips goals it has recorded - instead of refusing)
#
# Why run_census.py and not the seeded suite (evolve.suite.start): the suite's
# loop tasks run inside the PRIMARY dev sandbox and are BLOCKED when none is up
# (sandbox_mode=require) - the only seeded census-default run on record
# (2026-09-08) failed all 12 tasks in 0.1s that way. run_census.py drives prod's
# own loop, waits for a free box before every goal, records the applied wall cap
# and provenance on every row, and is the instrument every archived run came from.
#
# Archive naming (the only baseline there is depends on it):
#   default template      -> census.run<N>.jsonl / .log      N = next in the series
#   any other template    -> census.<template>-run<N>.jsonl / .log
#   fewer rows than goals -> the name carries -partial (census.runs hides it)
#   runner exit != 0      -> the name carries -failed-exit<code>
# run_census.py reserves N at start (census.active.json `census_run`) so its rows
# and the run records it posts to Vera name the archive before it exists; archive()
# uses that reservation when it is this template's. Every archived run is then
# posted whole to Vera's task system (ingest_census.py -> evolve.result.ingest):
# a census run is a driver run beside the suites, and the rows stay the baseline.

# Who drives it (DEEP-21 D21-14): the scheduler passes CENSUS_OPERATOR=vera-scheduler,
# VERA_CENSUS_TOOL=sched and CENSUS_SESSION_ID=sched:<schedule id>; a hand launch keeps the old values.
SID="${CENSUS_SESSION_ID:-c4cceff0-829e-4853-b114-5abce77afbad}"
OPERATOR="${CENSUS_OPERATOR:-claude}"
TOOL="${VERA_CENSUS_TOOL:-census_all.sh}"
BASE="${CENSUS_BASE:-https://localhost:8999}"
CODE="$(cd "$(dirname "$0")" && pwd)"            # this harness's code (the repo)
# Its DATA - archives, logs, state, templates - is not the code folder:
HERE="${VERA_CENSUS_DIR:-$HOME/loop-census}"
LOG="$HERE/census_all.log"
STATE="$HERE/census_all.state"
PY="${PY:-python3}"
# CENSUS_TEMPLATES overrides the list (e.g. to resume a set part-way through).
TEMPLATES="${CENSUS_TEMPLATES:-default exec-family code-family prose-family data-family research-family model-compare operator-family business-family markets-family infra-family mesh-family specialist-loops}"

log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }
mcp() { curl -sk --max-time 120 -X POST "$BASE/mcp/call" -H 'Content-Type: application/json' -d "$1"; }
health() { curl -sk --max-time 10 -o /dev/null -w '%{http_code}' "$BASE/health"; }
loops_running() {
  curl -sk --max-time 20 "$BASE/workshop/agent_loop/sessions?status=running" \
    | "$PY" -c 'import sys,json; print(json.load(sys.stdin).get("count", 0))' 2>/dev/null || echo unknown
}
goals_in() {  # number of goals a template declares
  "$PY" -c 'import json,sys; t=json.load(open(sys.argv[1])); g=t.get("goals", t if isinstance(t, list) else []); print(len(g))' "$HERE/templates/$1.json" 2>/dev/null || echo 0
}
next_n() {    # next run number for a series: default -> census.runN ; other -> census.<t>-runN
  "$PY" - "$HERE" "$1" <<'EOF'
import os, re, sys
here, t = sys.argv[1], sys.argv[2]
pat = re.compile(r"^census\.run(\d+)" if t == "default" else r"^census\.%s-run(\d+)" % re.escape(t))
ns = [int(m.group(1)) for f in os.listdir(here) for m in [pat.match(f)] if m]
print((max(ns) + 1) if ns else (51 if t == "default" else 1))
EOF
}
reserved_run() {   # the archive id run_census.py reserved at start, when it is this template's
  "$PY" -c 'import json,sys
try:
    a = json.load(open(sys.argv[1]))
    print(a.get("census_run", "") if a.get("template") == sys.argv[2] else "")
except Exception: print("")' "$HERE/census.active.json" "$1" 2>/dev/null || echo ""
}
control_drop() {   # 1 if census.control.json asks for a drop
  "$PY" -c 'import json,sys
try: print("1" if json.load(open(sys.argv[1])).get("drop") else "0")
except Exception: print("0")' "$HERE/census.control.json" 2>/dev/null || echo 0
}
archive() {   # archive "$1"=template "$2"=runner exit code
  t="$1"; rc="$2"
  n=$(next_n "$t")
  rows=$(wc -l < "$HERE/census.jsonl" 2>/dev/null || echo 0)
  goals=$(goals_in "$t")
  base="census.run$n"; [ "$t" = "default" ] || base="census.$t-run$n"
  reserved=$(reserved_run "$t")
  [ -n "$reserved" ] && base="census.$reserved"
  [ "$rows" -ge "$goals" ] || base="$base-partial"
  if [ "$rc" = "3" ]; then base="$base-dropped"; elif [ "$rc" != "0" ]; then base="$base-failed-exit$rc"; fi
  if [ -e "$HERE/$base.jsonl" ]; then base="$base-dup$(date -u +%H%M%S)"; fi
  mv "$HERE/census.jsonl" "$HERE/$base.jsonl" 2>/dev/null
  mv "$HERE/census.log" "$HERE/$base.log" 2>/dev/null
  log "archived $t -> $base.jsonl ($rows/$goals rows, rc=$rc)"
  # The archived run, whole, into Vera's task system. Best effort: the rows are
  # the record; a prod without the capability is told so in the log.
  if [ -s "$HERE/$base.jsonl" ]; then
    out=$("$PY" "$CODE/ingest_census.py" "$HERE/$base.jsonl" 2>&1 | tail -n 1)
    log "ingest: $out"
  fi
}

log "census_all: start mode=${1:-run} pid=$$ templates=$(echo $TEMPLATES | wc -w)"
echo "started $(date -u +%FT%TZ) pid=$$" > "$STATE"

if [ "$1" != "resume" ] && { [ -s "$HERE/census.jsonl" ] || [ -s "$HERE/census.log" ]; }; then
  log "ABORT: census.jsonl/census.log already hold data - park them first, or start with 'resume'"
  exit 1
fi
[ "$1" = "resume" ] && log "resume: first template continues from $(wc -l < "$HERE/census.jsonl" 2>/dev/null || echo 0) recorded row(s)"

if [ "$1" = "restart" ]; then
  r=$(loops_running)
  if [ "$r" != "0" ]; then log "ABORT: running loops=$r - not restarting prod under someone"; exit 1; fi
  log "restarting prod via sys.dev.restart"
  mcp "{\"name\":\"sys.dev.restart\",\"arguments\":{\"confirm\":true,\"reason\":\"census_all: fresh process before a full census set\"},\"caller_kind\":\"claude\",\"session_id\":\"$SID\"}" >> "$LOG" 2>&1
  echo >> "$LOG"
  sleep 8
  i=0
  while [ $i -lt 120 ]; do
    [ "$(health)" = "200" ] && break
    sleep 5; i=$((i+1))
  done
  code=$(health)
  log "health after restart: $code (waited $((i*5))s)"
  [ "$code" = "200" ] || { log "ABORT: prod did not come back"; exit 1; }
  log "settling 90s before the first census"
  sleep 90
fi

for t in $TEMPLATES; do
  if [ ! -f "$HERE/templates/$t.json" ]; then log "SKIP $t: no template file"; continue; fi
  if [ "$(control_drop)" = "1" ]; then log "DROP on file before $t - stopping the set"; echo "dropped $(date -u +%FT%TZ) before $t" > "$STATE"; exit 3; fi
  # D21-14: a census on an unhealthy prod records 'skipped: box busy' rows that read as results.
  # Wait up to 10 min for /health, then stop the set instead.
  h=0
  while [ "$(health)" != "200" ] && [ $h -lt 60 ]; do sleep 10; h=$((h+1)); done
  if [ "$(health)" != "200" ]; then
    log "ABORT before $t: prod unhealthy for 10 min - no rows recorded"
    echo "aborted $(date -u +%FT%TZ) before $t: prod unhealthy" > "$STATE"; exit 4
  fi
  echo "running $t since $(date -u +%FT%TZ)" > "$STATE"
  log "=== census $t: $(goals_in "$t") goals, starting"
  ( cd "$HERE" && "$PY" "$CODE/run_census.py" --template "$t" \
      --operator "$OPERATOR" --skill improve-vera-sandboxed --tool "$TOOL" \
      --session-id "$SID" >> "$HERE/census_all.runner.out" 2>&1 )
  rc=$?
  log "=== census $t: runner exit $rc"
  archive "$t" "$rc"
  if [ "$rc" = "3" ]; then log "census_all: DROPPED during $t - stopping the set"; echo "dropped $(date -u +%FT%TZ) during $t" > "$STATE"; exit 3; fi
  sleep 60
done
log "census_all: all templates done"
echo "done $(date -u +%FT%TZ)" > "$STATE"
