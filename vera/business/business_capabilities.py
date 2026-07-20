"""
business_capabilities.py — Agentic business operations (core)
=============================================================

The **Business** tab, radically widened from a single e-commerce shop into a
multi-stream *operations control surface* that a team of Vera agents can run
end-to-end, with one human overseeing.

Everything is modelled around **income streams** — a stream is one way money
comes in (an eBay shop, a Fiverr gig board, a YouTube channel, a bug-bounty
practice, a trading-card inventory, a freelance client book …). Each stream
owns its own inventory, money accounts, tasks and marketing, so the operator
can see per-stream performance and the agents can be scoped to one stream.

Subsystems (all plain ``@capability`` so the agent loop drives them, with the
panel — ``business_panel.html`` — as the human oversight + conversational
surface):

  • Streams      — the income streams themselves (kind, platform, monthly goal).
  • Money        — bank / PayPal / Stripe / Wise / crypto / cash / platform
                   balances, with a signed ledger (income/expense/fee/payout…)
                   that keeps each account's balance and drives the P&L.
  • Inventory    — generic *stream-scoped* inventory (so trading cards, print
                   stock, craft supplies … each live under their own stream)
                   with an audit trail. E-commerce product stock still lives in
                   the sibling ``commerce_*`` modules and is folded into the
                   dashboard.
  • Gigs         — freelance / Fiverr / Upwork jobs (lead → delivered → paid).
  • Content      — YouTube / short-form / blog pipeline (idea → published) with
                   metrics.
  • Bounties     — bug-bounty submissions (researching → paid) across HackerOne
                   / Bugcrowd / Intigriti / direct programs.
  • Marketing    — campaigns + social accounts (reusing the shared *accounts*
                   system for real credentials where present).
  • Tasks        — operational to-dos that schedule themselves into the Calendar
                   (post this, ship that, renew the other).
  • Graph        — a business network graph (streams ↔ accounts ↔ customers ↔
                   platforms ↔ content …) for the panel + the simulator.
  • Dashboard    — one aggregate "state of the business", and ``biz.brief`` — a
                   compact natural-language briefing the conversational agent
                   loads as context.

Simulation + agent evaluation live in the sibling ``business_sim.py``; the USB
thermal-printer / serial-forwarding subsystem lives in
``thermal_printer_capabilities.py`` (deliberately its own module, surfaced as an
element inside this panel).

Storage: the shared Data-Fabric SQLite db (``_sqlite_conn``), same as commerce
and markets. Fresh WAL connections are opened per operation inside
``run_in_executor`` so we never block the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path as _Path
from typing import Dict, List, Optional

log = logging.getLogger("vera.business")

try:
    from Vera.vera.capability_orchestration import (
        APP, capability, emit_event, now_iso, register_ui, enum_schema,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.business").warning("business caps unavailable: %s", e)
    _CAP_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Constants — the shape of the business
# ─────────────────────────────────────────────────────────────────────────────

STREAM_KINDS   = ["ecommerce", "freelance", "content", "bounty", "cards",
                  "services", "reselling", "affiliate", "saas", "other"]
STREAM_STATUS  = ["active", "paused", "planning", "archived"]

ACCOUNT_KINDS  = ["bank", "paypal", "stripe", "wise", "crypto", "cash",
                  "platform_balance", "revolut", "other"]
LEDGER_CATS    = ["income", "expense", "fee", "payout", "refund", "transfer",
                  "adjustment", "tax"]

INV_STATUS     = ["in_stock", "listed", "sold", "reserved", "damaged"]

GIG_PLATFORMS  = ["fiverr", "upwork", "freelancer", "peopleperhour", "direct", "other"]
GIG_STATUS     = ["lead", "quoted", "active", "review", "delivered", "paid",
                  "cancelled"]

CONTENT_PLATFORMS = ["youtube", "tiktok", "instagram", "x", "linkedin",
                     "blog", "podcast", "other"]
CONTENT_STATUS = ["idea", "scripting", "recording", "editing", "scheduled",
                  "published", "archived"]

BOUNTY_PLATFORMS = ["hackerone", "bugcrowd", "intigriti", "yeswehack",
                    "direct", "other"]
BOUNTY_STATUS  = ["researching", "submitted", "triaged", "accepted",
                  "paid", "duplicate", "informative", "rejected"]

SOCIAL_PLATFORMS = ["youtube", "x", "instagram", "tiktok", "facebook",
                    "linkedin", "fiverr", "ebay", "etsy", "reddit", "other"]
CAMPAIGN_STATUS = ["draft", "scheduled", "running", "paused", "done"]

TASK_KINDS     = ["post", "list", "ship", "restock", "research", "outreach",
                  "invoice", "admin", "content", "followup", "other"]
TASK_STATUS    = ["todo", "scheduled", "doing", "done", "blocked", "cancelled"]

_SCHEMA_READY = False


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"

def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def _i(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

def _b(v, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("1", "true", "yes", "on")

def _json_load(v, default):
    if v is None or v == "":
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default

async def _run(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

def _add_col(conn, table: str, col: str, decl: str):
    """Add a column to an existing table if missing (SQLite has no IF NOT EXISTS)."""
    try:
        have = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    except Exception:
        pass


def _ensure_schema_sync():
    global _SCHEMA_READY
    conn = _sqlite_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS biz_streams (
                id           TEXT PRIMARY KEY,
                name         TEXT,
                kind         TEXT,
                platform     TEXT,
                status       TEXT DEFAULT 'active',
                goal_monthly REAL DEFAULT 0,
                currency     TEXT DEFAULT 'USD',
                color        TEXT,
                icon         TEXT,
                description  TEXT,
                is_sim       INTEGER DEFAULT 0,
                config       TEXT,
                created_at   TEXT,
                updated_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS biz_accounts (
                id           TEXT PRIMARY KEY,
                name         TEXT,
                kind         TEXT,
                currency     TEXT DEFAULT 'USD',
                balance      REAL DEFAULT 0,
                stream_id    TEXT,
                external_ref TEXT,
                acct_id      TEXT,
                is_sim       INTEGER DEFAULT 0,
                config       TEXT,
                created_at   TEXT,
                updated_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS biz_ledger (
                id           TEXT PRIMARY KEY,
                account_id   TEXT,
                stream_id    TEXT,
                ts           TEXT,
                amount       REAL DEFAULT 0,
                category     TEXT,
                description  TEXT,
                ref          TEXT,
                is_sim       INTEGER DEFAULT 0,
                meta         TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_biz_ledger_acct ON biz_ledger(account_id);
            CREATE INDEX IF NOT EXISTS ix_biz_ledger_stream ON biz_ledger(stream_id);
            CREATE INDEX IF NOT EXISTS ix_biz_ledger_ts ON biz_ledger(ts);

            CREATE TABLE IF NOT EXISTS biz_inventory (
                id            TEXT PRIMARY KEY,
                stream_id     TEXT,
                kind          TEXT,
                sku           TEXT,
                name          TEXT,
                category      TEXT,
                condition     TEXT,
                grade         TEXT,
                set_name      TEXT,
                qty           INTEGER DEFAULT 0,
                cost          REAL DEFAULT 0,
                market_value  REAL DEFAULT 0,
                price         REAL DEFAULT 0,
                currency      TEXT DEFAULT 'USD',
                location      TEXT,
                status        TEXT DEFAULT 'in_stock',
                listed_on     TEXT,
                is_sim        INTEGER DEFAULT 0,
                attributes    TEXT,
                created_at    TEXT,
                updated_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_biz_inv_stream ON biz_inventory(stream_id);

            CREATE TABLE IF NOT EXISTS biz_gigs (
                id           TEXT PRIMARY KEY,
                stream_id    TEXT,
                platform     TEXT,
                title        TEXT,
                client       TEXT,
                status       TEXT DEFAULT 'lead',
                price        REAL DEFAULT 0,
                currency     TEXT DEFAULT 'USD',
                due_at       TEXT,
                deliverables TEXT,
                notes        TEXT,
                is_sim       INTEGER DEFAULT 0,
                created_at   TEXT,
                updated_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS biz_content (
                id           TEXT PRIMARY KEY,
                stream_id    TEXT,
                platform     TEXT,
                title        TEXT,
                status       TEXT DEFAULT 'idea',
                publish_at   TEXT,
                url          TEXT,
                script       TEXT,
                tags         TEXT,
                metrics      TEXT,
                notes        TEXT,
                is_sim       INTEGER DEFAULT 0,
                created_at   TEXT,
                updated_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS biz_bounties (
                id           TEXT PRIMARY KEY,
                stream_id    TEXT,
                platform     TEXT,
                program      TEXT,
                target       TEXT,
                title        TEXT,
                severity     TEXT,
                status       TEXT DEFAULT 'researching',
                reward       REAL DEFAULT 0,
                currency     TEXT DEFAULT 'USD',
                submitted_at TEXT,
                url          TEXT,
                notes        TEXT,
                is_sim       INTEGER DEFAULT 0,
                created_at   TEXT,
                updated_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS biz_social (
                id           TEXT PRIMARY KEY,
                stream_id    TEXT,
                platform     TEXT,
                handle       TEXT,
                acct_id      TEXT,
                url          TEXT,
                followers    INTEGER DEFAULT 0,
                status       TEXT DEFAULT 'active',
                is_sim       INTEGER DEFAULT 0,
                config       TEXT,
                created_at   TEXT,
                updated_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS biz_campaigns (
                id           TEXT PRIMARY KEY,
                stream_id    TEXT,
                name         TEXT,
                goal         TEXT,
                channels     TEXT,
                budget       REAL DEFAULT 0,
                currency     TEXT DEFAULT 'USD',
                status       TEXT DEFAULT 'draft',
                start_at     TEXT,
                end_at       TEXT,
                metrics      TEXT,
                notes        TEXT,
                is_sim       INTEGER DEFAULT 0,
                created_at   TEXT,
                updated_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS biz_tasks (
                id             TEXT PRIMARY KEY,
                stream_id      TEXT,
                title          TEXT,
                kind           TEXT,
                status         TEXT DEFAULT 'todo',
                due_at         TEXT,
                assigned_agent TEXT,
                cal_event_id   TEXT,
                cal_todo_id    TEXT,
                ref            TEXT,
                notes          TEXT,
                is_sim         INTEGER DEFAULT 0,
                created_at     TEXT,
                updated_at     TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_biz_tasks_stream ON biz_tasks(stream_id);
            CREATE INDEX IF NOT EXISTS ix_biz_tasks_status ON biz_tasks(status);
        """)
        # Self-heal is_sim scoping on pre-existing databases: EVERY business
        # entity is namespaced live vs simulated so a sim run can never read or
        # write the real books (accounts/ledger had the column from day one).
        for _t in ("biz_streams", "biz_inventory", "biz_gigs", "biz_content",
                   "biz_bounties", "biz_social", "biz_campaigns", "biz_tasks"):
            _add_col(conn, _t, "is_sim", "INTEGER DEFAULT 0")
        conn.commit()
    finally:
        conn.close()
    _SCHEMA_READY = True

