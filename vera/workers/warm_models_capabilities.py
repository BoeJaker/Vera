"""Warm model slots - every Ollama node keeps its models loaded, and a hot
workload takes the slots over.

    ollama.warm.status   the plan per node (slots, planned models and where they
                         came from, what is resident now, what was dropped and
                         why), the workload scenarios and their demand, the
                         last actions
    ollama.warm.set      change the config: on/off, per-node models / slots /
                         reserve, the CPU window, spill limits, scenarios
    ollama.warm.apply    plan and act now (dry run by default)

The decisions are in warm_models_core (pure). This module supplies the live
facts - the nodes, the routing rules, /api/ps, model sizes, node memory, the
demand the router records - runs the plan once a minute, and publishes it to
`_orch.WARM_STATE`, which the request path reads to keep a planned model on its
planned runner (keep_alive=-1 at the planned window) and the picker reads for
warm spill. The plan is also written to the coordination Redis so a dev
sandbox's calls keep the same runners (a sandbox never ACTS on the nodes).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from pathlib import Path

from fastapi.responses import HTMLResponse, Response

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability, emit_event, register_ui, schedule

try:
    from Vera.vera.workers import warm_models_core as core
except ImportError:                                        # pragma: no cover
    from vera.workers import warm_models_core as core      # type: ignore

log = logging.getLogger("vera.ollama.warm")

_PLAN_KEY = "vera:ollama:warm:plan"      # coordination Redis: the published plan
_TICK_LOCK = "vera:ollama:warm:lock"     # one acting process per estate
_LOCK = asyncio.Lock()
_ACTING: Dict[str, str] = {}             # node -> what is being done there now
_LAST: Dict[str, Any] = {"at": 0.0, "actions": [], "results": []}
_SIZES: Dict[str, Any] = {"at": 0.0, "by_node": {}}
_SIZES_TTL = 600.0
_TIMEOUT = 300.0                          # a cold 35b on CPU is ~62 s; leave room


def _rawcap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return (c.get("raw") or c.get("func")) if c else None


def _s(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v or "")


# ── config + state ───────────────────────────────────────────────────────────
async def load_config() -> Dict[str, Any]:
    r = _orch.REDIS
    raw = None
    try:
        raw = await r.get(core.REDIS_KEY) if r is not None else None
    except Exception:
        pass
    try:
        return core.merge_config(json.loads(_s(raw)) if raw else None)
    except ValueError:
        return core.merge_config(None)


async def _stored() -> Dict[str, Any]:
    r = _orch.REDIS
    try:
        raw = await r.get(core.REDIS_KEY) if r is not None else None
        return json.loads(_s(raw)) if raw else {}
    except Exception:
        return {}


async def _load_state() -> Dict[str, Any]:
    r = _orch.REDIS
    try:
        raw = await r.get(core.STATE_KEY) if r is not None else None
        return json.loads(_s(raw)) if raw else {}
    except Exception:
        return {}


async def _save_state(st: Dict[str, Any]) -> None:
    r = _orch.REDIS
    try:
        if r is not None:
            await r.set(core.STATE_KEY, json.dumps(st))
    except Exception:
        pass


# ── live facts ───────────────────────────────────────────────────────────────
def _effective_rules() -> Dict[str, Dict[str, Any]]:
    out = {}
    for jt in getattr(_orch, "OLLAMA_JOB_TYPES", []) or []:
        try:
            out[jt] = dict(_orch._resolve_rule(jt) or {})
        except Exception:
            continue
    return out


def _aliases(rules: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    return {
        "@default": str(getattr(_orch, "OLLAMA_MODEL", "") or ""),
        "@naming": str((rules.get("naming") or {}).get("model") or "")
                   or str(getattr(_orch, "OLLAMA_MODEL", "") or ""),
        "@embed": str((rules.get("embedding") or {}).get("model") or "")
                  or str(getattr(_orch, "OLLAMA_EMBED_MODEL", "") or ""),
        "@long_horizon": str(getattr(_orch, "LONG_HORIZON_CPU_MODEL", "") or ""),
    }


def _instances() -> Dict[str, Dict[str, Any]]:
    return dict(getattr(_orch, "OLLAMA_INSTANCES", {}) or {})


async def _get(url: str, timeout: float = 6.0) -> Optional[Dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(url)
            return r.json() if r.status_code == 200 else None
    except Exception:
        return None


async def _residency(insts: Dict[str, Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """{node: /api/ps rows}; a node that does not answer is left OUT (unknown)."""
    async def one(iid, inst):
        d = await _get(f"{inst.get('url')}/api/ps")
        return iid, (core.ps_rows(d) if isinstance(d, dict) else None)
    res = await asyncio.gather(*[one(i, x) for i, x in insts.items() if x.get("url")])
    return {iid: rows for iid, rows in res if rows is not None}


async def _sizes(insts: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """{node: {model: bytes}} from /api/tags, cached - weights change on a pull."""
    now = time.time()
    if now - _SIZES["at"] < _SIZES_TTL and _SIZES["by_node"]:
        return _SIZES["by_node"]
    out: Dict[str, Dict[str, int]] = {}
    for iid, inst in insts.items():
        d = await _get(f"{inst.get('url')}/api/tags", timeout=8.0)
        if isinstance(d, dict):
            out[iid] = {m.get("name", ""): int(m.get("size") or 0) for m in d.get("models") or []}
    _SIZES.update(at=now, by_node=out)
    return out


async def _memory(insts: Dict[str, Dict[str, Any]]) -> Dict[str, float]:
    """GB each node's planned models may use: VRAM on a GPU node, RAM on a CPU
    node, and for the CPU sibling of a GPU node the host's RAM (the GPU
    runner lives on the card; its host footprint is small) - from the node
    catalog's hardware record."""
    by: Dict[str, Dict[str, Any]] = {}
    fn = _rawcap("catalog.nodes")
    if fn:
        try:
            d = await fn()
            rows = d.get("nodes", d) if isinstance(d, dict) else d
            for n in (rows.values() if isinstance(rows, dict) else rows or []):
                if isinstance(n, dict) and n.get("id"):
                    by[str(n["id"])] = n
        except Exception as e:
            log.debug("catalog.nodes: %s", e)
    out: Dict[str, float] = {}
    for iid, inst in insts.items():
        cls = core.node_class(iid, inst, insts)
        hw = by.get(iid) or {}
        if cls == "gpu":
            out[iid] = float(hw.get("vram_gb") or 0)
        elif cls == "cpu_sibling":
            parent = by.get(iid[:-len("-cpu")]) or {}
            out[iid] = float(hw.get("ram_gb") or 0) or float(parent.get("ram_gb") or 0) * 0.8
        else:
            out[iid] = float(hw.get("ram_gb") or 0)
    return out


