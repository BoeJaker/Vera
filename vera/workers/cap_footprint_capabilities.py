"""
cap_footprint_capabilities.py — node parity, by evidence

The runtime half of cap_footprint_core (read that first).

  the tracer           an audit hook on the Vera host: every file, SQLite,
                       process and socket touch made while a capability runs
                       is attributed to it (RUNNING_CAPS) and kept as a small
                       footprint in Redis
  nodes.parity.audit   runs the static audit (node_cap_audit) over the tree
  nodes.parity.status  per namespace: vetted / host-bound / unproven, and why
  nodes.parity.vet     marks the capabilities both halves clear as node-safe
                       (class `extended`, opt-in per node); dry run first
  caps.footprint       one capability's footprint, or a namespace's
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, emit_event, schedule

try:
    from Vera.vera.workers import cap_footprint_core as core
    from Vera.vera.workers import worker_placement_core as _wp
except ImportError:                                           # pragma: no cover
    from vera.workers import cap_footprint_core as core       # type: ignore
    from vera.workers import worker_placement_core as _wp     # type: ignore

log = logging.getLogger("vera.footprint")

STATIC_KEY = "vera:caps:static_audit"     # json {at, rows}
_STATE: Dict[str, Any] = {"on": False, "installed": False, "env": {}, "flushed": 0.0,
                          "events": 0, "dropped": 0}
#: cap -> (kinds {kind: n}, targets {kind: set}) since the last flush
_PENDING: Dict[str, Tuple[Dict[str, int], Dict[str, Set[str]]]] = {}


def _s(v) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v or "")


def _endpoints() -> Dict[str, Any]:
    """What counts as shared, a node, or this host."""
    cfg = getattr(_orch, "cfg", None)
    shared, ports = set(), set()

    def add(url: str, default_port: int = 0):
        if not url:
            return
        u = urlparse(url if "://" in url else "x://" + url)
        if u.hostname:
            p = u.port or default_port
            shared.add((u.hostname, p))
            if p:
                ports.add(p)
    add(str(getattr(_orch, "REDIS_URL", "") or os.getenv("REDIS_URL", "")), 6379)
    for k, d in (("POSTGRES_URL", 5432), ("NEO4J_URI", 7687)):
        add(str(os.getenv(k) or getattr(cfg, k, "") or ""), d)
    ch = str(os.getenv("CHROMA_HOST") or getattr(cfg, "CHROMA_HOST", "") or "")
    cp = int(os.getenv("CHROMA_PORT") or getattr(cfg, "CHROMA_PORT", 8000) or 8000)
    if ch:
        shared.add((ch, cp))
        ports.add(cp)
    nodes = set()
    for k in ("OLLAMA_BASE_URL", "OLLAMA_GPU_URL", "OLLAMA_CPU_A_URL", "OLLAMA_CPU_B_URL",
              "OLLAMA_EMBED_URL", "GPU_INFER_URL"):
        v = str(os.getenv(k) or getattr(cfg, k, "") or "")
        h = urlparse(v).hostname if v else None
        if h:
            nodes.add(h)
    for inst in (getattr(_orch, "OLLAMA_INSTANCES", None) or {}).values() if isinstance(
            getattr(_orch, "OLLAMA_INSTANCES", None), dict) else []:
        try:
            h = urlparse(inst.get("url", "")).hostname
            if h:
                nodes.add(h)
        except Exception:
            pass
    host_ips = set()
    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if getattr(a, "address", "") and ":" not in a.address[:5]:
                    host_ips.add(a.address)
    except Exception:
        pass
    root = os.path.dirname(os.path.dirname(os.path.abspath(_orch.__file__)))
    return {"shared": frozenset(shared), "shared_ports": frozenset(ports),
            "nodes": frozenset(nodes - {h for h, _ in shared}), "host_ips": frozenset(host_ips),
            "code_roots": (root + os.sep,), "code_files": _tracked_files(root)}


def _tracked_files(root: str) -> Optional[frozenset]:
    """Absolute paths of the files git tracks in the code tree: what every node
    has. None (reads in the tree count as code, the old rule) when git cannot
    say - better a weaker rule than a tracer that will not start."""
    try:
        p = subprocess.run(["git", "-c", "safe.directory=*", "-C", root, "ls-files", "-z"],
                           capture_output=True, timeout=60)
        if p.returncode != 0 or not p.stdout:
            return None
        return frozenset(os.path.join(root, f) for f in p.stdout.decode("utf-8", "replace").split("\0") if f)
    except Exception:
        return None


def _hook(event: str, args: tuple) -> None:
    # cheapest checks first: this runs for every audited event in the process
    if event not in core.WATCH or not _STATE["on"]:
        return
    stack = _orch.RUNNING_CAPS.get()
    if not stack:
        return
    env = _STATE["env"]
    try:
        r = core.classify(event, args, **env)
    except Exception:
        return
    if r is None:
        return
    kind, target = r
    _STATE["events"] += 1
    for cap in set(stack):
        rec = _PENDING.get(cap)
        if rec is None:
            if len(_PENDING) > 5000:
                _STATE["dropped"] += 1
                continue
            rec = _PENDING[cap] = ({}, {})
        rec[0][kind] = rec[0].get(kind, 0) + 1
        ts = rec[1].setdefault(kind, set())
        if len(ts) < core.MAX_TARGETS:
            ts.add(target)


async def _flush() -> int:
    r = _orch.REDIS
    if r is None:
        return 0
    pending = dict(_PENDING)
    _PENDING.clear()
    runs = dict(_orch.CAP_RUN_COUNTS)
    _orch.CAP_RUN_COUNTS.clear()
    caps = set(pending) | set(runs)
    if not caps:
        return 0
    now = time.time()
    names = sorted(caps)
    try:
        olds = await r.hmget(core.FOOTPRINT_KEY, *names)
    except Exception:
        olds = [None] * len(names)
    mapping = {}
    for name, raw in zip(names, olds):
        try:
            old = json.loads(raw) if raw else None
        except ValueError:
            old = None
        kinds, targets = pending.get(name, ({}, {}))
        mapping[name] = json.dumps(core.merge(old, kinds, targets, runs.get(name, 0), now))
    try:
        await r.hset(core.FOOTPRINT_KEY, mapping=mapping)
    except Exception as e:
        log.debug("footprint flush: %s", e)
    _STATE["flushed"] = now
    return len(mapping)


async def _footprint_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await _flush()
            if time.time() - _STATE.get("env_at", 0) > 600:
                _STATE["env"] = _endpoints()
                _STATE["env_at"] = time.time()
        except Exception as e:
            log.debug("footprint loop: %s", e)


async def _footprint_startup():
    """Install the tracer on the Vera host (never on a node worker or a dev
    sandbox: their footprints are not the host's)."""
    if getattr(_orch, "_IS_WORKER", False) or _orch.is_dev_sandbox():
        return
    if os.getenv("VERA_CAP_FOOTPRINT", "1").strip().lower() in ("0", "off", "false", "no"):
        return
    if _STATE["installed"]:
        return
    _STATE["env"] = _endpoints()
    _STATE["env_at"] = time.time()
    # (run_in_executor work is attributed too: the orchestrator's default
    # executor carries the caller's context into its threads)
    sys.addaudithook(_hook)
    _STATE["installed"] = True
    _STATE["on"] = True
    asyncio.create_task(_footprint_loop())
    log.info("cap footprint tracer on (shared %s, nodes %s)",
             sorted(f"{h}:{p}" for h, p in _STATE["env"]["shared"]), sorted(_STATE["env"]["nodes"]))


schedule(_footprint_startup, interval=999999, name="cap_footprint_startup")


#: The latest nightly vet preview (json): what nodes.parity.vet would add/drop.
PREVIEW_KEY = "vera:caps:parity:preview"


async def _parity_nightly():
    """Once a night (02:00-05:00 host time) on the Vera host: re-run the static
    audit and a vet DRY RUN, and keep the preview for review. Nothing is
    vetted here - a person approves each batch (owner, 2026-10-04)."""
    if getattr(_orch, "_IS_WORKER", False) or _orch.is_dev_sandbox():
        return
    if not 2 <= time.localtime().tm_hour < 5:
        return
    r = _orch.REDIS
    if r is None:
        return
    day = time.strftime("%Y-%m-%d")
    if not await r.set(f"vera:caps:parity:nightly:{day}", "1", nx=True, ex=86400):
        return
    try:
        a = await cap_parity_audit()
        v = await cap_parity_vet(dry_run=True)
    except Exception as e:
        log.warning("parity nightly: %s", e)
        return
    preview = {"at": time.time(), "audit": a, "would_vet": v.get("would_vet") or [],
               "would_drop": v.get("would_drop") or []}
    await r.set(PREVIEW_KEY, json.dumps(preview))
    await emit_event({"type": "nodes.parity.preview", "would_vet": len(preview["would_vet"]),
                      "would_drop": len(preview["would_drop"])})


schedule(_parity_nightly, interval=1800, name="nodes.parity.nightly")


# ═════════════════════════════════════════════════════════════════════════════
#  the parity capabilities
# ═════════════════════════════════════════════════════════════════════════════
async def _footprints(names: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    r = _orch.REDIS
    if r is None:
        return {}
    try:
        if names:
            vals = await r.hmget(core.FOOTPRINT_KEY, *names)
            raw = dict(zip(names, vals))
        else:
            raw = {_s(k): v for k, v in (await r.hgetall(core.FOOTPRINT_KEY)).items()}
    except Exception:
        return {}
    out = {}
    for k, v in raw.items():
        if v:
            try:
                out[k] = json.loads(v)
            except ValueError:
                pass
    return out


async def _static() -> Dict[str, Any]:
    r = _orch.REDIS
    try:
        raw = await r.get(STATIC_KEY) if r is not None else None
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


@capability(
    "nodes.parity.audit",
    http_method="POST", http_path="/nodes/parity/audit", http_tags=["nodes"],
    memory="off",
    description="Run the static half of the node-parity evidence over the running tree "
                "(node_cap_audit: what each capability's code path touches - host stores, "
                "processes, local files, environment a worker lacks, module state - across "
                "modules) in a subprocess (~2 min of CPU) and keep the result for "
                "nodes.parity.status / vet. Output: {ok, caps, seconds}.",
)
async def cap_parity_audit(trace_id=None) -> Dict:
    root = os.path.dirname(os.path.dirname(os.path.abspath(_orch.__file__)))
    t0 = time.time()
    loop = asyncio.get_running_loop()
    proc = await loop.run_in_executor(None, lambda: subprocess.run(
        [sys.executable, "-m", "Vera.vera.workers.node_cap_audit", root],
        capture_output=True, timeout=1200, cwd="/tmp",
        env={**os.environ, "PYTHONPATH": os.path.dirname(root) + os.pathsep + os.environ.get("PYTHONPATH", "")}))
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.decode("utf-8", "replace")[-600:]}
    rows = json.loads(proc.stdout)
    await _orch.REDIS.set(STATIC_KEY, json.dumps({"at": time.time(), "rows": rows}))
    return {"ok": True, "caps": len(rows), "seconds": round(time.time() - t0, 1)}


async def _verdicts() -> Tuple[Dict[str, Tuple[str, str]], Dict[str, Any]]:
    st = await _static()
    rows = st.get("rows") or {}
    safe_now = {c for c in _orch.CAPABILITY_REGISTRY
                if _wp.placement(c)[0] == "any" and c not in _wp.VETTED}
    eff = core.static_markers(rows, safe_now)
    fps = await _footprints()
    out = {}
    for c in _orch.CAPABILITY_REGISTRY:
        if c in safe_now:
            out[c] = ("safe", _wp.placement(c)[1])
            continue
        where, why = _wp.placement(c)
        # only the operator's brake is final here; a setter-by-name is judged on
        # the evidence like any other cap (the audit catches state writes)
        if why == "listed in VERA_WORKER_HOST_ONLY":
            out[c] = ("host", why)
            continue
        out[c] = core.verdict(fps.get(c), eff.get(c, ()), static_known=c in rows)
    return out, {"static_at": st.get("at"), "footprints": len(fps)}


@capability(
    "nodes.parity.status",
    http_method="GET", http_path="/nodes/parity/status", http_tags=["nodes"],
    memory="off", silent=True,
    description="Node parity by evidence: for every capability, whether it can run on a "
                "node - safe (vetted by hand), vetted (static audit and observed host "
                "footprint both clean), unproven (clean so far, too few observed runs), or "
                "host (why) - summarised per namespace. Input: namespace (str - list that "
                "namespace's capabilities). Output: {totals, namespaces, caps?, static_at, "
                "tracer}.",
    # DA-01e (1): namespaces x verdicts (host, vetted, unproven, safe) - the counts on the heat map
    contract={"presentation": {"kind": "matrix", "map": {"cells": "namespaces"}, "title": "Node parity by namespace",
                               "empty": "No parity audit yet"}},
)
async def cap_parity_status(namespace: str = "", trace_id=None) -> Dict:
    v, meta = await _verdicts()
    totals: Dict[str, int] = {}
    per: Dict[str, Dict[str, int]] = {}
    for c, (k, _w) in v.items():
        totals[k] = totals.get(k, 0) + 1
        ns = c.split(".", 1)[0]
        per.setdefault(ns, {})
        per[ns][k] = per[ns].get(k, 0) + 1
    out = {"ok": True, "totals": totals,
           "namespaces": dict(sorted(per.items(), key=lambda kv: -sum(kv[1].values()))),
           "static_at": meta["static_at"], "footprints": meta["footprints"],
           "tracer": {k: _STATE[k] for k in ("on", "events", "dropped", "flushed")},
           "vetted_applied": len(_wp.VETTED)}
    try:
        raw = await _orch.REDIS.get(PREVIEW_KEY) if _orch.REDIS is not None else None
        p = json.loads(raw) if raw else {}
        out["nightly_preview"] = {"at": p.get("at"), "would_vet": len(p.get("would_vet") or []),
                                  "would_drop": len(p.get("would_drop") or [])}
    except Exception:
        pass
    if namespace:
        out["caps"] = {c: {"verdict": k, "why": w} for c, (k, w) in sorted(v.items())
                       if c.split(".", 1)[0] == namespace}
    return out


@capability(
    "nodes.parity.vet",
    http_method="POST", http_path="/nodes/parity/vet", http_tags=["nodes"],
    memory="off",
    description="Mark as node-safe every capability the evidence clears (static audit "
                "clean AND at least 3 observed runs that touched nothing host-bound). "
                "They join the `extended` class, which no node takes until it is enabled "
                "there (Workers roles). A vetted capability whose footprint later shows a "
                "host touch is dropped on the next vet. Inputs: dry_run (bool, default "
                "true), namespace (str - limit to one), only (list - vet just these, a "
                "list a person reviewed; each must still clear the evidence, or it is "
                "listed in not_cleared). Output: {ok, vetted, dropped, would_vet, not_cleared}.",
)
async def cap_parity_vet(dry_run: bool = True, namespace: str = "",
                         only: Optional[List[str]] = None, trace_id=None) -> Dict:
    r = _orch.REDIS
    if r is None:
        return {"ok": False, "error": "no Redis"}
    v, meta = await _verdicts()
    if not meta["static_at"]:
        return {"ok": False, "error": "no static audit yet - run nodes.parity.audit"}
    # `only`: a list a person reviewed - it scopes the adds AND the drops, and
    # the evidence still has to clear each one (core.plan_vet)
    cur = {_s(k): json.loads(x) for k, x in (await r.hgetall(core.VETTED_KEY)).items()}
    plan = core.plan_vet(v, cur, namespace=namespace, only=only or ())
    add, drop, not_cleared = plan["add"], plan["drop"], plan["not_cleared"]
    want = {c: w for c, (_k, w) in v.items()}
    if dry_run:
        return {"ok": True, "dry_run": True, "would_vet": add, "would_drop": drop,
                "count": len(add), "not_cleared": not_cleared}
    if add:
        await r.hset(core.VETTED_KEY, mapping={c: json.dumps({"cls": core.VETTED_CLASS, "why": want[c],
                                                              "at": time.time()}) for c in add})
    if drop:
        await r.hdel(core.VETTED_KEY, *drop)
    await emit_event({"type": "nodes.parity.vet", "added": len(add), "dropped": len(drop)})
    return {"ok": True, "vetted": add, "dropped": drop, "not_cleared": not_cleared}


@capability(
    "caps.footprint",
    http_method="GET", http_path="/caps/footprint", http_tags=["caps", "nodes"],
    memory="off", silent=True,
    description="What a capability touched on the Vera host while it ran (the footprint "
                "tracer): runs, kinds (file_read/file_write/sqlite/subprocess/socket_local "
                "are host-bound; socket_shared/socket_node/socket_external are fine on a "
                "node) and sample targets. Inputs: name (str - one capability) or prefix "
                "(str - every capability starting with it). Output: {caps: {name: footprint}}.",
)
async def cap_caps_footprint(name: str = "", prefix: str = "", trace_id=None) -> Dict:
    if name:
        return {"caps": await _footprints([name])}
    fps = await _footprints()
    return {"caps": {k: v for k, v in sorted(fps.items()) if not prefix or k.startswith(prefix)}}
