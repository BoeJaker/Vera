"""Models and Performance in Loop Lab (owner, 18 Oct 2026) - pure shaping, no I/O.

"id like to be able to see the benchmarks and kpis of models somewhere in the loop lab and a holistic view of
performance across the loop lab and veras normal use."

The numbers already exist in five places that had no Loop Lab view: bench.passive (every model call Vera made, per
model and node), ollama.route_stats (moving averages per model, node and job type), bench.results (the benchmark
runs), decide.shadow.report (the students scored in shadow) and, for Performance, census.runs, the releases view,
background.status, chat.corpus.last and obs.health. This module turns their outputs into the two sections' rows;
models_perf_capabilities.py does the reads.

Two rules the sections keep, and this module encodes:
  * a broken measure is not a score - a benchmark where nothing passed, or a chat corpus where every case
    errored, is reported as `broken` with the reason, never as 0%;
  * every KPI names the capability it comes from (`source`), so a number can be checked.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

BENCH_STALE_DAYS = 14        # a benchmark older than this no longer describes the models in use
CORPUS_STALE_DAYS = 8        # the chat corpus runs weekly (Sun 14:00)
MIN_OUTCOMES = 20            # a student's accuracy against real outcomes means little on fewer
STALE_MODEL_DAYS = 7         # a (model, node) not called for this long is history, not a live path - 18 Oct:
                             # qwen2.5-coder:14b (457 s a call, last used 10 Sep) read as the slowest live path


def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _ts(v: Any) -> Optional[datetime]:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), tz=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _age_days(v: Any, now: datetime) -> Optional[float]:
    d = _ts(v)
    return round((now - d).total_seconds() / 86400, 1) if d else None


def _median(xs: Iterable[Optional[float]]) -> Optional[float]:
    v = [x for x in xs if x is not None]
    return statistics.median(v) if v else None


def job_group(job_type: Any) -> str:
    """Who a model call was for: the Loop Lab, the owner's chat, background work, or other."""
    j = str(job_type or "")
    if j.startswith("loop_"):
        return "loop"
    if j == "chat":
        return "chat"
    if j.startswith("idle_") or j in ("plan_enrich", "dream"):
        return "background"
    return "other"