async def _gpu_windows(insts: Dict[str, Dict[str, Any]], model: str) -> Dict[str, int]:
    """The window the router itself asks for on each GPU node (stable per node),
    so a warm load and a real call share one runner."""
    out: Dict[str, int] = {}
    for iid, inst in insts.items():
        if not inst.get("has_gpu") or not model:
            continue
        try:
            out[iid] = int(await asyncio.wait_for(
                _orch.effective_num_ctx(model, iid, True), timeout=8.0) or 0)
        except Exception:
            out[iid] = 0
    return out


def _inflight_by_job() -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in (getattr(_orch, "OLLAMA_INFLIGHT", {}) or {}).values():
        jt = str((v or {}).get("job_type") or "")
        if jt:
            out[jt] = out.get(jt, 0) + 1
    return out


async def _census_busy() -> bool:
    try:
        return bool((await _orch._health_census()).get("busy"))
    except Exception:
        return False


def _options_for(iid: str, inst: Dict[str, Any], num_ctx: int) -> Dict[str, Any]:
    """The options a real routed call carries on this node at this window -
    the parts that decide WHICH runner answers (num_ctx, num_keep,
    num_thread). A load with anything else spawns a runner no call uses."""
    opts: Dict[str, Any] = {}
    if num_ctx:
        opts["num_ctx"] = int(num_ctx)
        try:
            k = _orch._ctx_keep_tokens(int(num_ctx))
            if k:
                opts["num_keep"] = k
        except Exception:
            pass
    try:
        nt = _orch._node_threads_core.threads_for(
            has_gpu=bool(inst.get("has_gpu")), node_num_thread=inst.get("num_thread"),
            default=_orch._CPU_NODE_THREADS)
        if nt:
            opts["num_thread"] = nt
    except Exception:
        pass
    return opts


