"""
business_sim.py — Business simulation & agent-loop evaluation
=============================================================

A **simulation mode** for the Business tab. It gives the operator (and the DAG
Workshop's "Loop Evaluation" view) a safe sandbox in which to:

  1. Spin up a scenario — a set of *simulated* income streams and money accounts
     (``is_sim=1`` in the shared business tables, so they never touch the real
     books yet use the exact same capabilities the agents use for real work).
  2. Drive **synthetic activity** — realistic-ish sales, payouts, fees, gig
     wins, content views, bounty rewards — either a full random step, or a
     targeted nudge, so the operator can watch how the numbers move.
  3. **Evaluate an agent** — record a goal + a scoring rubric, run the real
     agent loop (``/workshop/agent_loop/stream``) against the *simulated*
     accounts, then score the outcome from the change in the sim ledger. This is
     the "can the agent actually operate the business toward a goal?" check that
     the DAG UI front-ends as *agentic loop evaluation*.

Everything the agent does in a sim run is namespaced by ``is_sim=1``; the live
dashboard (``biz.dashboard`` with ``is_sim=0``) is untouched, and
``biz.dashboard?is_sim=1`` shows the simulated world.

Scoring is deliberately mechanical (delta in net / goal attainment / task
completion) so it is reproducible and explainable — the score, its components
and the run transcript pointer are all persisted so a run can be audited later.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

log = logging.getLogger("vera.business.sim")

try:
    from Vera.vera.capability_orchestration import (
        capability, emit_event, now_iso, enum_schema,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.business.sim").warning("business_sim unavailable: %s", e)
    _CAP_AVAILABLE = False


def _biz():
    """The core business module (loaded before this one)."""
    import sys
    return sys.modules.get("business_capabilities")


async def _run(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


# ─────────────────────────────────────────────────────────────────────────────
# Scenario library — each seeds a plausible small business the agent can run.
# ─────────────────────────────────────────────────────────────────────────────

SCENARIOS: Dict[str, dict] = {
    "reseller": {
        "label": "eBay / card reseller",
        "description": "A one-person reselling + trading-card operation: buy low, "
                       "list, ship, restock. Tests inventory + pricing + cashflow.",
        "streams": [
            {"name": "SIM eBay Store", "kind": "ecommerce", "platform": "ebay",
             "goal_monthly": 2000, "icon": "📦"},
            {"name": "SIM Card Flips", "kind": "cards", "platform": "tcgplayer",
             "goal_monthly": 800, "icon": "🃏"},
        ],
        "accounts": [
            {"name": "SIM Bank", "kind": "bank", "opening": 1500},
            {"name": "SIM PayPal", "kind": "paypal", "opening": 300},
        ],
        # Seed inventory (stream index → items) so 'find low stock / list items /
        # reprice' goals have something real to chew on from step one.
        "inventory": [
            {"stream": 0, "kind": "stock", "sku": "SIM-LEGO-01", "name": "SIM Lego Technic set",
             "qty": 6, "cost": 22.0, "market_value": 38.0, "price": 42.0, "status": "in_stock"},
            {"stream": 0, "kind": "stock", "sku": "SIM-HDMI-02", "name": "SIM HDMI cable 2m (bulk)",
             "qty": 40, "cost": 1.2, "market_value": 4.5, "price": 5.99, "status": "listed"},
            {"stream": 0, "kind": "stock", "sku": "SIM-RETRO-03", "name": "SIM Retro console",
             "qty": 1, "cost": 55.0, "market_value": 95.0, "price": 110.0, "status": "in_stock"},
            {"stream": 1, "kind": "card", "sku": "SIM-CARD-CHZ", "name": "SIM Charizard holo",
             "qty": 2, "cost": 80.0, "market_value": 140.0, "price": 155.0, "status": "in_stock",
             "grade": "PSA 8", "set_name": "Base Set"},
            {"stream": 1, "kind": "card", "sku": "SIM-CARD-PKB", "name": "SIM Pikachu bulk lot",
             "qty": 120, "cost": 0.1, "market_value": 0.4, "price": 0.5, "status": "in_stock"},
        ],
        "tasks": [
            {"stream": 0, "title": "SIM: relist stale eBay items", "kind": "list"},
            {"stream": 1, "title": "SIM: grade the Charizard", "kind": "research"},
        ],
        "activity_bias": {"sale": 0.55, "fee": 0.15, "expense": 0.2, "payout": 0.1},
        "avg_sale": 28.0,
    },
    "creator": {
        "label": "Content creator",
        "description": "A YouTube + short-form creator monetising via ads, "
                       "sponsorships and affiliate links. Tests the content "
                       "pipeline + marketing.",
        "streams": [
            {"name": "SIM YouTube", "kind": "content", "platform": "youtube",
             "goal_monthly": 1200, "icon": "🎬"},
            {"name": "SIM Affiliate", "kind": "affiliate", "platform": "amazon",
             "goal_monthly": 400, "icon": "🔗"},
        ],
        "accounts": [
            {"name": "SIM Bank", "kind": "bank", "opening": 800},
            {"name": "SIM Stripe", "kind": "stripe", "opening": 0},
        ],
        "content": [
            {"stream": 0, "platform": "youtube", "title": "SIM: 'Top 5 budget mics' review",
             "status": "editing"},
            {"stream": 0, "platform": "youtube", "title": "SIM: studio tour short",
             "status": "idea"},
            {"stream": 1, "platform": "blog", "title": "SIM: best-sellers roundup post",
             "status": "scripting"},
        ],
        "tasks": [
            {"stream": 0, "title": "SIM: publish the mic review", "kind": "content"},
        ],
        "activity_bias": {"sale": 0.4, "fee": 0.1, "expense": 0.25, "payout": 0.25},
        "avg_sale": 55.0,
    },
    "freelancer": {
        "label": "Freelance + bounties",
        "description": "A freelancer running Fiverr/Upwork gigs alongside a "
                       "bug-bounty practice. Tests gig throughput + reward capture.",
        "streams": [
            {"name": "SIM Fiverr", "kind": "freelance", "platform": "fiverr",
             "goal_monthly": 2500, "icon": "🛠️"},
            {"name": "SIM Bounties", "kind": "bounty", "platform": "hackerone",
             "goal_monthly": 1500, "icon": "🐛"},
        ],
        "accounts": [
            {"name": "SIM Bank", "kind": "bank", "opening": 2000},
            {"name": "SIM Wise", "kind": "wise", "opening": 500},
        ],
        "gigs": [
            {"stream": 0, "platform": "fiverr", "title": "SIM: logo design for cafe",
             "client": "SIM client A", "status": "active", "price": 120.0},
            {"stream": 0, "platform": "upwork", "title": "SIM: WordPress fix",
             "client": "SIM client B", "status": "lead", "price": 250.0},
        ],
        "tasks": [
            {"stream": 0, "title": "SIM: deliver the logo drafts", "kind": "followup"},
        ],
        "activity_bias": {"sale": 0.5, "fee": 0.1, "expense": 0.15, "payout": 0.25},
        "avg_sale": 180.0,
    },
    "empty": {
        "label": "Empty sandbox",
        "description": "A blank simulated business — one bank account, no streams. "
                       "For free-form agent evaluation where the agent must build "
                       "the business itself.",
        "streams": [],
        "accounts": [{"name": "SIM Bank", "kind": "bank", "opening": 1000}],
        "activity_bias": {"sale": 0.5, "expense": 0.5},
        "avg_sale": 40.0,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Sim-run store (its own tiny table — the seeded streams/accounts live in the
# shared biz_* tables with is_sim=1).
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_sim_schema_sync():
    conn = _sqlite_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS biz_sim_runs (
                id          TEXT PRIMARY KEY,
                scenario    TEXT,
                label       TEXT,
                status      TEXT DEFAULT 'active',
                seed        INTEGER,
                steps       INTEGER DEFAULT 0,
                config      TEXT,
                stream_ids  TEXT,
                account_ids TEXT,
                created_at  TEXT,
                updated_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS biz_sim_evals (
                id          TEXT PRIMARY KEY,
                run_id      TEXT,
                goal        TEXT,
                rubric      TEXT,
                agent_name  TEXT,
                status      TEXT DEFAULT 'defined',
                baseline    TEXT,
                score       REAL,
                components  TEXT,
                transcript  TEXT,
                created_at  TEXT,
                updated_at  TEXT
            );
        """)
        conn.commit()
    finally:
        conn.close()

