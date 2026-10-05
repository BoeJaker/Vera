#!/usr/bin/env python3
"""Post an ARCHIVED census run into Vera's own task system (evolve.result.ingest):
every row becomes a run record (source=census, keyed by its loop session) and
the run a suite scoreboard tagged census-<template>, so evolve.runs, the suite
views, the race-to-green board and the task-through-time index all hold it.
Idempotent - post the same file twice and the store has it once.

    ingest_census.py census.run52.jsonl [census.exec-family-run3.jsonl ...]
    ingest_census.py --all          # every archive in this directory, oldest first

census_all.sh runs it on every archive it writes; run it by hand to backfill
older archives, or after renaming one (the marker in the new name - failed,
partial, ... - is posted with it, so the store excludes it as the archive
reader does). The live census.jsonl is refused: its archive id is not known
until the driver names it, and run_census.py posts those rows itself.

by claude, session c4cceff0-829e-4853-b114-5abce77afbad, 2026-09-10.
"""
import json, os, re, subprocess, sys, tempfile, time

BASE = os.environ.get("CENSUS_BASE", "").strip().rstrip("/") or "https://localhost:8999"
CODE = os.path.dirname(os.path.abspath(__file__))       # this harness's code (the repo)
# Its DATA - archives, logs, state, templates, stall dumps - is not the code folder:
HERE = os.path.expanduser(os.environ.get("VERA_CENSUS_DIR", "").strip() or "~/loop-census")
SESSION_ID = os.getenv("VERA_CENSUS_SESSION_ID") or "c4cceff0-829e-4853-b114-5abce77afbad"
# census_core.EXCLUDE_MARKERS, as of 2026-09-10 - a name carrying one is not history.
MARKERS = ("partial", "failed", "stalled", "aborted", "interrupted", "invalid", "wedged", "abandoned")
_NAME = re.compile(r"^census(?:\.(?P<name>[^.]+))?\.jsonl$", re.I)


def identify(filename):
    """(census_run, excluded, template) from an archive's name, or None."""
    m = _NAME.match(os.path.basename(filename))
    if not m or not m.group("name"):
        return None
    name = m.group("name")
    excluded = next((mk for mk in MARKERS if mk in name.lower()), "")
    base = name
    if excluded:
        i = name.lower().find(excluded)
        base = name[:i].rstrip("-") if i > 0 else name
    template = "default" if base.startswith("run") else base.split("-run")[0]
    return base, excluded, template


def archive_time(path):
    """When the run ended, ISO UTC, for rows that carry no time of their own
    (before 2026-09-10 rows had none). The `.log` beside the archive is written
    as the run goes and never touched again, so its mtime is the end of the
    run; the `.jsonl` is rewritten by backfill_code.py / fix_reroutes.py and
    its mtime is whenever that last happened. Prefer the log."""
    base = path[:-len(".jsonl")] if path.endswith(".jsonl") else path
    lg = base + ".log"
    st = os.stat(lg) if os.path.exists(lg) else os.stat(path)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))


def post(payload, timeout=180):
    """One /mcp/call with the payload in a temp file: a run's rows can be a
    megabyte, which is past what an argv can carry."""
    body = json.dumps({"name": "evolve.result.ingest", "arguments": payload,
                       "caller_kind": "claude", "session_id": SESSION_ID})
    fd, path = tempfile.mkstemp(prefix="ingest-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        r = subprocess.run(["curl", "-sk", "--max-time", str(timeout), "-X", "POST", BASE + "/mcp/call",
                            "-H", "Content-Type: application/json", "--data-binary", "@" + path],
                           capture_output=True, text=True, timeout=timeout + 20)
        try:
            return json.loads(r.stdout)
        except Exception:
            return {"detail": (r.stdout or r.stderr or "no response")[:200]}
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


def ingest(path):
    ident = identify(path)
    if not ident:
        return "%s: not an archive (the live census.jsonl is posted by the harness itself)" % os.path.basename(path)
    census_run, excluded, template = ident
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if isinstance(r, dict) and r.get("id"):
                rows.append(r)
    if not rows:
        return "%s: no rows" % os.path.basename(path)
    template = next((str(r.get("template")) for r in rows if r.get("template")), template)
    resp = post({"template": template, "census_run": census_run, "rows": rows, "suite": True,
                 "archive": os.path.basename(path), "excluded": excluded,
                 "ts_fallback": archive_time(path)})
    res = resp.get("content") if isinstance(resp, dict) else None
    if isinstance(res, dict) and res.get("ok"):
        return "%s: ingested %d row(s) as %s/%s (%d added, %d replaced, %d older than the store's window), suite %s%s" % (
            os.path.basename(path), res.get("ingested", 0), template, census_run,
            res.get("added", 0), res.get("replaced", 0), res.get("skipped_old", 0), res.get("suite", "?"),
            (" EXCLUDED (%s)" % excluded) if excluded else "")
    err = ((res or {}).get("error") if isinstance(res, dict) else None) or \
          (resp.get("detail") if isinstance(resp, dict) else None) or "no response"
    return "%s: NOT ingested - %s" % (os.path.basename(path), str(err)[:200])


def main(argv):
    files = [a for a in argv if a != "--all"]
    if "--all" in argv:
        names = [f for f in os.listdir(HERE) if _NAME.match(f) and f != "census.jsonl"]

        def key(f):
            m = re.search(r"run(\d+)", f)
            return (0, int(m.group(1)), f) if m else (1, 0, f)
        files += [os.path.join(HERE, f) for f in sorted(names, key=key)]
    if not files:
        print(__doc__.strip().splitlines()[0])
        print("usage: ingest_census.py <census.*.jsonl> ... | --all")
        return 2
    rc = 0
    for f in files:
        f = f if os.path.isabs(f) else os.path.join(HERE, f)
        if not os.path.exists(f):
            print("%s: missing" % f); rc = 1; continue
        line = ingest(f)
        print("%s %s" % (time.strftime("%H:%M:%S"), line), flush=True)
        if "NOT ingested" in line or "not an archive" in line:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