# ── one pass ─────────────────────────────────────────────────────────────────
async def compute() -> Dict[str, Any]:
    """Everything a pass needs: config, scenario states, plan, residency,
    actions. No side effects on the nodes."""
    cfg = await load_config()
    insts = _instances()
    rules = _effective_rules()
    aliases = _aliases(rules)
    now = time.time()
    census = await _census_busy()
    prev = (await _load_state()).get("scenarios") or {}
    states = core.scenario_states(cfg.get("scenarios") or [], list(getattr(_orch, "ROUTE_DEMAND", []) or []),
                                  _inflight_by_job(), prev, now,
                                  blocked="a census goal is in flight" if census else "")
    sizes = await _sizes(insts)
    mem = await _memory(insts)
    gw = await _gpu_windows(insts, aliases["@default"])
    plan = core.plan(insts, cfg, rules, aliases, states, sizes, mem, gw)
    running = await _residency(insts)
    busy = core.busy_nodes(insts, getattr(_orch, "_LAST_PICKED", {}) or {}, now)
    for iid in _ACTING:
        busy.setdefault(iid, "the warmer is already working there")
    if census:
        for iid, inst in insts.items():
            if inst.get("has_gpu"):
                busy.setdefault(iid, "a census goal is in flight")
    embed_windows = {}
    acts = core.actions(plan, running, busy, now, embed_windows) if cfg.get("enabled", True) else []
    return {"config": cfg, "aliases": aliases, "states": states, "plan": plan, "prev": prev,
            "running": running, "busy": busy, "actions": acts, "census_busy": census,
            "enabled": bool(cfg.get("enabled", True)), "now": now}


def _publish_local(c: Dict[str, Any]) -> Dict[str, Any]:
    """What the request path and the picker read."""
    if not c.get("enabled"):
        _orch.WARM_STATE = {}
        return {}
    cfg = c["config"]
    insts = _instances()
    spill = set()
    for sc in cfg.get("scenarios") or []:
        if sc.get("spill") and (c["states"].get(sc.get("name")) or {}).get("active"):
            spill.update(str(j) for j in sc.get("job_types") or [])
    ws = {"planned": core.planned_pairs(c["plan"]),
          "embed_urls": {str(insts[i].get("url")): n["embed"] for i, n in c["plan"].items()
                         if n.get("embed") and i in insts},
          "spill_job_types": sorted(spill),
          "spill_min_tps": float(cfg.get("spill_min_tps") or 3.0),
          "spill_max_ctx": int(cfg.get("spill_max_ctx") or 8192),
          "at": c["now"]}
    _orch.WARM_STATE = ws
    return ws


async def _publish(c: Dict[str, Any]) -> None:
    ws = _publish_local(c)
    r = getattr(_orch, "COORD_REDIS", None) or _orch.REDIS
    try:
        if r is not None:
            await r.set(_PLAN_KEY, json.dumps(ws), ex=600)
    except Exception:
        pass


async def _post(url: str, body: Dict[str, Any], timeout: float = _TIMEOUT) -> Dict[str, Any]:
    hdr = _orch.vera_origin_header(job_type="warm", cap="ollama.warm")
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(url, json=body, headers=hdr)
            ok = r.status_code == 200
            return {"ok": ok, "status": r.status_code, "s": round(time.time() - t0, 1),
                    "error": "" if ok else r.text[:200]}
    except Exception as e:
        return {"ok": False, "s": round(time.time() - t0, 1), "error": f"{type(e).__name__}: {e}"}


