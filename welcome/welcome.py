#!/usr/bin/env python3
"""
welcome.py — Vera guided tour & smoke test (stdlib only).

    python welcome/welcome.py            # interactive guided tour
    python welcome/welcome.py --check    # non-interactive smoke test (CI-friendly)
"""
import argparse, json, sys, urllib.request, webbrowser
from urllib.error import URLError

BASE = "http://localhost:8999"
C = {"h": "\033[36m", "g": "\033[32m", "y": "\033[33m", "r": "\033[31m",
     "b": "\033[1m", "x": "\033[0m"}

BANNER = r"""
 __     __        _____  __ _
 \ \   / /__ _ _|  __ \/ _` |
  \ \ / / -_) '_| |_) | (_| |   Distributed Capability Runtime
   \_/  \___|_| |_.__/ \__,_|   guided tour
"""

STOPS = [
    ("What is Vera?",
     "A distributed capability runtime. Everything - LLM calls, memory, DAGs,\n"
     "research, the IDE - is a 'capability': a decorated async function that is\n"
     "simultaneously a REST endpoint, an MCP tool, and a DAG node."),
    ("The orchestrator",
     "A FastAPI app on :8999. It registers caps, mounts their routes,\n"
     "streams events over Redis, and dispatches work to Ollama worker nodes."),
    ("Backends",
     "Redis (events/queues), Postgres (state), ChromaDB (vectors),\n"
     "Neo4j (memory graph). They connect lazily - Vera boots without them."),
    ("Talking to caps",
     "GET  /mcp/tools            list every capability\n"
     "POST /mcp/call             {\"name\":..., \"arguments\":{...}}\n"
     "GET  /health               backends + worker + cap counts\n"
     "GET  /docs                 Swagger UI for every REST route"),
    ("The harness UI",
     f"Open {BASE}/ for the single-page harness: panels for caps, DAGs,\n"
     "memory graph, data fabric, IDE and the live event stream."),
]


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=4) as r:
        return json.loads(r.read().decode())


def is_up():
    for p in ("/health", "/debug/health"):
        try:
            return _get(p)
        except Exception:
            continue
    return None


def smoke():
    print(f"{C['b']}Vera smoke test -> {BASE}{C['x']}")
    health = is_up()
    if not health:
        print(f"{C['r']}x orchestrator not reachable. Start it with `make up` or `make run`.{C['x']}")
        return 1
    print(f"{C['g']}+ orchestrator up{C['x']}  "
          f"caps={health.get('caps','?')} workers={health.get('workers','?')} "
          f"mode={health.get('mode','?')}")
    for k in ("redis", "postgres", "chroma", "neo4j"):
        ok = health.get(k)
        mark = f"{C['g']}+{C['x']}" if ok else f"{C['y']}-{C['x']}"
        print(f"   {mark} {k}")
    try:
        tools = _get("/mcp/tools")
        items = tools.get("tools") if isinstance(tools, dict) else tools
        names = [t.get("name") for t in items]
        print(f"{C['g']}+ {len(names)} capabilities registered{C['x']}  "
              f"e.g. {', '.join(filter(None, names[:6]))} ...")
    except Exception as e:
        print(f"{C['y']}! could not list caps: {e}{C['x']}")
    return 0


def tour():
    print(C["h"] + BANNER + C["x"])
    health = is_up()
    if health:
        print(f"{C['g']}* connected to a running orchestrator "
              f"({health.get('caps','?')} caps){C['x']}\n")
    else:
        print(f"{C['y']}* no running orchestrator detected - start one with "
              f"`make up` or `make run` to try the live calls{C['x']}\n")
    for i, (title, body) in enumerate(STOPS, 1):
        print(f"{C['b']}[{i}/{len(STOPS)}] {title}{C['x']}")
        print(body + "\n")
        try:
            input(f"{C['h']}  [enter] next ...{C['x']}")
        except (EOFError, KeyboardInterrupt):
            print(); return 0
        print()
    if health:
        print(f"{C['b']}Live: a few capabilities on this instance{C['x']}")
        smoke()
    try:
        if input(f"\n{C['h']}Open the HTML welcome guide in your browser? [y/N] {C['x']}").strip().lower() == "y":
            import pathlib
            webbrowser.open((pathlib.Path(__file__).parent / "index.html").resolve().as_uri())
    except (EOFError, KeyboardInterrupt):
        pass
    print(f"\n{C['g']}Enjoy Vera.{C['x']}  Docs: {BASE}/docs - UI: {BASE}/")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Vera guided tour / smoke test")
    ap.add_argument("--check", action="store_true", help="non-interactive smoke test")
    args = ap.parse_args()
    sys.exit(smoke() if args.check else tour())
