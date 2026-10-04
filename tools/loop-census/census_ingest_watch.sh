#!/bin/sh
# census_ingest_watch.sh - while a census set runs on the OLD driver (the one
# started before census_all.sh learned to post each archive into Vera's task
# system), post every archive every 15 minutes, then once more when the set is
# done. Idempotent (ingest_census.py replaces; fix_reroutes.py repairs only rows
# from before the reroute fix). Exits by itself.
#   nohup sh census_ingest_watch.sh > /dev/null 2>&1 &
# by claude, session c4cceff0-829e-4853-b114-5abce77afbad, 2026-09-10.
CODE="$(cd "$(dirname "$0")" && pwd)"            # this harness's code (the repo)
# Its DATA - archives, logs, state, templates - is not the code folder:
HERE="${VERA_CENSUS_DIR:-$HOME/loop-census}"
LOG="$HERE/census_ingest_watch.log"
log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }
log "watch: start pid=$$"
while :; do
  python3 "$CODE/fix_reroutes.py" --all 2>&1 | grep -v ", 0 fixed" | sed 's/^/  /' >> "$LOG"
  python3 "$CODE/ingest_census.py" --all 2>&1 | grep -c ": ingested" | sed 's/^/  ingested archives: /' >> "$LOG"
  state="$(cat "$HERE/census_all.state" 2>/dev/null)"
  case "$state" in
    running*) sleep 900 ;;
    *) log "watch: set is '$state' - final pass done, exiting"; exit 0 ;;
  esac
done