async def act(a: Dict[str, Any]) -> Dict[str, Any]:
    """Carry out one action against its node's Ollama."""
    inst = _instances().get(a["node"]) or {}
    url = str(inst.get("url") or "")
    if not url:
        return {**a, "ok": False, "error": "node has no url"}
    _ACTING[a["node"]] = f"{a['action']} {a['model']}"
    try:
        if a["action"] == "release":
            res = await _post(f"{url}/api/generate", {"model": a["model"], "keep_alive": 0}, 60)
        elif a.get("embed"):
            body = {"model": a["model"], "input": "warm", "keep_alive": core.KEEP_FOREVER}
            opts = _options_for(a["node"], inst, 0)
            if opts:
                body["options"] = opts
            res = await _post(f"{url}/api/embed", body)
        else:
            # load or re-arm: no prompt (load only), the routed call's runner
            res = await _post(f"{url}/api/generate",
                              {"model": a["model"], "keep_alive": core.KEEP_FOREVER,
                               "options": _options_for(a["node"], inst, int(a.get("num_ctx") or 0))})
    finally:
        _ACTING.pop(a["node"], None)
    out = {**a, **res}
    await emit_event({"type": "ollama.warm", "node": a["node"], "action": a["action"],
                      "model": a["model"], "ok": res.get("ok"), "s": res.get("s")})
    return out


async def run_pass(execute: bool = True) -> Dict[str, Any]:
    c = await compute()
    await _save_state({"scenarios": {k: {"since": v.get("since"), "last_hot": v.get("last_hot")}
                                     for k, v in c["states"].items()}})
    await _publish(c)
    results = []
    if execute and c["actions"] and not _orch.is_dev_sandbox():
        # one action per node, nodes in parallel (a load on cpu-247 must not
        # wait for one on cpu-246)
        by_node: Dict[str, Dict[str, Any]] = {}
        for a in c["actions"]:
            by_node.setdefault(a["node"], a)
        results = list(await asyncio.gather(*[act(a) for a in by_node.values()]))
        _LAST.update(at=time.time(), actions=c["actions"], results=results)
    for name, st in c["states"].items():
        was = bool((c.get("prev") or {}).get(name, {}).get("since"))
        if bool(st.get("active")) != was:
            await emit_event({"type": "ollama.warm.scenario", "scenario": name,
                              "active": bool(st.get("active")), "why": st.get("why", "")})
    return {**c, "results": results}


async def _sandbox_refresh() -> None:
    """A dev sandbox never acts on prod's nodes, but its calls should land on
    the same runners - read the plan prod published."""
    r = getattr(_orch, "COORD_REDIS", None) or _orch.REDIS
    try:
        raw = await r.get(_PLAN_KEY) if r is not None else None
        _orch.WARM_STATE = json.loads(_s(raw)) if raw else {}
    except Exception:
        pass


async def _tick() -> None:
    if _orch.is_dev_sandbox():
        await _sandbox_refresh()
        return
    if _LOCK.locked():
        return
    r = _orch.REDIS
    token = _orch.new_id()
    try:
        # every prod-like process computes and publishes; only the lock holder
        # acts, so two orchestrators never load the same model twice
        leader = bool(r is not None and await r.set(_TICK_LOCK, token, nx=True, ex=int(core.TICK_S * 1.5)))
    except Exception:
        leader = False
    async with _LOCK:
        try:
            await run_pass(execute=leader)
        except Exception as e:
            log.warning("ollama.warm tick: %s", e)


schedule(_tick, core.TICK_S, name="ollama.warm.tick")


# ── capabilities ─────────────────────────────────────────────────────────────
def _view(c: Dict[str, Any]) -> Dict[str, Any]:
    now = c["now"]
    nodes = []
    for iid in sorted(set(c["plan"]) | set(c["running"])):
        p = c["plan"].get(iid) or {}
        rows = c["running"].get(iid)
        nodes.append({
            "node": iid, "class": p.get("class"), "slots": p.get("slots"),
            "source": p.get("source"), "scenarios": p.get("scenarios") or [],
            "planned": p.get("models") or [], "embed": p.get("embed") or "",
            "dropped": p.get("dropped") or [], "busy": c["busy"].get(iid, ""),
            "acting": _ACTING.get(iid, ""),
            "resident": None if rows is None else [
                {"model": r["name"], "num_ctx": r["context_length"],
                 "gb": round(r["size"] / 1e9, 1),
                 "expires_in_s": (None if r["expires_at_s"] - now > core.FOREVER_AFTER_S
                                  else int(r["expires_at_s"] - now)),
                 "pinned": r["expires_at_s"] - now > core.FOREVER_AFTER_S}
                for r in rows]})
    return {"enabled": c["enabled"], "census_busy": c["census_busy"], "nodes": nodes,
            "scenarios": [{**sc, "state": c["states"].get(sc.get("name"), {})}
                          for sc in c["config"].get("scenarios") or []],
            "aliases": c["aliases"], "actions": c["actions"],
            "config": {k: c["config"].get(k) for k in
                       ("enabled", "cpu_num_ctx", "num_ctx", "nodes", "class_defaults",
                        "embed_classes", "spill_min_tps", "spill_max_ctx")},
            "spill_job_types": (getattr(_orch, "WARM_STATE", {}) or {}).get("spill_job_types", []),
            "last": {"at": _LAST["at"], "results": _LAST["results"]}}


