"""output.claims.harvest: the claims of finished loop runs onto the ledger, for the candidate matrix (four-minds
FM-5.2; see claims_core).

Each night (in the judges' window, before their jobs run) the runs that finished in the last two days are read from
Redis - their `done` summary and their evidence tool results - and each checkable claim is recorded as an
`output.claim_supported` decision (the baseline word-overlap rule as its answer) with the claim and its evidence
passage in the training store. The nightly judges then answer every one in shadow. No run is read twice.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, schedule

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import test_activity as _TA
except Exception:                        # worktree / app-free import
    from vera.minds import test_activity as _TA   # type: ignore

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import claims_core as CL, ledger_core as LC, judge_core as JC
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.minds import claims_core as CL, ledger_core as LC, judge_core as JC   # type: ignore
    from vera.minds import contract_core as _MC                                     # type: ignore

log = logging.getLogger("vera.minds.claims")
DONE_KEY = "vera:claims:harvested:" + CL.EVIDENCE_VERSION   # session ids read with this evidence version
NIGHT_KEY = "vera:claims:night:"
_STATS: Dict[str, int] = {"runs": 0, "claims": 0, "errors": 0}


def _s(v: Any) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v or "")


async def _recent_runs(r, hours: int, limit: int) -> List[str]:
    """Session ids of loop runs that finished within `hours` and were not harvested yet, newest first."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    found = []
    async for key in r.scan_iter(match="vera:loop:history:run:*", count=500):
        k = _s(key)
        sid = k.rsplit(":", 1)[-1]
        try:
            if await r.sismember(DONE_KEY, sid):
                continue
            h = {_s(a): _s(b) for a, b in (await r.hgetall(k)).items()}
        except Exception:
            continue
        if h.get("status") != "done":
            continue
        try:
            ts = datetime.fromisoformat(h.get("updated_at", "").replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts >= since:
            found.append((ts, sid))
    found.sort(reverse=True)
    return [sid for _ts, sid in found[:max(1, int(limit))]]


async def harvest(hours: int = 48, max_runs: int = 40) -> Dict[str, Any]:
    r = getattr(_orch, "REDIS", None)
    if r is None:
        return {"ok": False, "error": "redis unavailable"}
    try:
        from Vera.vera.minds import ledger_capabilities as L
    except Exception:                    # pragma: no cover
        from vera.minds import ledger_capabilities as L  # type: ignore
    ts_mod = sys.modules.get("training_state") or sys.modules.get("Vera.vera.minds.training_state")
    runs = await _recent_runs(r, int(hours or 48), int(max_runs or 40))
    recorded = 0
    for sid in runs:
        try:
            raw = await r.lrange(f"vera:loop:events:{sid}", 0, -1)
            events = []
            for x in raw:
                try:
                    events.append(json.loads(x))
                except Exception:
                    continue
            for p in CL.pairs(events):
                row = LC.make_decision(
                    CL.QUESTION, CL.baseline(p), origin="background", surface="loop", qtype="binary",
                    labels=list(CL.LABELS), state={"claim": p["claim"], "url": p["url"]},
                    state_ref={"kind": "claim", "id": CL.claim_ref(sid, p["claim"])}, decider="rule",
                    implementation=CL.RULE, mode="rule", run_id=sid[:80], session_id=sid[:80],
                    detail={"overlap": p["overlap"], "evidence": p.get("version"), "sources": p.get("sources")})
                res = await L.append(row)
                if res.get("ok"):
                    recorded += 1
                    if ts_mod is not None:
                        await ts_mod.put_now(row, {"claim": p["claim"], "evidence": p["evidence"], "url": p["url"],
                                                   "title": p["title"], "overlap": p["overlap"]})
            await r.sadd(DONE_KEY, sid)
            _STATS["runs"] += 1
        except Exception as e:
            _STATS["errors"] += 1
            log.debug("claims harvest %s: %s", sid, e)
    _STATS["claims"] += recorded
    return {"ok": True, "runs": len(runs), "claims": recorded, "process": dict(_STATS)}


@capability(
    "output.claims.harvest", http_method="POST", http_path="/output/claims/harvest", http_tags=["minds"],
    contract=_MC.contract("output.claims.harvest", effects=["read", "write"], idempotency="idempotent"),
    memory="off",
    description="The claims of finished loop runs onto the ledger for the candidate matrix (four-minds FM-5.2): for "
                "each run that finished in the last `hours` and was not read before, every checkable sentence of its "
                "deliverable (a figure, date, version or name) paired with the evidence passage that shares most of "
                "its words, recorded as an output.claim_supported decision (baseline: word overlap >= 0.6) with the "
                "pair in the training store - every nightly judge then answers it in shadow. Runs nightly. Inputs: "
                "hours (int=48), max_runs (int=40). Output: {ok, runs, claims}.",
)
async def cap_output_claims_harvest(hours: int = 48, max_runs: int = 40, trace_id=None) -> Dict[str, Any]:
    async with _TA.declared("four-minds:claims-harvest", "claims harvest (FM-5.2)", ttl_s=1800):
        return await harvest(hours, max_runs)


async def _night_job() -> None:
    if getattr(_orch, "_IS_WORKER", False):
        return
    now = datetime.now()
    if not JC.in_window(now.hour):
        return
    r = getattr(_orch, "REDIS", None)
    night = (now.date() if now.hour >= 12 else now.date().fromordinal(now.date().toordinal() - 1)).isoformat()
    if r is None or await r.get(NIGHT_KEY + night):
        return
    try:
        out = await harvest()
        if out.get("ok"):
            await r.set(NIGHT_KEY + night, "1", ex=86400)
        log.info("claims harvest night %s: %s", night, out)
    except Exception as e:
        log.warning("claims harvest: %s", e)


# On the Loop Lab schedule, not an in-code timer (owner, 18 Oct 2026: "make sure you are making use of the schedule system and not creating ad-hoc timing rules"). Its entry calls output.claims.harvest.
