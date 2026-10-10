#!/bin/sh
# Run probe_layout.py over "template goal layout" lines (stdin or a file), one at a time,
# waiting for a free box before each. Log: $OUTD/batch.log. Added 4 Oct 2026.
T="$(cd "$(dirname "$0")" && pwd)"
PY="${CENSUS_PY:-$HOME/langchain/bin/python3}"   # needs redis-py (rev.py)
OUTD="${CENSUS_PROBE_LAYOUT_OUT:-$HOME/loop-census/probe_layout}"
mkdir -p "$OUTD"
while read -r tpl gid layout; do
  [ -z "$tpl" ] && continue
  case "$tpl" in \#*) continue ;; esac
  n=0
  until "$PY" "$T/probe_layout.py" "$tpl" "$gid" "${layout:-L0}" 1800 >> $OUTD/batch.log 2>&1; do
    n=$((n+1)); [ $n -ge 30 ] && { echo "$(date -u +%FT%TZ) gave up waiting: $tpl $gid" >> $OUTD/batch.log; break; }
    sleep 120
  done
done < "${1:-/dev/stdin}"
echo "$(date -u +%FT%TZ) batch done" >> $OUTD/batch.log