_SIM_SCHEMA_READY = False
async def _ensure_sim_schema():
    global _SIM_SCHEMA_READY
    if not _SIM_SCHEMA_READY:
        await _run(_ensure_sim_schema_sync)
        _SIM_SCHEMA_READY = True


def _sim_run_out(row) -> dict:
    d = dict(row)
    for k in ("config", "stream_ids", "account_ids"):
        try: d[k] = json.loads(d.get(k) or ("{}" if k == "config" else "[]"))
        except Exception: d[k] = {} if k == "config" else []
    return d

def _db_active_run() -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM biz_sim_runs WHERE status='active' "
                         "ORDER BY updated_at DESC LIMIT 1").fetchone()
        return _sim_run_out(r) if r else None
    finally:
        conn.close()

def _db_get_run(rid: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM biz_sim_runs WHERE id=?", (rid,)).fetchone()
        return _sim_run_out(r) if r else None
    finally:
        conn.close()

def _db_save_run(run: dict) -> dict:
    conn = _sqlite_conn()
    try:
        now = now_iso()
        conn.execute(
            "INSERT OR REPLACE INTO biz_sim_runs (id,scenario,label,status,seed,steps,"
            "config,stream_ids,account_ids,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run["id"], run.get("scenario", ""), run.get("label", ""),
             run.get("status", "active"), int(run.get("seed", 0)), int(run.get("steps", 0)),
             json.dumps(run.get("config") or {}), json.dumps(run.get("stream_ids") or []),
             json.dumps(run.get("account_ids") or []),
             run.get("created_at") or now, now))
        conn.commit()
        return _db_get_run(run["id"])
    finally:
        conn.close()