@capability("ollama.warm.status", memory="off", silent=True,
            http_method="GET", http_path="/ollama/warm/status", http_tags=["ollama", "nodes"],
            description="Warm model slots per Ollama node: slots (GPU 1, CPU 2 - embedders ride "
                        "beside them), the planned models and where they came from (node config, "
                        "the routing rules that point at the node, the class default), what is "
                        "resident now and whether it is pinned, what was dropped (memory, not on "
                        "the node) and why, the workload scenarios with their demand and state, "
                        "and the next actions. Read-only.")
async def cap_warm_status(trace_id=None) -> Dict[str, Any]:
    return _view(await compute())


@capability("ollama.warm.set", memory="off",
            http_method="POST", http_path="/ollama/warm/set", http_tags=["ollama", "nodes"],
            description="Change the warm-slot config. Inputs (all optional): enabled (bool), "
                        "cpu_num_ctx (int - the window planned models get on CPU nodes), "
                        "spill_min_tps (float), spill_max_ctx (int), num_ctx ({model: window}), "
                        "node + node_config ({models:[...] ('@default', '@naming', '@embed', "
                        "'@long_horizon' allowed; [] = back to routes), slots, embed, reserve, "
                        "ram_gb} - null removes the node's override), scenario (one scenario "
                        "{name, label, enabled, job_types, min_requests, window_s, min_inflight, "
                        "hold_s, fill ('all'|n), models {gpu, cpu, cpu_sibling}, spill} - upserted "
                        "by name), remove_scenario (name), scenario_enabled ({name: bool}). "
                        "Output: {ok, config}.")
async def cap_warm_set(enabled: Optional[bool] = None, cpu_num_ctx: Optional[int] = None,
                       spill_min_tps: Optional[float] = None, spill_max_ctx: Optional[int] = None,
                       num_ctx: Optional[Dict[str, int]] = None, node: str = "",
                       node_config: Optional[Dict[str, Any]] = None,
                       scenario: Optional[Dict[str, Any]] = None, remove_scenario: str = "",
                       scenario_enabled: Optional[Dict[str, bool]] = None,
                       trace_id=None) -> Dict[str, Any]:
    r = _orch.REDIS
    if r is None:
        return {"ok": False, "error": "redis not connected"}
    st = await _stored()
    if enabled is not None:
        st["enabled"] = bool(enabled)
    if cpu_num_ctx is not None:
        if int(cpu_num_ctx) < 2048:
            return {"ok": False, "error": "cpu_num_ctx must be at least 2048"}
        st["cpu_num_ctx"] = int(cpu_num_ctx)
    if spill_min_tps is not None:
        st["spill_min_tps"] = max(0.0, float(spill_min_tps))
    if spill_max_ctx is not None:
        st["spill_max_ctx"] = max(0, int(spill_max_ctx))
    if num_ctx is not None:
        st["num_ctx"] = {str(k): int(v) for k, v in (num_ctx or {}).items() if int(v or 0) > 0}
    if node:
        nodes = dict(st.get("nodes") or {})
        if node_config is None:
            nodes.pop(node, None)
        else:
            allowed = {"models", "slots", "embed", "reserve", "ram_gb"}
            bad = set(node_config) - allowed
            if bad:
                return {"ok": False, "error": f"unknown node_config keys: {sorted(bad)}"}
            nodes[node] = {k: v for k, v in node_config.items() if v is not None}
        st["nodes"] = nodes
    scs = list(st.get("scenarios") if "scenarios" in st else core.DEFAULT_CONFIG["scenarios"])
    if scenario:
        name = str(scenario.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "a scenario needs a name"}
        if scenario.get("fill", "all") != "all":
            try:
                int(scenario["fill"])
            except (TypeError, ValueError):
                return {"ok": False, "error": "fill must be 'all' or a number"}
        base = next((s for s in scs if s.get("name") == name), None) or {}
        merged = {**base, **scenario}
        scs = [s for s in scs if s.get("name") != name] + [merged]
        st["scenarios"] = scs
    if remove_scenario:
        st["scenarios"] = [s for s in scs if s.get("name") != remove_scenario]
        scs = st["scenarios"]
    if scenario_enabled:
        for s in scs:
            if s.get("name") in scenario_enabled:
                s["enabled"] = bool(scenario_enabled[s["name"]])
        st["scenarios"] = scs
    await r.set(core.REDIS_KEY, json.dumps(st))
    await emit_event({"type": "ollama.warm.config", "keys": sorted(st)})
    return {"ok": True, "config": core.merge_config(st)}