def _key(model: Any, node: Any) -> str:
    return "%s@%s" % (model or "?", node or "?")


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def four_minds_view(ledger: Dict[str, Any], ledger_stats: Dict[str, Any],
                    sense: Dict[str, Any], recovery_eval: Dict[str, Any]) -> Dict[str, Any]:
    """Payload-free operator projection of the current decision evidence."""
    rows = []
    for decision in list((ledger or {}).get("decisions") or [])[:30]:
        if not isinstance(decision, dict):
            continue
        outcome = decision.get("outcome") if isinstance(decision.get("outcome"), dict) else None
        missing = ["route evidence", "context evidence"]
        if outcome is None:
            missing.append("outcome")
        if decision.get("decider") in ("student", "llm") and not decision.get("package"):
            missing.append("model package")
        if decision.get("decider") == "student" and not decision.get("node"):
            missing.append("compute node")
        rows.append({
            "decision_id": str(decision.get("decision_id") or ""),
            "at": str(decision.get("ts") or ""),
            "question": str(decision.get("question_id") or ""),
            "surface": str(decision.get("surface") or ""),
            "origin": str(decision.get("origin") or ""),
            "decider": str(decision.get("decider") or ""),
            "implementation": str(decision.get("implementation") or ""),
            "package": str(decision.get("package") or ""),
            "node": str(decision.get("node") or ""),
            "mode": str(decision.get("mode") or ""),
            "minds_digest": str(decision.get("minds_digest") or ""),
            "run_id": str(decision.get("run_id") or ""),
            "session_id": str(decision.get("session_id") or ""),
            "decision_ms": _f(decision.get("ms")),
            "answer_digest": _digest(decision.get("answer")),
            "state_digest": str(decision.get("state_hash") or ""),
            "outcome": ({
                "at": str(outcome.get("ts") or ""),
                "source": str(outcome.get("source") or ""),
                "strength": str(outcome.get("strength") or ""),
                "label_digest": _digest(outcome.get("label")),
            } if outcome else None),
            "missing": missing,
        })
    questions = list((ledger_stats or {}).get("questions") or [])
    decisions = sum(int(row.get("decisions") or 0) for row in questions)
    with_outcome = sum(int(row.get("with_outcome") or 0) for row in questions)
    evaluation = recovery_eval if isinstance(recovery_eval, dict) else {}
    metrics = evaluation.get("metrics") if isinstance(evaluation.get("metrics"), dict) else {}
    required_metrics = (
        "regression_detection_rate", "evidence_recovery_rate",
        "rollback_identity_rate", "rollback_completion_rate",
    )
    evaluation_ok = bool(
        evaluation.get("evidence_scope") == "frozen_fixture_conformance" and
        evaluation.get("decision") == "insufficient_for_production" and
        all(not isinstance(metrics.get(key), bool) and
            isinstance(metrics.get(key), (int, float)) and
            float(metrics[key]) == 1.0 for key in required_metrics))
    sense_stats = (sense or {}).get("stats") if isinstance((sense or {}).get("stats"), dict) else {}
    return {
        "native_owner": "decision ledger",
        "runtime_join_status": "not produced",
        "decisions": decisions,
        "with_outcome": with_outcome,
        "outcome_coverage": round(with_outcome / decisions, 3) if decisions else None,
        "recent": rows,
        "recent_count": len(rows),
        "sense": {
            "calls": int(sense_stats.get("calls") or 0),
            "cached": int(sense_stats.get("cached") or 0),
            "degraded": int(sense_stats.get("degraded") or 0),
            "cards_version": str((sense or {}).get("cards_version") or ""),
            "cards_embedded": bool((sense or {}).get("cards_embedded")),
        },
        "gaps": [
            "Route revision, deployment and portable package evidence are not stored with ledger decisions.",
            "Context and JEPA evidence identities are not joined to the exact decision input.",
            "Sense retains card version and health, but not exact provider, deployment and package identity.",
            "The joined envelope is available as a pure contract but no runtime producer persists it yet.",
        ],
        "frozen_evaluation": {
            "evaluation_id": str(evaluation.get("evaluation_id") or ""),
            "case_count": int(evaluation.get("case_count") or 0),
            "metrics": dict(evaluation.get("metrics") or {}),
            "conformant": evaluation_ok,
            "scope": str(evaluation.get("evidence_scope") or ""),
            "recovery_scope": str(evaluation.get("recovery_scope") or ""),
            "rollback_scope": str(evaluation.get("rollback_scope") or ""),
            "production_ready": bool(evaluation.get("production_ready")),
            "routes_executed": bool(evaluation.get("routes_executed")),
            "rollback_executed": bool(evaluation.get("rollback_executed")),
        },
        "authority_changed": False,
        "controls": False,
        "sources": ["decide.ledger", "decide.ledger.stats", "sense.stats",
                    "frozen Four Minds evidence evaluation"],
    }