async def _ensure_schema():
    if not _SCHEMA_READY:
        await _run(_ensure_schema_sync)


# ─────────────────────────────────────────────────────────────────────────────
# Generic table layer — every entity is a flat row with a few JSON columns.
# Keeps the (large) capability surface small and uniform.
# ─────────────────────────────────────────────────────────────────────────────

# table -> (columns tuple, json columns set, id prefix)
_TABLES = {
    "biz_streams":   (("id", "name", "kind", "platform", "status", "goal_monthly",
                       "currency", "color", "icon", "description", "is_sim",
                       "config", "created_at", "updated_at"), {"config"}, "stream"),
    "biz_accounts":  (("id", "name", "kind", "currency", "balance", "stream_id",
                       "external_ref", "acct_id", "is_sim", "config",
                       "created_at", "updated_at"), {"config"}, "acct"),
    "biz_inventory": (("id", "stream_id", "kind", "sku", "name", "category",
                       "condition", "grade", "set_name", "qty", "cost",
                       "market_value", "price", "currency", "location", "status",
                       "listed_on", "is_sim", "attributes", "created_at",
                       "updated_at"), {"attributes"}, "item"),
    "biz_gigs":      (("id", "stream_id", "platform", "title", "client", "status",
                       "price", "currency", "due_at", "deliverables", "notes",
                       "is_sim", "created_at", "updated_at"), {"deliverables"}, "gig"),
    "biz_content":   (("id", "stream_id", "platform", "title", "status",
                       "publish_at", "url", "script", "tags", "metrics", "notes",
                       "is_sim", "created_at", "updated_at"), {"tags", "metrics"}, "vid"),
    "biz_bounties":  (("id", "stream_id", "platform", "program", "target", "title",
                       "severity", "status", "reward", "currency", "submitted_at",
                       "url", "notes", "is_sim", "created_at", "updated_at"), set(), "bnty"),
    "biz_social":    (("id", "stream_id", "platform", "handle", "acct_id", "url",
                       "followers", "status", "is_sim", "config", "created_at",
                       "updated_at"), {"config"}, "soc"),
    "biz_campaigns": (("id", "stream_id", "name", "goal", "channels", "budget",
                       "currency", "status", "start_at", "end_at", "metrics",
                       "notes", "is_sim", "created_at", "updated_at"),
                      {"channels", "metrics"}, "camp"),
    "biz_tasks":     (("id", "stream_id", "title", "kind", "status", "due_at",
                       "assigned_agent", "cal_event_id", "cal_todo_id", "ref",
                       "notes", "is_sim", "created_at", "updated_at"), set(), "task"),
}

# integer columns per table (coerced on write, so booleans/counts stay clean)
_INT_COLS = {"qty", "followers", "is_sim"}
_FLOAT_COLS = {"goal_monthly", "balance", "amount", "cost", "market_value",
               "price", "reward", "budget"}


def _row_out(table: str, row) -> dict:
    if row is None:
        return None
    _, json_cols, _pfx = _TABLES[table]
    d = dict(row)
    for c in json_cols:
        d[c] = _json_load(d.get(c), {} if c in ("config", "metrics") else [])
    return d

def _coerce(col: str, v):
    if col in _INT_COLS:
        return _i(v)
    if col in _FLOAT_COLS:
        return _f(v)
    return v

def _db_upsert(table: str, fields: dict) -> dict:
    cols, json_cols, pfx = _TABLES[table]
    conn = _sqlite_conn()
    try:
        rid = fields.get("id") or ""
        existing = conn.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone() if rid else None
        base = _row_out(table, existing) if existing else {}
        rid = base.get("id") or rid or _new_id(pfx)
        merged = {**base, **{k: v for k, v in fields.items() if v is not None}}
        now = now_iso()
        row = {}
        for c in cols:
            if c == "id":
                row[c] = rid
            elif c == "created_at":
                row[c] = base.get("created_at") or now
            elif c == "updated_at":
                row[c] = now
            elif c in json_cols:
                row[c] = json.dumps(merged.get(c) if merged.get(c) is not None
                                    else ({} if c in ("config", "metrics") else []))
            else:
                row[c] = _coerce(c, merged.get(c, ""))
        placeholders = ",".join("?" for _ in cols)
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
            tuple(row[c] for c in cols))
        conn.commit()
        return _row_out(table, conn.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone())
    finally:
        conn.close()