def _db_purge_sim_data(stream_ids: List[str], account_ids: List[str]):
    """Remove ALL simulated data for a reset: the tracked streams/accounts plus
    every is_sim=1 row in every business table (seeded inventory/gigs/content/
    tasks and anything the agent created during eval runs)."""
    conn = _sqlite_conn()
    try:
        for sid in stream_ids:
            conn.execute("DELETE FROM biz_streams WHERE id=?", (sid,))
        for aid in account_ids:
            conn.execute("DELETE FROM biz_accounts WHERE id=?", (aid,))
        for tbl in ("biz_ledger", "biz_streams", "biz_accounts", "biz_inventory",
                    "biz_gigs", "biz_content", "biz_bounties", "biz_social",
                    "biz_campaigns", "biz_tasks"):
            try:
                conn.execute(f"DELETE FROM {tbl} WHERE is_sim=1")
            except Exception:
                pass    # column missing on an unmigrated table — nothing to purge
        conn.commit()
    finally:
        conn.close()

def _db_sim_drain_inventory() -> int:
    """A synthetic sale consumes sim stock: decrement one random in-stock
    is_sim=1 item. Returns 1 when an item was drained, else 0."""
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT id, qty FROM biz_inventory WHERE is_sim=1 AND qty>0 "
                         "ORDER BY RANDOM() LIMIT 1").fetchone()
        if not r:
            return 0
        conn.execute("UPDATE biz_inventory SET qty=qty-1, updated_at=? WHERE id=?",
                     (now_iso(), r[0]))
        conn.commit()
        return 1
    finally:
        conn.close()


