#!/usr/bin/env python3
"""docgen — Vera documentation / screenshot CLI (thin wrapper over the operator).

The harness itself lives in Vera as the ``operator.*`` / ``docs.*`` capabilities;
this CLI is the "singular tool" entry point so you can drive it standalone.

Modes
-----
• Against a running orchestrator (has the loop-lab sandbox):
      docgen run                       # boots a sandbox, shoots every panel, writes docs
      docgen run --only markets,dream
      docgen run --orchestrator http://localhost:8999
• Directly against any live Vera (no orchestrator call; needs playwright locally):
      docgen run --base-url http://localhost:8999
• Utilities:
      docgen test                      # run the pytest unit suite
      docgen gallery                   # rebuild documentation/README.md from the manifest

Screenshots land in documentation/assets/<domain>/ and the docs' managed
auto-blocks + gallery are regenerated in place.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # repo root (contains vera/, documentation/)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _post(url: str, body: dict, timeout: int = 1800) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (local)
        raw = r.read().decode()
    return json.loads(raw) if raw.strip() else {}


def _run_via_orchestrator(orch: str, target: str, domains, capture: bool) -> dict:
    url = orch.rstrip("/") + "/docs/build"
    print(f"→ POST {url}  (target={target}, domains={domains or 'all'})")
    return _post(url, {"target": target, "domains": domains or [], "capture": capture})


def _run_in_process(base_url: str, domains, capture: bool) -> dict:
    """Run the documentation mission locally against a live base_url."""
    from vera.operator.missions import run_mission

    async def _noop(name, args=None):
        return {}

    async def _emit(ev):
        stage = ev.get("stage", "")
        msg = ev.get("message", "")
        if stage in ("domain", "target", "discover", "done", "warn"):
            print(f"  [{stage}] {msg}")

    ctx = {"call_cap": _noop, "emit": _emit, "repo_root": str(ROOT),
           "default_base_url": base_url}
    params = {"target": {"kind": "live", "base_url": base_url}, "base_url": base_url,
              "domains": domains or [], "capture": capture, "write_docs": True}
    return asyncio.run(run_mission("documentation", params, ctx))


def cmd_run(args) -> int:
    domains = [s.strip() for s in (args.only or "").split(",") if s.strip()]
    capture = not args.no_capture
    if args.base_url and not args.orchestrator:
        res = _run_in_process(args.base_url, domains, capture)
    else:
        orch = args.orchestrator or "http://localhost:8999"
        target = "sandbox" if args.sandbox else "live"
        res = _run_via_orchestrator(orch, target, domains, capture)
    print(json.dumps(res, indent=2))
    return 0 if not res.get("error") else 1


def cmd_gallery(args) -> int:
    orch = args.orchestrator or "http://localhost:8999"
    print(json.dumps(_post(orch.rstrip("/") + "/docs/gallery", {}), indent=2))
    return 0


def cmd_test(args) -> int:
    cmd = [sys.executable, "-m", "pytest", "tests", "-q"]
    if args.k:
        cmd += ["-k", args.k]
    return subprocess.call(cmd, cwd=str(ROOT))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="docgen", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="capture screenshots + regenerate docs")
    r.add_argument("--base-url", default="", help="drive this live Vera directly (in-process)")
    r.add_argument("--orchestrator", default="", help="call a running orchestrator's /docs/build")
    r.add_argument("--sandbox", action="store_true", help="use a loop-lab sandbox (needs orchestrator)")
    r.add_argument("--only", default="", help="comma-separated domain slugs (default: all)")
    r.add_argument("--no-capture", action="store_true", help="regenerate docs without screenshots")
    r.set_defaults(func=cmd_run)

    g = sub.add_parser("gallery", help="rebuild documentation/README.md from the manifest")
    g.add_argument("--orchestrator", default="", help="orchestrator base url")
    g.set_defaults(func=cmd_gallery)

    # `shots` is an alias of `run`
    s = sub.add_parser("shots", help="alias for run")
    s.add_argument("--base-url", default="")
    s.add_argument("--orchestrator", default="")
    s.add_argument("--sandbox", action="store_true")
    s.add_argument("--only", default="")
    s.add_argument("--no-capture", action="store_true")
    s.set_defaults(func=cmd_run)

    t = sub.add_parser("test", help="run the pytest unit suite")
    t.add_argument("-k", default="", help="pytest -k expression")
    t.set_defaults(func=cmd_test)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
