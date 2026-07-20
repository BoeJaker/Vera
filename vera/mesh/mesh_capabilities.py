"""
mesh_capabilities.py — ESP32 Mesh Manager
==========================================

Manage a fleet of ESP32 (or any HTTP/MQTT/serial-speaking) edge nodes. Each node
enrolls, advertises a set of **modules** (sensor / web_fetch / watch / alert /
kiosk / control) and can be **sent work** or stream **telemetry**. The whole mesh
is browsable as a node graph in the Mesh panel.

Design — one durable queue, many drains
---------------------------------------
Every command sent to a node is a durable ``mesh_jobs`` row (queued → sent →
done/error). That row is the single source of truth; whichever transport the node
happens to be using drains the *same* queue:

  • HTTP long-poll  (always on)  — node GETs /mesh/poll, held open until a command
                                   is queued or `wait` seconds elapse.
  • WebSocket       (always on)  — /mesh/ws, server pushes commands instantly.
  • MQTT            (optional)   — needs aiomqtt + VERA_MQTT_URL; pub/sub broker.
  • Serial (server) (optional)   — needs pyserial + VERA_MESH_SERIAL_PORTS; a USB
                                   node wired straight into the host.
  • Serial (browser)            — the panel uses the Web Serial API (no backend
                                   dep); it relay-enrolls a USB node via /mesh/hello.

`_deliver` persists the job then *nudges* every live channel; `_drain_commands`
atomically pops queued rows (BEGIN IMMEDIATE) so two drains never double-deliver.
Fallback is automatic: if no push channel is live the row simply waits for the
node's next poll. Optional transports are dependency-guarded (HAS_AIOMQTT /
HAS_PYSERIAL) exactly like markets guards ccxt — their absence never breaks
startup.

Storage mirrors vera/markets: fresh WAL SQLite connections opened in an executor.
Sensor telemetry is also best-effort appended to Data Fabric datasets
``mesh.<node_id>.<metric>`` so readings are queryable/chartable alongside the fast
local table the panel reads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue as _queue
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path as _Path
from typing import Dict, List, Optional

log = logging.getLogger("vera.mesh")

try:
    from fastapi import File, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
    from fastapi.responses import (
        FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse,
    )

    from Vera.vera.capability_orchestration import (
        APP, capability, emit_event, now_iso, register_ui, schedule,
    )
    from Vera.vera.fabric.data_fabric import _sqlite_conn, _sqlite_insert_record
    _CAP_AVAILABLE = True
except ImportError as e:                       # pragma: no cover
    logging.getLogger("vera.mesh").warning("mesh caps unavailable: %s", e)
    _CAP_AVAILABLE = False

# Optional transports — guarded; absence is fine.
try:
    import aiomqtt
    HAS_AIOMQTT = True
except Exception:                              # pragma: no cover
    HAS_AIOMQTT = False

try:
    import serial as _pyserial                 # pyserial
    HAS_PYSERIAL = True
except Exception:                              # pragma: no cover
    HAS_PYSERIAL = False

# Wi-Fi credentials are sealed at rest with the shared Fernet helper (same pattern
# as calendar/email/telegram). Falls back to redact-only if cryptography is absent.
try:
    from Vera.vera.security.secrets import seal as _seal, open_secret as _open_secret, redact as _redact
    HAS_SECRETS = True
except Exception:                              # pragma: no cover
    HAS_SECRETS = False
    def _seal(x): return x or ""
    def _open_secret(x): return x or ""
    def _redact(x): return "••••••••" if x else ""


# ─────────────────────────────────────────────────────────────────────────────
# Config & constants
# ─────────────────────────────────────────────────────────────────────────────

MESH_TOKEN          = os.getenv("VERA_MESH_TOKEN", "")            # optional shared secret
MQTT_URL            = os.getenv("VERA_MQTT_URL", "")              # e.g. mqtt://user:pass@host:1883
SERIAL_PORTS        = [p.strip() for p in os.getenv("VERA_MESH_SERIAL_PORTS", "").split(",") if p.strip()]
SERIAL_BAUD         = int(os.getenv("VERA_MESH_SERIAL_BAUD", "115200"))
HEARTBEAT_S         = int(os.getenv("VERA_MESH_HEARTBEAT", "30")) # expected node check-in cadence
POLL_MAX_WAIT       = 30
TELEMETRY_KEEP      = 500                                          # rows kept per node in the fast table
MQTT_TOPIC_UP       = "vera/mesh/+/up"
HUB_ID              = "__hub__"

MODULE_KINDS = ["sensor", "web_fetch", "watch", "alert", "kiosk", "control"]

_TABLES_READY = False

# In-memory channel state (per running process)
_CMD_EVENTS:  Dict[str, asyncio.Event] = {}     # node_id -> wakeup event for waiters
_WS_CONNS:    Dict[str, "WebSocket"]   = {}      # node_id -> live websocket
_MQTT:        Dict[str, object]        = {"client": None, "seen": {}}   # seen: node_id -> epoch
_SERIAL:      Dict[str, object]        = {"out": {}, "node_port": {}, "stop": False, "loop": None}
_STARTED                               = False


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _iso_to_epoch(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None

def _age_s(iso: str) -> Optional[float]:
    e = _iso_to_epoch(iso)
    return None if e is None else max(0.0, time.time() - e)

def _status_of(last_seen: str, heartbeat: int = HEARTBEAT_S) -> str:
    age = _age_s(last_seen)
    if age is None:
        return "new"
    if age < 2 * heartbeat:
        return "online"
    if age < 10 * heartbeat:
        return "stale"
    return "offline"

def _num(v) -> Optional[float]:
    try:
        if isinstance(v, bool):
            return 1.0 if v else 0.0
        return float(v)
    except Exception:
        return None

def _jloads(s, default):
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s) if s else default
    except Exception:
        return default

def _norm_modules(v) -> Dict[str, dict]:
    """Accept ['sensor','kiosk'] or {'sensor':{...}} → {'sensor':{'enabled':True,...}}."""
    out: Dict[str, dict] = {}
    if isinstance(v, list):
        for m in v:
            m = str(m).strip()
            if m:
                out[m] = {"enabled": True}
    elif isinstance(v, dict):
        for m, cfg in v.items():
            c = dict(cfg) if isinstance(cfg, dict) else {"enabled": bool(cfg)}
            c.setdefault("enabled", True)
            out[str(m)] = c
    return out

def _new_token() -> str:
    return uuid.uuid4().hex

def _cmd_event(node_id: str) -> asyncio.Event:
    ev = _CMD_EVENTS.get(node_id)
    if ev is None:
        ev = asyncio.Event()
        _CMD_EVENTS[node_id] = ev
    return ev


# ─────────────────────────────────────────────────────────────────────────────
# SQLite schema + sync helpers (fresh WAL connections in an executor)
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_tables_sync():
    global _TABLES_READY
    conn = _sqlite_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mesh_nodes (
                node_id        TEXT PRIMARY KEY,
                name           TEXT,
                role           TEXT,
                group_name     TEXT,
                modules        TEXT,      -- JSON: advertised module availability
                config         TEXT,      -- JSON: server-managed per-module settings
                parent_id      TEXT,      -- mesh uplink (empty => direct to hub)
                ip             TEXT,
                mac            TEXT,
                board          TEXT,
                fw_version     TEXT,
                channels       TEXT,      -- JSON list of live transports
                rssi           INTEGER,
                token          TEXT,
                last_seen      TEXT,
                last_telemetry TEXT,      -- JSON {metric: value}
                created_at     TEXT
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mesh_jobs (
                job_id     TEXT PRIMARY KEY,
                node_id    TEXT,
                type       TEXT,
                payload    TEXT,
                status     TEXT,          -- queued | sent | done | error
                channel    TEXT,
                result     TEXT,
                error      TEXT,
                created_at TEXT,
                sent_at    TEXT,
                done_at    TEXT
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mesh_telemetry (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id   TEXT,
                metric    TEXT,
                value     REAL,
                str_value TEXT,
                unit      TEXT,
                ts        TEXT
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mesh_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mesh_jobs_node ON mesh_jobs(node_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mesh_tele_node ON mesh_telemetry(node_id, id DESC)")
        conn.commit()
    finally:
        conn.close()
    _TABLES_READY = True

async def _ensure_tables():
    if _TABLES_READY:
        return
    await asyncio.get_running_loop().run_in_executor(None, _ensure_tables_sync)

def _node_get_sync(node_id: str) -> Optional[dict]:
    conn = _sqlite_conn()
    try:
        r = conn.execute("SELECT * FROM mesh_nodes WHERE node_id=?", (node_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()

def _nodes_all_sync() -> List[dict]:
    conn = _sqlite_conn()
    try:
        rows = conn.execute("SELECT * FROM mesh_nodes ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def _upsert_node_sync(node: dict) -> dict:
    """Insert or merge a node. Returns the stored row (with token assigned)."""
    conn = _sqlite_conn()
    try:
        existing = conn.execute("SELECT * FROM mesh_nodes WHERE node_id=?",
                                (node["node_id"],)).fetchone()
        existing = dict(existing) if existing else None
        token = (existing or {}).get("token") or node.get("token") or _new_token()
        # Server-managed config persists across re-enrolls; device advertises modules.
        config = _jloads((existing or {}).get("config"), {})
        merged_modules = _norm_modules(node.get("modules"))
        row = {
            "node_id":    node["node_id"],
            "name":       node.get("name") or (existing or {}).get("name") or node["node_id"],
            "role":       node.get("role") or (existing or {}).get("role") or "",
            "group_name": node.get("group") or node.get("group_name") or (existing or {}).get("group_name") or "",
            "modules":    json.dumps(merged_modules or _jloads((existing or {}).get("modules"), {})),
            "config":     json.dumps(config),
            "parent_id":  node.get("parent_id", (existing or {}).get("parent_id") or ""),
            "ip":         node.get("ip") or (existing or {}).get("ip") or "",
            "mac":        node.get("mac") or (existing or {}).get("mac") or "",
            "board":      node.get("board") or (existing or {}).get("board") or "esp32",
            "fw_version": node.get("fw") or node.get("fw_version") or (existing or {}).get("fw_version") or "",
            "channels":   json.dumps(node.get("channels") or _jloads((existing or {}).get("channels"), [])),
            "rssi":       node.get("rssi") if node.get("rssi") is not None else (existing or {}).get("rssi"),
            "token":      token,
            "last_seen":  now_iso(),
            "last_telemetry": (existing or {}).get("last_telemetry") or "{}",
            "created_at": (existing or {}).get("created_at") or now_iso(),
        }
        conn.execute(
            "INSERT OR REPLACE INTO mesh_nodes "
            "(node_id,name,role,group_name,modules,config,parent_id,ip,mac,board,"
            " fw_version,channels,rssi,token,last_seen,last_telemetry,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["node_id"], row["name"], row["role"], row["group_name"], row["modules"],
             row["config"], row["parent_id"], row["ip"], row["mac"], row["board"],
             row["fw_version"], row["channels"], row["rssi"], row["token"],
             row["last_seen"], row["last_telemetry"], row["created_at"]))
        conn.commit()
        return row
    finally:
        conn.close()

def _touch_node_sync(node_id: str, channel: str = "", ip: str = "", rssi=None):
    conn = _sqlite_conn()
    try:
        row = conn.execute("SELECT channels FROM mesh_nodes WHERE node_id=?", (node_id,)).fetchone()
        if not row:
            return
        chans = _jloads(row["channels"], [])
        if channel and channel not in chans:
            chans.append(channel)
        sets = ["last_seen=?", "channels=?"]
        vals: list = [now_iso(), json.dumps(chans)]
        if ip:
            sets.append("ip=?"); vals.append(ip)
        if rssi is not None:
            sets.append("rssi=?"); vals.append(rssi)
        vals.append(node_id)
        conn.execute(f"UPDATE mesh_nodes SET {','.join(sets)} WHERE node_id=?", vals)
        conn.commit()
    finally:
        conn.close()

def _set_node_fields_sync(node_id: str, fields: dict) -> bool:
    if not fields:
        return False
    conn = _sqlite_conn()
    try:
        cols, vals = [], []
        for k, v in fields.items():
            cols.append(f"{k}=?")
            vals.append(json.dumps(v) if isinstance(v, (dict, list)) else v)
        vals.append(node_id)
        cur = conn.execute(f"UPDATE mesh_nodes SET {','.join(cols)} WHERE node_id=?", vals)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()

def _delete_node_sync(node_id: str, delete_data: bool) -> dict:
    conn = _sqlite_conn()
    try:
        conn.execute("DELETE FROM mesh_nodes WHERE node_id=?", (node_id,))
        conn.execute("DELETE FROM mesh_jobs WHERE node_id=?", (node_id,))
        tele = 0
        if delete_data:
            cur = conn.execute("DELETE FROM mesh_telemetry WHERE node_id=?", (node_id,))
            tele = cur.rowcount
        conn.commit()
        return {"telemetry_deleted": tele}
    finally:
        conn.close()


# ── Jobs ─────────────────────────────────────────────────────────────────────

def _queue_job_sync(node_id: str, jtype: str, payload: dict) -> str:
    job_id = uuid.uuid4().hex[:12]
    conn = _sqlite_conn()
    try:
        conn.execute(
            "INSERT INTO mesh_jobs (job_id,node_id,type,payload,status,created_at) "
            "VALUES (?,?,?,?, 'queued', ?)",
            (job_id, node_id, jtype, json.dumps(payload or {}), now_iso()))
        conn.commit()
    finally:
        conn.close()
    return job_id

def _drain_jobs_sync(node_id: str, channel: str) -> List[dict]:
    """Atomically pop all queued commands for a node (BEGIN IMMEDIATE so two
    concurrent drains can never double-deliver)."""
    conn = _sqlite_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT job_id,type,payload FROM mesh_jobs WHERE node_id=? AND status='queued' "
            "ORDER BY created_at LIMIT 100", (node_id,)).fetchall()
        out = []
        for r in rows:
            conn.execute("UPDATE mesh_jobs SET status='sent', channel=?, sent_at=? WHERE job_id=?",
                         (channel, now_iso(), r["job_id"]))
            out.append({"job_id": r["job_id"], "type": r["type"],
                        "payload": _jloads(r["payload"], {})})
        conn.commit()
        return out
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _job_result_sync(job_id: str, status: str, result, error: str) -> Optional[str]:
    conn = _sqlite_conn()
    try:
        row = conn.execute("SELECT node_id FROM mesh_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE mesh_jobs SET status=?, result=?, error=?, done_at=? WHERE job_id=?",
            (status, json.dumps(result) if result is not None else None,
             (error or "")[:500], now_iso(), job_id))
        conn.commit()
        return row["node_id"]
    finally:
        conn.close()

def _jobs_query_sync(node_id: str, limit: int) -> List[dict]:
    conn = _sqlite_conn()
    try:
        if node_id:
            rows = conn.execute(
                "SELECT * FROM mesh_jobs WHERE node_id=? ORDER BY created_at DESC LIMIT ?",
                (node_id, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM mesh_jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = _jloads(d.get("payload"), {})
            d["result"] = _jloads(d.get("result"), None)
            out.append(d)
        return out
    finally:
        conn.close()


# ── Telemetry ────────────────────────────────────────────────────────────────

def _store_telemetry_sync(node_id: str, readings: List[dict]) -> dict:
    conn = _sqlite_conn()
    latest: Dict[str, object] = {}
    try:
        for r in readings:
            metric = str(r.get("metric", "")).strip()
            if not metric:
                continue
            val = r.get("value")
            num = _num(val)
            conn.execute(
                "INSERT INTO mesh_telemetry (node_id,metric,value,str_value,unit,ts) "
                "VALUES (?,?,?,?,?,?)",
                (node_id, metric, num,
                 None if num is not None else str(val),
                 str(r.get("unit", "")), r.get("ts") or now_iso()))
            latest[metric] = num if num is not None else val
        # prune to last TELEMETRY_KEEP rows for this node
        conn.execute(
            "DELETE FROM mesh_telemetry WHERE node_id=? AND id NOT IN "
            "(SELECT id FROM mesh_telemetry WHERE node_id=? ORDER BY id DESC LIMIT ?)",
            (node_id, node_id, TELEMETRY_KEEP))
        # store latest snapshot on the node row
        cur = conn.execute("SELECT last_telemetry FROM mesh_nodes WHERE node_id=?", (node_id,)).fetchone()
        snap = _jloads(cur["last_telemetry"], {}) if cur else {}
        snap.update(latest)
        conn.execute("UPDATE mesh_nodes SET last_telemetry=?, last_seen=? WHERE node_id=?",
                     (json.dumps(snap), now_iso(), node_id))
        conn.commit()
    finally:
        conn.close()
    return latest

def _telemetry_query_sync(node_id: str, metric: str, limit: int) -> List[dict]:
    conn = _sqlite_conn()
    try:
        if metric:
            rows = conn.execute(
                "SELECT metric,value,str_value,unit,ts FROM mesh_telemetry "
                "WHERE node_id=? AND metric=? ORDER BY id DESC LIMIT ?",
                (node_id, metric, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT metric,value,str_value,unit,ts FROM mesh_telemetry "
                "WHERE node_id=? ORDER BY id DESC LIMIT ?", (node_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Settings (persisted Wi-Fi profile + server URL; survives reboots) ─────────

def _settings_get_sync() -> dict:
    conn = _sqlite_conn()
    try:
        rows = conn.execute("SELECT key, value FROM mesh_settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()

def _settings_set_sync(pairs: dict):
    conn = _sqlite_conn()
    try:
        for k, v in pairs.items():
            conn.execute("INSERT OR REPLACE INTO mesh_settings (key, value) VALUES (?,?)", (k, v))
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Inbound handlers (shared by every transport)
# ─────────────────────────────────────────────────────────────────────────────

def _authed(provided: str, node_row: Optional[dict]) -> bool:
    """Device-route auth: open LAN unless a shared token or per-node token exists."""
    if MESH_TOKEN:
        if provided == MESH_TOKEN:
            return True
        if node_row and provided and provided == node_row.get("token"):
            return True
        return False
    # no shared secret — honour a per-node token if one was issued
    if node_row and node_row.get("token"):
        return (not provided) or provided == node_row.get("token") or provided == "open"
    return True

async def _handle_hello(msg: dict, channel: str, ip: str = "") -> dict:
    await _ensure_tables()
    node_id = str(msg.get("node_id") or msg.get("id") or "").strip()
    if not node_id:
        node_id = "esp32-" + uuid.uuid4().hex[:8]
    msg = dict(msg)
    msg["node_id"] = node_id
    if ip and not msg.get("ip"):
        msg["ip"] = ip
    chans = msg.get("channels") or [channel]
    if channel not in chans:
        chans.append(channel)
    msg["channels"] = chans
    loop = asyncio.get_running_loop()
    row = await loop.run_in_executor(None, _upsert_node_sync, msg)
    await emit_event({"type": "mesh.node", "stage": "hello", "node_id": node_id,
                      "name": row["name"], "channel": channel,
                      "modules": list(_jloads(row["modules"], {}).keys())})
    # Auto-OTA: if the node opted in (config.ota.auto) and its firmware trails the
    # served version, the UI module queues an update. Best-effort, non-blocking.
    try:
        _uimod = sys.modules.get("mesh_ui_capabilities")
        if _uimod and hasattr(_uimod, "maybe_auto_ota"):
            asyncio.create_task(_uimod.maybe_auto_ota(
                node_id, msg.get("fw", ""), _jloads(row["config"], {}), chans))
    except Exception:
        pass
    return {
        "ok": True, "node_id": node_id, "token": row["token"],
        "modules": _jloads(row["modules"], {}), "config": _jloads(row["config"], {}),
        "heartbeat": HEARTBEAT_S, "poll_url": "/mesh/poll", "server_ts": now_iso(),
    }

async def _handle_telemetry(node_id: str, readings) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    norm: List[dict] = []
    if isinstance(readings, dict):                      # {"temp":21.5,"motion":1}
        for k, v in readings.items():
            norm.append({"metric": k, "value": v})
    elif isinstance(readings, list):                    # [{"metric","value","unit"}]
        for r in readings:
            if isinstance(r, dict):
                norm.append(r)
    if not norm:
        return {"ok": True, "stored": 0}
    loop = asyncio.get_running_loop()
    latest = await loop.run_in_executor(None, _store_telemetry_sync, node_id, norm)
    # best-effort: append numeric readings to Data Fabric history
    for r in norm:
        num = _num(r.get("value"))
        if num is None:
            continue
        ds = f"mesh.{node_id}.{r.get('metric')}"
        try:
            await _sqlite_insert_record({
                "id": f"{ds}:{int(time.time()*1000)}:{uuid.uuid4().hex[:4]}",
                "dataset_id": ds,
                "text": f"{node_id} {r.get('metric')}={num}{r.get('unit','')}",
                "data": {"node_id": node_id, "metric": r.get("metric"), "value": num,
                         "unit": r.get("unit", ""), "ts": r.get("ts") or now_iso()},
                "source_id": f"mesh:{node_id}",
                "tags": ["mesh", "telemetry", node_id, str(r.get("metric"))],
                "created_at": r.get("ts") or now_iso(),
            })
        except Exception:
            pass
    # Bridge CSI device-free presence (csi_motion/csi_present metrics) into the
    # positioning toolkit so mesh.presence + the map reflect it.
    try:
        if isinstance(readings, dict) and "csi_motion" in readings:
            _tk = sys.modules.get("mesh_toolkit_capabilities")
            if _tk and hasattr(_tk, "ingest_csi_presence"):
                asyncio.create_task(_tk.ingest_csi_presence(node_id, readings))
    except Exception:
        pass
    await emit_event({"type": "mesh.telemetry", "node_id": node_id, "readings": latest})
    return {"ok": True, "stored": len(norm), "latest": latest}

async def _handle_result(node_id: str, job_id: str, status: str, result, error: str) -> dict:
    if not job_id:
        return {"error": "job_id required"}
    loop = asyncio.get_running_loop()
    nid = await loop.run_in_executor(None, _job_result_sync, job_id,
                                     status or "done", result, error or "")
    if nid:
        await loop.run_in_executor(None, _touch_node_sync, nid, "", "", None)
        await emit_event({"type": "mesh.job", "stage": status or "done",
                          "node_id": nid, "job_id": job_id})
        # Bridge WiFi scans into the Network Map. wifi_scan results are a list of
        # APs (or {aps:[...]}). Shape-detect so we don't need the job type here.
        try:
            from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY as _CR
            _aps = (result.get("aps") or result.get("networks")) \
                if isinstance(result, dict) else (result if isinstance(result, list) else None)
            if isinstance(_aps, list) and any(
                isinstance(a, dict) and (a.get("bssid") or a.get("ssid")
                                         or a.get("BSSID") or a.get("SSID"))
                for a in _aps):
                _ing = _CR.get("netscan.wifi.ingest")
                if _ing:
                    asyncio.create_task(_ing["func"](observer=nid, aps=_aps, trace_id=""))
        except Exception:
            pass
        # Bridge the extended toolkit results (BLE scans, rf_range) into the
        # netmap + positioning stores. Shape-detected so the firmware just
        # reports naturally; each is a no-op if the shape doesn't match.
        try:
            _tk = sys.modules.get("mesh_toolkit_capabilities")
            if _tk:
                asyncio.create_task(_tk.ingest_ble_result(nid, result))
                asyncio.create_task(_tk.ingest_rf_range_result(nid, result))
        except Exception:
            pass
    return {"ok": bool(nid), "job_id": job_id}


def _fw_truthy(v) -> Optional[bool]:
    """Parse a flash-time toggle query param. Returns None when unset (leave the
    firmware's own default), else True/False."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if s == "":
        return None
    if s in ("0", "false", "no", "off", "none", "disable", "disabled"):
        return False
    return True                                     # any other value (1/true/ili9488/on…) = enable


def _bake_firmware_options(text: str, flavor: str, params: dict) -> str:
    """Rewrite the firmware's config lines from panel-supplied query params so a
    flashed/pushed node already has the display, SD, Wi-Fi and toolkit set the way
    the user picked — no post-flash step. Unset params leave the file default.

    Recognised: display (ILI9488 TFT), sd, wifi_ssid, wifi_pass, csi, ble, and
    board (a boards.json profile id → bakes its tft/sd/neopixel pin map)."""
    display = _fw_truthy(params.get("display"))
    sd = _fw_truthy(params.get("sd"))
    csi = _fw_truthy(params.get("csi"))
    ble = _fw_truthy(params.get("ble"))
    ssid = params.get("wifi_ssid")
    pw = params.get("wifi_pass")

    # Board profile → concrete pin map (tft/sd/neopixel), baked into the defaults
    # so a freshly flashed node already has the right pins for its board.
    bio = None
    board = params.get("board")
    if board:
        try:
            _bm = sys.modules.get("mesh_boards_capabilities")
            if _bm and hasattr(_bm, "board_io"):
                bio = _bm.board_io(board)
        except Exception as e:
            log.debug("board bake lookup %s: %s", board, e)
    b_tft = (bio or {}).get("tft") if isinstance(bio, dict) else None
    b_sd = (bio or {}).get("sd") if isinstance(bio, dict) else None
    b_neo = (bio or {}).get("neopixel") if isinstance(bio, dict) else None

    if flavor == "micropython":
        if isinstance(b_tft, dict) and b_tft.get("d"):
            lit = ('{"rst": %d, "cs": %d, "dc": %d, "wr": %d, "rd": %d, "d": %s}'
                   % (b_tft["rst"], b_tft["cs"], b_tft["dc"], b_tft["wr"], b_tft["rd"],
                      json.dumps(b_tft["d"])))
            text = re.sub(r"TFT_PINS\s*=\s*\{.*?\}", "TFT_PINS = " + lit, text, count=1, flags=re.S)
        if isinstance(b_sd, dict):
            lit = ('{"clk": %d, "miso": %d, "mosi": %d, "cs": %d}'
                   % (b_sd["clk"], b_sd["miso"], b_sd["mosi"], b_sd["cs"]))
            text = re.sub(r"SD_PINS\s*=\s*\{.*?\}", "SD_PINS = " + lit, text, count=1, flags=re.S)
        if isinstance(b_neo, int) and b_neo >= 0:
            text = re.sub(r"(?m)^NEO_PIN\s*=\s*None", "NEO_PIN = %d" % b_neo, text)
        if display is not None:
            text = re.sub(r"(?m)^(TFT_ENABLED\s*=\s*)(?:True|False)",
                          r"\g<1>" + ("True" if display else "False"), text)
        if sd is not None:
            text = re.sub(r"(?m)^(SD_ENABLED\s*=\s*)(?:True|False)",
                          r"\g<1>" + ("True" if sd else "False"), text)
        if ssid is not None:
            text = re.sub(r'(?m)^(WIFI_SSID\s*=\s*)"[^"]*"',
                          lambda m: m.group(1) + json.dumps(ssid), text)
        if pw is not None:
            text = re.sub(r'(?m)^(WIFI_PASS\s*=\s*)"[^"]*"',
                          lambda m: m.group(1) + json.dumps(pw), text)

    elif flavor == "arduino":
        if display is not None:
            text = re.sub(r"(?m)^(#define\s+USE_TFT_ILI9488_P8\s+)\d+",
                          r"\g<1>" + ("1" if display else "0"), text)
        if sd is not None:
            text = re.sub(r"(?m)^(#define\s+USE_SD_SPI\s+)\d+",
                          r"\g<1>" + ("1" if sd else "0"), text)
        if csi is not None:
            text = re.sub(r"(?m)^(#define\s+TOOLKIT_CSI\s+)\d+",
                          r"\g<1>" + ("1" if csi else "0"), text)
        if ble is not None:
            text = re.sub(r"(?m)^(#define\s+TOOLKIT_BLE\s+)\d+",
                          r"\g<1>" + ("1" if ble else "0"), text)

        def _redef(txt, macro, val):
            return re.sub(r"(?m)^(#define\s+%s\s+)-?\d+" % re.escape(macro),
                          r"\g<1>" + str(int(val)), txt)
        if isinstance(b_tft, dict):
            for macro, key in (("TFT_P8_RST", "rst"), ("TFT_P8_CS", "cs"), ("TFT_P8_DC", "dc"),
                               ("TFT_P8_WR", "wr"), ("TFT_P8_RD", "rd")):
                if key in b_tft:
                    text = _redef(text, macro, b_tft[key])
            for i, dp in enumerate(b_tft.get("d") or []):
                text = _redef(text, "TFT_P8_D%d" % i, dp)
        if isinstance(b_sd, dict):
            for macro, key in (("SD_PIN_CLK", "clk"), ("SD_PIN_MISO", "miso"),
                               ("SD_PIN_MOSI", "mosi"), ("SD_PIN_CS", "cs")):
                if key in b_sd:
                    text = _redef(text, macro, b_sd[key])
        if isinstance(b_neo, int) and b_neo >= 0:
            text = re.sub(r"(?m)^(int\s+neoPin\s*=\s*)-?\d+", r"\g<1>" + str(b_neo), text)
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Outbound delivery — persist, then nudge every live channel
# ─────────────────────────────────────────────────────────────────────────────

async def _deliver(node_id: str, jtype: str, payload: dict) -> str:
    job_id = await asyncio.get_running_loop().run_in_executor(
        None, _queue_job_sync, node_id, jtype, payload or {})
    await _nudge(node_id)
    return job_id

async def _nudge(node_id: str):
    """Wake every live channel so it drains the queue. Drains are atomic, so the
    first channel to fire wins and the others get nothing (no double-delivery)."""
    ev = _CMD_EVENTS.get(node_id)
    if ev:
        ev.set()                                        # HTTP long-poll + WS pusher
    if _MQTT.get("client") and node_id in (_MQTT.get("seen") or {}):
        asyncio.create_task(_mqtt_push(node_id))
    if node_id in _SERIAL["node_port"]:
        asyncio.create_task(_serial_push(node_id))

async def _drain_for(node_id: str, channel: str) -> List[dict]:
    return await asyncio.get_running_loop().run_in_executor(
        None, _drain_jobs_sync, node_id, channel)


# ─────────────────────────────────────────────────────────────────────────────
# Device-facing routes (raw @APP, machine-to-machine, excluded from schema)
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    _HERE         = _Path(__file__).parent
    _FW_DIR       = _HERE / "firmware"
    _BIN_DIR      = _FW_DIR / "bin"            # flashable .bin images (local + uploaded)
    _CATALOG_PATH = _FW_DIR / "catalog.json"

    def _safe_name(name: str) -> str:
        """Reject path traversal — bin/file names are basenames only."""
        return os.path.basename(name or "").replace("\\", "").strip()

    def _client_ip(req: "Request") -> str:
        try:
            return req.client.host if req.client else ""
        except Exception:
            return ""

    @APP.post("/mesh/hello", include_in_schema=False)
    async def _mesh_hello(req: "Request"):
        try:
            body = await req.json()
        except Exception:
            body = {}
        if MESH_TOKEN and (body.get("token") or req.headers.get("x-mesh-token")) != MESH_TOKEN:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        out = await _handle_hello(body, channel="http", ip=_client_ip(req))
        return JSONResponse(out)

    @APP.get("/mesh/poll", include_in_schema=False)
    async def _mesh_poll(req: "Request"):
        node_id = req.query_params.get("node_id", "")
        wait = max(0, min(POLL_MAX_WAIT, int(req.query_params.get("wait", "25") or 25)))
        token = req.query_params.get("token", "") or req.headers.get("x-mesh-token", "")
        if not node_id:
            return JSONResponse({"error": "node_id required"}, status_code=400)
        await _ensure_tables()
        loop = asyncio.get_running_loop()
        row = await loop.run_in_executor(None, _node_get_sync, node_id)
        if not _authed(token, row):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        await loop.run_in_executor(None, _touch_node_sync, node_id, "http", _client_ip(req), None)
        ev = _cmd_event(node_id)
        ev.clear()                                      # clear before drain (no missed nudge)
        cmds = await _drain_for(node_id, "http")
        if not cmds:
            try:
                await asyncio.wait_for(ev.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            cmds = await _drain_for(node_id, "http")
        return JSONResponse({"jobs": cmds, "heartbeat": HEARTBEAT_S, "ts": now_iso()})

    @APP.post("/mesh/telemetry", include_in_schema=False)
    async def _mesh_telemetry(req: "Request"):
        try:
            body = await req.json()
        except Exception:
            body = {}
        node_id = body.get("node_id", "")
        loop = asyncio.get_running_loop()
        row = await loop.run_in_executor(None, _node_get_sync, node_id)
        if not _authed(body.get("token", "") or req.headers.get("x-mesh-token", ""), row):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        await loop.run_in_executor(None, _touch_node_sync, node_id, "http",
                                   _client_ip(req), body.get("rssi"))
        out = await _handle_telemetry(node_id, body.get("readings") or body.get("metrics"))
        return JSONResponse(out)

    @APP.post("/mesh/result", include_in_schema=False)
    async def _mesh_result(req: "Request"):
        try:
            body = await req.json()
        except Exception:
            body = {}
        node_id = body.get("node_id", "")
        loop = asyncio.get_running_loop()
        row = await loop.run_in_executor(None, _node_get_sync, node_id)
        if not _authed(body.get("token", "") or req.headers.get("x-mesh-token", ""), row):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        out = await _handle_result(node_id, body.get("job_id", ""), body.get("status", "done"),
                                   body.get("result"), body.get("error", ""))
        return JSONResponse(out)

    @APP.websocket("/mesh/ws")
    async def _mesh_ws(ws: "WebSocket"):
        await ws.accept()
        node_id = ""
        try:
            while True:
                msg = await ws.receive_json()
                kind = msg.get("kind") or msg.get("action")
                if kind == "hello":
                    if MESH_TOKEN and msg.get("token") != MESH_TOKEN:
                        await ws.send_json({"type": "error", "message": "unauthorized"})
                        return
                    node_id = str(msg.get("node_id") or "").strip()
                    out = await _handle_hello(msg, channel="ws",
                                              ip=(ws.client.host if ws.client else ""))
                    node_id = out["node_id"]
                    _WS_CONNS[node_id] = ws
                    await ws.send_json({"type": "hello_ok", **out})
                    asyncio.create_task(_ws_pusher(node_id, ws))
                    _cmd_event(node_id).set()           # flush anything already queued
                elif kind == "telemetry":
                    await _handle_telemetry(node_id, msg.get("readings") or msg.get("metrics"))
                elif kind == "result":
                    await _handle_result(node_id, msg.get("job_id", ""),
                                         msg.get("status", "done"), msg.get("result"),
                                         msg.get("error", ""))
                elif kind in ("poll", "ack"):
                    _cmd_event(node_id).set()
                elif kind == "ping":
                    await ws.send_json({"type": "pong", "ts": now_iso()})
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.debug("mesh ws [%s]: %s", node_id, e)
        finally:
            if node_id and _WS_CONNS.get(node_id) is ws:
                _WS_CONNS.pop(node_id, None)

    async def _ws_pusher(node_id: str, ws: "WebSocket"):
        """Single sender of commands over a node's websocket."""
        ev = _cmd_event(node_id)
        while _WS_CONNS.get(node_id) is ws:
            ev.clear()
            cmds = await _drain_for(node_id, "ws")
            if cmds:
                try:
                    await ws.send_json({"type": "jobs", "jobs": cmds})
                except Exception:
                    break
                continue
            try:
                await asyncio.wait_for(ev.wait(), timeout=25)
            except asyncio.TimeoutError:
                pass

    @APP.get("/mesh/panel", include_in_schema=False)
    async def _mesh_panel_route():
        p = _HERE / "mesh_panel.html"
        if p.exists():
            return HTMLResponse(p.read_text(encoding="utf-8"))
        return HTMLResponse("<p style='color:red'>mesh_panel.html not found</p>")

    @APP.get("/mesh/firmware", include_in_schema=False)
    async def _mesh_firmware(req: "Request"):
        flavor = (req.query_params.get("flavor", "arduino") or "arduino").lower()
        node_id = req.query_params.get("node_id", "")
        files = {
            "arduino":     _HERE / "firmware" / "arduino" / "vera_mesh_node.ino",
            "micropython": _HERE / "firmware" / "micropython" / "main.py",
            "esp-idf":     _HERE / "firmware" / "esp-idf" / "main" / "vera_mesh_main.c",
        }
        p = files.get(flavor)
        if not p or not p.exists():
            return PlainTextResponse(f"// firmware flavor '{flavor}' not found", status_code=404)
        # Server URL baked into the firmware: explicit ?server= wins (workers on a
        # different network / Twingate), else the saved profile, else this request's host.
        server = req.query_params.get("server", "").strip()
        if not server:
            try:
                s = await asyncio.get_running_loop().run_in_executor(None, _settings_get_sync)
                server = (s.get("server_url") or "").strip()
            except Exception:
                server = ""
        base = server.rstrip("/") if server else str(req.base_url).rstrip("/")
        text = p.read_text(encoding="utf-8")
        text = (text.replace("{{SERVER_URL}}", base)
                    .replace("{{MESH_TOKEN}}", MESH_TOKEN or "open")
                    .replace("{{NODE_ID}}", node_id or "esp32-001"))
        # Bake flash-time options so the user configures once in the panel and the
        # downloaded/pushed firmware already has them — no post-flash config step.
        text = _bake_firmware_options(text, flavor, dict(req.query_params))
        return PlainTextResponse(text)

    # ── Firmware catalog / flashing assets ──────────────────────────────────────
    # The panel flashes .bin images over Web Serial (esptool-js). Binaries are
    # served from firmware/bin/ (local + uploaded), and remote runtime images
    # (e.g. official MicroPython builds) are proxied through the server so the
    # browser never hits a cross-origin CORS wall. .ino/.py "scripts" are pushed
    # to the device a different way (Arduino compile, or MicroPython REPL upload).

    def _load_catalog() -> list:
        try:
            data = json.loads(_CATALOG_PATH.read_text(encoding="utf-8")) if _CATALOG_PATH.exists() else []
        except Exception:
            data = []
        return data if isinstance(data, list) else (data.get("artifacts", []) if isinstance(data, dict) else [])

    _MP_URL_CACHE: Dict[str, tuple] = {}

    async def _mp_latest_url(board: str) -> str:
        """Resolve the newest *stable* MicroPython .bin for a board (e.g.
        'ESP32_GENERIC_S3-SPIRAM_OCT') from micropython.org, so catalog urls never
        rot as new versions ship. Previews/rc builds are skipped. Cached 1h."""
        import re as _re
        cached = _MP_URL_CACHE.get(board)
        if cached and (time.time() - cached[1] < 3600):
            return cached[0]
        # The download page is per *board* (ESP32_GENERIC_S3); the variant suffix
        # (-SPIRAM_OCT) only appears in the filename — so the page slug drops it.
        page_board = board.split("-")[0]
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                r = await c.get(f"https://micropython.org/download/{page_board}/")
            if r.status_code != 200:
                return ""
            rx = _re.compile(_re.escape(board) + r"-(\d{8})-v(\d+)\.(\d+)\.(\d+)\.bin")
            best = None
            for m in rx.finditer(r.text):
                key = (int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(1)))
                if best is None or key > best[0]:
                    best = (key, m.group(0))
            url = f"https://micropython.org/resources/firmware/{best[1]}" if best else ""
            if url:
                _MP_URL_CACHE[board] = (url, time.time())
            return url
        except Exception as e:
            log.debug("mp latest %s: %s", board, e)
            return ""

    @APP.get("/mesh/firmware/bin/{name}", include_in_schema=False)
    async def _mesh_firmware_bin(name: str):
        n = _safe_name(name)
        p = _BIN_DIR / n
        if not n.endswith(".bin") or not p.exists():
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(str(p), media_type="application/octet-stream", filename=n)

    @APP.get("/mesh/firmware/fetch", include_in_schema=False)
    async def _mesh_firmware_fetch(req: "Request"):
        """Proxy a remote .bin through the server (dodges browser CORS at flash time).

        Validates the response so a 404/403 HTML page can never be flashed as
        firmware (which bricks the board → 'invalid header: 0xffffffff'). ESP image
        files start with magic byte 0xE9.
        """
        import httpx, hashlib
        url = req.query_params.get("url", "")
        cid = req.query_params.get("id", "")
        if cid and not url:
            entry = next((e for e in _load_catalog() if e.get("id") == cid), None)
            if entry:
                # Prefer the pinned url (fast + reliable); only scrape "latest" if none.
                url = entry.get("url", "")
                if not url and entry.get("source") == "micropython_latest" and entry.get("mp_board"):
                    url = await _mp_latest_url(entry["mp_board"])
        if not (url.startswith("http://") or url.startswith("https://")):
            return PlainTextResponse("could not resolve a firmware url for this item", status_code=400)
        # Cache the full image ONCE server-side, then serve byte ranges from disk.
        # The panel pulls it in small chunks so a proxy/tunnel can't reset the big
        # (1.7 MB) single response; caching means we hit upstream only once.
        start = req.query_params.get("start")
        end = req.query_params.get("end")
        try:
            cache_dir = _BIN_DIR / ".cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cpath = cache_dir / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".bin")
            if (not cpath.exists()) or cpath.stat().st_size == 0:
                async with httpx.AsyncClient(timeout=180, follow_redirects=True) as c:
                    rr = await c.get(url)
                if rr.status_code != 200:
                    return PlainTextResponse(f"upstream returned {rr.status_code} for {url}", status_code=502)
                full = rr.content
                if not full or full[0] != 0xE9:
                    return PlainTextResponse(
                        "not an ESP firmware image (bad magic 0x%02X) — the url likely returned an "
                        "error page; update the catalog url or upload the .bin" % (full[0] if full else 0),
                        status_code=502)
                cpath.write_bytes(full)
            total = cpath.stat().st_size
            s = int(start) if (start not in (None, "")) else 0
            e = int(end) if (end not in (None, "")) else total - 1
            s = max(0, min(s, total)); e = min(e, total - 1)
            with open(cpath, "rb") as f:
                f.seek(s); chunk = f.read(max(0, e - s + 1))
            resp = Response(content=chunk, media_type="application/octet-stream")
            resp.headers["X-Total-Length"] = str(total)
            return resp
        except Exception as e:
            return PlainTextResponse(f"fetch failed: {e}", status_code=502)

    @APP.post("/mesh/firmware/upload", include_in_schema=False)
    async def _mesh_firmware_upload(file: "UploadFile" = File(...)):
        _BIN_DIR.mkdir(parents=True, exist_ok=True)
        n = _safe_name(file.filename) or ("upload-" + uuid.uuid4().hex[:8] + ".bin")
        if not n.endswith(".bin"):
            n += ".bin"
        data = await file.read()
        (_BIN_DIR / n).write_bytes(data)
        await emit_event({"type": "mesh.firmware", "stage": "uploaded", "name": n, "bytes": len(data)})
        return JSONResponse({"ok": True, "name": n, "bytes": len(data), "url": f"/mesh/firmware/bin/{n}"})


# ─────────────────────────────────────────────────────────────────────────────
# MQTT transport (optional) — VERA_MQTT_URL + aiomqtt
# ─────────────────────────────────────────────────────────────────────────────

def _parse_mqtt_url(url: str) -> dict:
    from urllib.parse import urlparse
    u = urlparse(url)
    return {"hostname": u.hostname or "localhost", "port": u.port or 1883,
            "username": u.username or None, "password": u.password or None}

async def _mqtt_push(node_id: str):
    client = _MQTT.get("client")
    if not client:
        return
    cmds = await _drain_for(node_id, "mqtt")
    if not cmds:
        return
    try:
        await client.publish(f"vera/mesh/{node_id}/down",
                             json.dumps({"jobs": cmds}).encode())
    except Exception as e:
        log.debug("mqtt push %s: %s", node_id, e)

async def _mqtt_loop():
    if not (HAS_AIOMQTT and MQTT_URL):
        return
    cfg = _parse_mqtt_url(MQTT_URL)
    while True:
        try:
            async with aiomqtt.Client(**cfg) as client:
                _MQTT["client"] = client
                await client.subscribe(MQTT_TOPIC_UP)
                log.info("mesh MQTT connected → %s:%s", cfg["hostname"], cfg["port"])
                async for message in client.messages:
                    try:
                        topic = str(message.topic)
                        parts = topic.split("/")
                        nid = parts[2] if len(parts) > 2 else ""
                        payload = json.loads(message.payload.decode() or "{}")
                        kind = payload.get("kind", "")
                        _MQTT.setdefault("seen", {})[nid] = time.time()
                        if kind == "hello":
                            await _handle_hello(payload, channel="mqtt")
                            await _mqtt_push(nid)
                        elif kind == "telemetry":
                            await _handle_telemetry(nid, payload.get("readings") or payload.get("metrics"))
                        elif kind == "result":
                            await _handle_result(nid, payload.get("job_id", ""),
                                                 payload.get("status", "done"),
                                                 payload.get("result"), payload.get("error", ""))
                        elif kind in ("poll", "ack"):
                            await _mqtt_push(nid)
                    except Exception as e:
                        log.debug("mqtt msg: %s", e)
        except Exception as e:
            _MQTT["client"] = None
            log.debug("mqtt loop reconnect in 5s: %s", e)
            await asyncio.sleep(5)


# ─────────────────────────────────────────────────────────────────────────────
# Serial-to-server transport (optional) — VERA_MESH_SERIAL_PORTS + pyserial
# ─────────────────────────────────────────────────────────────────────────────

async def _serial_push(node_id: str):
    port = _SERIAL["node_port"].get(node_id)
    if not port:
        return
    cmds = await _drain_for(node_id, "serial")
    outq = _SERIAL["out"].get(port)
    if outq is not None:
        for c in cmds:
            outq.put(json.dumps({"type": "jobs", "jobs": [c]}) + "\n")

def _serial_thread(port: str, loop):
    if not HAS_PYSERIAL:
        return
    try:
        ser = _pyserial.Serial(port, SERIAL_BAUD, timeout=1)
    except Exception as e:
        log.warning("mesh serial %s: %s", port, e)
        return
    log.info("mesh serial reader on %s @ %d", port, SERIAL_BAUD)
    outq = _SERIAL["out"].setdefault(port, _queue.Queue())
    while not _SERIAL["stop"]:
        try:
            # drain outbound first
            while not outq.empty():
                try:
                    ser.write(outq.get_nowait().encode())
                except Exception:
                    break
            line = ser.readline()
            if not line:
                continue
            try:
                msg = json.loads(line.decode(errors="ignore").strip())
            except Exception:
                continue
            nid = str(msg.get("node_id") or "").strip()
            if nid:
                _SERIAL["node_port"][nid] = port
            asyncio.run_coroutine_threadsafe(_dispatch_serial(msg, port), loop)
        except Exception as e:
            log.debug("serial %s: %s", port, e)
            time.sleep(0.5)
    try:
        ser.close()
    except Exception:
        pass

async def _dispatch_serial(msg: dict, port: str):
    kind = msg.get("kind", "")
    nid = str(msg.get("node_id") or "").strip()
    if kind == "hello":
        await _handle_hello(msg, channel="serial")
        await _serial_push(nid)
    elif kind == "telemetry":
        await _handle_telemetry(nid, msg.get("readings") or msg.get("metrics"))
    elif kind == "result":
        await _handle_result(nid, msg.get("job_id", ""), msg.get("status", "done"),
                             msg.get("result"), msg.get("error", ""))
    elif kind in ("poll", "ack"):
        await _serial_push(nid)


# ─────────────────────────────────────────────────────────────────────────────
# Management capabilities
# ─────────────────────────────────────────────────────────────────────────────

if _CAP_AVAILABLE:

    def _public_node(row: dict) -> dict:
        modules = _jloads(row.get("modules"), {})
        return {
            "node_id": row["node_id"], "name": row.get("name"), "role": row.get("role"),
            "group": row.get("group_name"), "parent_id": row.get("parent_id") or "",
            "modules": modules, "config": _jloads(row.get("config"), {}),
            "channels": _jloads(row.get("channels"), []),
            "board": row.get("board"), "fw_version": row.get("fw_version"),
            "ip": row.get("ip"), "mac": row.get("mac"), "rssi": row.get("rssi"),
            "last_seen": row.get("last_seen"),
            "status": _status_of(row.get("last_seen")),
            "telemetry": _jloads(row.get("last_telemetry"), {}),
        }

    @capability(
        "mesh.nodes", http_method="GET", http_path="/mesh/nodes",
        http_tags=["mesh"], memory="off", silent=True,
        description="List all mesh nodes with computed status, modules, transports and "
                    "latest telemetry. Output: {nodes:[...], count, transports}.",
    )
    async def cap_mesh_nodes(trace_id=None) -> dict:
        await _ensure_tables()
        rows = await asyncio.get_running_loop().run_in_executor(None, _nodes_all_sync)
        return {
            "nodes": [_public_node(r) for r in rows], "count": len(rows),
            "transports": {
                "http": True, "ws": True,
                "mqtt": bool(HAS_AIOMQTT and MQTT_URL),
                "serial": bool(HAS_PYSERIAL and SERIAL_PORTS),
            },
            "serial_ports": [
                {"port": p, "node_id": next((nid for nid, pp in _SERIAL["node_port"].items() if pp == p), None)}
                for p in SERIAL_PORTS],
            "module_kinds": MODULE_KINDS, "heartbeat": HEARTBEAT_S,
        }

    @capability(
        "mesh.node", http_method="GET", http_path="/mesh/node",
        http_tags=["mesh"], memory="off", silent=True,
        description="One node's detail incl. recent telemetry and job history. "
                    "Input: node_id (str!). Output: {node, telemetry:[...], jobs:[...]}.",
    )
    async def cap_mesh_node(node_id: str = "", trace_id=None) -> dict:
        if not node_id:
            return {"error": "node_id required"}
        await _ensure_tables()
        loop = asyncio.get_running_loop()
        row = await loop.run_in_executor(None, _node_get_sync, node_id)
        if not row:
            return {"error": "unknown node", "node_id": node_id}
        tele = await loop.run_in_executor(None, _telemetry_query_sync, node_id, "", 120)
        jobs = await loop.run_in_executor(None, _jobs_query_sync, node_id, 40)
        return {"node": _public_node(row), "telemetry": tele, "jobs": jobs}

    @capability(
        "mesh.graph", http_method="GET", http_path="/mesh/graph",
        http_tags=["mesh"], memory="off", silent=True,
        description="Mesh topology as a node graph. Edges follow reported parent_id "
                    "uplinks (root→relay→leaf) else a star to the Vera hub. "
                    "Output: {nodes:[{id,label,kind,status,...}], edges:[{from,to,channel}]}.",
    )
    async def cap_mesh_graph(trace_id=None) -> dict:
        await _ensure_tables()
        rows = await asyncio.get_running_loop().run_in_executor(None, _nodes_all_sync)
        ids = {r["node_id"] for r in rows}
        nodes = [{"id": HUB_ID, "label": "Vera Hub", "kind": "hub", "status": "online"}]
        edges = []
        for r in rows:
            chans = _jloads(r.get("channels"), [])
            nodes.append({
                "id": r["node_id"], "label": r.get("name") or r["node_id"], "kind": "node",
                "status": _status_of(r.get("last_seen")),
                "role": r.get("role"), "modules": list(_jloads(r.get("modules"), {}).keys()),
                "channels": chans, "rssi": r.get("rssi"),
                "telemetry": _jloads(r.get("last_telemetry"), {}),
                "parent_id": r.get("parent_id") or "",
            })
            parent = r.get("parent_id") if (r.get("parent_id") in ids) else HUB_ID
            edges.append({"from": r["node_id"], "to": parent,
                          "channel": chans[0] if chans else ""})
        # Server-attached USB serial ports with no enrolled node yet — show them too.
        for p in SERIAL_PORTS:
            if any(pp == p for pp in _SERIAL["node_port"].values()):
                continue
            nodes.append({"id": "serial:" + p, "label": p, "kind": "serial_port",
                          "status": "online", "channels": ["serial"]})
            edges.append({"from": "serial:" + p, "to": HUB_ID, "channel": "serial"})
        return {"nodes": nodes, "edges": edges, "count": len(rows)}

    @capability(
        "mesh.telemetry", http_method="GET", http_path="/mesh/telemetry",
        http_tags=["mesh"], memory="off", silent=True,
        description="Recent telemetry rows for a node. Input: node_id (str!), "
                    "metric (str optional), limit (int=120). Output: {readings:[...]}.",
    )
    async def cap_mesh_telemetry(node_id: str = "", metric: str = "",
                                 limit: int = 120, trace_id=None) -> dict:
        if not node_id:
            return {"error": "node_id required"}
        await _ensure_tables()
        rows = await asyncio.get_running_loop().run_in_executor(
            None, _telemetry_query_sync, node_id, metric, max(1, min(2000, limit)))
        return {"node_id": node_id, "metric": metric, "readings": rows, "count": len(rows)}

    @capability(
        "mesh.send", http_method="POST", http_path="/mesh/send",
        http_tags=["mesh"], memory="on",
        description="Queue a command to a node (delivered via its best live transport, "
                    "else on next poll). Input: node_id (str!), type (str! e.g. web_fetch|"
                    "kiosk_set|control_set|alert|read_sensor|config|reboot|identify|"
                    "serial_write — forward ESC/POS/UART bytes {data_b64,baud} to a "
                    "USB thermal printer or serial peripheral on the node), "
                    "payload (dict). Output: {ok, job_id}.",
    )
    async def cap_mesh_send(node_id: str = "", type: str = "", payload=None, trace_id=None) -> dict:
        if not node_id or not type:
            return {"error": "node_id and type required"}
        await _ensure_tables()
        row = await asyncio.get_running_loop().run_in_executor(None, _node_get_sync, node_id)
        if not row:
            return {"error": "unknown node", "node_id": node_id}
        payload = _jloads(payload, {}) if isinstance(payload, str) else (payload or {})
        job_id = await _deliver(node_id, type, payload)
        await emit_event({"type": "mesh.job", "stage": "queued", "node_id": node_id,
                          "job_id": job_id, "job_type": type})
        return {"ok": True, "job_id": job_id, "node_id": node_id, "type": type}

    @capability(
        "mesh.config", http_method="POST", http_path="/mesh/config",
        http_tags=["mesh"], memory="on",
        description="Set a node's per-module config (persisted + pushed to the device). "
                    "Input: node_id (str!), config (dict — {module:{enabled,...}}), "
                    "merge (bool=True). Output: {ok, config, job_id}.",
    )
    async def cap_mesh_config(node_id: str = "", config=None, merge: bool = True, trace_id=None) -> dict:
        if not node_id:
            return {"error": "node_id required"}
        await _ensure_tables()
        loop = asyncio.get_running_loop()
        row = await loop.run_in_executor(None, _node_get_sync, node_id)
        if not row:
            return {"error": "unknown node", "node_id": node_id}
        new_cfg = _jloads(config, {}) if isinstance(config, str) else (config or {})
        cur = _jloads(row.get("config"), {})
        merged = {**cur, **new_cfg} if merge else new_cfg
        await loop.run_in_executor(None, _set_node_fields_sync, node_id, {"config": json.dumps(merged)})
        job_id = await _deliver(node_id, "config", {"config": merged})
        await emit_event({"type": "mesh.node", "stage": "config", "node_id": node_id})
        return {"ok": True, "config": merged, "job_id": job_id}

    @capability(
        "mesh.update", http_method="POST", http_path="/mesh/update",
        http_tags=["mesh"], memory="on",
        description="Update a node's metadata. Input: node_id (str!), name (str), "
                    "role (str), group (str), parent_id (str). Output: {ok}.",
    )
    async def cap_mesh_update(node_id: str = "", name: str = "", role: str = "",
                              group: str = "", parent_id: str = "", trace_id=None) -> dict:
        if not node_id:
            return {"error": "node_id required"}
        await _ensure_tables()
        fields = {}
        if name:   fields["name"] = name
        if role:   fields["role"] = role
        if group:  fields["group_name"] = group
        if parent_id is not None and parent_id != "":
            fields["parent_id"] = parent_id
        ok = await asyncio.get_running_loop().run_in_executor(
            None, _set_node_fields_sync, node_id, fields)
        return {"ok": ok, "node_id": node_id, "updated": list(fields.keys())}

    @capability(
        "mesh.forget", http_method="POST", http_path="/mesh/forget",
        http_tags=["mesh"], memory="on",
        description="Remove a node from the mesh. Input: node_id (str!), "
                    "delete_data (bool=False — also drop its telemetry). Output: {ok}.",
    )
    async def cap_mesh_forget(node_id: str = "", delete_data: bool = False, trace_id=None) -> dict:
        if not node_id:
            return {"error": "node_id required"}
        await _ensure_tables()
        res = await asyncio.get_running_loop().run_in_executor(
            None, _delete_node_sync, node_id, bool(delete_data))
        _WS_CONNS.pop(node_id, None)
        _CMD_EVENTS.pop(node_id, None)
        await emit_event({"type": "mesh.node", "stage": "forgotten", "node_id": node_id})
        return {"ok": True, "node_id": node_id, **res}

    @capability(
        "mesh.jobs", http_method="GET", http_path="/mesh/jobs",
        http_tags=["mesh"], memory="off", silent=True,
        description="Job queue / history. Input: node_id (str optional), limit (int=60). "
                    "Output: {jobs:[...]}.",
    )
    async def cap_mesh_jobs(node_id: str = "", limit: int = 60, trace_id=None) -> dict:
        await _ensure_tables()
        rows = await asyncio.get_running_loop().run_in_executor(
            None, _jobs_query_sync, node_id, max(1, min(500, limit)))
        return {"jobs": rows, "count": len(rows)}

    @capability(
        "mesh.broadcast", http_method="POST", http_path="/mesh/broadcast",
        http_tags=["mesh"], memory="on",
        description="Send a command to many nodes by filter. Input: type (str!), payload (dict), "
                    "group (str), role (str), module (str — only nodes advertising it). "
                    "Output: {ok, job_ids, nodes}.",
    )
    async def cap_mesh_broadcast(type: str = "", payload=None, group: str = "",
                                 role: str = "", module: str = "", trace_id=None) -> dict:
        if not type:
            return {"error": "type required"}
        await _ensure_tables()
        rows = await asyncio.get_running_loop().run_in_executor(None, _nodes_all_sync)
        payload = _jloads(payload, {}) if isinstance(payload, str) else (payload or {})
        targets = []
        for r in rows:
            if group and r.get("group_name") != group:
                continue
            if role and r.get("role") != role:
                continue
            if module and module not in _jloads(r.get("modules"), {}):
                continue
            targets.append(r["node_id"])
        job_ids = [await _deliver(nid, type, payload) for nid in targets]
        await emit_event({"type": "mesh.job", "stage": "broadcast", "job_type": type,
                          "nodes": len(targets)})
        return {"ok": True, "job_ids": job_ids, "nodes": targets, "count": len(targets)}

    # ── Typed convenience wrappers (one per module — agent & UI friendly) ───────

    @capability(
        "mesh.web_fetch", http_method="POST", http_path="/mesh/web_fetch",
        http_tags=["mesh"], memory="on",
        description="Ask a node to HTTP-fetch a URL and return the result. "
                    "Input: node_id (str!), url (str!), method (str=GET). Output: {ok, job_id}.",
    )
    async def cap_mesh_web_fetch(node_id: str = "", url: str = "", method: str = "GET",
                                 trace_id=None) -> dict:
        if not node_id or not url:
            return {"error": "node_id and url required"}
        return await cap_mesh_send(node_id=node_id, type="web_fetch", payload={"url": url, "method": method})

    @capability(
        "mesh.kiosk_set", http_method="POST", http_path="/mesh/kiosk_set",
        http_tags=["mesh"], memory="on",
        description="Set what a kiosk/touchscreen node displays. Input: node_id (str!), "
                    "url (str) OR text (str), brightness (int 0-100 optional). Output: {ok, job_id}.",
    )
    async def cap_mesh_kiosk_set(node_id: str = "", url: str = "", text: str = "",
                                 brightness: int = -1, trace_id=None) -> dict:
        if not node_id:
            return {"error": "node_id required"}
        payload = {}
        if url:  payload["url"] = url
        if text: payload["text"] = text
        if brightness >= 0: payload["brightness"] = brightness
        return await cap_mesh_send(node_id=node_id, type="kiosk_set", payload=payload)

    @capability(
        "mesh.control_set", http_method="POST", http_path="/mesh/control_set",
        http_tags=["mesh"], memory="on",
        description="Actuate a node output (relay/GPIO). Input: node_id (str!), "
                    "channel (str/int — output id), value (any — 1/0/on/off/pwm). Output: {ok, job_id}.",
    )
    async def cap_mesh_control_set(node_id: str = "", channel: str = "", value=None,
                                   trace_id=None) -> dict:
        if not node_id or channel == "":
            return {"error": "node_id and channel required"}
        return await cap_mesh_send(node_id=node_id, type="control_set", payload={"channel": channel, "value": value})

    @capability(
        "mesh.alert", http_method="POST", http_path="/mesh/alert",
        http_tags=["mesh"], memory="on",
        description="Raise an alert on a node (buzzer/LED/screen). Input: node_id (str!), "
                    "message (str), level (str=info|warn|crit), sound (bool=True). Output: {ok, job_id}.",
    )
    async def cap_mesh_alert(node_id: str = "", message: str = "", level: str = "info",
                             sound: bool = True, trace_id=None) -> dict:
        if not node_id:
            return {"error": "node_id required"}
        return await cap_mesh_send(node_id=node_id, type="alert",
                                   payload={"message": message, "level": level, "sound": bool(sound)})

    @capability(
        "mesh.identify", http_method="POST", http_path="/mesh/identify",
        http_tags=["mesh"], memory="on",
        description="Blink a node's LED so you can find it physically. Input: node_id (str!). "
                    "Output: {ok, job_id}.",
    )
    async def cap_mesh_identify(node_id: str = "", trace_id=None) -> dict:
        if not node_id:
            return {"error": "node_id required"}
        return await cap_mesh_send(node_id=node_id, type="identify", payload={"seconds": 5})

    @capability(
        "mesh.reboot", http_method="POST", http_path="/mesh/reboot",
        http_tags=["mesh"], memory="on",
        description="Reboot a node. Input: node_id (str!). Output: {ok, job_id}.",
    )
    async def cap_mesh_reboot(node_id: str = "", trace_id=None) -> dict:
        if not node_id:
            return {"error": "node_id required"}
        return await cap_mesh_send(node_id=node_id, type="reboot", payload={})

    # ── IO control (works over any transport — queued like every other job) ──────

    @capability(
        "mesh.io.set", http_method="POST", http_path="/mesh/io/set",
        http_tags=["mesh"], memory="on",
        description="Drive a GPIO on a node. Input: node_id (str!), pin (int/str!), "
                    "value (any — 1/0/on/off/toggle or 0-255 for pwm), mode (str=digital|pwm|input). "
                    "Output: {ok, job_id}.",
    )
    async def cap_mesh_io_set(node_id: str = "", pin="", value=None, mode: str = "digital",
                              trace_id=None) -> dict:
        if not node_id or pin == "" or pin is None:
            return {"error": "node_id and pin required"}
        return await cap_mesh_send(node_id=node_id, type="io_set", payload={"pin": pin, "value": value, "mode": mode})

    @capability(
        "mesh.io.read", http_method="POST", http_path="/mesh/io/read",
        http_tags=["mesh"], memory="on",
        description="Read a GPIO/ADC on a node (result returns via telemetry). Input: node_id (str!), "
                    "pin (int/str!), analog (bool=False). Output: {ok, job_id}.",
    )
    async def cap_mesh_io_read(node_id: str = "", pin="", analog: bool = False, trace_id=None) -> dict:
        if not node_id or pin == "" or pin is None:
            return {"error": "node_id and pin required"}
        return await cap_mesh_send(node_id=node_id, type="io_read", payload={"pin": pin, "analog": bool(analog)})

    @capability(
        "mesh.wifi.scan", http_method="POST", http_path="/mesh/wifi/scan",
        http_tags=["mesh"], memory="on",
        description="Ask a node to scan Wi-Fi and report nearby APs (ssid/bssid/channel/rssi/auth). "
                    "The AP list comes back as the job result. Input: node_id (str!). Output: {ok, job_id}.",
    )
    async def cap_mesh_wifi_scan(node_id: str = "", trace_id=None) -> dict:
        if not node_id:
            return {"error": "node_id required"}
        return await cap_mesh_send(node_id=node_id, type="wifi_scan", payload={})

    # ── Program over Wi-Fi (OTA) ────────────────────────────────────────────────

    @capability(
        "mesh.ota", http_method="POST", http_path="/mesh/ota",
        http_tags=["mesh"], memory="on",
        description="Program a node over Wi-Fi. mode='bin' does an Arduino/IDF OTA flash from a "
                    ".bin URL; mode='file' pushes a source file (e.g. MicroPython main.py) and resets. "
                    "Input: node_id (str!), url (str) OR artifact (str — a name under firmware/bin or "
                    "a catalog id), mode (str=bin|file), filename (str='main.py'). Output: {ok, job_id, url}.",
    )
    async def cap_mesh_ota(node_id: str = "", url: str = "", artifact: str = "",
                           mode: str = "bin", filename: str = "main.py", trace_id=None) -> dict:
        if not node_id:
            return {"error": "node_id required"}
        if artifact and not url:
            url = f"/mesh/firmware/bin/{_safe_name(artifact)}"
        if not url:
            return {"error": "url or artifact required"}
        payload = {"url": url, "mode": mode}
        if mode == "file":
            payload["filename"] = filename
        res = await cap_mesh_send(node_id=node_id, type="ota", payload=payload)
        if isinstance(res, dict):
            res["url"] = url
        return res

    # ── Worker assignment (recurring edge tasks the node runs autonomously) ──────

    @capability(
        "mesh.worker.assign", http_method="POST", http_path="/mesh/worker/assign",
        http_tags=["mesh"], memory="on",
        description="Give a worker node a recurring task it runs on-device and reports back. "
                    "Input: node_id (str!), task (dict — {type, interval_s, payload}), "
                    "replace (bool=False — replace the task list vs append). Output: {ok, tasks}.",
    )
    async def cap_mesh_worker_assign(node_id: str = "", task=None, replace: bool = False,
                                     trace_id=None) -> dict:
        if not node_id:
            return {"error": "node_id required"}
        await _ensure_tables()
        loop = asyncio.get_running_loop()
        row = await loop.run_in_executor(None, _node_get_sync, node_id)
        if not row:
            return {"error": "unknown node", "node_id": node_id}
        cfg = _jloads(row.get("config"), {})
        worker = cfg.get("worker") or {}
        tasks = [] if replace else list(worker.get("tasks") or [])
        t = _jloads(task, {}) if isinstance(task, str) else (task or {})
        if t:
            tasks.append(t)
        worker["tasks"] = tasks
        worker["enabled"] = True
        cfg["worker"] = worker
        await loop.run_in_executor(None, _set_node_fields_sync, node_id, {"config": json.dumps(cfg)})
        job_id = await _deliver(node_id, "config", {"config": cfg})
        return {"ok": True, "tasks": tasks, "job_id": job_id}

    # ── Firmware catalog & on-device tooling ────────────────────────────────────

    @capability(
        "mesh.firmware.catalog", http_method="GET", http_path="/mesh/firmware/catalog",
        http_tags=["mesh"], memory="off", silent=True,
        description="List flashable firmware/tools (runtimes, sketches, uploaded .bins) for the "
                    "panel flasher, plus host tool availability. Output: {artifacts:[...], tools}.",
    )
    async def cap_mesh_firmware_catalog(trace_id=None) -> dict:
        import shutil
        artifacts = list(_load_catalog())
        # add any uploaded/local .bin images not already in the catalog
        try:
            _BIN_DIR.mkdir(parents=True, exist_ok=True)
            known = {e.get("id") for e in artifacts}
            for p in sorted(_BIN_DIR.glob("*.bin")):
                aid = "bin:" + p.name
                if aid in known:
                    continue
                artifacts.append({"id": aid, "label": p.name, "kind": "bin", "chip": "auto",
                                  "offset": "auto", "source": "local_bin",
                                  "url": f"/mesh/firmware/bin/{p.name}",
                                  "bytes": p.stat().st_size})
        except Exception as e:
            log.debug("catalog bin scan: %s", e)
        try:
            has_esptool = bool(shutil.which("esptool.py") or shutil.which("esptool"))
        except Exception:
            has_esptool = False
        # Best-effort probe of the vera-builder container (server-side compiler).
        builder = {"url": os.environ.get("VERA_BUILDER_URL", "http://vera-builder:8080"), "ok": False}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=2.5) as c:
                r = await c.get(builder["url"].rstrip("/") + "/health")
            if r.status_code == 200:
                h = r.json()
                builder["ok"] = True
                builder["tools"] = h.get("tools")
        except Exception:
            pass
        return {
            "artifacts": artifacts,
            "tools": {
                "arduino_cli": bool(shutil.which("arduino-cli"))
                or bool((builder.get("tools") or {}).get("arduino_cli")),
                "esptool": has_esptool,
                "builder": builder,
                "can_build": builder["ok"] or bool(shutil.which("arduino-cli")),
                "flash_via": "browser-web-serial",   # esptool-js runs client-side
            },
        }

    async def _build_arduino_via_builder(source: str, main: str, fqbn: str) -> Optional[dict]:
        """POST the sketch to the vera-builder service. Returns the builder JSON,
        or None if no builder is reachable (so we can fall back to a local cli)."""
        import httpx
        url = os.environ.get("VERA_BUILDER_URL", "http://vera-builder:8080").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=1200) as c:
                r = await c.post(url + "/build/arduino",
                                 json={"source": source, "main": main, "fqbn": fqbn})
            if r.status_code != 200:
                return {"ok": False, "error": f"builder {r.status_code}", "log": r.text[:1500]}
            return r.json()
        except Exception as e:
            log.debug("builder unreachable (%s): %s", url, e)
            return None

    @capability(
        "mesh.firmware.build", http_method="POST", http_path="/mesh/firmware/build",
        http_tags=["mesh"], memory="on",
        description="Compile the Vera node Arduino sketch to a flashable, merged (0x0) .bin and add it "
                    "to the catalog so the panel can flash it. Uses the vera-builder container if "
                    "reachable (VERA_BUILDER_URL), else a local arduino-cli. Input: sketch (str — "
                    "reserved), fqbn (str, default esp32:esp32:esp32s3:CDCOnBoot=default — S3 with USB-CDC "
                    "off so GPIO19/20 are free for the parallel TFT). Output: {ok, name, url, via} or {error, hint}.",
    )
    async def cap_mesh_firmware_build(sketch: str = "",
                                      fqbn: str = "esp32:esp32:esp32s3:CDCOnBoot=default",
                                      trace_id=None) -> dict:
        import shutil, base64, asyncio as _aio
        src = _HERE / "firmware" / "arduino" / "vera_mesh_node.ino"
        if not src.exists():
            return {"error": "sketch not found"}
        source = src.read_text(encoding="utf-8")
        # Resolve template placeholders so the sketch compiles standalone; the node
        # still self-provisions NODE from its chip id and SERVER/Wi-Fi over serial.
        try:
            s = await _aio.get_running_loop().run_in_executor(None, _settings_get_sync)
            server = (s.get("server_url") or "").strip()
        except Exception:
            server = ""
        source = (source.replace("{{SERVER_URL}}", server)
                        .replace("{{MESH_TOKEN}}", MESH_TOKEN or "open")
                        .replace("{{NODE_ID}}", ""))
        _BIN_DIR.mkdir(parents=True, exist_ok=True)
        name = "vera_node_arduino.bin"

        # 1) Preferred: the vera-builder container (no compiler needed in Vera).
        res = await _build_arduino_via_builder(source, "vera_mesh_node.ino", fqbn)
        if res is not None:
            if res.get("ok") and res.get("bin_b64"):
                (_BIN_DIR / name).write_bytes(base64.b64decode(res["bin_b64"]))
                return {"ok": True, "name": name, "url": f"/mesh/firmware/bin/{name}",
                        "via": "builder", "chip": res.get("chip"), "merged": res.get("merged"),
                        "fqbn": fqbn, "log": (res.get("log") or "")[-800:]}
            return {"error": res.get("error", "compile failed"), "via": "builder",
                    "log": (res.get("log") or "")[-1500:]}

        # 2) Fallback: a local arduino-cli if one happens to be installed.
        if shutil.which("arduino-cli"):
            out_dir = _BIN_DIR / "build"
            try:
                proc = await _aio.create_subprocess_exec(
                    "arduino-cli", "compile", "--fqbn", fqbn, "--output-dir", str(out_dir), str(src.parent),
                    stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.STDOUT)
                out, _ = await proc.communicate()
                if proc.returncode != 0:
                    return {"error": "compile failed", "via": "local",
                            "log": (out or b"").decode(errors="ignore")[-1500:]}
                bins = sorted(out_dir.glob("*.bin"))
                if not bins:
                    return {"error": "no .bin produced", "via": "local"}
                (_BIN_DIR / name).write_bytes(bins[0].read_bytes())
                return {"ok": True, "name": name, "url": f"/mesh/firmware/bin/{name}", "via": "local"}
            except Exception as e:
                return {"error": str(e), "via": "local"}

        return {"error": "no builder reachable and no local arduino-cli",
                "hint": "start the build container (docker compose up -d vera-builder), or set "
                        "VERA_BUILDER_URL, or build vera_mesh_node.ino in the Arduino IDE "
                        "(USB CDC On Boot: Disabled)."}

    @capability(
        "mesh.firmware.probe", http_method="GET", http_path="/mesh/firmware/probe",
        http_tags=["mesh"], memory="off", silent=True,
        description="Diagnose a firmware download from the SERVER's point of view (small response, "
                    "so it works even when the big proxied download fails): resolves the url and "
                    "range-fetches the first bytes. Input: id (str) or url (str). "
                    "Output: {ok, url, status, content_length, magic_ok, error}.",
    )
    async def cap_mesh_firmware_probe(id: str = "", url: str = "", trace_id=None) -> dict:
        import httpx
        if id and not url:
            entry = next((e for e in _load_catalog() if e.get("id") == id), None)
            if entry:
                url = entry.get("url", "")
                if not url and entry.get("source") == "micropython_latest" and entry.get("mp_board"):
                    url = await _mp_latest_url(entry["mp_board"])
        if not (url.startswith("http://") or url.startswith("https://")):
            return {"ok": False, "url": url, "error": "no resolvable url for this item"}
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                r = await c.get(url, headers={"Range": "bytes=0-63"})
            first = r.content[:1]
            magic_ok = first == b"\xe9"
            return {
                "ok": r.status_code in (200, 206) and magic_ok,
                "url": url, "status": r.status_code,
                "content_length": r.headers.get("content-range") or r.headers.get("content-length"),
                "magic_ok": magic_ok,
                "error": "" if r.status_code in (200, 206) else f"upstream http {r.status_code}",
            }
        except Exception as e:
            return {"ok": False, "url": url, "error": str(e)[:300]}

    # ── Settings: persisted provisioning profile (server URL + Wi-Fi) ───────────

    @capability(
        "mesh.settings.get", http_method="GET", http_path="/mesh/settings",
        http_tags=["mesh"], memory="off", silent=True,
        description="Get the saved provisioning profile (remembered across reboots). The Wi-Fi "
                    "password is redacted unless reveal=1 (the browser needs it to push creds to a "
                    "device). Input: reveal (bool=False). Output: {server_url, wifi_ssid, "
                    "wifi_password, has_wifi_password, token, secrets}.",
    )
    async def cap_mesh_settings_get(reveal: bool = False, trace_id=None) -> dict:
        await _ensure_tables()
        s = await asyncio.get_running_loop().run_in_executor(None, _settings_get_sync)
        sealed = s.get("wifi_pass", "")
        return {
            "server_url": s.get("server_url", ""),
            "wifi_ssid": s.get("wifi_ssid", ""),
            "wifi_password": (_open_secret(sealed) if reveal else ("••••••••" if sealed else "")),
            "has_wifi_password": bool(sealed),
            "token": s.get("token", ""),
            "secrets": HAS_SECRETS,
        }

    @capability(
        "mesh.settings.set", http_method="POST", http_path="/mesh/settings/set",
        http_tags=["mesh"], memory="on",
        description="Save the provisioning profile (persisted; Wi-Fi password sealed at rest). Only "
                    "the fields you pass are updated; a blank wifi_password keeps the existing one. "
                    "Input: server_url (str), wifi_ssid (str), wifi_password (str), token (str). Output: {ok, saved}.",
    )
    async def cap_mesh_settings_set(server_url=None, wifi_ssid=None, wifi_password=None,
                                    token=None, trace_id=None) -> dict:
        await _ensure_tables()
        pairs = {}
        if server_url is not None:
            pairs["server_url"] = str(server_url).strip()
        if wifi_ssid is not None:
            pairs["wifi_ssid"] = str(wifi_ssid)
        if token is not None:
            pairs["token"] = str(token)
        if wifi_password:                       # only overwrite when a new password is supplied
            try:
                pairs["wifi_pass"] = _seal(str(wifi_password))
            except Exception as e:
                return {"error": f"could not seal Wi-Fi password: {e}"}
        if pairs:
            await asyncio.get_running_loop().run_in_executor(None, _settings_set_sync, pairs)
        return {"ok": True, "saved": [k for k in pairs if k != "wifi_pass"] + (["wifi_password"] if "wifi_pass" in pairs else [])}

    # ── Panel registration ──────────────────────────────────────────────────────

    register_ui(
        "mesh", "Mesh", "⬢",   # monochrome node-network glyph (distinct from Cap Hub's ⬡)
        """<div id="mesh-mount" style="height:100%;display:flex;flex-direction:column;">
            <iframe src="/mesh/panel"
                    style="flex:1;border:none;width:100%;height:100%"
                    allow="serial; clipboard-read; clipboard-write"></iframe>
        </div>""",
        "",
        ui_caps=["mesh.nodes", "mesh.node", "mesh.graph", "mesh.telemetry", "mesh.send",
                 "mesh.config", "mesh.update", "mesh.forget", "mesh.jobs", "mesh.broadcast",
                 "mesh.web_fetch", "mesh.kiosk_set", "mesh.control_set", "mesh.alert",
                 "mesh.identify", "mesh.reboot", "mesh.io.set", "mesh.io.read", "mesh.ota",
                 "mesh.worker.assign", "mesh.firmware.catalog", "mesh.firmware.build",
                 "mesh.firmware.probe", "mesh.settings.get", "mesh.settings.set", "mesh.wifi.scan"],
        mode="tab", tab_order=68,
    )

    # ── Schedulers / startup ────────────────────────────────────────────────────

    async def _liveness_tick():
        """Mark nodes stale/offline and emit status changes for the live stream."""
        try:
            await _ensure_tables()
            rows = await asyncio.get_running_loop().run_in_executor(None, _nodes_all_sync)
            for r in rows:
                st = _status_of(r.get("last_seen"))
                if st in ("stale", "offline"):
                    await emit_event({"type": "mesh.node", "stage": "status",
                                      "node_id": r["node_id"], "status": st})
        except Exception as e:
            log.debug("liveness tick: %s", e)

    async def _startup():
        global _STARTED
        if _STARTED:
            return
        _STARTED = True
        await _ensure_tables()
        try:
            _BIN_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        if HAS_AIOMQTT and MQTT_URL:
            asyncio.create_task(_mqtt_loop())
        if HAS_PYSERIAL and SERIAL_PORTS:
            _SERIAL["loop"] = asyncio.get_running_loop()
            for port in SERIAL_PORTS:
                threading.Thread(target=_serial_thread, args=(port, _SERIAL["loop"]),
                                 daemon=True).start()
        log.info("mesh capabilities loaded — http+ws on, mqtt=%s, serial=%s, token=%s",
                 bool(HAS_AIOMQTT and MQTT_URL), bool(HAS_PYSERIAL and SERIAL_PORTS),
                 bool(MESH_TOKEN))

    schedule(_liveness_tick, HEARTBEAT_S, "mesh_liveness")
    schedule(_startup, interval=999999, name="mesh_startup")
    try:
        _loop = asyncio.get_event_loop()
        if _loop.is_running():
            _loop.create_task(_startup())
    except Exception:
        pass
