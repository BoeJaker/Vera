#!/usr/bin/env python3
"""Backfill `code_version` on census rows that predate code capture, from the
loop trace's own event stamps (counters.code_version = ['<sha10>@<branch>']).

by claude c4cceff0, 2026-09-10. Rows written before the harness recorded
`code` (obs.provenance) can still say which code they ran on for as long as
the loop's event log is retained (7 days), and the census UI reads
`code_version` as the fallback. Only ARCHIVED files: never the live
census.jsonl, which the runner is appending to. Atomic rewrite; a row that
already carries `code` or `code_version` is left alone; a trace that has aged
out leaves the row untouched and is counted.

usage: backfill_code.py census.run52.jsonl [more files...]
"""
import json, os, subprocess, sys

BASE = os.environ.get("CENSUS_BASE", "").strip().rstrip("/") or "https://localhost:8999"
SID = "c4cceff0-829e-4853-b114-5abce77afbad"


def trace(session_id):
    body = json.dumps({"name": "workshop.agent_loop.trace", "arguments": {"session_id": session_id},
                       "caller_kind": "claude", "session_id": SID})
    try:
        out = subprocess.run(["curl", "-sk", "--max-time", "90", "-X", "POST", BASE + "/mcp/call",
                              "-H", "Content-Type: application/json", "-d", body],
                             capture_output=True, text=True, timeout=120).stdout
        return json.loads(out).get("content") or {}
    except Exception:
        return {}


def backfill(path):
    if os.path.basename(path) in ("census.jsonl", "census.log"):
        print("refusing the live file:", path); return
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    filled = skipped = aged = 0
    for r in rows:
        if r.get("code") or r.get("code_version"):
            skipped += 1; continue
        sid = r.get("session_id")
        if not sid:
            aged += 1; continue
        cv = ((trace(sid).get("counters") or {}).get("code_version")) or []
        if cv:
            r["code_version"] = cv
            r["code_version_backfilled"] = True
            filled += 1
        else:
            aged += 1
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    os.replace(tmp, path)
    print("%s: %d rows, filled %d, already had code %d, no trace %d" % (os.path.basename(path), len(rows), filled, skipped, aged))


if __name__ == "__main__":
    for p in sys.argv[1:]:
        backfill(p)
