#!/usr/bin/env python3
"""Info-seeking eval, retrieval layer: ask info.seek each latest/fact/overview question, one at a time, and score the
evidence packs (score.py). No model call, so it does not need the GPU; it does hit the search engines, so questions
are spaced (--gap) and it should not run beside a census or someone's evening work (plan: SK-4.1/4.2).

    python3 tools/info-eval/run_seek.py [--kinds latest,fact,overview] [--limit N] [--gap 20]
                                        [--target https://127.0.0.1:8999] [--out ~/info-eval]

Writes <out>/<timestamp>/{results.json, summary.md} and prints the summary.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import score  # noqa: E402


def seek(target: str, question: str, timeout: float = 240.0) -> dict:
    body = json.dumps({"name": "info.seek", "arguments": {"question": question},
                       "caller_kind": "eval", "session_id": "info-eval"}).encode()
    req = urllib.request.Request(target.rstrip("/") + "/mcp/call", body, {"Content-Type": "application/json"})
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
            d = json.load(r)
        c = d.get("content", d)
        return c if isinstance(c, dict) else {"error": str(c)[:200]}
    except Exception as e:
        return {"error": str(e)[:200]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kinds", default="latest,fact,overview")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--gap", type=float, default=20.0)
    # 127.0.0.1, not localhost: Python resolves localhost to ::1 first and prod listens on IPv4 only - every call hung
    ap.add_argument("--target", default=os.environ.get("VERA_URL", "https://127.0.0.1:8999"))
    ap.add_argument("--out", default=os.path.expanduser("~/info-eval"))
    a = ap.parse_args(argv)
    qs = json.loads((Path(__file__).resolve().parent / "questions.json").read_text(encoding="utf-8"))["questions"]
    kinds = {k.strip() for k in a.kinds.split(",") if k.strip()}
    qs = [q for q in qs if q["kind"] in kinds]
    if a.limit:
        qs = qs[:a.limit]
    started = datetime.now(timezone.utc)
    out = Path(a.out) / started.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    rows, packs = [], {}
    for i, q in enumerate(qs):
        if i:
            time.sleep(a.gap)
        pack = seek(a.target, q["q"])
        packs[q["id"]] = pack
        r = score.score_pack(q, pack)
        rows.append(r)
        print(f"{q['id']} {r['elapsed_ms']:>6} ms  read {r['read']} dated {r['dated']}/{r['items']}  "
              f"on_target={r['on_target']}" + (f" fresh={r.get('fresh')}" if q['kind'] == 'latest' else ""), flush=True)
        (out / "results.json").write_text(json.dumps({"rows": rows, "packs": packs}, indent=1), encoding="utf-8")
    meta = {"started": started.isoformat(timespec="seconds"), "target": a.target, "gap_s": a.gap}
    summ = score.summarise(rows)
    md = score.markdown(summ, rows, meta)
    (out / "summary.md").write_text(md, encoding="utf-8")
    (out / "results.json").write_text(json.dumps({"meta": meta, "summary": summ, "rows": rows, "packs": packs},
                                                 indent=1), encoding="utf-8")
    print(md)
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