def _db_sim_net(stream_ids: List[str]) -> dict:
    """Net / income / expense across the sim ledger for the given streams."""
    conn = _sqlite_conn()
    try:
        if stream_ids:
            marks = ",".join("?" for _ in stream_ids)
            income = conn.execute(
                f"SELECT COALESCE(SUM(amount),0) FROM biz_ledger WHERE is_sim=1 "
                f"AND amount>0 AND stream_id IN ({marks})", stream_ids).fetchone()[0]
            expense = conn.execute(
                f"SELECT COALESCE(SUM(amount),0) FROM biz_ledger WHERE is_sim=1 "
                f"AND amount<0 AND stream_id IN ({marks})", stream_ids).fetchone()[0]
        else:
            income = conn.execute("SELECT COALESCE(SUM(amount),0) FROM biz_ledger "
                                  "WHERE is_sim=1 AND amount>0").fetchone()[0]
            expense = conn.execute("SELECT COALESCE(SUM(amount),0) FROM biz_ledger "
                                   "WHERE is_sim=1 AND amount<0").fetchone()[0]
        cash = conn.execute("SELECT COALESCE(SUM(balance),0) FROM biz_accounts "
                            "WHERE is_sim=1").fetchone()[0]
        return {"income": round(float(income), 2), "expense": round(abs(float(expense)), 2),
                "net": round(float(income) + float(expense), 2), "cash": round(float(cash), 2)}
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    @capability(
        "business.sim.scenarios", http_method="GET", http_path="/business/sim/scenarios",
        http_tags=["business"], memory="off", silent=True,
        description="List available simulation scenarios. Output: {scenarios:[...]}.")
    async def cap_sim_scenarios(trace_id=None):
        return {"scenarios": [
            {"id": k, "label": v["label"], "description": v["description"],
             "streams": len(v["streams"]), "accounts": len(v["accounts"])}
            for k, v in SCENARIOS.items()]}

    @capability(
        "business.sim.start", http_method="POST", http_path="/business/sim/start",
        http_tags=["business"],
        schema=enum_schema(scenario=list(SCENARIOS.keys())),
        description="Start a simulation run — seeds simulated streams + money "
                    "accounts (is_sim=1) from a scenario. Any previous active run "
                    "is closed. Input: scenario (str!), seed (int — for "
                    "reproducibility, default random). Output: {ok, run}.")
    async def cap_sim_start(scenario: str = "reseller", seed: int = 0, trace_id=None):
        await _ensure_sim_schema()
        biz = _biz()
        if not biz:
            return {"error": "business_capabilities not loaded"}
        await biz._ensure_schema()
        scn = SCENARIOS.get(scenario)
        if not scn:
            return {"error": f"unknown scenario '{scenario}'"}
        # Close any previous active run (leaves its data; use reset to purge).
        prev = await _run(_db_active_run)
        if prev:
            prev["status"] = "closed"; await _run(_db_save_run, prev)
        seed = int(seed) or random.randint(1, 10_000_000)
        run_id = f"sim_{uuid.uuid4().hex[:10]}"
        stream_ids, account_ids = [], []
        for sdef in scn["streams"]:
            s = await _run(biz._db_upsert, "biz_streams", {
                "name": sdef["name"], "kind": sdef["kind"], "platform": sdef.get("platform", ""),
                "status": "active", "goal_monthly": sdef.get("goal_monthly", 0),
                "icon": sdef.get("icon", "🧪"), "description": "Simulated stream",
                "is_sim": 1, "config": {"sim": True, "run_id": run_id}})
            stream_ids.append(s["id"])
        for adef in scn["accounts"]:
            a = await _run(biz._db_upsert, "biz_accounts", {
                "name": adef["name"], "kind": adef["kind"], "currency": "USD",
                "balance": adef.get("opening", 0), "is_sim": 1,
                "stream_id": stream_ids[0] if stream_ids else None,
                "config": {"run_id": run_id}})
            account_ids.append(a["id"])

        def _sid_for(d: dict) -> Optional[str]:
            i = int(d.get("stream", 0))
            return stream_ids[i] if 0 <= i < len(stream_ids) else None

        # Seed working entities (inventory / gigs / content / tasks) so an agent
        # dropped into the sim has real state to query and act on from step one.
        seeded = {"inventory": 0, "gigs": 0, "content": 0, "tasks": 0}
        for idef in scn.get("inventory") or []:
            await _run(biz._db_upsert, "biz_inventory", {
                **{k: v for k, v in idef.items() if k != "stream"},
                "stream_id": _sid_for(idef), "is_sim": 1})
            seeded["inventory"] += 1
        for gdef in scn.get("gigs") or []:
            await _run(biz._db_upsert, "biz_gigs", {
                **{k: v for k, v in gdef.items() if k != "stream"},
                "stream_id": _sid_for(gdef), "is_sim": 1})
            seeded["gigs"] += 1
        for cdef in scn.get("content") or []:
            await _run(biz._db_upsert, "biz_content", {
                **{k: v for k, v in cdef.items() if k != "stream"},
                "stream_id": _sid_for(cdef), "is_sim": 1})
            seeded["content"] += 1
        for tdef in scn.get("tasks") or []:
            await _run(biz._db_upsert, "biz_tasks", {
                **{k: v for k, v in tdef.items() if k != "stream"},
                "stream_id": _sid_for(tdef), "status": "todo", "is_sim": 1})
            seeded["tasks"] += 1

        run = await _run(_db_save_run, {
            "id": run_id, "scenario": scenario, "label": scn["label"],
            "status": "active", "seed": seed, "steps": 0,
            "config": {"scenario": scenario}, "stream_ids": stream_ids,
            "account_ids": account_ids})
        await emit_event({"type": "business.sim", "stage": "start",
                          "message": f"sim '{scenario}' started ({len(stream_ids)} streams, "
                                     f"{len(account_ids)} accounts, "
                                     f"{sum(seeded.values())} seeded entities)",
                          "run_id": run_id})
        return {"ok": True, "run": run, "seeded": seeded}

    @capability(
        "business.sim.step", http_method="POST", http_path="/business/sim/step",
        http_tags=["business"],
        description="Advance the active simulation by generating synthetic activity "
                    "(sales, fees, expenses, payouts) against the simulated accounts. "
                    "Input: n (int — how many synthetic transactions, default 8), "
                    "run_id (str — default the active run). Output: {ok, generated, totals}.")
    async def cap_sim_step(n: int = 8, run_id: str = "", trace_id=None):
        await _ensure_sim_schema()
        biz = _biz()
        if not biz:
            return {"error": "business_capabilities not loaded"}
        run = await _run(_db_get_run, run_id) if run_id else await _run(_db_active_run)
        if not run:
            return {"error": "no active simulation run — call biz.sim.start first"}
        scn = SCENARIOS.get(run["scenario"], SCENARIOS["empty"])
        rng = random.Random(int(run.get("seed", 0)) + int(run.get("steps", 0)))
        bias = scn.get("activity_bias", {"sale": 0.6, "expense": 0.4})
        cats = list(bias.keys()); weights = list(bias.values())
        avg = scn.get("avg_sale", 40.0)
        acct_ids = run.get("account_ids") or []
        stream_ids = run.get("stream_ids") or []
        if not acct_ids:
            return {"error": "run has no simulated accounts"}
        generated = 0
        for _ in range(max(1, min(200, int(n)))):
            kind = rng.choices(cats, weights=weights, k=1)[0]
            acct = rng.choice(acct_ids)
            stream = rng.choice(stream_ids) if stream_ids else ""
            base = avg * rng.uniform(0.4, 2.4)
            if kind in ("sale", "payout"):
                amount = round(base, 2); cat = "income" if kind == "sale" else "payout"
                desc = "Simulated sale" if kind == "sale" else "Simulated platform payout"
            elif kind == "fee":
                amount = -round(base * rng.uniform(0.05, 0.15), 2); cat = "fee"
                desc = "Simulated platform fee"
            else:  # expense
                amount = -round(base * rng.uniform(0.3, 1.1), 2); cat = "expense"
                desc = "Simulated expense (stock/ads/tools)"
            await _run(biz._db_ledger_add, {
                "account_id": acct, "stream_id": stream, "amount": amount,
                "category": cat, "description": desc, "is_sim": 1,
                "ref": run["id"], "meta": {"sim_step": run.get("steps", 0)}})
            if kind == "sale":
                # sales consume sim stock, so restock/low-stock goals stay live
                await _run(_db_sim_drain_inventory)
            generated += 1
        run["steps"] = int(run.get("steps", 0)) + 1
        await _run(_db_save_run, run)
        totals = await _run(_db_sim_net, stream_ids)
        await emit_event({"type": "business.sim", "stage": "step",
                          "message": f"sim step {run['steps']}: +{generated} txns, "
                                     f"net {totals['net']:,.2f}", "run_id": run["id"]})
        return {"ok": True, "generated": generated, "step": run["steps"], "totals": totals}

    @capability(
        "business.sim.status", http_method="GET", http_path="/business/sim/status",
        http_tags=["business"], memory="off", silent=True,
        description="Current simulation status: the active run, its step count and "
                    "sim totals, plus any recorded evaluations. "
                    "Input: run_id (str — default active). Output: {run, totals, evals}.")
    async def cap_sim_status(run_id: str = "", trace_id=None):
        await _ensure_sim_schema()
        run = await _run(_db_get_run, run_id) if run_id else await _run(_db_active_run)
        if not run:
            return {"run": None, "totals": None, "evals": []}
        totals = await _run(_db_sim_net, run.get("stream_ids") or [])
        evals = await _run(_db_list_evals, run["id"])
        return {"run": run, "totals": totals, "evals": evals}

    @capability(
        "business.sim.reset", http_method="POST", http_path="/business/sim/reset",
        http_tags=["business"],
        description="Reset simulation — purge ALL simulated streams, accounts and "
                    "sim ledger entries. The live business is untouched. "
                    "Input: confirm (bool!). Output: {ok, purged}.")
    async def cap_sim_reset(confirm: bool = False, trace_id=None):
        await _ensure_sim_schema()
        if not confirm:
            return {"error": "pass confirm=true to purge all simulated data"}
        runs = await _run(_db_all_runs)
        sids, aids = [], []
        for r in runs:
            sids += r.get("stream_ids") or []
            aids += r.get("account_ids") or []
        await _run(_db_purge_sim_data, sids, aids)
        await _run(_db_clear_runs)
        await emit_event({"type": "business.sim", "stage": "reset",
                          "message": "simulation reset — sim data purged"})
        return {"ok": True, "purged": {"streams": len(set(sids)), "accounts": len(set(aids))}}

    @capability(
        "business.sim.evaluate", http_method="POST", http_path="/business/sim/evaluate",
        http_tags=["business"],
        description="Record an agent-loop evaluation against the active sim, capture "
                    "the baseline sim state, and return the ready-to-run agent-loop "
                    "request the UI (or DAG Workshop) fires at /workshop/agent_loop/"
                    "stream. Score with biz.sim.score after the loop finishes. "
                    "Input: goal (str!), rubric (str — how success is judged), "
                    "agent_name (default 'business-operator'), run_id (default active). "
                    "Output: {ok, eval, agent_request}.")
    async def cap_sim_evaluate(goal: str = "", rubric: str = "",
                               agent_name: str = "business-operator",
                               run_id: str = "", trace_id=None):
        await _ensure_sim_schema()
        if not goal:
            return {"error": "goal required"}
        run = await _run(_db_get_run, run_id) if run_id else await _run(_db_active_run)
        if not run:
            return {"error": "no active simulation run — call biz.sim.start first"}
        baseline = await _run(_db_sim_net, run.get("stream_ids") or [])
        eid = f"eval_{uuid.uuid4().hex[:10]}"
        ev = {"id": eid, "run_id": run["id"], "goal": goal, "rubric": rubric,
              "agent_name": agent_name, "status": "defined", "baseline": baseline}
        await _run(_db_save_eval, ev)
        # Scope the agent to the simulated business + the operating toolkit.
        # The sandbox is ENFORCED server-side by cap_guard (execution-time
        # allowlist + is_sim pinned to 1 on every business.* call) — the note
        # below is orientation for the agent, not the security boundary.
        sim_note = (f"[SIMULATION EVAL {eid}] You are being evaluated inside the SIMULATED "
                    f"business sandbox. All business.* capabilities are locked to the sim "
                    f"world (is_sim=1 is applied for you) and your toolkit is fixed — "
                    f"capabilities outside it will be refused, so do not plan around them. "
                    f"Ground yourself FIRST with business.brief then business.dashboard. "
                    f"Simulated streams: "
                    f"{', '.join(run.get('stream_ids') or []) or '(none — you may create them)'}.")
        agent_request = {
            "goal": f"{sim_note}\n\nGOAL: {goal}",
            "version": "v6", "max_cycles": 12, "agent_name": agent_name,
            "record_history": True, "record_agent_name": "business-sim-eval",
            "satisfaction_check": True, "enable_expand": False, "require_approval": False,
            "allowed_caps": ",".join(_SIM_EVAL_CAPS),
            "base_toolkit": ",".join(_SIM_EVAL_CAPS),
            "cap_guard": {
                "label": "business-sim",
                "allow": list(_SIM_EVAL_CAPS),
                # belt-and-braces: even if the allowlist is edited, the real
                # store/commerce surface and the sim controls stay out of reach
                "deny": ["business.store.*", "business.product.*", "business.order.*",
                         "business.stock.*", "business.customer.*", "business.service.*",
                         "business.platform.*", "business.listing.*", "business.ship.*",
                         "business.sim.*"],
                "pin_args": {"business.*": {"is_sim": 1}},
            },
            "meta": {"biz_sim_eval": eid, "run_id": run["id"]},
        }
        await emit_event({"type": "business.sim", "stage": "evaluate",
                          "message": f"eval {eid} defined for goal: {goal[:80]}",
                          "run_id": run["id"], "eval_id": eid})
        return {"ok": True, "eval": ev, "agent_request": agent_request}

    @capability(
        "business.sim.score", http_method="POST", http_path="/business/sim/score",
        http_tags=["business"],
        description="Score a finished evaluation from the change in the sim ledger "
                    "since its baseline. Components: net income delta, goal "
                    "attainment, task completion. Input: eval_id (str!), "
                    "transcript (str — optional loop summary/pointer). "
                    "Output: {ok, score (0-100), components, delta}.")
    async def cap_sim_score(eval_id: str = "", transcript: str = "", trace_id=None):
        await _ensure_sim_schema()
        if not eval_id:
            return {"error": "eval_id required"}
        ev = await _run(_db_get_eval, eval_id)
        if not ev:
            return {"error": "eval not found"}
        run = await _run(_db_get_run, ev["run_id"])
        stream_ids = (run or {}).get("stream_ids") or []
        after = await _run(_db_sim_net, stream_ids)
        base = ev.get("baseline") or {"net": 0, "income": 0}
        net_delta = round(after["net"] - float(base.get("net", 0)), 2)
        income_delta = round(after["income"] - float(base.get("income", 0)), 2)
        # Goal component: measure the monthly goal of the simulated streams.
        biz = _biz()
        goal_total = 0.0
        if biz and stream_ids:
            for sid in stream_ids:
                s = await _run(biz._db_get, "biz_streams", sid)
                if s:
                    goal_total += float(s.get("goal_monthly") or 0)
        # Count tasks the agent created/completed in this run window.
        tasks_done = await _run(_db_count_sim_tasks)
        comp = {
            "net_income": max(0.0, net_delta),
            "goal_attainment_pct": round(income_delta / goal_total * 100, 1) if goal_total else None,
            "tasks_completed": tasks_done,
        }
        # Blend into a 0-100 score: reward positive net, goal progress, activity.
        net_score = max(0.0, min(50.0, net_delta / 20.0)) if net_delta > 0 else 0.0
        goal_score = 0.0
        if goal_total:
            goal_score = max(0.0, min(35.0, income_delta / goal_total * 35.0))
        task_score = min(15.0, tasks_done * 5.0)
        score = round(net_score + goal_score + task_score, 1)
        ev.update({"status": "scored", "score": score, "components": comp,
                   "transcript": transcript})
        await _run(_db_save_eval, ev)
        await emit_event({"type": "business.sim", "stage": "score",
                          "message": f"eval {eval_id} scored {score}/100 "
                                     f"(net {net_delta:+,.2f})", "eval_id": eval_id})
        return {"ok": True, "score": score, "components": comp,
                "delta": {"net": net_delta, "income": income_delta},
                "eval": ev}


