"""Models and Performance in Loop Lab (owner, 18 Oct 2026).

"id like to be able to see the benchmarks and kpis of models somewhere in the loop lab and a holistic view of
performance across the loop lab and veras normal use."

Two reads for the panel's Models and Performance sections. Each gathers capabilities that already exist and hands
their outputs to models_perf_core, which does all the shaping (and is tested on its own). Nothing here writes.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability

try:
    from Vera.vera.evolve import models_perf_core as core
except ImportError:                                        # pragma: no cover
    from vera.evolve import models_perf_core as core       # type: ignore

log = logging.getLogger("vera.evolve.models_perf")


async def _call(cap_name: str, /, **kw) -> Any:
    cap = CAPABILITY_REGISTRY.get(cap_name)
    if not cap or not cap.get("func"):
        return {"error": f"capability not available: {cap_name}"}
    try:
        return await cap["func"](**kw)
    except Exception as e:
        log.debug("models/perf: %s failed: %s", cap_name, e)
        return {"error": f"{cap_name}: {e}"}


def _d(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


@capability("evolve.models.view", memory="off", silent=True,
            http_method="GET", http_path="/evolve/models/view", http_tags=["evolve", "models"],
            description="Every model Vera uses, in one read (Loop Lab Models section): per model and node the live "
                        "numbers from real traffic (calls, tokens a second, wall per call, jobs served, warm now) beside "
                        "its newest benchmark; a job type x model matrix; every benchmark run (a run where no item "
                        "passed is reported as broken, never as 0%); the students scored in shadow per question with "
                        "the leader marked; nodes; and issues (stale or broken benchmarks). Input: bench_limit "
                        "(int=60). Output: {models, matrix, benchmarks, students, nodes, issues, summary, sources}.")
async def cap_models_view(bench_limit: int = 60, trace_id=None) -> Dict[str, Any]:
    status = _d(await _call("decide.student.status"))
    questions = [q for q in status.get("questions") or [] if isinstance(q, str)][:16]
    passive, route, bench, nodes, *reports = await asyncio.gather(
        _call("bench.passive"), _call("ollama.route_stats"),
        _call("bench.results", limit=max(1, min(int(bench_limit or 60), 200))), _call("bench.node_perf"),
        *[_call("decide.shadow.report", question_id=q) for q in questions])
    out = core.models_view(_d(passive), _d(route), _d(bench), _d(nodes),
                           {q: _d(r) for q, r in zip(questions, reports)})
    out["errors"] = [str(x.get("error")) for x in (passive, route, bench, nodes) if isinstance(x, dict) and x.get("error")]
    out["student_switch"] = {"enabled": status.get("enabled"), "window": status.get("window"), "guard": status.get("guard")}
    return out


@capability("evolve.performance.view", memory="off", silent=True,
            http_method="GET", http_path="/evolve/performance/view", http_tags=["evolve", "performance"],
            description="How well things run, Loop Lab and Vera's normal use side by side (Loop Lab Performance "
                        "section). Loop Lab: census goals done, quality and wall per goal for the last `days` against "
                        "the days before, with a daily series (census.runs, full runs only); gates run, passed and "
                        "their time, adopt-to-prod lead time, changes waiting on bleeding-edge (evolve.releases.view); "
                        "the background queue's depth, oldest wait and jobs that wait for quiet "
                        "(background.status). Vera use: chat reply time, tokens and replies (ollama.route_stats), "
                        "the chat test corpus (chat.corpus.last), services up (obs.health). Also who uses the models, "
                        "and broken measures (a corpus where every case errored, a benchmark where nothing passed). "
                        "Every block names its source. Input: days (int=7). Output: {days, at, loop_lab, vera_use, "
                        "share, broken}.")
async def cap_performance_view(days: int = 7, trace_id=None) -> Dict[str, Any]:
    days = max(1, min(int(days or 7), 30))
    census, rel, bg, route, corpus, health, bench = await asyncio.gather(
        _call("census.runs"), _call("evolve.releases.view", days=days), _call("background.status"),
        _call("ollama.route_stats"), _call("chat.corpus.last"), _call("obs.health"), _call("bench.results", limit=60))
    out = core.performance_view(_d(census), _d(rel), _d(bg), _d(route), _d(corpus), _d(health), days=days,
                                bench=_d(bench))
    out["errors"] = [str(x.get("error")) for x in (census, rel, bg, route, corpus, health)
                     if isinstance(x, dict) and x.get("error")]
    return out
