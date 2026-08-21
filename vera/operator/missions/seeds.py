"""seeds.py — representative-data fixtures for the documentation mission.

Before a panel is screenshotted, its domain's seed (if any) populates the target
Vera with a little representative data so the panel renders *populated* rather
than empty. Seeds call capabilities on the **target** (sandbox or live) over its
``/mcp/call`` endpoint via the injected ``call_target`` coroutine, and write only
to that target's isolated state.

Seeds are deliberately **light and defensive** for now (the user's plan: seeded
fixtures today, scripted live scenarios once Vera's own flows are complete):
every call is best-effort — a missing/renamed cap just no-ops with a note, never
failing the capture. Each returns ``{ok, notes:[...]}``.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List

log = logging.getLogger("vera.operator.seeds")

CallTarget = Callable[..., Awaitable[Any]]


async def _try(call_target: CallTarget, notes: List[str], name: str, **args) -> Any:
    """Call a cap on the target; record a short outcome note; never raise."""
    try:
        res = await call_target(name, args)
    except Exception as e:  # pragma: no cover - network dependent
        notes.append(f"{name}: exception {e}")
        return {"error": str(e)}
    if isinstance(res, dict) and res.get("error"):
        notes.append(f"{name}: {str(res['error'])[:70]}")
    else:
        notes.append(f"{name}: ok")
    return res


async def seed_markets(call_target: CallTarget) -> Dict[str, Any]:
    notes: List[str] = []
    # Pull a little OHLCV so charts/datasets render; list datasets to populate UI.
    await _try(call_target, notes, "markets.datasets")
    await _try(call_target, notes, "markets.ingest.ohlcv",
               exchange="kraken", symbol="BTC/USD", timeframe="1h", limit=200)
    return {"ok": True, "notes": notes}


async def seed_dream(call_target: CallTarget) -> Dict[str, Any]:
    notes: List[str] = []
    # A project + goal makes the Dream boards non-empty without starting a cycle.
    await _try(call_target, notes, "project.create",
               name="Demo Project", description="Sample project for documentation.")
    await _try(call_target, notes, "dream.status")
    return {"ok": True, "notes": notes}


async def seed_dag(call_target: CallTarget) -> Dict[str, Any]:
    notes: List[str] = []
    await _try(call_target, notes, "loops.profiles")
    await _try(call_target, notes, "dag.list")
    return {"ok": True, "notes": notes}


async def seed_memory(call_target: CallTarget) -> Dict[str, Any]:
    notes: List[str] = []
    for i, txt in enumerate([
        "Vera is a capability-first agent operating system.",
        "The operator drives web UIs by observe→think→act.",
    ]):
        await _try(call_target, notes, "memory.write", text=txt, kind="note")
    await _try(call_target, notes, "memory.seek", query="operator", max_chars=400)
    return {"ok": True, "notes": notes}


async def seed_fabric(call_target: CallTarget) -> Dict[str, Any]:
    notes: List[str] = []
    import json
    fixtures = {
        "vera.docs": [
            {"title": "Capability framework", "kind": "documentation", "status": "published"},
            {"title": "Operator missions", "kind": "documentation", "status": "published"}],
        "vera.runtime": [
            {"service": "operator", "role": "browser automation", "health": "ready"},
            {"service": "fabric", "role": "knowledge substrate", "health": "ready"}],
        "vera.agents": [
            {"agent": "author", "role": "implementation"},
            {"agent": "reviewer", "role": "quality gate"}],
    }
    for dataset_id, records in fixtures.items():
        await _try(call_target, notes, "fabric.ingest", dataset_id=dataset_id,
                   records=json.dumps(records), source="documentation-fixture",
                   tags="vera,documentation")
    await _try(call_target, notes, "fabric.link_datasets",
               from_id="vera.docs", to_id="vera.runtime", rel_type="DESCRIBES")
    await _try(call_target, notes, "fabric.link_datasets",
               from_id="vera.agents", to_id="vera.runtime", rel_type="OPERATES")
    await _try(call_target, notes, "fabric.datasets")
    await _try(call_target, notes, "fabric.query", text="vera", top_k=5)
    return {"ok": True, "notes": notes}


async def seed_chat(call_target: CallTarget) -> Dict[str, Any]:
    notes: List[str] = []
    # List sessions so the chat panel has history to show (no LLM call — cheap).
    await _try(call_target, notes, "chat.sessions")
    return {"ok": True, "notes": notes}


# domain slug (from domain_map.seed) → seed coroutine
SEEDS: Dict[str, Callable[[CallTarget], Awaitable[Dict[str, Any]]]] = {
    "markets": seed_markets,
    "dream": seed_dream,
    "dag": seed_dag,
    "memory": seed_memory,
    "fabric": seed_fabric,
    "chat": seed_chat,
}


async def run_seed(name: str, call_target: CallTarget) -> Dict[str, Any]:
    fn = SEEDS.get(name or "")
    if not fn:
        return {"ok": True, "skipped": True, "notes": []}
    try:
        return await fn(call_target)
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": str(e), "notes": []}
