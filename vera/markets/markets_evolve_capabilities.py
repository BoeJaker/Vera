"""
markets_evolve_capabilities.py — the markets self-improving loop
================================================================

A perpetual optimisation loop that improves the markets system on two fronts,
reusing the *existing* (tested) markets + Loop Lab capabilities rather than
re-implementing anything:

  1. **Strategies & backtests** — for each target (a saved strategy + a dataset)
     it derives a parameter grid from the strategy's own spec, runs it through
     the native backtest **sweep** engine (`markets.backtest.sweep`), takes the
     best by the chosen metric, and — when the best beats both the incumbent and
     the acceptance floor — writes the improved params back
     (`markets.strategy.save`) and puts the strategy live to monitor
     (`markets.strategy.accept`). Underperformers are archived. Each iteration
     RE-CENTRES the grid on the current best and, when a target stops improving,
     widens the search — a self-correcting hill-climb over strategy space.

  2. **Its own agent loop** — every N ticks it kicks a Loop Lab **improve
     session** (`evolve.improve.start`) scoped to markets benchmark tasks, so
     the agentic loop Vera uses to reason about markets keeps getting better via
     the critic/editor harness. Markets benchmark tasks are seeded into Loop Lab
     on startup.

The loop runs as a background scheduler (start/stop) and is also exposed as a
single-iteration `markets.evolve.tick` so the dream system / a human can drive
it. Everything is persisted so the leaderboard and history survive restarts.

Storage (Redis):
    vera:markets:evolve:config       config JSON
    vera:markets:evolve:state        per-target hill-climb state (grid centres)
    vera:markets:evolve:leaderboard  best-known metric per strategy
    vera:markets:evolve:history      list of iteration records (newest first)
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    CAPABILITY_REGISTRY, capability, emit_event, now_iso, schedule,
)

log = logging.getLogger("vera.markets.evolve")

KEY_CONFIG = "vera:markets:evolve:config"
KEY_STATE  = "vera:markets:evolve:state"
KEY_BOARD  = "vera:markets:evolve:leaderboard"
KEY_HIST   = "vera:markets:evolve:history"
HIST_CAP   = 200

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled":          False,     # perpetual loop off until turned on
    "interval_minutes": 180,       # how often the background loop ticks
    "metric":           "sharpe",  # ranking metric (any SWEEP_METRICS value)
    "min_metric":       0.8,       # acceptance floor — don't go live below this
    "grid_steps":       5,         # values per axis
    "grid_span":        0.5,       # ±50% around the current value
    "max_axes":         3,         # sweep engine caps at 3 axes / 400 combos
    "auto_accept":      True,      # put improved strategies live (monitor)
    "archive_floor":    -0.5,      # archive strategies whose best metric < this
    "improve_agent_loop": True,    # also run Loop Lab improve sessions
    "improve_every_ticks": 6,      # …every N ticks
    "sweep_timeout_s":  900,       # per-sweep budget
    # Optional explicit targets: [{dataset_id, strategy_id, axes?}]. Empty =
    # auto-discover from saved strategies that carry a monitor dataset.
    "targets":          [],
}


def _redis():
    return getattr(_orch, "REDIS", None)


async def _call(name: str, /, **kw) -> Any:
    # `/` = positional-only, so callers can pass name=… THROUGH to caps that
    # take a `name` argument (markets.strategy.save) without a kwarg collision.
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap or not cap.get("func"):
        return {"error": f"capability unavailable: {name}"}
    try:
        return await cap["func"](**kw)
    except Exception as e:
        return {"error": f"{name}: {e}"}


async def _get_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    r = _redis()
    if r:
        try:
            raw = await r.get(KEY_CONFIG)
            if raw:
                cfg.update(json.loads(raw.decode() if isinstance(raw, bytes) else raw))
        except Exception:
            pass
    return cfg


async def _save_config(cfg: Dict[str, Any]):
    r = _redis()
    if r:
        try:
            await r.set(KEY_CONFIG, json.dumps(cfg, default=str))
        except Exception:
            pass


async def _get_json(key: str, default):
    r = _redis()
    if not r:
        return default
    try:
        raw = await r.get(key)
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw) if raw else default
    except Exception:
        return default


async def _set_json(key: str, val):
    r = _redis()
    if r:
        try:
            await r.set(key, json.dumps(val, default=str))
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# GRID DERIVATION — turn a strategy spec's numeric params into sweep axes
# ─────────────────────────────────────────────────────────────────────────────

_RISK_KEYS = ("stop_loss_pct", "take_profit_pct", "size_pct")


def _collect_numeric_paths(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Walk a strategy spec and collect tunable numeric params as
    [{path, value, is_int}]. Indicator periods (…params.n) come first (highest
    leverage), then risk knobs."""
    found: List[Dict[str, Any]] = []

    def walk(node: Any, path: str, under_params: bool):
        if isinstance(node, dict):
            for k, v in node.items():
                p = f"{path}.{k}" if path else k
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    if under_params or k in _RISK_KEYS:
                        found.append({"path": p, "value": float(v),
                                      "is_int": (under_params and float(v).is_integer()
                                                 and abs(v) >= 2)})
                else:
                    walk(v, p, under_params or k == "params")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}.{i}", under_params)

    walk(spec, "", False)
    # period params (under params.*) before risk knobs; stable order otherwise
    found.sort(key=lambda f: (0 if ".params." in f["path"] else 1, f["path"]))
    return found


