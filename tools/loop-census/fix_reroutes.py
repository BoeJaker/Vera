#!/usr/bin/env python3
"""Recompute `routing.calls.reroutes` on ARCHIVED census rows written before
2026-09-10T16:45Z, whose harness flagged every GPU call as a re-route.

run_census.routing_calls() marked a call as re-routed when its route
explanation contained any of a word list that included "deny_gpu" and "busy";
an ordinary placement reads "job-type rule 'chat' (deny_gpu=False, ...)", so
every call matched and every row said reroutes=N for its N calls. The per-call
`reroute` text was kept, so the list can be rebuilt from it with the corrected
words (the same tuple run_census.py uses now). Nothing else on a row changes;
`spill`/`escalated` flags were computed separately and are re-applied as they
were. Only archived files: never the live census.jsonl (the harness owns it).

    fix_reroutes.py census.run52.jsonl census.exec-family-run3.jsonl ...
    fix_reroutes.py --all

by claude, session c4cceff0-829e-4853-b114-5abce77afbad, 2026-09-10.
"""
import json, os, re, sys

CODE = os.path.dirname(os.path.abspath(__file__))       # this harness's code (the repo)
# Its DATA - archives, logs, state, templates, stall dumps - is not the code folder:
HERE = os.path.expanduser(os.environ.get("VERA_CENSUS_DIR", "").strip() or "~/loop-census")
sys.path.insert(0, CODE)
from run_census import _REROUTE_WORDS  # noqa: E402  the corrected list, one source

_NAME = re.compile(r"^census\.[^.]+\.jsonl$", re.I)


def real_reroute(text):
    """The parts of a stored `reroute` note that still say re-route under the
    corrected rule: 'escalated' and 'cpu spill (...)' were computed from flags
    and stay; a route explanation stays only if it carries a deviation phrase."""
    keep = []
    for part in str(text or "").split("; "):
        p = part.strip()
        if not p:
            continue
        if p == "escalated" or p.startswith("cpu spill"):
            keep.append(p)
        elif any(w in p.lower() for w in _REROUTE_WORDS):
            keep.append(p)
    return "; ".join(keep)


def fix_row(row):
    ro = row.get("routing") if isinstance(row.get("routing"), dict) else None
    calls = (ro or {}).get("calls") if ro else None
    if not isinstance(calls, dict) or not isinstance(calls.get("calls"), list):
        return 0, 0
    before = len(calls.get("reroutes") or [])
    reroutes = []
    for c in calls["calls"]:
        if not isinstance(c, dict):
            continue
        # The explanation is not stored on the call, only the note built from
        # it; rebuild the note from that note (the deviation phrases survive).
        note = real_reroute(c.get("reroute"))
        if note:
            c["reroute"] = note
            reroutes.append(c)
        elif "reroute" in c:
            del c["reroute"]
    calls["reroutes"] = reroutes
    calls["reroutes_fixed"] = True
    return before, len(reroutes)


def fix_file(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            rows.append(json.loads(line))
    changed = 0
    log = []
    for r in rows:
        b, a = fix_row(r)
        if b != a:
            changed += 1
            log.append("%s %d->%d" % (r.get("id"), b, a))
    if changed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        os.replace(tmp, path)
    return "%s: %d row(s), %d fixed%s" % (os.path.basename(path), len(rows), changed,
                                           (" [" + ", ".join(log) + "]") if log else "")


def main(argv):
    files = [a for a in argv if a != "--all"]
    if "--all" in argv:
        files += sorted(os.path.join(HERE, f) for f in os.listdir(HERE) if _NAME.match(f))
    if not files:
        print("usage: fix_reroutes.py <census.<run>.jsonl> ... | --all   (never census.jsonl)")
        return 2
    for f in files:
        f = f if os.path.isabs(f) else os.path.join(HERE, f)
        if os.path.basename(f) == "census.jsonl":
            print("census.jsonl: refused (live; the harness owns it)"); continue
        if not os.path.exists(f):
            print("%s: missing" % f); continue
        print(fix_file(f))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
