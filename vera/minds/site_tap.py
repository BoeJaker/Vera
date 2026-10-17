"""decide.sites.*: short-answer LLM call sites as applications of the candidate matrix (four-minds FM-2 / FM-12; see
site_tap_core).

capability_orchestration.ollama_generate hands every answer to tap() after it returns; tap() keeps the short ones in
a capped stream when the switch `decide.site_capture` is on (read here every minute, never on the call path). Each
night decide.shadow.sites finds the sites that behave like decisions and has the matrix's candidates answer a sample
of the same prompts, rotating site x candidate pairs on a budget; decide.sites.report is the table.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability, schedule

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import site_tap_core as SC, matrix_core as MX, judge_core as JC
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.minds import site_tap_core as SC, matrix_core as MX, judge_core as JC   # type: ignore
    from vera.minds import contract_core as _MC                                       # type: ignore

log = logging.getLogger("vera.minds.site_tap")
SWITCH = "decide.site_capture"
MODE: Dict[str, Any] = {"on": False, "at": 0.0}
MAX_ANSWER = SC.MAX_ANSWER
APPS_KEY = "vera:decide:sites:apps"
RESULTS_KEY = "vera:decide:sites:results"
LAST_KEY = "vera:decide:sites:last"
NIGHT_KEY = "vera:decide:sites:night:"
JOBS_PER_NIGHT = int(os.environ.get("VERA_DECIDE_SITES_JOBS", "24") or 24)
SAMPLES = 20
ZEROSHOT = "zeroshot:deberta-base"
_PENDING: set = set()
_STATS: Dict[str, int] = {"tapped": 0, "skipped": 0, "errors": 0}


async def _refresh_mode() -> None:
    try:
        eff = (CAPABILITY_REGISTRY.get("minds.effective") or {}).get("func")
        if eff:
            MODE.update(on=(((await eff()) or {}).get("modes") or {}).get(SWITCH) == "on", at=time.time())
    except Exception as e:
        log.debug("site capture switch: %s", e)


schedule(_refresh_mode, interval=60, name="decide_site_capture_mode", singleton=True, skip_in_sandbox=True)


def tap(caller: Dict[str, Any], stage: str, job_type: str, system: str, prompt: str, answer: str, model: str) -> None:
    """Keep one short answer (background, never raises, never waits)."""
    try:
        if not MODE["on"] or not SC.wanted(answer, job_type=job_type, stage=stage,
                                           module=str((caller or {}).get("caller_module") or "")):
            _STATS["skipped"] += 1
            return
        e = SC.entry(caller or {}, stage, system, prompt, answer, model)
        task = asyncio.get_running_loop().create_task(_write(e))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
    except Exception as e:
        _STATS["errors"] += 1
        log.debug("site tap: %s", e)


async def _write(e: Dict[str, str]) -> None:
    r = getattr(_orch, "REDIS", None)
    if r is None:
        return
    try:
        await r.xadd(SC.STREAM, e, maxlen=SC.STREAM_MAXLEN, approximate=True)
        _STATS["tapped"] += 1
    except Exception as ex:
        _STATS["errors"] += 1
        log.debug("site tap write: %s", ex)


def _d(x: Any) -> str:
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x)


async def _entries(count: int = SC.STREAM_MAXLEN) -> List[Dict[str, str]]:
    r = getattr(_orch, "REDIS", None)
    if r is None:
        return []
    rows = await r.xrevrange(SC.STREAM, count=count)
    return [{_d(k): _d(v) for k, v in f.items()} for _id, f in rows]


@capability(
    "decide.sites.discover", http_method="POST", http_path="/decide/sites/discover", http_tags=["minds"],
    contract=_MC.contract("decide.sites.discover", effects=["read", "write"], idempotency="idempotent"),
    memory="off", silent=True,
    description="Which LLM call sites in Vera behave like decisions (four-minds FM-2 / FM-12): from the captured short "
                "answers (switch decide.site_capture), the sites with >= 30 calls whose answers fall into 2-12 labels "
                "covering >= 90% of them - each an application of the candidate matrix. Output: {sites: {site: "
                "{labels, calls, coverage, module, func, stage}}, captured, mode}.",
)
async def cap_decide_sites_discover(trace_id=None) -> Dict[str, Any]:
    es = await _entries()
    apps = SC.discover(es)
    r = getattr(_orch, "REDIS", None)
    if r is not None:
        await r.set(APPS_KEY, json.dumps(apps), ex=7 * 86400)
    return {"sites": apps, "captured": len(es), "mode": "on" if MODE["on"] else "off", "process": dict(_STATS)}


async def _candidate_answer(cand: str, e: Dict[str, str], labels: List[str]) -> str:
    if cand == ZEROSHOT:
        zs = (CAPABILITY_REGISTRY.get("nlp.zeroshot") or {}).get("func")
        out = await zs(text=e.get("prompt", "")[-2000:], labels=labels, multi_label=False) if zs else {}
        return str((out or {}).get("top") or "")
    model = cand.split(":", 1)[1] if cand.startswith("llm:") else cand
    return await _orch.ollama_generate(e.get("prompt", ""), system=e.get("system", ""), model=model,
                                       job_type="idle_decide", think=False,
                                       options={"num_predict": 12, "temperature": 0}, timeout=120)


@capability(
    "decide.shadow.sites", http_method="POST", http_path="/decide/shadow/sites", http_tags=["minds"],
    memory="off",
    description="The call-site row of the candidate matrix (four-minds FM-2 / FM-12): for tonight's site x candidate "
                "pairs (rotated, never-scored first, up to `jobs`), each candidate - every small LLM on the nodes and "
                "zero-shot over the site's labels - answers the site's last `samples` captured prompts, and its "
                "agreement with the answers given live is kept (decide.sites.report). Background work (idle_decide). "
                "Inputs: jobs (int=24), samples (int=20). Output: {ok, pairs, sites}.",
)
async def cap_decide_shadow_sites(jobs: int = JOBS_PER_NIGHT, samples: int = SAMPLES, trace_id=None) -> Dict[str, Any]:
    disc = await cap_decide_sites_discover()
    apps = disc.get("sites") or {}
    if not apps:
        return {"ok": True, "pairs": 0, "sites": 0, "note": "no call site behaves like a decision yet"}
    try:
        from Vera.vera.minds import judge as J
    except Exception:                    # pragma: no cover
        from vera.minds import judge as J  # type: ignore
    cands = [f"llm:{m}" for m in await J.matrix_models()] + [ZEROSHOT]
    r = getattr(_orch, "REDIS", None)
    raw = await r.hgetall(LAST_KEY) if r is not None else {}
    last = {_d(k): _d(v) for k, v in (raw or {}).items()}
    pairs = MX.rotate([f"{s}||{c}" for s in apps for c in cands], last, int(jobs or JOBS_PER_NIGHT))
    es = await _entries()
    by_site: Dict[str, List[Dict[str, str]]] = {}
    for e in es:
        by_site.setdefault(e.get("site", ""), []).append(e)
    night = datetime.now().date().isoformat()
    done = 0
    for p in pairs:
        site, cand = p.split("||", 1)
        sample = by_site.get(site, [])[:max(1, int(samples or SAMPLES))]
        res = []
        for e in sample:
            t0 = time.monotonic()
            try:
                ans = await _candidate_answer(cand, e, apps[site]["labels"])
            except Exception as ex:
                log.debug("site candidate %s: %s", cand, ex)
                continue
            res.append((e.get("answer"), SC.pick_label(ans, apps[site]["labels"]) or "",
                        int((time.monotonic() - t0) * 1000)))
        if r is not None:
            await r.hset(RESULTS_KEY, p, json.dumps(dict(SC.agreement(res), night=night)))
            await r.hset(LAST_KEY, p, night)
        done += 1
    return {"ok": True, "pairs": done, "sites": len(apps)}


@capability(
    "decide.sites.report", http_method="GET", http_path="/decide/sites/report", http_tags=["minds"],
    contract=_MC.contract("decide.sites.report", effects=["read"], idempotency="idempotent"),
    memory="off", silent=True,
    description="The call-site matrix (four-minds FM-2 / FM-12): every LLM call site that behaves like a decision, and "
                "per candidate model its agreement with the answers given live on the same prompts, best first, with "
                "latency; the candidates not yet tried there. Agreement, not accuracy: how replaceable each call site "
                "is. Output: {sites: {site: {labels, calls, candidates, untested}}, mode, captured}.",
)
async def cap_decide_sites_report(trace_id=None) -> Dict[str, Any]:
    r = getattr(_orch, "REDIS", None)
    if r is None:
        return {"sites": {}, "error": "redis unavailable"}
    apps_raw = await r.get(APPS_KEY)
    apps = json.loads(apps_raw) if apps_raw else {}
    res = {_d(k): json.loads(_d(v)) for k, v in (await r.hgetall(RESULTS_KEY) or {}).items()}
    try:
        from Vera.vera.minds import judge as J
    except Exception:                    # pragma: no cover
        from vera.minds import judge as J  # type: ignore
    cands = [f"llm:{m}" for m in await J.matrix_models()] + [ZEROSHOT]
    out = {}
    for site, a in apps.items():
        rows = sorted(({"candidate": k.split("||", 1)[1], **v} for k, v in res.items() if k.startswith(site + "||")),
                      key=lambda x: -(x.get("agree") or 0))
        tried = {x["candidate"] for x in rows}
        out[site] = dict(a, candidates=rows, untested=[c for c in cands if c not in tried])
    try:
        captured = await r.xlen(SC.STREAM)
    except Exception:
        captured = None
    return {"sites": out, "mode": "on" if MODE["on"] else "off", "captured": captured, "process": dict(_STATS)}


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
    enq = (CAPABILITY_REGISTRY.get("background.cap.enqueue") or {})
    fn = enq.get("raw") or enq.get("func")
    if not fn:
        return
    try:
        res = await fn(name="decide.shadow.sites", title="call-site matrix: tonight's site x candidate pairs",
                       arguments={"jobs": JOBS_PER_NIGHT, "samples": SAMPLES}, timeout_s=3600,
                       id="sites-" + night, submitted_by="decide.shadow")
        if (res or {}).get("ok"):
            await r.set(NIGHT_KEY + night, "1", ex=86400)
    except Exception as e:
        log.warning("site matrix night: %s", e)


schedule(_night_job, interval=900, name="decide_site_matrix_night", singleton=True, skip_in_sandbox=True)