def _axis_for(param: Dict[str, Any], span: float, steps: int,
              centre: Optional[float] = None) -> Optional[Dict[str, Any]]:
    v = float(centre if centre is not None else param["value"])
    if v == 0:
        return None
    lo = v * (1.0 - span)
    hi = v * (1.0 + span)
    if param["is_int"]:
        lo = max(2, round(lo))
        hi = max(lo + 1, round(hi))
        step = max(1, round((hi - lo) / max(1, steps - 1)))
        return {"path": param["path"], "from": lo, "to": hi, "step": step}
    lo = round(max(0.0001, lo), 6)
    hi = round(hi, 6)
    step = round((hi - lo) / max(1, steps - 1), 6) or 0.0001
    return {"path": param["path"], "from": lo, "to": hi, "step": step}


def _derive_axes(spec: Dict[str, Any], cfg: Dict[str, Any],
                 centres: Optional[Dict[str, float]] = None) -> List[Dict[str, Any]]:
    params = _collect_numeric_paths(spec)[: int(cfg.get("max_axes", 3))]
    axes = []
    for p in params:
        ax = _axis_for(p, float(cfg.get("grid_span", 0.5)),
                       int(cfg.get("grid_steps", 5)),
                       centre=(centres or {}).get(p["path"]))
        if ax:
            axes.append(ax)
    return axes


# ─────────────────────────────────────────────────────────────────────────────
# ONE TARGET — sweep, evaluate, accept/archive, re-centre
# ─────────────────────────────────────────────────────────────────────────────

async def _await_sweep(sweep_id: str, timeout_s: int) -> Dict[str, Any]:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        st = await _call("markets.backtest.sweep_status", id=sweep_id)
        s = (st or {}).get("sweep") if isinstance(st, dict) else None
        if not s:
            return {"error": "sweep status unavailable"}
        if s.get("status") in ("done", "cancelled", "error"):
            return s
        await asyncio.sleep(3)
    await _call("markets.backtest.sweep_status", id=sweep_id, cancel=True)
    return {"error": "sweep timed out"}