def _db_get(table: str, rid: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
        return _row_out(table, r)
    finally:
        conn.close()

def _db_delete(table: str, rid: str) -> bool:
    conn = _sqlite_conn()
    try:
        cur = conn.execute(f"DELETE FROM {table} WHERE id=?", (rid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()

def _db_list(table: str, where: dict = None, order: str = "updated_at DESC",
             limit: int = 500, like: dict = None) -> List[dict]:
    conn = _sqlite_conn()
    try:
        sql = f"SELECT * FROM {table}"
        clauses, args = [], []
        for k, v in (where or {}).items():
            if v is None or v == "":
                continue
            clauses.append(f"{k}=?"); args.append(_coerce(k, v))
        for k, v in (like or {}).items():
            if v:
                clauses.append(f"{k} LIKE ?"); args.append(f"%{v}%")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {order} LIMIT ?"; args.append(int(limit))
        return [_row_out(table, r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Money — balance is derived from the ledger so it can never drift.
# ─────────────────────────────────────────────────────────────────────────────

def _db_ledger_add(entry: dict) -> dict:
    conn = _sqlite_conn()
    try:
        eid = _new_id("txn")
        now = now_iso()
        acct = conn.execute("SELECT * FROM biz_accounts WHERE id=?",
                            (entry.get("account_id"),)).fetchone()
        conn.execute(
            "INSERT INTO biz_ledger (id,account_id,stream_id,ts,amount,category,"
            "description,ref,is_sim,meta) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid, entry.get("account_id", ""),
             entry.get("stream_id") or (dict(acct).get("stream_id") if acct else ""),
             entry.get("ts") or now, _f(entry.get("amount")),
             entry.get("category", "income"), entry.get("description", ""),
             entry.get("ref", ""), _i(entry.get("is_sim", 0)),
             json.dumps(entry.get("meta") or {})))
        # Roll the account balance forward.
        if acct:
            newbal = _f(dict(acct).get("balance")) + _f(entry.get("amount"))
            conn.execute("UPDATE biz_accounts SET balance=?, updated_at=? WHERE id=?",
                         (newbal, now, entry["account_id"]))
        conn.commit()
        r = conn.execute("SELECT * FROM biz_ledger WHERE id=?", (eid,)).fetchone()
        d = dict(r); d["meta"] = _json_load(d.get("meta"), {})
        return d
    finally:
        conn.close()

def _db_ledger_list(account_id: str = "", stream_id: str = "", category: str = "",
                    is_sim: Optional[int] = None, limit: int = 300) -> List[dict]:
    conn = _sqlite_conn()
    try:
        sql = "SELECT * FROM biz_ledger"; clauses, args = [], []
        if account_id: clauses.append("account_id=?"); args.append(account_id)
        if stream_id:  clauses.append("stream_id=?");  args.append(stream_id)
        if category:   clauses.append("category=?");   args.append(category)
        if is_sim is not None: clauses.append("is_sim=?"); args.append(int(is_sim))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts DESC LIMIT ?"; args.append(int(limit))
        out = []
        for r in conn.execute(sql, args).fetchall():
            d = dict(r); d["meta"] = _json_load(d.get("meta"), {}); out.append(d)
        return out
    finally:
        conn.close()

def _db_recalc_balance(account_id: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM biz_accounts WHERE id=?", (account_id,)).fetchone()
        if not r:
            return None
        tot = conn.execute("SELECT COALESCE(SUM(amount),0) FROM biz_ledger WHERE account_id=?",
                           (account_id,)).fetchone()[0]
        conn.execute("UPDATE biz_accounts SET balance=?, updated_at=? WHERE id=?",
                     (_f(tot), now_iso(), account_id))
        conn.commit()
        return _row_out("biz_accounts", conn.execute(
            "SELECT * FROM biz_accounts WHERE id=?", (account_id,)).fetchone())
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Inventory stock adjust (audit via ledger-like reuse is overkill; qty column
# is authoritative for stream-scoped inventory).
# ─────────────────────────────────────────────────────────────────────────────

def _db_inv_adjust(item_id: str, delta: int) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM biz_inventory WHERE id=? OR sku=?",
                        (item_id, item_id)).fetchone()
        if not r:
            return None
        row = dict(r)
        newq = _i(row.get("qty")) + int(delta)
        conn.execute("UPDATE biz_inventory SET qty=?, updated_at=? WHERE id=?",
                     (newq, now_iso(), row["id"]))
        conn.commit()
        row["qty"] = newq
        return _row_out("biz_inventory", row)
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard + P&L aggregate
# ─────────────────────────────────────────────────────────────────────────────

def _db_dashboard(is_sim: int = 0) -> dict:
    conn = _sqlite_conn()
    try:
        streams = [_row_out("biz_streams", r) for r in
                   conn.execute("SELECT * FROM biz_streams WHERE status!='archived' "
                                "AND is_sim=? ORDER BY name", (is_sim,)).fetchall()]
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        per_stream = []
        tot_income = tot_expense = 0.0
        goal_total = 0.0
        for s in streams:
            inc = conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM biz_ledger WHERE stream_id=? "
                "AND is_sim=? AND amount>0 AND ts>=?", (s["id"], is_sim, cutoff)).fetchone()[0]
            exp = conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM biz_ledger WHERE stream_id=? "
                "AND is_sim=? AND amount<0 AND ts>=?", (s["id"], is_sim, cutoff)).fetchone()[0]
            tot_income += _f(inc); tot_expense += _f(exp); goal_total += _f(s.get("goal_monthly"))
            per_stream.append({
                "id": s["id"], "name": s["name"], "kind": s["kind"],
                "status": s["status"], "goal_monthly": _f(s.get("goal_monthly")),
                "revenue_30d": round(_f(inc), 2), "expense_30d": round(abs(_f(exp)), 2),
                "net_30d": round(_f(inc) + _f(exp), 2),
                "goal_pct": round(_f(inc) / _f(s["goal_monthly"]) * 100, 1)
                            if _f(s.get("goal_monthly")) else None,
                "color": s.get("color"), "icon": s.get("icon"),
            })
        cash = conn.execute("SELECT COALESCE(SUM(balance),0) FROM biz_accounts "
                            "WHERE is_sim=?", (is_sim,)).fetchone()[0]
        n_acct = conn.execute("SELECT COUNT(*) FROM biz_accounts WHERE is_sim=?",
                             (is_sim,)).fetchone()[0]
        open_gigs = conn.execute(
            "SELECT COUNT(*) FROM biz_gigs WHERE status IN "
            "('lead','quoted','active','review') AND is_sim=?", (is_sim,)).fetchone()[0]
        pipe_content = conn.execute(
            "SELECT COUNT(*) FROM biz_content WHERE status NOT IN "
            "('published','archived') AND is_sim=?", (is_sim,)).fetchone()[0]
        open_bounties = conn.execute(
            "SELECT COUNT(*) FROM biz_bounties WHERE status IN "
            "('researching','submitted','triaged','accepted') AND is_sim=?",
            (is_sim,)).fetchone()[0]
        inv_val = conn.execute(
            "SELECT COALESCE(SUM(market_value*qty),0) FROM biz_inventory "
            "WHERE is_sim=?", (is_sim,)).fetchone()[0]
        open_tasks = conn.execute(
            "SELECT COUNT(*) FROM biz_tasks WHERE status IN "
            "('todo','scheduled','doing','blocked') AND is_sim=?", (is_sim,)).fetchone()[0]
        campaigns = conn.execute(
            "SELECT COUNT(*) FROM biz_campaigns WHERE status IN ('scheduled','running') "
            "AND is_sim=?", (is_sim,)).fetchone()[0]
        return {
            "streams": per_stream,
            "n_streams": len(streams),
            "cash_balance": round(_f(cash), 2),
            "n_accounts": _i(n_acct),
            "revenue_30d": round(tot_income, 2),
            "expense_30d": round(abs(tot_expense), 2),
            "net_30d": round(tot_income + tot_expense, 2),
            "goal_monthly_total": round(goal_total, 2),
            "goal_pct": round(tot_income / goal_total * 100, 1) if goal_total else None,
            "open_gigs": _i(open_gigs),
            "content_pipeline": _i(pipe_content),
            "open_bounties": _i(open_bounties),
            "inventory_value": round(_f(inv_val), 2),
            "open_tasks": _i(open_tasks),
            "active_campaigns": _i(campaigns),
            "is_sim": is_sim,
        }
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Business network graph — nodes + edges for vera_graph.js and the simulator.
# ─────────────────────────────────────────────────────────────────────────────

def _db_graph(is_sim: int = 0) -> dict:
    conn = _sqlite_conn()
    try:
        nodes, edges = [], []
        def add(nid, label, ntype, **extra):
            nodes.append({"id": nid, "label": label, "type": ntype, **extra})
        def link(a, b, rel):
            edges.append({"source": a, "target": b, "label": rel, "rel": rel})

        add("biz:root", "Business" + (" (sim)" if is_sim else ""), "root")
        for s in conn.execute("SELECT * FROM biz_streams WHERE status!='archived' "
                              "AND is_sim=?", (is_sim,)).fetchall():
            s = dict(s)
            add(s["id"], s["name"], "stream", kind=s.get("kind"),
                color=s.get("color"), status=s.get("status"))
            link("biz:root", s["id"], "runs")
        for a in conn.execute("SELECT * FROM biz_accounts WHERE is_sim=?", (is_sim,)).fetchall():
            a = dict(a)
            add(a["id"], f"{a['name']} ({a.get('currency')})", "account",
                kind=a.get("kind"), balance=a.get("balance"))
            if a.get("stream_id"):
                link(a["stream_id"], a["id"], "funds")
            else:
                link("biz:root", a["id"], "holds")
        for tbl, ntype, label_col, rel in (
            ("biz_gigs", "gig", "title", "gig"),
            ("biz_content", "content", "title", "content"),
            ("biz_bounties", "bounty", "program", "bounty"),
            ("biz_social", "social", "handle", "channel"),
            ("biz_campaigns", "campaign", "name", "campaign"),
        ):
            for r in conn.execute(f"SELECT * FROM {tbl} WHERE is_sim=? LIMIT 300",
                                  (is_sim,)).fetchall():
                r = dict(r)
                add(r["id"], r.get(label_col) or r["id"], ntype,
                    status=r.get("status"), platform=r.get("platform"))
                parent = r.get("stream_id") or "biz:root"
                link(parent, r["id"], rel)
                if tbl == "biz_social" and r.get("platform"):
                    pnode = f"plat:{r['platform']}"
                    if not any(n["id"] == pnode for n in nodes):
                        add(pnode, r["platform"].title(), "platform")
                    link(r["id"], pnode, "on")
        return {"nodes": nodes, "edges": edges,
                "counts": {"nodes": len(nodes), "edges": len(edges)}}
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# Capabilities
# ═════════════════════════════════════════════════════════════════════════════

if _CAP_AVAILABLE:

    # ── Streams ──────────────────────────────────────────────────────────────

    @capability(
        "business.stream.list", http_method="GET", http_path="/business/stream/list",
        http_tags=["business"], memory="off", silent=True,
        schema=enum_schema(status=STREAM_STATUS, kind=STREAM_KINDS),
        description="List income streams. Input: status (str), kind (str), "
                    "is_sim (int 0/1 — 0 live business (default), 1 simulation "
                    "sandbox, -1 both), limit (int). Output: {streams:[...], count}.")
    async def cap_stream_list(status: str = "", kind: str = "", is_sim: int = 0,
                              limit: int = 200, trace_id=None):
        await _ensure_schema()
        items = await _run(_db_list, "biz_streams",
                           {"status": status, "kind": kind,
                            "is_sim": None if _i(is_sim) < 0 else _i(is_sim)},
                           "name ASC", int(limit), None)
        return {"streams": items, "count": len(items)}

    @capability(
        "business.stream.upsert", http_method="POST", http_path="/business/stream/upsert",
        http_tags=["business"],
        schema=enum_schema(kind=STREAM_KINDS, status=STREAM_STATUS),
        description="Create or update an income stream. Input: id (omit to create), "
                    "name (str!), kind (str), platform, status, goal_monthly (float — "
                    "monthly revenue target), currency, color, icon (emoji), description, "
                    "is_sim (int 0/1 — 1 = simulation sandbox; omit to keep existing), "
                    "config (dict). Output: {ok, stream}.")
    async def cap_stream_upsert(
        id: str = "", name: str = "", kind: str = "other", platform: str = "",
        status: str = "active", goal_monthly: float = 0.0, currency: str = "USD",
        color: str = "", icon: str = "", description: str = "",
        is_sim: int = -1, config: Dict = None, trace_id=None):
        await _ensure_schema()
        if not (name or id):
            return {"error": "name required"}
        s = await _run(_db_upsert, "biz_streams", {
            "id": id or None, "name": name or None, "kind": kind, "platform": platform,
            "status": status, "goal_monthly": goal_monthly, "currency": currency,
            "color": color or None, "icon": icon or None,
            "description": description or None,
            "is_sim": None if _i(is_sim) < 0 else _i(is_sim), "config": config})
        await emit_event({"type": "biz.progress", "stage": "stream",
                          "message": f"saved stream {s.get('name')}"})
        return {"ok": True, "stream": s}

    @capability(
        "business.stream.delete", http_method="POST", http_path="/business/stream/delete",
        http_tags=["business"],
        description="Delete an income stream by id. Input: id (str!). Output: {ok}.")
    async def cap_stream_delete(id: str = "", trace_id=None):
        await _ensure_schema()
        if not id:
            return {"error": "id required"}
        ok = await _run(_db_delete, "biz_streams", id)
        return {"ok": ok} if ok else {"error": "not found"}

    # ── Money accounts ───────────────────────────────────────────────────────

    @capability(
        "business.account.list", http_method="GET", http_path="/business/account/list",
        http_tags=["business"], memory="off", silent=True,
        description="List money accounts (bank/paypal/stripe/crypto/cash/…). "
                    "Input: stream_id (str), is_sim (int 0/1 — filter live vs "
                    "simulated), limit (int). Output: {accounts:[...], count, total_balance}.")
    async def cap_account_list(stream_id: str = "", is_sim: int = 0, limit: int = 200, trace_id=None):
        await _ensure_schema()
        items = await _run(_db_list, "biz_accounts",
                           {"stream_id": stream_id, "is_sim": is_sim},
                           "name ASC", int(limit), None)
        tot = sum(_f(a.get("balance")) for a in items)
        return {"accounts": items, "count": len(items), "total_balance": round(tot, 2)}

    @capability(
        "business.account.upsert", http_method="POST", http_path="/business/account/upsert",
        http_tags=["business"],
        schema=enum_schema(kind=ACCOUNT_KINDS),
        description="Create or update a money account. Balance is normally driven "
                    "by the ledger — set balance only to seed an opening figure. "
                    "Input: id (omit to create), name (str!), kind, currency, "
                    "balance (float — opening), stream_id, external_ref, "
                    "acct_id (link to a shared accounts-system credential), "
                    "is_sim (int 0/1 — 1 = simulation sandbox; omit to keep "
                    "existing). Output: {ok, account}.")
    async def cap_account_upsert(
        id: str = "", name: str = "", kind: str = "bank", currency: str = "USD",
        balance: float = 0.0, stream_id: str = "", external_ref: str = "",
        acct_id: str = "", is_sim: int = -1, config: Dict = None, trace_id=None):
        await _ensure_schema()
        if not (name or id):
            return {"error": "name required"}
        fields = {"id": id or None, "name": name or None, "kind": kind,
                  "currency": currency, "stream_id": stream_id or None,
                  "external_ref": external_ref or None, "acct_id": acct_id or None,
                  "is_sim": None if _i(is_sim) < 0 else _i(is_sim), "config": config}
        # Only seed balance on create (avoid clobbering the ledger-derived value).
        if not id:
            fields["balance"] = balance
        a = await _run(_db_upsert, "biz_accounts", fields)
        return {"ok": True, "account": a}

    @capability(
        "business.account.delete", http_method="POST", http_path="/business/account/delete",
        http_tags=["business"],
        description="Delete a money account by id. Input: id (str!). Output: {ok}.")
    async def cap_account_delete(id: str = "", trace_id=None):
        await _ensure_schema()
        if not id:
            return {"error": "id required"}
        ok = await _run(_db_delete, "biz_accounts", id)
        return {"ok": ok} if ok else {"error": "not found"}

    @capability(
        "business.txn.add", http_method="POST", http_path="/business/txn/add",
        http_tags=["business"],
        schema=enum_schema(category=LEDGER_CATS),
        description="Record a money transaction and roll the account balance. "
                    "Positive amount = money in, negative = money out. "
                    "Input: account_id (str!), amount (float!), category "
                    "(income|expense|fee|payout|refund|transfer|adjustment|tax), "
                    "description, stream_id (defaults to the account's), ref, "
                    "meta (dict), is_sim (int 0/1 — omit to inherit from the "
                    "account, so sim accounts always get sim transactions). "
                    "Output: {ok, txn, balance}.")
    async def cap_txn_add(
        account_id: str = "", amount: float = 0.0, category: str = "income",
        description: str = "", stream_id: str = "", ref: str = "",
        meta: Dict = None, is_sim: int = -1, trace_id=None):
        await _ensure_schema()
        if not account_id:
            return {"error": "account_id required"}
        if _f(amount) == 0:
            return {"error": "amount must be non-zero"}
        # Inherit the sim flag from the account when the caller didn't say —
        # a transaction on a simulated account can never leak into the live P&L.
        if _i(is_sim, -1) < 0:
            acct0 = await _run(_db_get, "biz_accounts", account_id)
            is_sim = _i((acct0 or {}).get("is_sim"))
        txn = await _run(_db_ledger_add, {
            "account_id": account_id, "amount": amount, "category": category,
            "description": description, "stream_id": stream_id or None,
            "ref": ref, "meta": meta or {}, "is_sim": _i(is_sim)})
        acct = await _run(_db_get, "biz_accounts", account_id)
        await emit_event({"type": "biz.progress", "stage": "txn",
                          "message": f"{category} {_f(amount):+.2f} → {acct.get('name') if acct else account_id}"})
        return {"ok": True, "txn": txn, "balance": acct.get("balance") if acct else None}

    @capability(
        "business.txn.list", http_method="GET", http_path="/business/txn/list",
        http_tags=["business"], memory="off", silent=True,
        schema=enum_schema(category=LEDGER_CATS),
        description="List ledger transactions. Input: account_id, stream_id, "
                    "category, is_sim (int 0/1), limit (int). "
                    "Output: {txns:[...], count, income, expense, net}.")
    async def cap_txn_list(account_id: str = "", stream_id: str = "", category: str = "",
                           is_sim: int = 0, limit: int = 300, trace_id=None):
        await _ensure_schema()
        txns = await _run(_db_ledger_list, account_id, stream_id, category, is_sim, int(limit))
        income = sum(_f(t["amount"]) for t in txns if _f(t["amount"]) > 0)
        expense = sum(_f(t["amount"]) for t in txns if _f(t["amount"]) < 0)
        return {"txns": txns, "count": len(txns), "income": round(income, 2),
                "expense": round(abs(expense), 2), "net": round(income + expense, 2)}

    @capability(
        "business.account.recalc", http_method="POST", http_path="/business/account/recalc",
        http_tags=["business"],
        description="Recompute an account's balance from its full ledger (repair "
                    "drift). Input: account_id (str!). Output: {ok, account}.")
    async def cap_account_recalc(account_id: str = "", trace_id=None):
        await _ensure_schema()
        if not account_id:
            return {"error": "account_id required"}
        a = await _run(_db_recalc_balance, account_id)
        return {"ok": True, "account": a} if a else {"error": "not found"}

    # ── Inventory (stream-scoped, generic — covers trading cards etc.) ────────

    @capability(
        "business.inventory.list", http_method="GET", http_path="/business/inventory/list",
        http_tags=["business"], memory="off", silent=True,
        schema=enum_schema(status=INV_STATUS),
        description="List stream-scoped inventory items (trading cards, print "
                    "stock, supplies…). Input: stream_id, kind (e.g. 'card'), "
                    "status, q (search name/sku/set), is_sim (int 0/1 — 0 live "
                    "(default), 1 simulation, -1 both), limit. "
                    "Output: {items:[...], count, total_value}.")
    async def cap_inventory_list(stream_id: str = "", kind: str = "", status: str = "",
                                 q: str = "", is_sim: int = 0, limit: int = 500,
                                 trace_id=None):
        await _ensure_schema()
        items = await _run(_db_list, "biz_inventory",
                           {"stream_id": stream_id, "kind": kind, "status": status,
                            "is_sim": None if _i(is_sim) < 0 else _i(is_sim)},
                           "updated_at DESC", int(limit),
                           {"name": q} if q else None)
        val = sum(_f(i.get("market_value")) * _i(i.get("qty")) for i in items)
        return {"items": items, "count": len(items), "total_value": round(val, 2)}

    @capability(
        "business.inventory.upsert", http_method="POST", http_path="/business/inventory/upsert",
        http_tags=["business"],
        schema=enum_schema(status=INV_STATUS),
        description="Create or update an inventory item. Card-friendly fields: "
                    "set_name, grade, condition, number in attributes. "
                    "Input: id (omit to create), stream_id (str!), kind (e.g. card), "
                    "sku, name (str!), category, condition, grade, set_name, qty (int), "
                    "cost (float), market_value (float), price (float), currency, "
                    "location, status, listed_on, is_sim (int 0/1 — 1 = simulation "
                    "sandbox; omit to keep existing), attributes (dict). "
                    "Output: {ok, item}.")
    async def cap_inventory_upsert(
        id: str = "", stream_id: str = "", kind: str = "", sku: str = "",
        name: str = "", category: str = "", condition: str = "", grade: str = "",
        set_name: str = "", qty: int = 0, cost: float = 0.0, market_value: float = 0.0,
        price: float = 0.0, currency: str = "USD", location: str = "",
        status: str = "in_stock", listed_on: str = "", is_sim: int = -1,
        attributes: Dict = None, trace_id=None):
        await _ensure_schema()
        if not (name or id):
            return {"error": "name required"}
        item = await _run(_db_upsert, "biz_inventory", {
            "id": id or None, "stream_id": stream_id or None, "kind": kind or None,
            "sku": sku or None, "name": name or None, "category": category or None,
            "condition": condition or None, "grade": grade or None,
            "set_name": set_name or None, "qty": qty, "cost": cost,
            "market_value": market_value, "price": price, "currency": currency,
            "location": location or None, "status": status,
            "listed_on": listed_on or None,
            "is_sim": None if _i(is_sim) < 0 else _i(is_sim),
            "attributes": attributes})
        return {"ok": True, "item": item}

    @capability(
        "business.inventory.adjust", http_method="POST", http_path="/business/inventory/adjust",
        http_tags=["business"],
        description="Adjust an inventory item's quantity by a signed delta. "
                    "Input: id (str! — item id or sku), delta (int!). "
                    "Output: {ok, item}.")
    async def cap_inventory_adjust(id: str = "", delta: int = 0, trace_id=None):
        await _ensure_schema()
        if not id:
            return {"error": "id required"}
        if int(delta) == 0:
            return {"error": "delta must be non-zero"}
        item = await _run(_db_inv_adjust, id, int(delta))
        return {"ok": True, "item": item} if item else {"error": "not found"}

    @capability(
        "business.inventory.delete", http_method="POST", http_path="/business/inventory/delete",
        http_tags=["business"],
        description="Delete an inventory item by id. Input: id (str!). Output: {ok}.")
    async def cap_inventory_delete(id: str = "", trace_id=None):
        await _ensure_schema()
        if not id:
            return {"error": "id required"}
        ok = await _run(_db_delete, "biz_inventory", id)
        return {"ok": ok} if ok else {"error": "not found"}

    # ── Gigs (freelance / Fiverr / Upwork) ───────────────────────────────────

    @capability(
        "business.gig.list", http_method="GET", http_path="/business/gig/list",
        http_tags=["business"], memory="off", silent=True,
        schema=enum_schema(status=GIG_STATUS, platform=GIG_PLATFORMS),
        description="List freelance gigs. Input: stream_id, status, platform, "
                    "is_sim (int 0/1 — 0 live (default), 1 simulation, -1 both), "
                    "limit. Output: {gigs:[...], count, pipeline_value}.")
    async def cap_gig_list(stream_id: str = "", status: str = "", platform: str = "",
                           is_sim: int = 0, limit: int = 200, trace_id=None):
        await _ensure_schema()
        gigs = await _run(_db_list, "biz_gigs",
                          {"stream_id": stream_id, "status": status, "platform": platform,
                           "is_sim": None if _i(is_sim) < 0 else _i(is_sim)},
                          "updated_at DESC", int(limit), None)
        pipe = sum(_f(g.get("price")) for g in gigs
                   if g.get("status") not in ("paid", "cancelled"))
        return {"gigs": gigs, "count": len(gigs), "pipeline_value": round(pipe, 2)}

    @capability(
        "business.gig.upsert", http_method="POST", http_path="/business/gig/upsert",
        http_tags=["business"],
        schema=enum_schema(platform=GIG_PLATFORMS, status=GIG_STATUS),
        description="Create or update a freelance gig / order. Input: id (omit to "
                    "create), stream_id, platform (fiverr|upwork|…), title (str!), "
                    "client, status, price (float), currency, due_at (ISO), "
                    "deliverables (list), notes, is_sim (int 0/1 — 1 = simulation "
                    "sandbox; omit to keep existing). Output: {ok, gig}.")
    async def cap_gig_upsert(
        id: str = "", stream_id: str = "", platform: str = "fiverr", title: str = "",
        client: str = "", status: str = "lead", price: float = 0.0, currency: str = "USD",
        due_at: str = "", deliverables: List = None, notes: str = "",
        is_sim: int = -1, trace_id=None):
        await _ensure_schema()
        if not (title or id):
            return {"error": "title required"}
        gig = await _run(_db_upsert, "biz_gigs", {
            "id": id or None, "stream_id": stream_id or None, "platform": platform,
            "title": title or None, "client": client or None, "status": status,
            "price": price, "currency": currency, "due_at": due_at or None,
            "deliverables": deliverables, "notes": notes or None,
            "is_sim": None if _i(is_sim) < 0 else _i(is_sim)})
        await emit_event({"type": "biz.progress", "stage": "gig",
                          "message": f"gig {gig.get('title')} → {status}"})
        return {"ok": True, "gig": gig}

    @capability(
        "business.gig.delete", http_method="POST", http_path="/business/gig/delete",
        http_tags=["business"],
        description="Delete a gig by id. Input: id (str!). Output: {ok}.")
    async def cap_gig_delete(id: str = "", trace_id=None):
        await _ensure_schema()
        ok = await _run(_db_delete, "biz_gigs", id) if id else False
        return {"ok": ok} if ok else {"error": "not found"}

    # ── Content (YouTube / short-form / blog) ────────────────────────────────

    @capability(
        "business.content.list", http_method="GET", http_path="/business/content/list",
        http_tags=["business"], memory="off", silent=True,
        schema=enum_schema(status=CONTENT_STATUS, platform=CONTENT_PLATFORMS),
        description="List content pieces. Input: stream_id, status, platform, "
                    "is_sim (int 0/1 — 0 live (default), 1 simulation, -1 both), "
                    "limit. Output: {content:[...], count}.")
    async def cap_content_list(stream_id: str = "", status: str = "", platform: str = "",
                               is_sim: int = 0, limit: int = 200, trace_id=None):
        await _ensure_schema()
        items = await _run(_db_list, "biz_content",
                           {"stream_id": stream_id, "status": status, "platform": platform,
                            "is_sim": None if _i(is_sim) < 0 else _i(is_sim)},
                           "COALESCE(publish_at, updated_at) DESC", int(limit), None)
        return {"content": items, "count": len(items)}

    @capability(
        "business.content.upsert", http_method="POST", http_path="/business/content/upsert",
        http_tags=["business"],
        schema=enum_schema(platform=CONTENT_PLATFORMS, status=CONTENT_STATUS),
        description="Create or update a content piece (YouTube video, short, blog "
                    "post…). Input: id (omit to create), stream_id, platform, "
                    "title (str!), status (idea→published), publish_at (ISO), url, "
                    "script (str), tags (list), metrics (dict — views/likes/…), "
                    "notes, is_sim (int 0/1 — 1 = simulation sandbox; omit to keep "
                    "existing). Output: {ok, content}.")
    async def cap_content_upsert(
        id: str = "", stream_id: str = "", platform: str = "youtube", title: str = "",
        status: str = "idea", publish_at: str = "", url: str = "", script: str = "",
        tags: List = None, metrics: Dict = None, notes: str = "",
        is_sim: int = -1, trace_id=None):
        await _ensure_schema()
        if not (title or id):
            return {"error": "title required"}
        item = await _run(_db_upsert, "biz_content", {
            "id": id or None, "stream_id": stream_id or None, "platform": platform,
            "title": title or None, "status": status, "publish_at": publish_at or None,
            "url": url or None, "script": script or None, "tags": tags,
            "metrics": metrics, "notes": notes or None,
            "is_sim": None if _i(is_sim) < 0 else _i(is_sim)})
        await emit_event({"type": "biz.progress", "stage": "content",
                          "message": f"content {item.get('title')} → {status}"})
        return {"ok": True, "content": item}

    @capability(
        "business.content.delete", http_method="POST", http_path="/business/content/delete",
        http_tags=["business"],
        description="Delete a content piece by id. Input: id (str!). Output: {ok}.")
    async def cap_content_delete(id: str = "", trace_id=None):
        await _ensure_schema()
        ok = await _run(_db_delete, "biz_content", id) if id else False
        return {"ok": ok} if ok else {"error": "not found"}

    # ── Bug bounties ─────────────────────────────────────────────────────────

    @capability(
        "business.bounty.list", http_method="GET", http_path="/business/bounty/list",
        http_tags=["business"], memory="off", silent=True,
        schema=enum_schema(status=BOUNTY_STATUS, platform=BOUNTY_PLATFORMS),
        description="List bug-bounty submissions. Input: stream_id, status, "
                    "platform, is_sim (int 0/1 — 0 live (default), 1 simulation, "
                    "-1 both), limit. Output: {bounties:[...], count, "
                    "reward_pending, reward_paid}.")
    async def cap_bounty_list(stream_id: str = "", status: str = "", platform: str = "",
                              is_sim: int = 0, limit: int = 200, trace_id=None):
        await _ensure_schema()
        items = await _run(_db_list, "biz_bounties",
                           {"stream_id": stream_id, "status": status, "platform": platform,
                            "is_sim": None if _i(is_sim) < 0 else _i(is_sim)},
                           "updated_at DESC", int(limit), None)
        pending = sum(_f(b.get("reward")) for b in items
                      if b.get("status") in ("submitted", "triaged", "accepted"))
        paid = sum(_f(b.get("reward")) for b in items if b.get("status") == "paid")
        return {"bounties": items, "count": len(items),
                "reward_pending": round(pending, 2), "reward_paid": round(paid, 2)}

    @capability(
        "business.bounty.upsert", http_method="POST", http_path="/business/bounty/upsert",
        http_tags=["business"],
        schema=enum_schema(platform=BOUNTY_PLATFORMS, status=BOUNTY_STATUS),
        description="Create or update a bug-bounty submission. Input: id (omit to "
                    "create), stream_id, platform (hackerone|bugcrowd|…), program, "
                    "target, title (str!), severity (low|medium|high|critical), "
                    "status, reward (float), currency, submitted_at (ISO), url, "
                    "notes, is_sim (int 0/1 — 1 = simulation sandbox; omit to "
                    "keep existing). Output: {ok, bounty}.")
    async def cap_bounty_upsert(
        id: str = "", stream_id: str = "", platform: str = "hackerone", program: str = "",
        target: str = "", title: str = "", severity: str = "", status: str = "researching",
        reward: float = 0.0, currency: str = "USD", submitted_at: str = "",
        url: str = "", notes: str = "", is_sim: int = -1, trace_id=None):
        await _ensure_schema()
        if not (title or program or id):
            return {"error": "title or program required"}
        item = await _run(_db_upsert, "biz_bounties", {
            "id": id or None, "stream_id": stream_id or None, "platform": platform,
            "program": program or None, "target": target or None,
            "title": title or None, "severity": severity or None, "status": status,
            "reward": reward, "currency": currency, "submitted_at": submitted_at or None,
            "url": url or None, "notes": notes or None,
            "is_sim": None if _i(is_sim) < 0 else _i(is_sim)})
        await emit_event({"type": "biz.progress", "stage": "bounty",
                          "message": f"bounty {item.get('program') or item.get('title')} → {status}"})
        return {"ok": True, "bounty": item}

    @capability(
        "business.bounty.delete", http_method="POST", http_path="/business/bounty/delete",
        http_tags=["business"],
        description="Delete a bounty by id. Input: id (str!). Output: {ok}.")
    async def cap_bounty_delete(id: str = "", trace_id=None):
        await _ensure_schema()
        ok = await _run(_db_delete, "biz_bounties", id) if id else False
        return {"ok": ok} if ok else {"error": "not found"}

    # ── Marketing: social accounts + campaigns ───────────────────────────────

    @capability(
        "business.social.list", http_method="GET", http_path="/business/social/list",
        http_tags=["business"], memory="off", silent=True,
        description="List marketing / social accounts. Input: stream_id, platform, "
                    "is_sim (int 0/1 — 0 live (default), 1 simulation, -1 both), "
                    "limit. Output: {socials:[...], count, total_followers}.")
    async def cap_social_list(stream_id: str = "", platform: str = "", is_sim: int = 0,
                              limit: int = 200, trace_id=None):
        await _ensure_schema()
        items = await _run(_db_list, "biz_social",
                           {"stream_id": stream_id, "platform": platform,
                            "is_sim": None if _i(is_sim) < 0 else _i(is_sim)},
                           "followers DESC", int(limit), None)
        return {"socials": items, "count": len(items),
                "total_followers": sum(_i(s.get("followers")) for s in items)}

    @capability(
        "business.social.upsert", http_method="POST", http_path="/business/social/upsert",
        http_tags=["business"],
        schema=enum_schema(platform=SOCIAL_PLATFORMS),
        description="Create or update a social / marketing account. Link a real "
                    "credential from the shared accounts system via acct_id. "
                    "Input: id (omit to create), stream_id, platform (str!), handle, "
                    "acct_id (shared accounts credential id), url, followers (int), "
                    "status, is_sim (int 0/1 — 1 = simulation sandbox; omit to keep "
                    "existing), config (dict). Output: {ok, social}.")
    async def cap_social_upsert(
        id: str = "", stream_id: str = "", platform: str = "", handle: str = "",
        acct_id: str = "", url: str = "", followers: int = 0, status: str = "active",
        is_sim: int = -1, config: Dict = None, trace_id=None):
        await _ensure_schema()
        if not (platform or handle or id):
            return {"error": "platform or handle required"}
        item = await _run(_db_upsert, "biz_social", {
            "id": id or None, "stream_id": stream_id or None, "platform": platform or None,
            "handle": handle or None, "acct_id": acct_id or None, "url": url or None,
            "followers": followers, "status": status,
            "is_sim": None if _i(is_sim) < 0 else _i(is_sim), "config": config})
        return {"ok": True, "social": item}

    @capability(
        "business.social.delete", http_method="POST", http_path="/business/social/delete",
        http_tags=["business"],
        description="Delete a social account by id. Input: id (str!). Output: {ok}.")
    async def cap_social_delete(id: str = "", trace_id=None):
        await _ensure_schema()
        ok = await _run(_db_delete, "biz_social", id) if id else False
        return {"ok": ok} if ok else {"error": "not found"}

    @capability(
        "business.social.accounts", http_method="GET", http_path="/business/social/accounts",
        http_tags=["business"], memory="off", silent=True,
        description="List shared-system accounts available to link as marketing "
                    "credentials (from the accounts subsystem). Output: {accounts:[...]}.")
    async def cap_social_accounts(trace_id=None):
        import sys
        acc_mod = sys.modules.get("accounts_capabilities")
        out = []
        if acc_mod and hasattr(acc_mod, "_all_raw"):
            try:
                raw = await acc_mod._all_raw()
                out = [{"id": a.get("id"), "label": a.get("label"),
                        "email": a.get("email")} for a in raw]
            except Exception as e:
                log.debug("social.accounts: %s", e)
        return {"accounts": out}

    @capability(
        "business.campaign.list", http_method="GET", http_path="/business/campaign/list",
        http_tags=["business"], memory="off", silent=True,
        schema=enum_schema(status=CAMPAIGN_STATUS),
        description="List marketing campaigns. Input: stream_id, status, is_sim "
                    "(int 0/1 — 0 live (default), 1 simulation, -1 both), limit. "
                    "Output: {campaigns:[...], count}.")
    async def cap_campaign_list(stream_id: str = "", status: str = "", is_sim: int = 0,
                                limit: int = 200, trace_id=None):
        await _ensure_schema()
        items = await _run(_db_list, "biz_campaigns",
                           {"stream_id": stream_id, "status": status,
                            "is_sim": None if _i(is_sim) < 0 else _i(is_sim)},
                           "updated_at DESC", int(limit), None)
        return {"campaigns": items, "count": len(items)}

    @capability(
        "business.campaign.upsert", http_method="POST", http_path="/business/campaign/upsert",
        http_tags=["business"],
        schema=enum_schema(status=CAMPAIGN_STATUS),
        description="Create or update a marketing campaign. Input: id (omit to "
                    "create), stream_id, name (str!), goal, channels (list of "
                    "platform names), budget (float), currency, status, "
                    "start_at (ISO), end_at (ISO), metrics (dict), notes, is_sim "
                    "(int 0/1 — 1 = simulation sandbox; omit to keep existing). "
                    "Output: {ok, campaign}.")
    async def cap_campaign_upsert(
        id: str = "", stream_id: str = "", name: str = "", goal: str = "",
        channels: List = None, budget: float = 0.0, currency: str = "USD",
        status: str = "draft", start_at: str = "", end_at: str = "",
        metrics: Dict = None, notes: str = "", is_sim: int = -1, trace_id=None):
        await _ensure_schema()
        if not (name or id):
            return {"error": "name required"}
        item = await _run(_db_upsert, "biz_campaigns", {
            "id": id or None, "stream_id": stream_id or None, "name": name or None,
            "goal": goal or None, "channels": channels, "budget": budget,
            "currency": currency, "status": status, "start_at": start_at or None,
            "end_at": end_at or None, "metrics": metrics, "notes": notes or None,
            "is_sim": None if _i(is_sim) < 0 else _i(is_sim)})
        return {"ok": True, "campaign": item}

    @capability(
        "business.campaign.delete", http_method="POST", http_path="/business/campaign/delete",
        http_tags=["business"],
        description="Delete a campaign by id. Input: id (str!). Output: {ok}.")
    async def cap_campaign_delete(id: str = "", trace_id=None):
        await _ensure_schema()
        ok = await _run(_db_delete, "biz_campaigns", id) if id else False
        return {"ok": ok} if ok else {"error": "not found"}

    # ── Tasks + calendar integration ─────────────────────────────────────────

    @capability(
        "business.task.list", http_method="GET", http_path="/business/task/list",
        http_tags=["business"], memory="off", silent=True,
        schema=enum_schema(status=TASK_STATUS, kind=TASK_KINDS),
        description="List operational business tasks. Input: stream_id, status, "
                    "kind, is_sim (int 0/1 — 0 live (default), 1 simulation, -1 "
                    "both), limit. Output: {tasks:[...], count}.")
    async def cap_task_list(stream_id: str = "", status: str = "", kind: str = "",
                            is_sim: int = 0, limit: int = 200, trace_id=None):
        await _ensure_schema()
        items = await _run(_db_list, "biz_tasks",
                           {"stream_id": stream_id, "status": status, "kind": kind,
                            "is_sim": None if _i(is_sim) < 0 else _i(is_sim)},
                           "COALESCE(due_at, updated_at) ASC", int(limit), None)
        return {"tasks": items, "count": len(items)}

    @capability(
        "business.task.upsert", http_method="POST", http_path="/business/task/upsert",
        http_tags=["business"],
        schema=enum_schema(kind=TASK_KINDS, status=TASK_STATUS),
        description="Create or update a business task. Input: id (omit to create), "
                    "stream_id, title (str!), kind (post|list|ship|…), status, "
                    "due_at (ISO), assigned_agent, ref, notes, is_sim (int 0/1 — "
                    "1 = simulation sandbox; omit to keep existing). Output: {ok, task}.")
    async def cap_task_upsert(
        id: str = "", stream_id: str = "", title: str = "", kind: str = "other",
        status: str = "todo", due_at: str = "", assigned_agent: str = "",
        ref: str = "", notes: str = "", is_sim: int = -1, trace_id=None):
        await _ensure_schema()
        if not (title or id):
            return {"error": "title required"}
        task = await _run(_db_upsert, "biz_tasks", {
            "id": id or None, "stream_id": stream_id or None, "title": title or None,
            "kind": kind, "status": status, "due_at": due_at or None,
            "assigned_agent": assigned_agent or None, "ref": ref or None,
            "notes": notes or None,
            "is_sim": None if _i(is_sim) < 0 else _i(is_sim)})
        return {"ok": True, "task": task}

    @capability(
        "business.task.schedule", http_method="POST", http_path="/business/task/schedule",
        http_tags=["business"],
        description="Schedule a business task into the Calendar — creates (or "
                    "updates) the task, then a calendar event (timed) or todo "
                    "(untimed) via the cal.* capabilities and links them back. "
                    "Input: id (existing task, optional), stream_id, title (str!), "
                    "kind, when (ISO start — omit for an untimed todo), duration_min "
                    "(int, default 30), notes, is_sim (int 0/1 — 1 = simulation "
                    "sandbox; sim tasks are NOT pushed to the real calendar). "
                    "Output: {ok, task, cal_event_id?, cal_todo_id?}.")
    async def cap_task_schedule(
        id: str = "", stream_id: str = "", title: str = "", kind: str = "other",
        when: str = "", duration_min: int = 30, notes: str = "",
        is_sim: int = -1, trace_id=None):
        await _ensure_schema()
        if not (title or id):
            return {"error": "title required"}
        import sys
        task = await _run(_db_upsert, "biz_tasks", {
            "id": id or None, "stream_id": stream_id or None, "title": title or None,
            "kind": kind, "status": "scheduled" if when else "todo",
            "due_at": when or None, "notes": notes or None,
            "is_sim": None if _i(is_sim) < 0 else _i(is_sim)})
        # A simulated task never reaches the REAL calendar — the sim sandbox
        # must not create outward-facing events.
        if _i(task.get("is_sim")):
            await emit_event({"type": "biz.progress", "stage": "schedule",
                              "message": f"sim task '{title}' recorded (calendar skipped)"})
            return {"ok": True, "task": task,
                    "note": "sim task — calendar intentionally skipped"}
        cal = sys.modules.get("calendar_capabilities")
        result = {"ok": True, "task": task}
        stream = await _run(_db_get, "biz_streams", stream_id) if stream_id else None
        label = f"[{stream['name']}] {title}" if stream else title
        if cal:
            try:
                if when:
                    try:
                        st = datetime.fromisoformat(when.replace("Z", "+00:00"))
                        end = (st + timedelta(minutes=int(duration_min or 30))).isoformat()
                    except Exception:
                        end = ""
                    ev = await cal.cap_event_upsert(
                        title=label, start=when, end=end,
                        description=notes or f"Business task ({kind})",
                        tags=["business", kind], color="#5a9e8f")
                    if ev.get("ok"):
                        eid = ev["event"]["id"]
                        task = await _run(_db_upsert, "biz_tasks",
                                          {"id": task["id"], "cal_event_id": eid})
                        result["cal_event_id"] = eid
                else:
                    td = await cal.cap_todo_upsert(
                        title=label, due=when or "", priority=1,
                        notes=notes or f"Business task ({kind})")
                    if td.get("ok"):
                        tid = td["todo"]["id"]
                        task = await _run(_db_upsert, "biz_tasks",
                                          {"id": task["id"], "cal_todo_id": tid})
                        result["cal_todo_id"] = tid
            except Exception as e:
                log.warning("task.schedule cal link failed: %s", e)
                result["cal_error"] = str(e)
        else:
            result["cal_error"] = "calendar module not loaded"
        result["task"] = task
        await emit_event({"type": "biz.progress", "stage": "schedule",
                          "message": f"scheduled '{title}'" + (f" @ {when}" if when else " (todo)")})
        return result

    @capability(
        "business.task.delete", http_method="POST", http_path="/business/task/delete",
        http_tags=["business"],
        description="Delete a business task by id. Input: id (str!). Output: {ok}.")
    async def cap_task_delete(id: str = "", trace_id=None):
        await _ensure_schema()
        ok = await _run(_db_delete, "biz_tasks", id) if id else False
        return {"ok": ok} if ok else {"error": "not found"}

    # ── Dashboard / graph / briefing ─────────────────────────────────────────

    @capability(
        "business.dashboard", http_method="GET", http_path="/business/dashboard",
        http_tags=["business"], memory="off", silent=True,
        description="State of the business: per-stream 30-day revenue/expense/net "
                    "vs goal, cash balance, open gigs, content pipeline, open "
                    "bounties, inventory value, open tasks, active campaigns. "
                    "Input: is_sim (int 0/1). Output: {...aggregate}.")
    async def cap_dashboard(is_sim: int = 0, trace_id=None):
        await _ensure_schema()
        d = await _run(_db_dashboard, int(is_sim))
        # The sim view is a self-contained sandbox — folding in the REAL stores/
        # commerce/tax rollups would leak live data into the simulated world
        # (and tempt agents toward real store caps they cannot use).
        if int(is_sim):
            return d
        # The store(s) are part of the business — fold their rollup in.
        import sys
        com = sys.modules.get("commerce_capabilities")
        if com and hasattr(com, "_db_dashboard"):
            try:
                d["commerce"] = await _run(com._db_dashboard)
            except Exception:
                d["commerce"] = None
        stores = sys.modules.get("commerce_stores")
        if stores and hasattr(stores, "_master_sync"):
            try:
                master = await _run(stores._master_sync)
                d["stores"] = master
                st = master.get("totals", {})
                # Surface store sales/profit next to the ledger figures.
                d["store_revenue_30d"] = st.get("revenue_30d", 0.0)
                d["store_net_30d"] = st.get("net_30d", 0.0)
                d["store_count"] = st.get("store_count", 0)
                d["store_inventory_value"] = st.get("inventory_retail", 0.0)
            except Exception:
                d["stores"] = None
        # Estimated tax set-aside for the whole business (current tax year).
        tax = sys.modules.get("commerce_uk_tax")
        if tax and hasattr(tax, "cap_tax_summary"):
            try:
                ts = await tax.cap_tax_summary(trace_id=None)
                if not ts.get("error"):
                    d["tax"] = {"year": ts.get("tax_year_label"),
                                "liability": ts.get("total_liability"),
                                "set_aside": ts.get("set_aside"),
                                "taxable_profit": ts.get("taxable_profit")}
            except Exception:
                pass
        return d

    @capability(
        "business.graph", http_method="GET", http_path="/business/graph",
        http_tags=["business"], memory="off", silent=True,
        description="Business network graph (streams ↔ accounts ↔ gigs ↔ content ↔ "
                    "bounties ↔ social ↔ campaigns ↔ platforms) for the panel graph "
                    "view. Input: is_sim (int 0/1). Output: {nodes:[...], edges:[...], counts}.")
    async def cap_graph(is_sim: int = 0, trace_id=None):
        await _ensure_schema()
        return await _run(_db_graph, int(is_sim))

    @capability(
        "business.brief", http_method="GET", http_path="/business/brief",
        http_tags=["business"], memory="off", silent=True,
        description="Compact natural-language briefing of the whole business — "
                    "loaded as grounding context by the conversational business "
                    "operator agent. Input: is_sim (int 0/1). Output: {brief, dashboard}.")
    async def cap_brief(is_sim: int = 0, trace_id=None):
        await _ensure_schema()
        d = await cap_dashboard(is_sim=is_sim, trace_id=None)
        lines = [f"BUSINESS BRIEFING ({now_iso()[:16].replace('T', ' ')} UTC)"
                 + (" — SIMULATION" if is_sim else "")]
        lines.append(
            f"Cash across {d['n_accounts']} account(s): {d['cash_balance']:,.2f}. "
            f"Last 30 days — revenue {d['revenue_30d']:,.2f}, expense {d['expense_30d']:,.2f}, "
            f"net {d['net_30d']:,.2f}"
            + (f" ({d['goal_pct']:.0f}% of the {d['goal_monthly_total']:,.0f} monthly goal)."
               if d.get("goal_pct") is not None else "."))
        _stores = (d.get("stores") or {}).get("stores") or []
        if _stores:
            lines.append(f"Stores ({d.get('store_count', len(_stores))}): "
                         f"30d sales £{d.get('store_revenue_30d', 0):,.2f}, "
                         f"profit £{d.get('store_net_30d', 0):,.2f}, "
                         f"stock £{d.get('store_inventory_value', 0):,.2f}.")
            for s in _stores[:6]:
                lines.append(f"  • {s['name']}: 30d sales £{s.get('revenue_30d', 0):,.2f}, "
                             f"profit £{s.get('net_30d', 0):,.2f}, {s.get('product_count', 0)} SKUs, "
                             f"{s.get('listings_live', 0)} live")
        if d.get("tax"):
            lines.append(f"Tax ({d['tax'].get('year')}): est. liability "
                         f"£{d['tax'].get('liability', 0):,.2f}; set aside "
                         f"£{d['tax'].get('set_aside', 0):,.2f}.")
        if d["streams"]:
            lines.append("Income streams:")
            for s in d["streams"]:
                gp = f", {s['goal_pct']:.0f}% to goal" if s.get("goal_pct") is not None else ""
                lines.append(f"  • {s['name']} ({s['kind']}, {s['status']}): "
                             f"30d net {s['net_30d']:,.2f}{gp}")
        else:
            lines.append("No income streams yet — the operator can create some with biz.stream.upsert.")
        flags = []
        if d["open_gigs"]:        flags.append(f"{d['open_gigs']} open gig(s)")
        if d["content_pipeline"]: flags.append(f"{d['content_pipeline']} content piece(s) in pipeline")
        if d["open_bounties"]:    flags.append(f"{d['open_bounties']} bounty submission(s) live")
        if d["open_tasks"]:       flags.append(f"{d['open_tasks']} open task(s)")
        if d["active_campaigns"]: flags.append(f"{d['active_campaigns']} active campaign(s)")
        if d["inventory_value"]:  flags.append(f"inventory worth {d['inventory_value']:,.2f}")
        if flags:
            lines.append("Attention: " + "; ".join(flags) + ".")
        return {"brief": "\n".join(lines), "dashboard": d}

    # ── Panel + registration ─────────────────────────────────────────────────

    _HERE = _Path(__file__).parent

    @APP.get("/biz/panel", include_in_schema=False)
    async def _biz_panel_route():
        from fastapi.responses import HTMLResponse
        p = _HERE / "business_panel.html"
        if p.exists():
            return HTMLResponse(p.read_text(encoding="utf-8"))
        return HTMLResponse("<p style='color:red'>business_panel.html not found</p>")

    _BIZ_UI_CAPS = [
        "business.dashboard", "business.graph", "business.brief",
        "business.stream.list", "business.stream.upsert", "business.stream.delete",
        "business.account.list", "business.account.upsert", "business.account.delete",
        "business.account.recalc", "business.txn.add", "business.txn.list",
        "business.inventory.list", "business.inventory.upsert", "business.inventory.adjust",
        "business.inventory.delete",
        "business.gig.list", "business.gig.upsert", "business.gig.delete",
        "business.content.list", "business.content.upsert", "business.content.delete",
        "business.bounty.list", "business.bounty.upsert", "business.bounty.delete",
        "business.social.list", "business.social.upsert", "business.social.delete", "business.social.accounts",
        "business.campaign.list", "business.campaign.upsert", "business.campaign.delete",
        "business.task.list", "business.task.upsert", "business.task.schedule", "business.task.delete",
        # simulation (business_sim.py)
        "business.sim.scenarios", "business.sim.start", "business.sim.step", "business.sim.status",
        "business.sim.reset", "business.sim.evaluate",
        # thermal printer (thermal_printer_capabilities.py)
        "print.status", "print.printers", "print.text", "print.receipt",
        "print.label", "print.raw",
        # calendar + accounts bridges
        "cal.event.upsert", "cal.todo.upsert",
        # ── Store / e-commerce (Stores tab + per-store surface) ──
        "business.store.dashboard", "business.store.list", "business.store.get",
        "business.store.upsert", "business.store.delete", "business.store.master",
        "business.product.list", "business.product.get", "business.product.upsert",
        "business.product.delete", "business.stock.adjust", "business.stock.moves",
        "business.product.low_stock",
        "business.order.list", "business.order.get", "business.order.upsert",
        "business.order.set_status",
        "business.customer.list", "business.customer.get", "business.customer.upsert",
        "business.customer.delete",
        "business.service.list", "business.service.get", "business.service.upsert",
        "business.service.set_status",
        "business.price.lookup", "business.price.snapshots", "business.price.suggest",
        "business.platform.connectors", "business.platform.accounts",
        "business.platform.connect", "business.platform.disconnect",
        "business.platform.listings.sync", "business.platform.listing.push",
        "business.platform.orders.sync",
        # ── UK reselling operation (Reselling sub-tab) ──
        "business.vinted.connect", "business.vinted.search",
        "business.tax.settings", "business.tax.settings.set",
        "business.profit.record", "business.profit.order", "business.profit.report",
        "business.tax.summary",
        "business.ship.carriers", "business.ship.rates", "business.ship.book",
        "business.ship.list", "business.ship.mark", "business.ship.label",
        "business.ship.postrun", "business.ship.pickup",
        "business.barcode.lookup", "business.scan.intake",
        "business.vision.describe_item", "business.ebay.defaults",
        "business.listing.draft", "business.listing.list",
        "business.listing.publish", "business.listing.archive",
        "business.market.search", "business.watch.upsert", "business.watch.list",
        "business.watch.delete", "business.watch.scan", "business.market.alerts",
        "business.market.alert.seen", "business.market.reprice",
    ]

    # One top-level "Business" tab. The panel (/biz/panel) carries its own
    # top-line tab bar with every feature as its own tab — Dashboard · Streams ·
    # Stores · Accounts · Ledger · Gigs · Content · Bounties · Marketing · Tasks ·
    # Network Graph · Simulation · Printer. "Stores" is the multi-store master
    # view; opening a store embeds the full e-commerce surface
    # (/business/store/panel) scoped to that store. The old separate "Shop"
    # (/commerce/panel) and "Reselling" sub-tabs are retired — everything is one
    # coherent Business area with no duplicated inventory/orders/dashboards.
    register_ui(
        "business", "Business", "▦",
        """<div style="height:100%;display:flex;background:var(--bg1,#111)">
            <iframe src="/biz/panel" style="flex:1;border:none;display:block"
                    allow="clipboard-read; clipboard-write; camera; serial; usb"></iframe>
        </div>""",
        "",
        ui_caps=_BIZ_UI_CAPS,
        mode="tab", tab_order=66,
    )

    log.info("business: core ready (streams / money / inventory / gigs / content / "
             "bounties / marketing / tasks / graph / brief)")