# ── Eval/run store helpers (kept below the caps for readability) ──────────────

_SIM_EVAL_CAPS = [
    "business.brief", "business.dashboard", "business.graph",
    "business.stream.list", "business.stream.upsert",
    "business.account.list", "business.account.upsert", "business.txn.add", "business.txn.list",
    "business.inventory.list", "business.inventory.upsert", "business.inventory.adjust",
    "business.gig.list", "business.gig.upsert",
    "business.content.list", "business.content.upsert",
    "business.bounty.list", "business.bounty.upsert",
    "business.campaign.list", "business.campaign.upsert",
    "business.task.list", "business.task.upsert", "business.task.schedule",
    "web.search", "llm.summarize", "llm.generate",
]

def _eval_out(row) -> dict:
    d = dict(row)
    for k, dflt in (("baseline", {}), ("components", {})):
        try: d[k] = json.loads(d.get(k) or "{}")
        except Exception: d[k] = dflt
    return d

def _db_save_eval(ev: dict) -> dict:
    conn = _sqlite_conn()
    try:
        now = now_iso()
        prev = conn.execute("SELECT created_at FROM biz_sim_evals WHERE id=?",
                            (ev["id"],)).fetchone()
        created = (prev[0] if prev and prev[0] else now)
        conn.execute(
            "INSERT OR REPLACE INTO biz_sim_evals (id,run_id,goal,rubric,agent_name,"
            "status,baseline,score,components,transcript,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev["id"], ev.get("run_id", ""), ev.get("goal", ""), ev.get("rubric", ""),
             ev.get("agent_name", ""), ev.get("status", "defined"),
             json.dumps(ev.get("baseline") or {}),
             ev.get("score"), json.dumps(ev.get("components") or {}),
             ev.get("transcript", ""), created, now))
        conn.commit()
        r = conn.execute("SELECT * FROM biz_sim_evals WHERE id=?", (ev["id"],)).fetchone()
        return _eval_out(r)
    finally:
        conn.close()

