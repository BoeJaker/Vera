#!/usr/bin/env python3
"""Web-ingress benchmark harness (shared-planning web-ingress, WI-0; budgets in PLAN §6).

Runs fixed workloads against a running Vera through /mcp/call, one call at a time, and writes the samples with the
git sha and the census state, so every ingress package can be compared with the baseline before it lands:

    python3 tools/web-ingress-bench/bench.py run --suite fixture        # local fixture site, no internet
    python3 tools/web-ingress-bench/bench.py run --suite live           # real sites; only in a quiet window
    python3 tools/web-ingress-bench/bench.py compare BASE.json RUN.json # deltas + the PLAN §6 verdict (exit 1 = fail)

Refuses to run while a census goal is in flight or an agent loop is running (their numbers and ours would both be
wrong), and refuses the live suite in the owner's evening (18:00-01:00 local) unless --force.
Results: <out>/<UTC timestamp>-<suite>.json (default ~/vera-state/bench/web_ingress).
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fixture_site as fixture  # noqa: E402
import stats  # noqa: E402

CTX = ssl._create_unverified_context()

LIVE_FETCH = ["https://en.wikipedia.org/wiki/Real_estate_investment_trust",
              "https://www.gov.uk/government/collections/uk-house-price-index-reports",
              "https://en.wikipedia.org/wiki/Commercial_property",
              "https://www.bbc.co.uk/news/business"]
LIVE_SEARCH = ["Bank of England base rate decision", "UK office vacancy rates", "industrial logistics rents UK",
               "build to rent pipeline 2026", "student housing investment UK", "life sciences lab space demand"]
LIVE_RECALL = ["house prices", "commercial real estate yields", "AI valuation", "MCP server", "Savills"]
FIXTURE_FETCH = ["/a", "/b", "/c", "/d", "/e", "/etag", "/files/report.pdf", "/feed.rss"]


def call(target: str, name: str, args: Dict[str, Any], timeout: float = 900.0) -> Tuple[float, Dict[str, Any]]:
    body = json.dumps({"name": name, "arguments": args, "caller_kind": "bench",
                       "session_id": "web-ingress-bench"}).encode()
    req = urllib.request.Request(target.rstrip("/") + "/mcp/call", body, {"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
            d = json.load(r)
        c = d.get("content", d)
        res = c if isinstance(c, dict) else {"value": c}
    except Exception as e:  # noqa: BLE001 - a failed call is a sample with an error, not a crash
        res = {"error": str(e)[:300]}
    return (time.perf_counter() - t0) * 1000.0, res


def get_json(target: str, path: str) -> Dict[str, Any]:
    try:
        with urllib.request.urlopen(target.rstrip("/") + path, context=CTX, timeout=30) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


def preflight(target: str, suite: str, force: bool) -> Dict[str, Any]:
    """The state the run starts in; raises SystemExit when it must not run."""
    h = get_json(target, "/health")
    census = h.get("census") or {}
    loops = get_json(target, "/workshop/agent_loop/sessions?status=running").get("sessions") or []
    hour = datetime.now().hour
    state = {"census_busy": bool(census.get("busy")), "census_state": census.get("state"),
             "gpu_owners": (h.get("gpu_gate") or {}).get("owners"), "running_loops": len(loops), "local_hour": hour}
    problems = []
    if state["census_busy"]:
        problems.append("a census goal is in flight")
    if loops:
        problems.append(f"{len(loops)} agent loop(s) running")
    if suite == "live" and (hour >= 18 or hour < 1):
        problems.append("the live suite does not run 18:00-01:00 (the owner's evening)")
    if "error" in h:
        problems.append(f"/health unreachable: {h['error']}")
    if problems and not force:
        raise SystemExit("refusing to run: " + "; ".join(problems) + " (--force to override)")
    state["forced_past"] = problems
    return state


def git_sha() -> str:
    try:
        return subprocess.run(["git", "-C", str(HERE), "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def run_fixture(target: str, rounds: int, port: int, crawl: bool) -> Tuple[Dict[str, List[float]], Dict[str, Any]]:
    srv = fixture.serve(port)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    samples: Dict[str, List[float]] = {"fixture.web_fetch": []}
    notes: Dict[str, Any] = {"errors": []}
    try:
        for _ in range(rounds):
            for p in FIXTURE_FETCH:
                ms, r = call(target, "web.fetch", {"url": base + p, "ingest_to_fabric": False})
                samples["fixture.web_fetch"].append(ms)
                if r.get("error"):
                    notes["errors"].append({"url": p, "error": str(r["error"])[:120]})
        if crawl:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            for mode, extra in (("enrich_on", {"extract_entities": True, "detect_surfaces": True,
                                               "extract_subtables": True}),
                                ("enrich_off", {"extract_entities": False, "detect_surfaces": False,
                                                "extract_subtables": False})):
                ms, r = call(target, "fabric.discover.crawl", dict(
                    {"url": base + "/", "dataset_id": f"bench.web_ingress.fixture.{stamp}.{mode}",
                     "max_pages": 40, "max_depth": 2, "extract_entities_llm": False, "llm_tagging": False,
                     "topic_brief": False, "consolidate_entities": False, "drift_guard": False}, **extra))
                samples[f"fixture.crawl.{mode}"] = [ms]
                notes[f"crawl.{mode}"] = {k: r.get(k) for k in ("status", "pages_fetched", "queue_remaining",
                                                                 "surfaces_found", "subtables_found",
                                                                 "entities_found", "error")}
            notes["fixture_urls"] = len(fixture.truth())
    finally:
        srv.shutdown()
    return samples, notes


def run_live(target: str, rounds: int, gap: float) -> Tuple[Dict[str, List[float]], Dict[str, Any]]:
    samples: Dict[str, List[float]] = {"live.web_fetch": [], "live.web_search": [], "live.recall": []}
    notes: Dict[str, Any] = {"errors": []}
    for _ in range(rounds):
        for u in LIVE_FETCH:
            ms, r = call(target, "web.fetch", {"url": u})
            samples["live.web_fetch"].append(ms)
            if r.get("error"):
                notes["errors"].append({"url": u, "error": str(r["error"])[:120]})
            time.sleep(gap)
        for q in LIVE_SEARCH:
            ms, r = call(target, "web.search", {"query": q, "limit": 8})
            samples["live.web_search"].append(ms)
            time.sleep(gap)
        for q in LIVE_RECALL:
            ms, _ = call(target, "discovery.recall_index.search", {"query": q, "limit": 10})
            samples["live.recall"].append(ms)
    return samples, notes


def cmd_run(a: argparse.Namespace) -> int:
    state = preflight(a.target, a.suite, a.force)
    t0 = datetime.now(timezone.utc)
    if a.suite == "fixture":
        samples, notes = run_fixture(a.target, a.rounds, a.port, not a.no_crawl)
    else:
        samples, notes = run_live(a.target, a.rounds, a.gap)
    rec = {"suite": a.suite, "started": t0.isoformat(), "finished": datetime.now(timezone.utc).isoformat(),
           "sha": git_sha(), "target": a.target, "state": state, "samples": samples, "notes": notes,
           "summary": {k: stats.summarise(v) for k, v in samples.items()}}
    out = Path(os.path.expanduser(a.out))
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{t0.strftime('%Y%m%dT%H%M%SZ')}-{a.suite}.json"
    path.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    for k, s in rec["summary"].items():
        print(f"{k:28s} n={s['n']:3d} p50={s['p50'] or 0:9.0f} p95={s['p95'] or 0:9.0f} max={s['max'] or 0:9.0f} ms")
    if notes.get("errors"):
        print(f"errors: {len(notes['errors'])} (see {path})")
    print(f"wrote {path}")
    return 0


def cmd_compare(a: argparse.Namespace) -> int:
    base = json.loads(Path(a.base).read_text(encoding="utf-8"))
    run = json.loads(Path(a.run).read_text(encoding="utf-8"))
    cmp = stats.compare(base.get("samples") or {}, run.get("samples") or {})
    print(f"base {base.get('sha')} {base.get('started')}  ->  run {run.get('sha')} {run.get('started')}")
    for name, r in cmp.items():
        s, b = r["run"], r["base"]
        print(f"{name:28s} p50 {b['p50'] or 0:8.0f} -> {s['p50'] or 0:8.0f}  p95 {b['p95'] or 0:8.0f} -> "
              f"{s['p95'] or 0:8.0f}  {'' if r['ok'] is None else ('ok' if r['ok'] else 'OVER BUDGET')} "
              f"{'; '.join(r['why'])}")
    v = stats.verdict(cmp)
    print("verdict:", v)
    return 1 if v == "fail" else 0


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--suite", choices=("fixture", "live"), default="fixture")
    r.add_argument("--target", default="https://127.0.0.1:8999")
    r.add_argument("--rounds", type=int, default=2)
    r.add_argument("--gap", type=float, default=2.0, help="seconds between live calls")
    r.add_argument("--port", type=int, default=8765)
    r.add_argument("--no-crawl", action="store_true")
    r.add_argument("--out", default="~/vera-state/bench/web_ingress")
    r.add_argument("--force", action="store_true")
    c = sub.add_parser("compare")
    c.add_argument("base")
    c.add_argument("run")
    a = ap.parse_args(argv)
    return cmd_run(a) if a.cmd == "run" else cmd_compare(a)


if __name__ == "__main__":
    sys.exit(main())