@capability("ollama.warm.apply", memory="off",
            http_method="POST", http_path="/ollama/warm/apply", http_tags=["ollama", "nodes"],
            description="Plan the warm slots and act now instead of waiting for the minute tick: "
                        "load what is absent (keep_alive -1 at the planned window - one load per "
                        "node, nodes in parallel), re-arm a planned model about to lapse, release "
                        "a model the warmer pinned that the plan dropped. Never touches a node "
                        "with a call in flight or one the router used moments ago, nor the GPU "
                        "while a census goal is in flight. Dry run by default; refused from a dev "
                        "sandbox. Input: dry_run (bool=true). Output: {plan view, results}.")
async def cap_warm_apply(dry_run: bool = True, trace_id=None) -> Dict[str, Any]:
    if not dry_run and _orch.is_dev_sandbox():
        return {"ok": False, "error": "this is a dev sandbox: the Ollama nodes are prod's"}
    async with _LOCK:
        c = await run_pass(execute=not dry_run)
    return {"ok": all(x.get("ok") for x in c["results"]) if c["results"] else True,
            "dry_run": bool(dry_run), **_view(c), "results": c["results"]}


# ── the pane: warm slots, workloads, routing, NLP and the catalog in one ─────
_EL = Path(__file__).resolve().parent / "node_models_element.js"


@APP.get("/ui/elements/node_models.js", include_in_schema=False)
async def _node_models_js():
    try:
        body = _EL.read_text(encoding="utf-8")
    except OSError:
        body = "console.error('node_models_element.js not found')"
    return Response(body, media_type="application/javascript")


@APP.get("/nodes/models/panel", include_in_schema=False)
async def _node_models_panel():
    return HTMLResponse("""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<script>(function(){try{var d=document.documentElement,S=window.localStorage;
var t=S.getItem('vera:ui:theme');if(t)d.setAttribute('data-theme',t);
var vf=S.getItem('vera:ui:themeVarsFor');if(t&&vf!==t)return;var v=JSON.parse(S.getItem('vera:ui:themeVars')||'null');
if(v)for(var k in v)d.style.setProperty(k,v[k]);}catch(e){}})();</script>
<title>Vera - Models &amp; NLP</title>
<style>:root{--bg:#0d0f12;--bg1:#14181d;--bg2:#1a1f26;--border:#232a33;--border2:#2e3742;--fg:#d8dde3;
--dim:#5f6975;--acc:#4a9eff;--acc2:#28c28a;--warn:#f5b341;--err:#ef5b5b}
html,body{margin:0;background:var(--bg0,var(--bg));color:var(--fg);min-height:100%}</style></head>
<body><vera-node-models></vera-node-models>
<script src="/ui/vera-ui.js"></script><script src="/ui/elements/node_models.js"></script></body></html>""")


register_ui(
    "node-models", "Models & NLP", "◈",
    """<div style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/nodes/models/panel" style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"></iframe>
</div>""",
    "",
    ui_caps=["ollama.warm.status", "ollama.warm.set", "ollama.warm.apply", "nlp.nodes",
             "nlp.config.get", "nlp.config.set", "fabric.nlp.get", "fabric.nlp.set",
             "specialist.status", "specialist.catalog", "specialist.install", "specialist.jobs",
             "specialist.store", "specialist.node_models", "provision.component.sync"],
    mode="element",
    tab_order=74,
)

log.info("ollama.warm ready - warm model slots")