def _db_get_eval(eid: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM biz_sim_evals WHERE id=?", (eid,)).fetchone()
        return _eval_out(r) if r else None
    finally:
        conn.close()

def _db_list_evals(run_id: str) -> List[dict]:
    conn = _sqlite_conn()
    try:
        rows = conn.execute("SELECT * FROM biz_sim_evals WHERE run_id=? "
                            "ORDER BY updated_at DESC", (run_id,)).fetchall()
        return [_eval_out(r) for r in rows]
    finally:
        conn.close()

def _db_all_runs() -> List[dict]:
    conn = _sqlite_conn()
    try:
        return [_sim_run_out(r) for r in
                conn.execute("SELECT * FROM biz_sim_runs").fetchall()]
    finally:
        conn.close()

def _db_clear_runs():
    conn = _sqlite_conn()
    try:
        conn.execute("DELETE FROM biz_sim_runs")
        conn.execute("DELETE FROM biz_sim_evals")
        conn.commit()
    finally:
        conn.close()

def _db_count_sim_tasks() -> int:
    """Completed SIM tasks (agent activity signal during eval) — real-book
    tasks must not inflate a simulation score."""
    conn = _sqlite_conn()
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM biz_tasks WHERE status='done' AND is_sim=1"
        ).fetchone()[0])
    finally:
        conn.close()


if _CAP_AVAILABLE:
    log.info("business_sim: ready (%d scenarios, agent-loop evaluation)", len(SCENARIOS))
