"""SearXNG self-healing (web-ingress §16): an active probe when nothing searches, a container restart for the
dead-network signature (rate-limited, cooled down, budgeted per day), and an escalation when restarts do not fix it.
Decisions are in searxng_heal_core (pure); detection is search_engines.searxng_status (unchanged).

Switch `web.searxng.self_heal`: 'off' (default) = probe and report only; 'on' = also restart and escalate.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional

import httpx

from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability, emit_event, schedule

try:
    from Vera.vera.web import search_engines as _engines
    from Vera.vera.web import searxng_heal_core as hc
except ImportError:                       # loaded from its file under a stand-in orchestration (tests)
    from vera.web import search_engines as _engines
    from vera.web import searxng_heal_core as hc

log = logging.getLogger("vera.searxng_heal")

CONTAINER = os.environ.get("VERA_SEARXNG_CONTAINER", "searxng")
SWITCH = "web.searxng.self_heal"
KEY = "vera:searxng:heal"
_STATE: Dict[str, Any] = {"last_probe": None, "heal": {}, "running": False}


def _has(name: str) -> bool:
    return bool((CAPABILITY_REGISTRY.get(name) or {}).get("func"))


async def _call(name: str, /, **kw) -> Dict[str, Any]:
    fn = (CAPABILITY_REGISTRY.get(name) or {}).get("func")
    if not fn:
        return {}
    r = await fn(**kw)
    return r if isinstance(r, dict) else {}


def _probe_host() -> str:
    """The SearXNG the searches use: research's configured source, else VERA_SEARXNG_URL, else web's default."""
    ra = sys.modules.get("researcher_api") or sys.modules.get("Vera.vera.research.researcher_api")
    try:
        src = next((s for s in getattr(ra, "sources", []) if getattr(s, "id", "") == "searxng"), None) if ra else None
        cfg = src.config if src is not None and isinstance(src.config, dict) else (
            json.loads(src.config) if src is not None and isinstance(src.config, str) and src.config else {})
        if cfg.get("host"):
            return str(cfg["host"]).rstrip("/")
    except Exception:  # noqa: BLE001
        pass
    wc = sys.modules.get("web_capabilities") or sys.modules.get("Vera.vera.web.web_capabilities")
    return (os.environ.get("VERA_SEARXNG_URL") or getattr(wc, "DEFAULT_SEARXNG", "") or "http://localhost:8088").rstrip("/")


async def probe() -> Dict[str, Any]:
    """One known query to SearXNG; the answer is folded into the same record organic searches feed."""
    host = _probe_host()
    body, err = None, ""
    for cand in (_engines.host_candidates(host) or [host]):
        try:
            async with httpx.AsyncClient(timeout=20.0, headers=_engines.SEARXNG_HEADERS) as c:
                r = await c.get(cand.rstrip("/") + "/search", params={"q": hc.PROBE_QUERY, "format": "json"})
            if r.status_code == 200:
                body = r.json()
                break
            err = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            err = str(e)[:160] or type(e).__name__
    _engines.note_searxng_answer(body, error=err)
    _STATE["last_probe"] = time.time()
    return _engines.searxng_status(_engines.SEARXNG_STATE)


async def _restart_container() -> Dict[str, Any]:
    try:
        p = await asyncio.create_subprocess_exec("docker", "restart", CONTAINER, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE)
        out, errb = await asyncio.wait_for(p.communicate(), timeout=90)
        return {"ok": p.returncode == 0, "rc": p.returncode, "err": (errb or b"").decode("utf-8", "replace")[:200]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": str(e)[:200]}


async def _switch_on() -> bool:
    try:
        eff = await _call("minds.effective")
        return str(((eff or {}).get("modes") or {}).get(SWITCH) or "off") == "on"
    except Exception:  # noqa: BLE001
        return False


async def _load_heal() -> Dict[str, Any]:
    try:
        import Vera.vera.capability_orchestration as _orch
        r = getattr(_orch, "REDIS", None)
        if r is not None:
            raw = await r.get(KEY)
            if raw:
                _STATE["heal"] = json.loads(raw)
    except Exception:  # noqa: BLE001
        pass
    return dict(_STATE["heal"] or {})


async def _save_heal(h: Dict[str, Any]) -> None:
    _STATE["heal"] = h
    try:
        import Vera.vera.capability_orchestration as _orch
        r = getattr(_orch, "REDIS", None)
        if r is not None:
            await r.set(KEY, json.dumps(h), ex=7 * 86400)
    except Exception:  # noqa: BLE001
        pass


async def heal_once(*, force_probe: bool = False, act: Optional[bool] = None) -> Dict[str, Any]:
    """Probe if due, decide, and (with the switch on, or act=True) restart or escalate. Returns what it saw and did."""
    now = time.time()
    checked = _engines.SEARXNG_STATE.get("checked_at")
    last_search = None
    if checked:
        try:
            from datetime import datetime
            last_search = datetime.fromisoformat(str(checked)).timestamp()
        except ValueError:
            last_search = None
    if force_probe or hc.should_probe(now, _STATE.get("last_probe"), last_search):
        status = await probe()
    else:
        status = _engines.searxng_status(_engines.SEARXNG_STATE)
    heal = await _load_heal()
    if hc.signature(status) in ("ok", "rate_limited", "degraded"):
        if heal.get("failed_streak") or heal.get("last_escalation"):
            await _save_heal(hc.on_healthy(heal))
        return {"status": status.get("state"), "signature": hc.signature(status), "action": "none"}
    d = hc.decide(now, status, heal)
    out = {"status": status.get("state"), "signature": hc.signature(status), "action": d["action"], "why": d["why"]}
    do_it = (await _switch_on()) if act is None else bool(act)
    if not do_it or d["action"] == "none":
        out["acted"] = False
        return out
    if d["action"] == "restart":
        rr = await _restart_container()
        await asyncio.sleep(15)
        after = await probe()
        recovered = hc.signature(after) not in ("dead_network", "unreachable") and int(after.get("last_count") or 0) > 0
        await _save_heal(hc.after_restart(heal, now, recovered, note=d["why"] + ("" if rr.get("ok") else f"; restart failed: {rr.get('err')}")))
        out.update(acted=True, restart=rr, recovered=recovered, after=after.get("state"))
        await emit_event({"type": "web.searxng.healed" if recovered else "web.searxng.heal_failed", "why": d["why"],
                          "restart_ok": rr.get("ok"), "recovered": recovered})
        log.warning("searxng self-heal: restarted %s (%s) -> %s", CONTAINER, d["why"], "recovered" if recovered else "STILL DOWN")
    elif d["action"] == "escalate":
        await _save_heal(hc.after_escalation(heal, now, d["why"]))
        msg = f"SearXNG is still down after restarts: {d['why']}. Web search is falling back to DuckDuckGo."
        if _has("tg.notify"):
            try:
                await _call("tg.notify", text=msg, idempotency_key=f"searxng-escalate-{int(now // hc.ESCALATE_EVERY_S)}")
            except Exception as e:  # noqa: BLE001
                log.debug("searxng escalate notify: %s", e)
        await emit_event({"type": "web.searxng.escalated", "why": d["why"]})
        log.warning("searxng self-heal: escalated (%s)", d["why"])
        out["acted"] = True
    return out


async def _tick() -> None:
    if _STATE["running"]:
        return
    _STATE["running"] = True
    try:
        await heal_once()
    except Exception as e:  # noqa: BLE001
        log.debug("searxng heal tick: %s", e)
    finally:
        _STATE["running"] = False


schedule(_tick, hc.PROBE_EVERY_S, name="web.searxng.heal", skip_in_sandbox=True, singleton=True)


@capability("web.searxng.heal.status", http_method="GET", http_path="/web/searxng/heal", http_tags=["web", "search"],
            memory="off", silent=True,
            description="SearXNG's health and the self-healer's record: state (ok | degraded | down | unknown), the "
                        "signature (dead_network | unreachable | rate_limited | degraded | ok), what the healer would do "
                        "now and why, recent restarts and escalations, and whether the switch web.searxng.self_heal is on. "
                        "Input: probe_now (bool: ask SearXNG now). Output: {status, signature, would, heal, switch}.")
async def cap_heal_status(probe_now: bool = False, trace_id=None):
    status = await probe() if probe_now else _engines.searxng_status(_engines.SEARXNG_STATE)
    heal = await _load_heal()
    return {"status": status, "signature": hc.signature(status), "would": hc.decide(time.time(), status, heal),
            "heal": heal, "switch": "on" if await _switch_on() else "off", "container": CONTAINER, "host": _probe_host()}


@capability("web.searxng.heal.run", http_method="POST", http_path="/web/searxng/heal/run", http_tags=["web", "search"],
            memory="off",
            description="Run the SearXNG self-healer once now: probe, decide, and restart / escalate if needed. "
                        "Input: act (bool, default false: report what it would do without restarting; true = act "
                        "whatever the switch says). Output: {status, signature, action, why, acted, recovered}.")
async def cap_heal_run(act: bool = False, trace_id=None):
    return await heal_once(force_probe=True, act=bool(act))
