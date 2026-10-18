"""decide.tests.status and the Loop Lab Tests page (/decide/tests): four-minds' tests in one view (see tests_core).

The schedule (entries, next windows, run history) says whether each test ran and how it ended; each test's own report
says what it found - read with a timeout and cached for five minutes, since some (the candidate matrix) are heavy.
Read-only: the page has no controls (a test is run, stopped or changed from the Schedule page).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, CAPABILITY_REGISTRY, capability

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import tests_core as TC
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.minds import tests_core as TC          # type: ignore
    from vera.minds import contract_core as _MC      # type: ignore

log = logging.getLogger("vera.minds.tests_monitor")
_HERE = Path(__file__).resolve().parent
HEADLINE_TTL_S = 300
CALL_TIMEOUT_S = 20.0
_CACHE: Dict[str, Any] = {"at": 0.0, "headlines": {}}


async def _call(name: str, **kw) -> Dict[str, Any]:
    cap = CAPABILITY_REGISTRY.get(name) or {}
    fn = cap.get("raw") or cap.get("func")
    if not fn:
        return {"error": f"{name} not loaded"}
    try:
        out = await asyncio.wait_for(fn(**kw), CALL_TIMEOUT_S)
        return out if isinstance(out, dict) else {"value": out}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}


async def _h_matrix() -> Dict[str, Any]:
    d = await _call("decide.matrix.report", days=7)
    if d.get("error"):
        return d
    best = {}
    for q, a in (d.get("applications") or {}).items():
        c = next((x for x in a.get("candidates") or [] if (x.get("outcomes_n") or 0) >= 30), None)
        if c:
            best[q] = f"{c['candidate']} {c.get('accuracy')} (live {((a.get('live') or {}).get('accuracy'))})"
    return {"pairs_scored": d.get("pairs_scored"), "pairs_untested": d.get("pairs_untested"),
            "applications": len(d.get("applications") or {}), "best_with_outcomes": best}


async def _h_shortlist() -> Dict[str, Any]:
    d = await _call("caps.shortlist.replay", history=1)
    snap = d.get("latest") or {}
    loop, chat = snap.get("loop") or {}, snap.get("chat") or {}
    return {"night": snap.get("night"), "index_rows": snap.get("index_rows"), "loop_n": loop.get("n"),
            "loop_hit@5": loop.get("hit@5"), "loop_recall@10": loop.get("recall@10"), "loop_mrr": loop.get("mrr"),
            "chat_turns_with_use": chat.get("turns_with_use"), "chat_unoffered_rate": chat.get("unoffered_rate"),
            "error": d.get("error")}


async def _h_memory() -> Dict[str, Any]:
    d = await _call("memory.perf.rows", kind="probe", days=14, limit=1)
    rows = d.get("rows") or []
    if not rows:
        return {"probe_rows": 0, "error": d.get("error")}
    r = rows[0]
    seek = (r.get("paths") or {}).get("memory.seek") or {}
    return {"at": r.get("ts"), "sha": r.get("sha_short"), "seek_p50_ms": (seek.get("total") or {}).get("p50"),
            "seek_p95_ms": (seek.get("total") or {}).get("p95"),
            "quality": (r.get("extra") or {}).get("quality")}


async def _h_chat_corpus() -> Dict[str, Any]:
    d = await _call("chat.corpus.last")
    # objects, not repr strings (vera-44, 18 Oct); broken = every case errored - a broken measure, never a 0% score
    if not isinstance(d, dict) or d.get("error") and not d.get("summary"):
        return {"error": (d or {}).get("error") if isinstance(d, dict) else "no answer"}
    summ = d.get("summary") or {}
    status = summ.get("status") or {}
    cases = summ.get("cases") or 0
    return {"run": d.get("run") or {}, "running": d.get("running"),
            "cases": cases, "status": status, "pass_rate": summ.get("pass_rate"), "ttft_ms": summ.get("ttft_ms"),
            "failed_n": len(summ.get("failed") or []), "errors_n": len(summ.get("errors") or []),
            "broken": bool(cases) and status.get("error", 0) == cases}


async def _h_claims() -> Dict[str, Any]:
    d = await _call("decide.ledger.stats")
    q = next((x for x in d.get("questions") or [] if x.get("question_id") == "output.claim_supported"), {})
    return {"decisions": q.get("decisions", 0), "with_outcome": q.get("with_outcome", 0), "error": d.get("error")}


async def _h_calibration() -> Dict[str, Any]:
    d = await _call("decide.calibration.history")
    h = (d.get("weeks") or [None])[0] or {}
    return {"week": h.get("week"), "alerts": len(h.get("alerts") or []), "error": d.get("error")}


async def _h_cascade() -> Dict[str, Any]:
    r = getattr(_orch, "REDIS", None)
    try:
        raw = await r.get("vera:decide:cascade:plan") if r is not None else None
        plan = json.loads(raw) if raw else {}
    except Exception as e:
        return {"error": str(e)[:120]}
    return {"eligible": {q: f"{p.get('impl')} >= {p.get('tau')}" for q, p in plan.items() if p.get("eligible")},
            "questions": len(plan)}


HEADLINES: Dict[str, Callable[[], Awaitable[Dict[str, Any]]]] = {
    "matrix": _h_matrix, "shortlist": _h_shortlist, "memory": _h_memory, "chat_corpus": _h_chat_corpus,
    "claims": _h_claims, "calibration": _h_calibration, "cascade": _h_cascade}


async def headlines(force: bool = False) -> Dict[str, Any]:
    if not force and time.time() - _CACHE["at"] < HEADLINE_TTL_S and _CACHE["headlines"]:
        return _CACHE["headlines"]
    keys = list(HEADLINES)
    vals = await asyncio.gather(*(HEADLINES[k]() for k in keys), return_exceptions=True)
    out = {k: (v if isinstance(v, dict) else {"error": str(v)[:120]}) for k, v in zip(keys, vals)}
    _CACHE.update(at=time.time(), headlines=out)
    return out


@capability(
    "decide.tests.status", http_method="GET", http_path="/decide/tests/status", http_tags=["minds", "evolve"],
    contract=_MC.contract("decide.tests.status", effects=["read"], idempotency="idempotent"),
    memory="off", silent=True,
    description="Four-minds' tests as the Loop Lab Tests page shows them: per test (candidate matrix, claims harvest, "
                "tool-choice replay, precedent index, cascade plan, calibration, memory census, chat corpus, census "
                "arms) its schedule entries with next fire, last result, recover / protect, its health (failed, "
                "interrupted, missed, recovering, running, never, ok, disabled), its last runs, and its report's "
                "headline (cached 5 min). Input: refresh (bool - re-read the headlines now). Output: {tests, counts, "
                "at, headlines_at}.",
)
async def cap_decide_tests_status(refresh: bool = False, trace_id=None) -> Dict[str, Any]:
    lst = await _call("evolve.schedule.list")
    hist = await _call("evolve.schedule.history", limit=300)
    heads = await headlines(force=bool(refresh))
    out = TC.status(lst.get("schedules") or [], hist.get("runs") or [], lst.get("next") or {},
                    datetime.now(timezone.utc), heads)
    out["headlines_at"] = datetime.fromtimestamp(_CACHE["at"], timezone.utc).isoformat(timespec="seconds") \
        if _CACHE["at"] else None
    if lst.get("error") or hist.get("error"):
        out["error"] = lst.get("error") or hist.get("error")
    return out


@APP.get("/decide/tests", include_in_schema=False)
async def _tests_page():
    from fastapi.responses import HTMLResponse
    p = _HERE / "tests_panel.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<pre>tests_panel.html not found</pre>", status_code=404)