async def _improve_target(target: Dict[str, Any], strat: Dict[str, Any],
                          cfg: Dict[str, Any], state: Dict[str, Any],
                          board: Dict[str, Any]) -> Dict[str, Any]:
    sid = strat.get("id")
    dataset_id = target.get("dataset_id") or (strat.get("monitor") or {}).get("dataset_id")
    metric = cfg.get("metric", "sharpe")
    rec: Dict[str, Any] = {"strategy_id": sid, "name": strat.get("name"),
                           "dataset_id": dataset_id, "metric": metric}
    if not dataset_id:
        rec["skipped"] = "no dataset (accept the strategy to a dataset first)"
        return rec

    tstate = state.setdefault(sid, {"centres": {}, "span": float(cfg["grid_span"]),
                                    "stale": 0})
    axes = target.get("axes") or _derive_axes(strat.get("spec") or {},
                                              {**cfg, "grid_span": tstate["span"]},
                                              tstate.get("centres"))
    if not axes:
        rec["skipped"] = "no tunable numeric params in spec"
        return rec

    sw = await _call("markets.backtest.sweep", dataset_id=dataset_id,
                     strategy_id=sid, params=axes, metric=metric,
                     name=f"evolve {strat.get('name','')}")
    if not isinstance(sw, dict) or sw.get("error") or not sw.get("sweep_id"):
        rec["error"] = (sw or {}).get("error", "sweep failed to start")
        return rec
    rec["sweep_id"] = sw["sweep_id"]
    rec["combos"] = sw.get("combos")
    res = await _await_sweep(sw["sweep_id"], int(cfg.get("sweep_timeout_s", 900)))
    if res.get("error"):
        rec["error"] = res["error"]
        return rec
    best = res.get("best") or {}
    best_metric = ((best.get("stats") or {}).get(metric))
    rec["best_metric"] = best_metric
    rec["best_values"] = best.get("values")
    rec["best_backtest_id"] = res.get("best_backtest_id")
    if best_metric is None:
        rec["skipped"] = "no valid backtest in sweep"
        tstate["stale"] += 1
        return rec

    prev = (board.get(sid) or {}).get("metric")
    improved = prev is None or best_metric > prev + 1e-9
    rec["prev_metric"] = prev
    rec["improved"] = improved

    if improved:
        # write the improved spec back + re-centre the grid on the winner
        if res.get("best_spec"):
            await _call("markets.strategy.save", id=sid,
                        name=strat.get("name") or "strategy",
                        spec=res["best_spec"],
                        kind=strat.get("kind") or "rule")
            rec["saved"] = True
        tstate["centres"] = dict(best.get("values") or {})
        tstate["stale"] = 0
        tstate["span"] = float(cfg["grid_span"])   # tighten back to default
        board[sid] = {"metric": best_metric, "ts": now_iso(),
                      "name": strat.get("name")}
        # put live if it clears the floor
        if cfg.get("auto_accept") and best_metric >= float(cfg.get("min_metric", 0.8)):
            acc = await _call("markets.strategy.accept", id=sid,
                              dataset_id=dataset_id, enabled=True)
            rec["accepted"] = bool(isinstance(acc, dict) and not acc.get("error"))
    else:
        # no improvement — widen the search next time (self-correction)
        tstate["stale"] += 1
        tstate["span"] = min(1.5, tstate["span"] * 1.4)
        # archive persistent losers
        if best_metric < float(cfg.get("archive_floor", -0.5)) and tstate["stale"] >= 3:
            await _call("markets.strategy.archive", id=sid)
            rec["archived"] = True
    return rec


# ─────────────────────────────────────────────────────────────────────────────
# TICK — one full iteration across all targets
# ─────────────────────────────────────────────────────────────────────────────

_TICK_RUNNING = False
_TICK_COUNT = 0