# ── Models ─────────────────────────────────────────────────────────────────────────────────────────────────
def bench_row(r: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    perf = r.get("perf") or {}
    total, passed = int(r.get("items_total") or 0), int(r.get("items_passed") or 0)
    broken = ""
    if total and not passed:
        broken = "no item passed (0 of %d) - the grader or the answer format is at fault, not a model score" % total
    return {"id": r.get("id") or "", "at": r.get("created_at") or "", "age_days": _age_days(r.get("created_at"), now),
            "model": r.get("model") or "", "node": r.get("instance_id") or "", "roles": list(r.get("roles") or []),
            "accuracy": None if broken else _f(r.get("accuracy")), "passed": passed, "total": total,
            "gen_tps": _f(perf.get("gen_tps")), "prompt_tps": _f(perf.get("prompt_tps")),
            "ttft_ms": _f(perf.get("ttft_ms")), "load_ms": _f(perf.get("load_ms")),
            "size_gb": _f(r.get("size_gb")), "quant": r.get("quant") or "", "params": r.get("params") or "",
            "broken": broken}


def warm_set(node_perf: Dict[str, Any]) -> Dict[str, List[str]]:
    """{node: [models loaded now]} from bench.node_perf."""
    out: Dict[str, List[str]] = {}
    for n in (node_perf or {}).get("nodes") or []:
        out[str(n.get("id") or "")] = [str(m.get("model") or m.get("name") or "") for m in n.get("loaded") or []]
    return out


def _same_model(a: str, b: str) -> bool:
    # ollama reports "x:latest" for a model routed as "x"
    return a == b or a.replace(":latest", "") == b.replace(":latest", "")


def model_rows(passive: Iterable[Dict[str, Any]], route: Iterable[Dict[str, Any]],
               bench: Iterable[Dict[str, Any]], warm: Dict[str, List[str]], now: datetime) -> List[Dict[str, Any]]:
    """One row per (model, node) that served calls: live numbers beside the newest benchmark of that pair."""
    jobs: Dict[str, List[Dict[str, Any]]] = {}
    for s in route or []:
        jobs.setdefault(_key(s.get("model"), s.get("instance")), []).append(
            {"job": s.get("job_type") or "", "group": job_group(s.get("job_type")), "n": int(s.get("n") or 0),
             "wall_s": _f(s.get("ema_elapsed_s")), "tps": _f(s.get("ema_tps")), "tokens": _f(s.get("ema_tokens")),
             "last": s.get("last_ts") or ""})
    newest: Dict[str, Dict[str, Any]] = {}
    for b in bench or []:
        k = _key(b.get("model"), b.get("instance_id"))
        if k not in newest or str(b.get("created_at") or "") > str(newest[k].get("created_at") or ""):
            newest[k] = b
    rows = []
    for p in passive or []:
        k = _key(p.get("model"), p.get("instance_id"))
        node = p.get("instance_id") or ""
        b = newest.get(k)
        age = _age_days(p.get("last_ts"), now)
        rows.append({"key": k, "model": p.get("model") or "", "node": node, "node_label": p.get("node_label") or "",
                     "gpu": bool(p.get("has_gpu")), "calls": int(p.get("calls") or 0), "tps": _f(p.get("obs_tps")),
                     "wall_s": _f(p.get("avg_latency_s")), "prompt_chars": _f(p.get("avg_prompt_chars")),
                     "last": p.get("last_ts") or "", "last_age_days": age,
                     "stale": bool(age is not None and age > STALE_MODEL_DAYS),
                     "job_types": list(p.get("job_types") or []),
                     "jobs": sorted(jobs.get(k, []), key=lambda j: -j["n"]),
                     "bench": bench_row(b, now) if b else None,
                     "warm": any(_same_model(m, str(p.get("model") or "")) for m in warm.get(node, []))})
    rows.sort(key=lambda r: -r["calls"])
    return rows


def role_matrix(route: Iterable[Dict[str, Any]], max_models: int = 6) -> Dict[str, Any]:
    """Job type x (model@node): how many calls, how long each took, tokens a second."""
    route = list(route or [])
    by_model: Dict[str, int] = {}
    by_job: Dict[str, int] = {}
    for s in route:
        k = _key(s.get("model"), s.get("instance"))
        by_model[k] = by_model.get(k, 0) + int(s.get("n") or 0)
        j = s.get("job_type") or ""
        by_job[j] = by_job.get(j, 0) + int(s.get("n") or 0)
    models = [k for k, _ in sorted(by_model.items(), key=lambda kv: -kv[1])][:max_models]
    jobs = [j for j, _ in sorted(by_job.items(), key=lambda kv: -kv[1])]
    cells: Dict[str, Dict[str, Any]] = {}
    for s in route:
        k = _key(s.get("model"), s.get("instance"))
        if k in models:
            cells.setdefault(s.get("job_type") or "", {})[k] = {
                "n": int(s.get("n") or 0), "wall_s": _f(s.get("ema_elapsed_s")), "tps": _f(s.get("ema_tps"))}
    return {"models": models, "jobs": [j for j in jobs if j in cells], "cells": cells,
            "source": "ollama.route_stats (moving averages)"}


def student_rows(reports: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every implementation scored in shadow, per question, with the leader per question marked."""
    out: List[Dict[str, Any]] = []
    for qid, rep in (reports or {}).items():
        if not isinstance(rep, dict) or rep.get("error"):
            continue
        live = rep.get("live_vs_outcomes") or {}
        rows = []
        for impl, v in (rep.get("by_implementation") or {}).items():
            vo = v.get("vs_outcomes") or {}
            acc, vn = _f(vo.get("accuracy")), int(vo.get("n") or 0)
            agree = _f(v.get("agreement"))
            rows.append({"question": qid, "impl": impl, "n": int(v.get("n") or 0), "agreement": agree,
                         "accuracy": acc, "outcomes": vn, "ms_p50": _f(v.get("ms_p50")), "auc": _f(v.get("auc")),
                         "live_accuracy": _f(live.get("accuracy")), "live_outcomes": int(live.get("n") or 0),
                         # agreeing with the live answer while being wrong against what really happened: the
                         # implementation copies the live path's bias (knn:minilm, 18 Oct: 0.78 agree, 0.26 right)
                         "agrees_not_right": bool(agree is not None and acc is not None and vn >= MIN_OUTCOMES
                                                  and agree >= 0.7 and acc < 0.5),
                         "leader": False})
        scored = [r for r in rows if r["accuracy"] is not None and r["outcomes"] >= MIN_OUTCOMES]
        if scored:
            max(scored, key=lambda r: (r["accuracy"], r["outcomes"]))["leader"] = True
        out += sorted(rows, key=lambda r: (-(r["accuracy"] if r["outcomes"] >= MIN_OUTCOMES and r["accuracy"] is not None else -1), -r["n"]))
    return out


def models_view(passive: Dict[str, Any], route: Dict[str, Any], bench: Dict[str, Any], node_perf: Dict[str, Any],
                reports: Dict[str, Dict[str, Any]], now: Optional[datetime] = None,
                four_minds: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    stats = (route or {}).get("stats") or []
    runs = sorted([bench_row(b, now) for b in (bench or {}).get("results") or []], key=lambda b: str(b["at"]), reverse=True)
    rows = model_rows((passive or {}).get("rows") or [], stats, (bench or {}).get("results") or [], warm_set(node_perf), now)
    students = student_rows(reports)
    last = runs[0] if runs else None
    issues = []
    if not runs:
        issues.append({"what": "benchmarks", "why": "no benchmark has ever been run", "tone": "warn"})
    elif last["age_days"] is not None and last["age_days"] > BENCH_STALE_DAYS:
        issues.append({"what": "benchmarks", "why": "the newest benchmark is %s days old" % int(last["age_days"]), "tone": "warn"})
    for b in runs:
        if b["broken"]:
            issues.append({"what": "benchmark %s (%s)" % (b["id"], b["model"]), "why": b["broken"], "tone": "bad"})
    live = [r for r in rows if r["tps"] is not None and r["calls"] >= 50 and not r["stale"]]
    return {"models": rows, "matrix": role_matrix(stats), "benchmarks": runs, "students": students,
            "four_minds": dict(four_minds or {}),
            "issues": issues, "nodes": [{"id": n.get("id"), "label": n.get("label"), "status": n.get("status"),
                                         "gpu": n.get("has_gpu"), "hw": n.get("hw") or {}, "ping_ms": n.get("ping_ms"),
                                         "loaded": [m.get("model") for m in n.get("loaded") or []]}
                                        for n in (node_perf or {}).get("nodes") or []],
            "summary": {"calls": int((passive or {}).get("total_calls") or sum(r["calls"] for r in rows)),
                        "models": len({r["model"] for r in rows}), "nodes": len({r["node"] for r in rows}),
                        "fastest": max(live, key=lambda r: r["tps"]) if live else None,
                        "slowest": max(live, key=lambda r: r["wall_s"] or 0) if live else None,
                        "best_student": next((s for s in students if s["leader"]), None) if students else None,
                        "last_bench": last, "bench_runs": len(runs)},
            "sources": ["bench.passive", "ollama.route_stats", "bench.results", "bench.node_perf", "decide.shadow.report"]}


# ── Performance ────────────────────────────────────────────────────────────────────────────────────────────
def _census_window(runs: List[Dict[str, Any]], lo: datetime, hi: datetime) -> Dict[str, Any]:
    rs = [r for r in runs if not r.get("excluded") and (lambda d: d is not None and lo <= d < hi)(_ts(r.get("ended_at")))]
    goals = sum(int(r.get("goals") or 0) for r in rs)
    done = sum(int(r.get("done") or 0) for r in rs)
    q_n = sum(int(r.get("quality_scored") or 0) for r in rs)
    q = sum((_f(r.get("quality_mean")) or 0) * int(r.get("quality_scored") or 0) for r in rs) / q_n if q_n else None
    per_goal = _median((_f(r.get("wall_total_s")) or 0) / int(r["goals"]) / 60 for r in rs if int(r.get("goals") or 0))
    return {"runs": len(rs), "goals": goals, "done": done, "done_rate": round(done / goals, 3) if goals else None,
            "quality": round(q, 3) if q is not None else None, "wall_capped": sum(int(r.get("wall_capped") or 0) for r in rs),
            "wall_per_goal_min": round(per_goal, 1) if per_goal is not None else None}


def census_kpis(census: Dict[str, Any], now: datetime, days: int = 7) -> Dict[str, Any]:
    runs = list((census or {}).get("runs") or [])
    cur = _census_window(runs, now - timedelta(days=days), now + timedelta(seconds=1))
    prev = _census_window(runs, now - timedelta(days=2 * days), now - timedelta(days=days))
    series = []
    for i in range(days - 1, -1, -1):
        d0 = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        w = _census_window(runs, d0, d0 + timedelta(days=1))
        series.append({"day": d0.date().isoformat(), "runs": w["runs"], "done_rate": w["done_rate"],
                       "quality": w["quality"], "wall_per_goal_min": w["wall_per_goal_min"]})
    return dict(cur, prev=prev, series=series, source="census.runs (full runs only; partial runs left out)")


def gate_kpis(rel: Dict[str, Any], now: datetime, days: int = 7) -> Dict[str, Any]:
    cols = (rel or {}).get("columns") or {}
    cards = [c for col in cols.values() for c in col or []]
    lo = now - timedelta(days=days)
    tested = [c for c in cards if (c.get("tests") or {}) and (_ts(c.get("last_at")) or now) >= lo]
    passed = [c for c in tested if (c.get("tests") or {}).get("ok")]
    released_at: Dict[str, datetime] = {}
    for r in (rel or {}).get("releases") or []:
        fin = _ts(r.get("finished_at"))
        for c in r.get("carried") or []:
            if fin and c.get("pipeline"):
                released_at.setdefault(str(c["pipeline"]), fin)
    leads = []
    for c in cards:
        st, fin = _ts(c.get("started_at")), released_at.get(str(c.get("id")))
        if st and fin and fin >= lo:
            leads.append((fin - st).total_seconds() / 3600)
    lead = _median(leads)
    rel_n = sum(1 for r in (rel or {}).get("releases") or [] if (_ts(r.get("finished_at")) or lo) > lo)
    return {"gates": len(tested), "passed": len(passed), "failed": len(tested) - len(passed),
            "pass_rate": round(len(passed) / len(tested), 3) if tested else None,
            "gate_min": _median(_f(c.get("minutes")) for c in tested),
            "lead_h": round(lead, 1) if lead is not None else None, "lead_n": len(leads),
            "on_edge": len(cols.get("edge") or []), "ready": len(cols.get("ready") or []),
            "edge_ahead": (rel or {}).get("edge_ahead"), "releases": rel_n,
            "source": "evolve.releases.view (pipelines + release history)"}


def queue_kpis(bg: Dict[str, Any]) -> Dict[str, Any]:
    q = (bg or {}).get("queue") or {}
    waiting = list(q.get("waiting") or [])
    run = q.get("running") or None
    ages = [_f(w.get("waiting_for_s")) for w in waiting] + ([_f(run.get("waiting_for_s"))] if run else [])
    oldest = max([a for a in ages if a is not None], default=None)
    # Outside the booked Background-work windows the queue still waits for this much quiet (inside a window it
    # does not). background.status `jobs` is NOT a list of held-back work: its one row, ide.claude_sessions.ingest,
    # is the drain's own label (the import runs every tick) - read as "deferred, never ran" on 18 Oct by mistake.
    min_quiet = (bg or {}).get("min_quiet_s")
    return {"depth": q.get("depth", len(waiting)), "oldest_h": round(oldest / 3600, 1) if oldest is not None else None,
            "running": {"title": run.get("title"), "waiting_h": round((_f(run.get("waiting_for_s")) or 0) / 3600, 1),
                        "stranded": run.get("stranded")} if run else None,
            "stranded": sum(int(w.get("stranded") or 0) for w in waiting) + (int(run.get("stranded") or 0) if run else 0),
            "min_quiet_s": min_quiet, "blocked": q.get("blocked_reason") or "", "source": "background.status"}


def chat_kpis(route: Dict[str, Any]) -> Dict[str, Any]:
    rows = [s for s in (route or {}).get("stats") or [] if s.get("job_type") == "chat"]
    n = sum(int(s.get("n") or 0) for s in rows)

    def wavg(field: str) -> Optional[float]:
        v = [(_f(s.get(field)), int(s.get("n") or 0)) for s in rows if _f(s.get(field)) is not None]
        w = sum(k for _, k in v)
        return round(sum(x * k for x, k in v) / w, 1) if w else None
    return {"replies": n, "wall_s": wavg("ema_elapsed_s"), "tps": wavg("ema_tps"), "tokens": wavg("ema_tokens"),
            "prompt_chars": wavg("ema_prompt_chars"),
            "by_model": sorted([{"model": s.get("model"), "node": s.get("instance"), "n": int(s.get("n") or 0),
                                 "wall_s": _f(s.get("ema_elapsed_s")), "tps": _f(s.get("ema_tps"))} for s in rows],
                               key=lambda r: -r["n"]),
            "source": "ollama.route_stats job_type=chat (moving averages)"}


def corpus_kpi(last: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    run = (last or {}).get("run") or {}
    s = (last or {}).get("summary") or {}
    cases = int(s.get("cases") or 0)
    status = s.get("status") or {}
    broken = ""
    if cases and int(status.get("error") or 0) == cases:
        reasons = sorted({str(e.get("reason") or "") for e in s.get("errors") or []})
        broken = "every case errored (%d of %d)%s" % (cases, cases, (": " + "; ".join(reasons[:2])) if reasons else "")
    age = _age_days(run.get("finished") or run.get("started"), now)
    return {"run_id": run.get("run_id"), "finished": run.get("finished"), "age_days": age, "cases": cases,
            "status": status, "pass_rate": None if broken else _f(s.get("pass_rate")), "broken": broken,
            "stale": bool(age is not None and age > CORPUS_STALE_DAYS), "ttft_ms": s.get("ttft_ms") or {},
            "source": "chat.corpus.last"}


def services(health: Dict[str, Any]) -> Dict[str, Any]:
    h = health or {}
    names = ("redis", "postgres", "chroma", "neo4j")
    up = {n: bool(h.get(n)) for n in names}
    nodes = {k: (v or {}).get("status") for k, v in (h.get("ollama") or {}).items()}
    return {"up": up, "ollama": nodes, "workers": h.get("workers"), "caps": h.get("caps"),
            "ok": sum(up.values()) + sum(1 for v in nodes.values() if v == "online"), "of": len(names) + len(nodes),
            "source": "obs.health"}


def share(route: Dict[str, Any]) -> Dict[str, Any]:
    """Who uses the models: share of every model call by group, with the biggest job types in each."""
    groups: Dict[str, Dict[str, int]] = {}
    for s in (route or {}).get("stats") or []:
        g = groups.setdefault(job_group(s.get("job_type")), {})
        j = s.get("job_type") or "?"
        g[j] = g.get(j, 0) + int(s.get("n") or 0)
    total = sum(sum(g.values()) for g in groups.values())
    out = []
    for name in ("loop", "chat", "background", "other"):
        g = groups.get(name) or {}
        n = sum(g.values())
        out.append({"group": name, "n": n, "pct": round(100 * n / total, 1) if total else 0.0,
                    "jobs": [{"job": j, "n": k} for j, k in sorted(g.items(), key=lambda kv: -kv[1])[:6]]})
    return {"total": total, "groups": out, "source": "ollama.route_stats"}


def performance_view(census: Dict[str, Any], rel: Dict[str, Any], bg: Dict[str, Any], route: Dict[str, Any],
                     corpus: Dict[str, Any], health: Dict[str, Any], days: int = 7,
                     now: Optional[datetime] = None, bench: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    c, g, q = census_kpis(census, now, days), gate_kpis(rel, now, days), queue_kpis(bg)
    ch, co, sv = chat_kpis(route), corpus_kpi(corpus, now), services(health)
    broken = []
    for b in (bench or {}).get("results") or []:
        r = bench_row(b, now)
        if r["broken"]:
            broken.append({"what": "benchmark %s (%s)" % (r["id"], r["model"]), "why": r["broken"], "source": "bench.results"})
    if co["broken"]:
        broken.append({"what": "chat test corpus", "why": co["broken"], "source": co["source"]})
    elif co["stale"]:
        broken.append({"what": "chat test corpus", "why": "last run %s days ago (weekly)" % int(co["age_days"]), "source": co["source"]})
    return {"days": days, "at": now.isoformat(), "loop_lab": {"census": c, "gates": g, "queue": q},
            "vera_use": {"chat": ch, "corpus": co, "services": sv}, "share": share(route), "broken": broken}