async def _resolve_targets(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    if cfg.get("targets"):
        return [t for t in cfg["targets"] if isinstance(t, dict) and t.get("strategy_id")]
    # auto: rule/fused strategies (draft or accepted), paired with their monitor
    lst = await _call("markets.strategy.list")
    out = []
    for s in (lst or {}).get("strategies", []) if isinstance(lst, dict) else []:
        if s.get("status") == "archived":
            continue
        ds = (s.get("monitor") or {}).get("dataset_id")
        if ds:
            out.append({"strategy_id": s.get("id"), "dataset_id": ds})
    return out


@capability("markets.evolve.tick", memory="on",
            http_method="POST", http_path="/markets/evolve/tick", http_tags=["markets"],
            description="Run ONE self-improvement iteration: for each target "
                        "(strategy+dataset) derive a parameter grid, sweep the "
                        "backtest engine, and — if the best beats the incumbent & "
                        "the acceptance floor — save the improved params and put "
                        "the strategy live; archive persistent losers; re-centre "
                        "the grid. Optionally also kicks a Loop Lab improve session "
                        "for the markets agent loop. Output: {ok, results:[…], "
                        "improved, accepted}.")
async def markets_evolve_tick(force_improve: bool = False, trace_id=None):
    global _TICK_RUNNING, _TICK_COUNT
    if _TICK_RUNNING:
        return {"error": "a tick is already in progress"}
    _TICK_RUNNING = True
    try:
        cfg = await _get_config()
        state = await _get_json(KEY_STATE, {})
        board = await _get_json(KEY_BOARD, {})
        targets = await _resolve_targets(cfg)
        tick_id = uuid.uuid4().hex[:8]
        await emit_event({"type": "markets.evolve.tick.start", "tick": tick_id,
                          "targets": len(targets)})
        if not targets:
            return {"ok": True, "results": [],
                    "note": "no targets — save a strategy and accept it to a "
                            "dataset (so it has a monitor), or set explicit "
                            "targets in markets.evolve.config."}
        results = []
        for i, t in enumerate(targets, 1):
            strat = None
            lst = await _call("markets.strategy.list")
            for s in (lst or {}).get("strategies", []):
                if s.get("id") == t.get("strategy_id"):
                    strat = s
                    break
            if not strat:
                results.append({"strategy_id": t.get("strategy_id"),
                                "error": "strategy not found"})
                continue
            await emit_event({"type": "markets.evolve.tick.progress", "tick": tick_id,
                              "done": i, "total": len(targets),
                              "strategy": strat.get("name")})
            rec = await _improve_target(t, strat, cfg, state, board)
            results.append(rec)
        await _set_json(KEY_STATE, state)
        await _set_json(KEY_BOARD, board)

        summary = {
            "tick_id": tick_id, "ts": now_iso(),
            "targets": len(targets),
            "improved": sum(1 for r in results if r.get("improved")),
            "accepted": sum(1 for r in results if r.get("accepted")),
            "archived": sum(1 for r in results if r.get("archived")),
            "results": results,
        }
        r = _redis()
        if r:
            try:
                await r.lpush(KEY_HIST, json.dumps(summary, default=str))
                await r.ltrim(KEY_HIST, 0, HIST_CAP - 1)
            except Exception:
                pass

        # ── Improve the markets AGENT LOOP periodically ──────────────────────
        _TICK_COUNT += 1
        if (cfg.get("improve_agent_loop")
                and (force_improve
                     or _TICK_COUNT % int(cfg.get("improve_every_ticks", 6)) == 0)):
            starter = CAPABILITY_REGISTRY.get("evolve.improve.start")
            if starter and starter.get("func"):
                try:
                    res = await starter["func"](profile="planning", tag="markets")
                    summary["agent_improve_session"] = (res or {}).get("session_id")
                except Exception as e:
                    log.debug("markets evolve: agent improve kick failed: %s", e)

        await emit_event({"type": "markets.evolve.tick.done", "tick": tick_id,
                          "improved": summary["improved"],
                          "accepted": summary["accepted"]})
        return {"ok": True, **summary}
    finally:
        _TICK_RUNNING = False


# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND LOOP + CONTROL
# ─────────────────────────────────────────────────────────────────────────────

_LOOP_TASK: Optional[asyncio.Task] = None


async def _loop():
    log.info("markets evolve loop started")
    await emit_event({"type": "markets.evolve.loop.started"})
    try:
        while True:
            cfg = await _get_config()
            if not cfg.get("enabled"):
                break
            try:
                await markets_evolve_tick()
            except Exception as e:
                log.warning("markets evolve tick error: %s", e)
            cfg = await _get_config()
            if not cfg.get("enabled"):
                break
            await asyncio.sleep(max(60, int(cfg.get("interval_minutes", 180)) * 60))
    finally:
        await emit_event({"type": "markets.evolve.loop.stopped"})
        log.info("markets evolve loop stopped")


@capability("markets.evolve.start", memory="on",
            http_method="POST", http_path="/markets/evolve/start", http_tags=["markets"],
            description="Start the perpetual markets self-improvement loop "
                        "(ticks every interval_minutes). Output: {ok, running}.")
async def markets_evolve_start(trace_id=None):
    global _LOOP_TASK
    cfg = await _get_config()
    cfg["enabled"] = True
    await _save_config(cfg)
    if _LOOP_TASK and not _LOOP_TASK.done():
        return {"ok": True, "running": True, "note": "already running"}
    _LOOP_TASK = asyncio.create_task(_loop())
    return {"ok": True, "running": True}


@capability("markets.evolve.stop", memory="on",
            http_method="POST", http_path="/markets/evolve/stop", http_tags=["markets"],
            description="Stop the perpetual markets self-improvement loop. "
                        "Output: {ok}.")
async def markets_evolve_stop(trace_id=None):
    cfg = await _get_config()
    cfg["enabled"] = False
    await _save_config(cfg)
    return {"ok": True, "running": False}


@capability("markets.evolve.status", memory="off", silent=True,
            http_method="GET", http_path="/markets/evolve/status", http_tags=["markets"],
            description="Markets self-improvement status: config, live flag, "
                        "leaderboard (best metric per strategy), recent ticks.")
async def markets_evolve_status(trace_id=None):
    cfg = await _get_config()
    board = await _get_json(KEY_BOARD, {})
    hist = await _get_json_list(KEY_HIST, 12)
    return {"config": cfg,
            "running": bool(_LOOP_TASK and not _LOOP_TASK.done()),
            "tick_running": _TICK_RUNNING,
            "leaderboard": sorted(
                [{"strategy_id": k, **v} for k, v in board.items()],
                key=lambda x: x.get("metric") or -1e9, reverse=True),
            "recent": hist}


async def _get_json_list(key: str, limit: int) -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return []
    try:
        rows = await r.lrange(key, 0, max(0, int(limit) - 1))
        out = []
        for row in rows or []:
            try:
                out.append(json.loads(row.decode() if isinstance(row, bytes) else row))
            except Exception:
                continue
        return out
    except Exception:
        return []


@capability("markets.evolve.history", memory="off", silent=True,
            http_method="GET", http_path="/markets/evolve/history", http_tags=["markets"],
            description="Recent self-improvement iterations (newest first). "
                        "Query: limit.")
async def markets_evolve_history(limit: int = 40, trace_id=None):
    return {"history": await _get_json_list(KEY_HIST, limit)}


@capability("markets.evolve.config.set", memory="on",
            http_method="POST", http_path="/markets/evolve/config/set", http_tags=["markets"],
            description="Update the markets self-improvement config. Pass any of: "
                        "interval_minutes, metric, min_metric, grid_steps, "
                        "grid_span, max_axes, auto_accept, archive_floor, "
                        "improve_agent_loop, improve_every_ticks, sweep_timeout_s, "
                        "targets (list [{dataset_id,strategy_id,axes?}]).")
async def markets_evolve_config_set(config: Optional[Dict[str, Any]] = None,
                                    trace_id=None, **fields):
    cfg = await _get_config()
    patch = dict(config or {})
    patch.update({k: v for k, v in fields.items() if v is not None})
    for k, v in patch.items():
        if k in DEFAULT_CONFIG or k == "targets":
            cfg[k] = v
    await _save_config(cfg)
    return {"ok": True, "config": cfg}


# ─────────────────────────────────────────────────────────────────────────────
# LOOP LAB TASKS — seed markets benchmark tasks so the agent loop can be tuned
# ─────────────────────────────────────────────────────────────────────────────

def _markets_tasks() -> List[Dict[str, Any]]:
    return [
        {
            "id": "markets-backtest-reason", "label": "Markets — run & read a backtest",
            "type": "loop", "profile": "planning", "tags": ["markets", "loop"],
            "goal": "List saved trading strategies with markets.strategy.list, pick "
                    "one, run markets.backtest.run on it against its dataset, and "
                    "report its Sharpe ratio and max drawdown from the result.",
            "allowed_caps": "markets.strategy.list,markets.backtest.run,markets.backtest.get",
            "max_steps": 6, "timeout_s": 600, "enabled": True,
            "checks": [
                {"type": "cap_called", "value": "markets.strategy.list"},
                {"type": "regex", "value": r"sharpe|drawdown"},
            ],
            "rubric": "Used the real backtest tools (not invented numbers); "
                      "reported Sharpe and drawdown grounded in the tool output.",
        },
        {
            "id": "markets-sweep-propose", "label": "Markets — propose a param sweep",
            "type": "loop", "profile": "planning", "tags": ["markets", "loop"],
            "goal": "Inspect a saved strategy (markets.strategy.list) and propose a "
                    "sensible 2-axis parameter sweep (indicator periods) as a JSON "
                    "list of {path, from, to, step} axes. Output only the JSON.",
            "allowed_caps": "markets.strategy.list",
            "max_steps": 4, "timeout_s": 420, "enabled": True,
            "checks": [
                {"type": "cap_called", "value": "markets.strategy.list"},
                {"type": "json_valid"},
            ],
            "rubric": "Axes reference real dotted paths that exist in the strategy "
                      "spec; ranges are reasonable for the indicator.",
        },
    ]


async def _startup():
    for _ in range(20):
        if _redis() is not None:
            break
        await asyncio.sleep(0.5)
    # Seed markets benchmark tasks into Loop Lab (idempotent upsert).
    up = CAPABILITY_REGISTRY.get("evolve.task.upsert")
    if up and up.get("func"):
        for t in _markets_tasks():
            try:
                await up["func"](task=t)
            except Exception as e:
                log.debug("markets evolve seed task %s: %s", t["id"], e)
    # Resume the perpetual loop if it was enabled before a restart.
    global _LOOP_TASK
    cfg = await _get_config()
    if cfg.get("enabled") and (not _LOOP_TASK or _LOOP_TASK.done()):
        _LOOP_TASK = asyncio.create_task(_loop())
        log.info("markets evolve: resumed perpetual loop (was enabled)")


schedule(_startup, interval=999999, name="markets_evolve_startup")

log.info("markets evolve loaded (%d benchmark tasks)", len(_markets_tasks()))
