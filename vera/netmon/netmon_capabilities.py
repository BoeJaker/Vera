"""
netmon_capabilities.py — Vera network presence / uptime monitor + alerts
========================================================================

Turns the Network Map from a point-in-time picture into a live monitor:

  • Device / target registry  — name devices by MAC, mark them "watch", choose
    which changes alert you and over which channels. External hosts/URLs too
    ("is my website down").
  • Presence engine           — one global on/off switch. When on, a scheduled
    tick lightly pings known LAN hosts (green/red on the graph), periodically
    sweeps for NEW devices, and checks external targets (icmp/tcp/http).
  • Alerts + notifications     — fully-configurable triggers (down, up, new
    device, attribute change, wifi AP appear/leave) dispatched to Telegram /
    email / browser.
  • WiFi ingestion             — netscan.wifi.ingest writes :WifiAP nodes from an
    ESP32 `mesh.wifi.scan` (browsers can't scan WiFi) or any client POST.

Design notes
────────────
This module is self-contained: it owns a lazy `_aux_run/_aux_read` (Fabric Neo4j)
and its own SQLite connection, and calls sibling capabilities at RUNTIME through
CAPABILITY_REGISTRY (order-independent), exactly like vera/monitor. It never lets
one target break the tick, and degrades silently when a source/channel is absent.

Capabilities (group `netmon.*` + `netscan.wifi.ingest`)
───────────────────────────────────────────────────────
  netmon.config.get / .set            — global monitoring on/off, interval, cidrs
  netmon.target.list / .save / .delete — device + external-host registry
  netmon.target.watch                  — opt a target into alerts (triggers+channels)
  netmon.device.name                   — name a MAC (relabels its :NetHost)
  netmon.alerts.list / .clear          — recent-alert feed for the panel
  netmon.test                          — fire a test alert through chosen channels
  netmon.scan_now                      — run a presence tick immediately
  netscan.wifi.ingest                  — ingest a WiFi AP scan into the graph
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from Vera.vera.config import cfg
from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, capability, emit_event, now_iso, schedule,
)

log = logging.getLogger("vera.netmon")

_BASE_TICK = 15.0          # scheduler wakeup; real cadence honoured via config
_ALERTS_MAX = 500

# Runtime state (presence tick bookkeeping + per-observer wifi diff)
_RUNTIME: Dict[str, Any] = {"tick": 0, "last_tick": None, "running": False,
                            "_last_run": 0, "wifi_seen": {}}


# ─────────────────────────────────────────────────────────────────────────────
# Loose-coupled sibling-cap call (vera/monitor pattern) + lazy Fabric Neo4j
# ─────────────────────────────────────────────────────────────────────────────
async def _call(cap_name: str, **kw) -> Any:
    cap = CAPABILITY_REGISTRY.get(cap_name)
    if not cap:
        return {"error": f"{cap_name} not loaded"}
    kw.setdefault("trace_id", "")
    try:
        return await cap["func"](**kw)
    except Exception as e:
        log.debug("netmon._call %s failed: %s", cap_name, e)
        return {"error": f"{type(e).__name__}: {e}"}


def _is_err(d: Any) -> bool:
    return isinstance(d, dict) and bool(d.get("error"))


def _exec():
    return (sys.modules.get("exec_capabilities")
            or sys.modules.get("Vera.vera.exec_capabilities"))


def _fabric_neo():
    mod = sys.modules.get("data_fabric") or sys.modules.get("Vera.vera.data_fabric")
    return getattr(mod, "FABRIC_NEO", None) if mod else None


def _neo_ok() -> bool:
    fn = _fabric_neo()
    return bool(fn and getattr(fn, "available", False))


async def _aux_run(cypher: str, **params) -> List[Dict]:
    fn = _fabric_neo()
    if not fn or not getattr(fn, "available", False):
        return []
    try:
        async with fn._driver.session() as s:
            res = await s.run(cypher, **params)
            return await res.data()
    except Exception as e:
        log.debug("netmon aux_run failed: %s", e)
        return []


async def _aux_read(cypher: str, **params) -> List[Dict]:
    return await _aux_run(cypher, **params)


# ── liveness helpers (reuse exec's; fall back to a bare TCP connect) ──────────
async def _icmp(ip: str, timeout: float = 1.0) -> bool:
    ex = _exec()
    fn = getattr(ex, "_icmp_ping", None) if ex else None
    if fn:
        try:
            return bool(await fn(ip, timeout))
        except Exception:
            return False
    return False


async def _tcp(host: str, port: int, timeout: float = 0.8) -> bool:
    ex = _exec()
    fn = getattr(ex, "_tcp_ping", None) if ex else None
    if fn:
        try:
            return bool(await fn(host, port, timeout))
        except Exception:
            return False
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        return True
    except Exception:
        return False


_LIVENESS_PORTS = (80, 443, 22, 445, 3389, 8080)


async def _is_alive(ip: str) -> bool:
    if await _icmp(ip, 1.0):
        return True
    for p in _LIVENESS_PORTS:
        if await _tcp(ip, p, 0.6):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# SQLite — own connection to the shared vera.db (open/close freely)
# ─────────────────────────────────────────────────────────────────────────────
def _db_path() -> Path:
    p = Path(cfg.get("VERA_DATA_DIR", "/tmp/vera")) / "vera.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _db() -> sqlite3.Connection:
    return sqlite3.connect(str(_db_path()), check_same_thread=False, timeout=10)


def _ensure_tables() -> None:
    conn = _db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS net_targets (
                id TEXT PRIMARY KEY, kind TEXT, mac TEXT, ip TEXT, host TEXT,
                url TEXT, name TEXT, vendor TEXT, notes TEXT,
                watch INTEGER DEFAULT 0, triggers TEXT, channels TEXT,
                email_to TEXT, check_type TEXT, port INTEGER, expect TEXT,
                status TEXT, last_seen TEXT, last_change TEXT, last_ip TEXT,
                last_ports TEXT, fail_count INTEGER DEFAULT 0,
                created_at TEXT, updated_at TEXT
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS net_seen (
                mac TEXT PRIMARY KEY, ip TEXT, hostname TEXT, vendor TEXT,
                first_seen TEXT, last_seen TEXT
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS netmon_config (
                k TEXT PRIMARY KEY, v TEXT
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS netmon_alerts (
                id TEXT PRIMARY KEY, ts TEXT, kind TEXT, target_id TEXT,
                title TEXT, detail TEXT, channels TEXT
            )""")
        conn.commit()
    finally:
        conn.close()


try:
    _ensure_tables()
except Exception as _e:
    log.debug("netmon table init: %s", _e)


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled":             False,
    "interval_sec":        60,
    "lan_cidrs":           [],        # [] = auto-derive from discovered :Subnet
    "discovery_every_n":   5,         # full sweep every Nth tick
    "new_device_alert":    False,
    "new_device_channels": ["browser"],
    "default_channels":    ["browser"],
    "default_email":       "",
}


def _load_config() -> Dict[str, Any]:
    pol = dict(_DEFAULT_CONFIG)
    try:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT v FROM netmon_config WHERE k='global'").fetchone()
        finally:
            conn.close()
        if row and row[0]:
            saved = json.loads(row[0])
            if isinstance(saved, dict):
                pol.update({k: v for k, v in saved.items() if k in _DEFAULT_CONFIG})
    except Exception as e:
        log.debug("netmon config load: %s", e)
    return pol


def _save_config(pol: Dict[str, Any]) -> None:
    conn = _db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO netmon_config (k, v) VALUES ('global', ?)",
            (json.dumps(pol),))
        conn.commit()
    finally:
        conn.close()


def _as_list(v) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    import re as _re
    return [s.strip() for s in _re.split(r"[\n,]", str(v)) if s.strip()]


@capability(
    "netmon.config.get",
    http_method="GET", http_path="/netmon/config", http_tags=["netmon"],
    memory="off", silent=True,
    description="Get the global presence-monitor config (on/off, interval, "
                "lan_cidrs, new-device alerting, default channels). "
                "Output: {config:{...}, running}.",
)
async def cap_netmon_config_get(trace_id=None) -> Dict:
    return {"config": _load_config(), "running": _RUNTIME.get("running", False),
            "last_tick": _RUNTIME.get("last_tick")}


@capability(
    "netmon.config.set",
    http_method="POST", http_path="/netmon/config/set", http_tags=["netmon"],
    description="Update the global presence-monitor config. Omitted fields are "
                "left unchanged. Input: enabled (bool — master on/off), "
                "interval_sec (int), lan_cidrs (list[str] — [] = auto), "
                "discovery_every_n (int), new_device_alert (bool), "
                "new_device_channels (list), default_channels (list), "
                "default_email (str). Output: {ok, config}.",
)
async def cap_netmon_config_set(
    enabled: Optional[bool] = None, interval_sec: Optional[int] = None,
    lan_cidrs=None, discovery_every_n: Optional[int] = None,
    new_device_alert: Optional[bool] = None, new_device_channels=None,
    default_channels=None, default_email: Optional[str] = None, trace_id=None,
) -> Dict:
    pol = _load_config()
    if enabled is not None:            pol["enabled"] = bool(enabled)
    if interval_sec is not None:       pol["interval_sec"] = max(15, int(interval_sec))
    if discovery_every_n is not None:  pol["discovery_every_n"] = max(1, int(discovery_every_n))
    if new_device_alert is not None:   pol["new_device_alert"] = bool(new_device_alert)
    if default_email is not None:      pol["default_email"] = str(default_email).strip()
    if lan_cidrs is not None:          pol["lan_cidrs"] = _as_list(lan_cidrs)
    if new_device_channels is not None: pol["new_device_channels"] = _as_list(new_device_channels)
    if default_channels is not None:   pol["default_channels"] = _as_list(default_channels)
    _save_config(pol)
    await emit_event({"type": "netmon.config", "enabled": pol["enabled"]})
    return {"ok": True, "config": pol}


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────
_TARGET_COLS = ("id", "kind", "mac", "ip", "host", "url", "name", "vendor",
                "notes", "watch", "triggers", "channels", "email_to",
                "check_type", "port", "expect", "status", "last_seen",
                "last_change", "last_ip", "last_ports", "fail_count",
                "created_at", "updated_at")


def _row_to_target(row) -> Dict:
    d = dict(zip(_TARGET_COLS, row))
    d["watch"] = bool(d.get("watch"))
    d["triggers"] = _as_list(d.get("triggers"))
    d["channels"] = _as_list(d.get("channels"))
    try: d["last_ports"] = json.loads(d.get("last_ports") or "[]")
    except Exception: d["last_ports"] = []
    return d


def _targets_all() -> List[Dict]:
    conn = _db()
    try:
        rows = conn.execute(
            f"SELECT {','.join(_TARGET_COLS)} FROM net_targets "
            "ORDER BY kind, name, host, ip").fetchall()
        return [_row_to_target(r) for r in rows]
    finally:
        conn.close()


def _target_get(tid: str) -> Optional[Dict]:
    conn = _db()
    try:
        row = conn.execute(
            f"SELECT {','.join(_TARGET_COLS)} FROM net_targets WHERE id=?",
            (tid,)).fetchone()
        return _row_to_target(row) if row else None
    finally:
        conn.close()


def _target_by_mac(mac: str) -> Optional[Dict]:
    if not mac:
        return None
    conn = _db()
    try:
        row = conn.execute(
            f"SELECT {','.join(_TARGET_COLS)} FROM net_targets "
            "WHERE lower(mac)=lower(?)", (mac,)).fetchone()
        return _row_to_target(row) if row else None
    finally:
        conn.close()


def _target_by_ip(ip: str) -> Optional[Dict]:
    if not ip:
        return None
    conn = _db()
    try:
        row = conn.execute(
            f"SELECT {','.join(_TARGET_COLS)} FROM net_targets WHERE ip=?",
            (ip,)).fetchone()
        return _row_to_target(row) if row else None
    finally:
        conn.close()


def _target_upsert(rec: Dict) -> None:
    conn = _db()
    try:
        existing = conn.execute(
            "SELECT created_at FROM net_targets WHERE id=?",
            (rec["id"],)).fetchone()
        rec.setdefault("created_at", (existing[0] if existing else now_iso()))
        rec["updated_at"] = now_iso()
        vals = [rec.get(c) for c in _TARGET_COLS]
        placeholders = ",".join("?" for _ in _TARGET_COLS)
        conn.execute(
            f"INSERT OR REPLACE INTO net_targets ({','.join(_TARGET_COLS)}) "
            f"VALUES ({placeholders})",
            [json.dumps(v) if c == "last_ports" and isinstance(v, list)
             else (1 if c == "watch" and v else (0 if c == "watch" else
                   (",".join(v) if c in ("triggers", "channels") and isinstance(v, list) else v)))
             for c, v in zip(_TARGET_COLS, vals)])
        conn.commit()
    finally:
        conn.close()


@capability(
    "netmon.target.list",
    http_method="GET", http_path="/netmon/target/list", http_tags=["netmon"],
    memory="off", silent=True,
    description="List monitored devices + external hosts (the registry). "
                "Output: {targets:[{id,kind,mac,ip,host,url,name,watch,triggers,"
                "channels,status,last_seen,...}]}.",
)
async def cap_netmon_target_list(trace_id=None) -> Dict:
    return {"targets": _targets_all()}


@capability(
    "netmon.target.save",
    http_method="POST", http_path="/netmon/target/save", http_tags=["netmon"],
    description="Add/update a monitored target. For a LAN device pass kind='lan' "
                "+ mac (and/or ip). For an external host/URL pass kind='external' "
                "+ host OR url, check_type (icmp|tcp|http), port (tcp), expect "
                "(http status or substring). Input: id (str — update), kind, mac, "
                "ip, host, url, name, notes, check_type, port, expect, watch (bool), "
                "triggers (list: down,up,new,attr,wifi), channels (list: telegram,"
                "email,browser), email_to. Output: {ok, target}.",
)
async def cap_netmon_target_save(
    id: str = "", kind: str = "", mac: str = "", ip: str = "", host: str = "",
    url: str = "", name: str = "", notes: str = "", check_type: str = "",
    port: int = 0, expect: str = "", watch: Optional[bool] = None,
    triggers=None, channels=None, email_to: str = "", trace_id=None,
) -> Dict:
    tid = id or uuid.uuid4().hex[:12]
    rec = _target_get(tid) or {"id": tid, "fail_count": 0, "status": "unknown"}
    k = (kind or rec.get("kind") or ("external" if (host or url) and not mac else "lan")).lower()
    rec.update({
        "id": tid, "kind": k,
        "mac": (mac or rec.get("mac") or "").lower(),
        "ip": ip or rec.get("ip") or "",
        "host": host or rec.get("host") or "",
        "url": url or rec.get("url") or "",
        "name": name or rec.get("name") or "",
        "notes": notes or rec.get("notes") or "",
        "check_type": (check_type or rec.get("check_type")
                       or ("http" if (url or "").startswith("http") else "icmp")),
        "port": int(port or rec.get("port") or 0),
        "expect": expect or rec.get("expect") or "",
        "email_to": email_to or rec.get("email_to") or "",
    })
    if watch is not None:
        rec["watch"] = bool(watch)
    if triggers is not None:
        rec["triggers"] = _as_list(triggers)
    elif "triggers" not in rec:
        rec["triggers"] = ["down", "up"]
    if channels is not None:
        rec["channels"] = _as_list(channels)
    elif "channels" not in rec:
        rec["channels"] = list(_load_config().get("default_channels") or ["browser"])
    _target_upsert(rec)
    await emit_event({"type": "netmon.target.saved", "id": tid, "name": rec["name"]})
    return {"ok": True, "target": _target_get(tid)}


@capability(
    "netmon.target.delete",
    http_method="POST", http_path="/netmon/target/delete", http_tags=["netmon"],
    description="Delete a monitored target. Input: id (str!). Output: {ok}.",
)
async def cap_netmon_target_delete(id: str = "", trace_id=None) -> Dict:
    conn = _db()
    try:
        conn.execute("DELETE FROM net_targets WHERE id=?", (id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "id": id}


@capability(
    "netmon.target.watch",
    http_method="POST", http_path="/netmon/target/watch", http_tags=["netmon"],
    description="Toggle alerting for a target and set which changes notify you and "
                "over which channels. Input: id (str!), watch (bool), triggers "
                "(list: down,up,new,attr,wifi), channels (list: telegram,email,"
                "browser), email_to (str). Output: {ok, target}.",
)
async def cap_netmon_target_watch(
    id: str = "", watch: Optional[bool] = None, triggers=None, channels=None,
    email_to: Optional[str] = None, trace_id=None,
) -> Dict:
    rec = _target_get(id)
    if not rec:
        return {"error": f"target not found: {id}"}
    if watch is not None:    rec["watch"] = bool(watch)
    if triggers is not None: rec["triggers"] = _as_list(triggers)
    if channels is not None: rec["channels"] = _as_list(channels)
    if email_to is not None: rec["email_to"] = str(email_to).strip()
    _target_upsert(rec)
    return {"ok": True, "target": _target_get(id)}


@capability(
    "netmon.device.name",
    http_method="POST", http_path="/netmon/device/name", http_tags=["netmon"],
    description="Give a MAC (or IP) a friendly device name — creates/updates its "
                "registry entry and relabels its :NetHost on the graph. Input: "
                "mac (str) and/or ip (str), name (str!). Output: {ok, id}.",
)
async def cap_netmon_device_name(mac: str = "", ip: str = "", name: str = "",
                                 trace_id=None) -> Dict:
    if not name.strip():
        return {"error": "name required"}
    if not mac and not ip:
        return {"error": "mac or ip required"}
    rec = (_target_by_mac(mac) if mac else None) or (_target_by_ip(ip) if ip else None)
    if not rec:
        rec = {"id": uuid.uuid4().hex[:12], "kind": "lan", "status": "unknown",
               "fail_count": 0, "triggers": ["down", "up"],
               "channels": list(_load_config().get("default_channels") or ["browser"])}
    rec["mac"] = (mac or rec.get("mac") or "").lower()
    rec["ip"] = ip or rec.get("ip") or ""
    rec["name"] = name.strip()
    _target_upsert(rec)
    # Relabel the matching :NetHost so the graph shows the friendly name
    if rec.get("ip"):
        await _aux_run(
            "MERGE (h:NetHost {id:$id}) SET h.name=$name, h.label=$name, "
            "h.device_name=$name, h.updated_at=$ts",
            id=f"net:{rec['ip']}", name=name.strip(), ts=now_iso())
    await emit_event({"type": "netmon.device.named", "name": name.strip(),
                      "mac": rec.get("mac"), "ip": rec.get("ip")})
    return {"ok": True, "id": rec["id"]}


# ─────────────────────────────────────────────────────────────────────────────
# Alerts + notification dispatch
# ─────────────────────────────────────────────────────────────────────────────
def _record_alert(kind: str, target_id: str, title: str, detail: str,
                  channels: List[str]) -> Dict:
    aid = uuid.uuid4().hex[:12]
    ts = now_iso()
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO netmon_alerts (id,ts,kind,target_id,title,detail,channels)"
            " VALUES (?,?,?,?,?,?,?)",
            (aid, ts, kind, target_id, title, detail, ",".join(channels)))
        # trim to the most recent _ALERTS_MAX
        conn.execute(
            "DELETE FROM netmon_alerts WHERE id NOT IN "
            "(SELECT id FROM netmon_alerts ORDER BY ts DESC LIMIT ?)",
            (_ALERTS_MAX,))
        conn.commit()
    finally:
        conn.close()
    return {"id": aid, "ts": ts, "kind": kind, "target_id": target_id,
            "title": title, "detail": detail, "channels": channels}


async def _dispatch(channels: List[str], title: str, body: str,
                    email_to: str = "") -> List[str]:
    sent: List[str] = []
    cfg_ = _load_config()
    for ch in channels:
        try:
            if ch == "telegram":
                r = await _call("tg.notify", text=f"{title}\n{body}")
                if not _is_err(r):
                    sent.append("telegram")
            elif ch == "email":
                to = email_to or cfg_.get("default_email", "")
                if to:
                    r = await _call("mail.send", to=to, subject=title, body=body)
                    if not _is_err(r):
                        sent.append("email")
            elif ch == "browser":
                sent.append("browser")    # delivered via the emit_event below
        except Exception as e:
            log.debug("netmon dispatch %s: %s", ch, e)
    return sent


async def _emit_alert(kind: str, *, title: str, body: str,
                      channels: List[str], email_to: str = "",
                      target_id: str = "") -> None:
    channels = channels or ["browser"]
    sent = await _dispatch(channels, title, body, email_to)
    rec = _record_alert(kind, target_id, title, body, channels)
    # Always emit a live event so the panel feed + (browser) toast/Notification fire
    await emit_event({
        "type": "netmon.alert", "kind": kind, "title": title, "body": body,
        "ts": rec["ts"], "id": rec["id"], "target_id": target_id,
        "channels": channels, "browser": ("browser" in channels),
    })
    log.info("netmon alert [%s] %s — channels=%s", kind, title, sent)


@capability(
    "netmon.alerts.list",
    http_method="GET", http_path="/netmon/alerts", http_tags=["netmon"],
    memory="off", silent=True,
    description="Recent presence/uptime alerts (newest first) for the panel feed. "
                "Input: limit (int=100). Output: {alerts:[{id,ts,kind,title,detail,"
                "channels}]}.",
)
async def cap_netmon_alerts_list(limit: int = 100, trace_id=None) -> Dict:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id,ts,kind,target_id,title,detail,channels FROM netmon_alerts "
            "ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
    finally:
        conn.close()
    return {"alerts": [
        {"id": r[0], "ts": r[1], "kind": r[2], "target_id": r[3],
         "title": r[4], "detail": r[5], "channels": _as_list(r[6])}
        for r in rows]}


@capability(
    "netmon.alerts.clear",
    http_method="POST", http_path="/netmon/alerts/clear", http_tags=["netmon"],
    description="Clear the alert history. Output: {ok}.",
)
async def cap_netmon_alerts_clear(trace_id=None) -> Dict:
    conn = _db()
    try:
        conn.execute("DELETE FROM netmon_alerts")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@capability(
    "netmon.test",
    http_method="POST", http_path="/netmon/test", http_tags=["netmon"],
    description="Send a test alert through the chosen channels to confirm "
                "notifications work. Input: channels (list: telegram,email,browser), "
                "email_to (str). Output: {ok, sent}.",
)
async def cap_netmon_test(channels=None, email_to: str = "", trace_id=None) -> Dict:
    chans = _as_list(channels) or list(_load_config().get("default_channels") or ["browser"])
    await _emit_alert("test", title="Vera netmon test alert",
                      body="If you can read this, this channel works.",
                      channels=chans, email_to=email_to)
    return {"ok": True, "sent": chans}


# ─────────────────────────────────────────────────────────────────────────────
# Presence engine
# ─────────────────────────────────────────────────────────────────────────────
_OUI = {
    "b8:27:eb": "Raspberry Pi", "dc:a6:32": "Raspberry Pi", "e4:5f:01": "Raspberry Pi",
    "00:1a:11": "Google", "f4:f5:d8": "Google", "fc:fc:48": "Apple",
    "a4:83:e7": "Apple", "ac:bc:32": "Apple", "00:0c:29": "VMware",
    "52:54:00": "QEMU/KVM", "00:15:5d": "Microsoft Hyper-V", "dc:a6:32": "Raspberry Pi",
    "b0:be:76": "TP-Link", "50:c7:bf": "TP-Link", "00:11:32": "Synology",
    "00:e0:4c": "Realtek", "ec:fa:bc": "Espressif", "24:0a:c4": "Espressif",
    "a0:20:a6": "Espressif", "30:ae:a4": "Espressif",
}


def _oui_vendor(mac: str) -> str:
    if not mac or len(mac) < 8:
        return ""
    return _OUI.get(mac.lower()[:8], "")


async def _set_host_status(ip: str, status: str, **extra) -> None:
    props = {"status": status, "presence": status, "last_seen": now_iso()}
    props.update({k: v for k, v in extra.items() if v not in (None, "")})
    await _aux_run(
        "MERGE (h:NetHost {id:$id}) SET h += $props, h.ip=coalesce(h.ip,$ip)",
        id=f"net:{ip}", props=props, ip=ip)


def _seen_get(mac: str) -> Optional[Dict]:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT mac,ip,hostname,vendor,first_seen,last_seen FROM net_seen "
            "WHERE lower(mac)=lower(?)", (mac,)).fetchone()
        if not row:
            return None
        return dict(zip(("mac", "ip", "hostname", "vendor", "first_seen",
                         "last_seen"), row))
    finally:
        conn.close()


def _seen_put(mac: str, ip: str, hostname: str, vendor: str) -> bool:
    """Returns True if this MAC was NOT seen before (i.e. a new device)."""
    existing = _seen_get(mac)
    conn = _db()
    try:
        if existing:
            conn.execute("UPDATE net_seen SET ip=?, hostname=?, last_seen=? "
                         "WHERE lower(mac)=lower(?)",
                         (ip, hostname, now_iso(), mac))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO net_seen "
                "(mac,ip,hostname,vendor,first_seen,last_seen) VALUES (?,?,?,?,?,?)",
                (mac.lower(), ip, hostname, vendor, now_iso(), now_iso()))
        conn.commit()
    finally:
        conn.close()
    return existing is None


async def _derive_cidrs(cfg_: Dict) -> List[str]:
    cidrs = list(cfg_.get("lan_cidrs") or [])
    if cidrs:
        return cidrs
    rows = await _aux_read("MATCH (s:Subnet) RETURN s.cidr AS cidr LIMIT 16")
    out = []
    for r in rows:
        c = r.get("cidr")
        if c:
            out.append(c)
    return out


def _channels_for(t: Dict) -> List[str]:
    chans = t.get("channels") or []
    return chans or list(_load_config().get("default_channels") or ["browser"])


async def _alert_status_change(t: Dict, new_status: str, reason: str) -> None:
    """Fire down/up alerts for a watched target whose status flipped."""
    if not t.get("watch"):
        return
    trig = t.get("triggers") or []
    kind = "down" if new_status == "down" else "up"
    if kind not in trig:
        return
    label = t.get("name") or t.get("host") or t.get("url") or t.get("ip") or t.get("mac") or t.get("id")
    where = t.get("url") or t.get("host") or t.get("ip") or ""
    if kind == "down":
        title = f"🔴 DOWN: {label}"
        body = f"{label} ({where}) is unreachable.\n{reason}"
    else:
        title = f"🟢 UP: {label}"
        body = f"{label} ({where}) is back online."
    await _emit_alert(kind, title=title, body=body, channels=_channels_for(t),
                      email_to=t.get("email_to", ""), target_id=t.get("id", ""))


async def _check_external(t: Dict) -> bool:
    ct = (t.get("check_type") or "icmp").lower()
    url = t.get("url") or ""
    host = t.get("host") or ""
    if ct == "http" or url.startswith("http"):
        target = url or (f"http://{host}" if host else "")
        if not target:
            return False
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                                         verify=False) as c:
                r = await c.get(target)
            exp = (t.get("expect") or "").strip()
            if exp:
                if exp.isdigit():
                    return r.status_code == int(exp)
                return exp.lower() in (r.text or "").lower()
            return 200 <= r.status_code < 400
        except Exception:
            return False
    if ct == "tcp" and host and t.get("port"):
        return await _tcp(host, int(t["port"]), 3.0)
    # icmp / default
    return await _is_alive(host or t.get("ip") or "")


async def _apply_external(t: Dict) -> None:
    up = await _check_external(t)
    new = "up" if up else "down"
    old = t.get("status") or "unknown"
    t["last_seen"] = now_iso() if up else t.get("last_seen")
    if new != old:
        t["status"] = new
        t["last_change"] = now_iso()
        await _alert_status_change(t, new, reason="check failed" if new == "down" else "")
    else:
        t["status"] = new
    t["fail_count"] = (t.get("fail_count", 0) + 1) if not up else 0
    _target_upsert(t)
    # Surface external targets on the graph too (red/green)
    ipkey = t.get("ip") or t.get("host") or t.get("url") or t.get("id")
    label = t.get("name") or t.get("host") or t.get("url") or ipkey
    await _aux_run(
        "MERGE (h:NetHost {id:$id}) SET h.status=$st, h.presence=$st, "
        "h.hostname=$hn, h.name=coalesce(h.name,$nm), h.label=$nm, "
        "h.kind='external', h.source=coalesce(h.source,'monitor'), "
        "h.last_seen=$ts, h.monitored=true",
        id=f"mon:{ipkey}", st=new, hn=(t.get("host") or t.get("url") or ""),
        nm=label, ts=now_iso())


async def _handle_lan_host(ip: str, mac: str, hostname: str, alive: bool,
                           open_ports: Optional[List[int]] = None) -> None:
    cfg_ = _load_config()
    new = "up" if alive else "down"
    await _set_host_status(ip, new, hostname=hostname, mac=mac)
    # New-device detection (global)
    if alive and mac:
        is_new = _seen_put(mac, ip, hostname, _oui_vendor(mac))
        if is_new and cfg_.get("new_device_alert"):
            vendor = _oui_vendor(mac)
            await _emit_alert(
                "new", title=f"🆕 New device: {hostname or ip}",
                body=f"New device on the LAN\nIP {ip}  MAC {mac}"
                     + (f"  ({vendor})" if vendor else "")
                     + (f"\nhostname: {hostname}" if hostname else ""),
                channels=_as_list(cfg_.get("new_device_channels")) or ["browser"])
    # Registry target tracking (status + attribute diffs)
    t = (_target_by_mac(mac) if mac else None) or _target_by_ip(ip)
    if not t:
        return
    old = t.get("status") or "unknown"
    trig = t.get("triggers") or []
    # attribute change: IP moved or new open ports
    if t.get("watch") and "attr" in trig and alive:
        msgs = []
        if t.get("last_ip") and ip and t["last_ip"] != ip:
            msgs.append(f"IP changed {t['last_ip']} → {ip}")
        if open_ports:
            prev = set(t.get("last_ports") or [])
            newp = sorted(set(open_ports) - prev)
            if prev and newp:
                msgs.append(f"new open port(s): {', '.join(map(str, newp))}")
        if msgs:
            label = t.get("name") or t.get("host") or ip
            await _emit_alert(
                "attr", title=f"⚠ Change: {label}",
                body=f"{label} ({ip})\n" + "\n".join(msgs),
                channels=_channels_for(t), email_to=t.get("email_to", ""),
                target_id=t.get("id", ""))
    if alive:
        t["last_seen"] = now_iso()
        if ip:
            t["last_ip"] = ip
        if open_ports:
            t["last_ports"] = sorted(set(open_ports))
    if new != old:
        t["status"] = new
        t["last_change"] = now_iso()
        await _alert_status_change(t, new, reason="no reply to ping/tcp")
    else:
        t["status"] = new
    _target_upsert(t)


async def _presence_tick() -> None:
    cfg_ = _load_config()
    _RUNTIME["running"] = bool(cfg_.get("enabled"))
    if not cfg_.get("enabled"):
        return
    now = time.time()
    interval = max(15, int(cfg_.get("interval_sec", 60)))
    if (now - (_RUNTIME.get("_last_run") or 0)) < interval - 1:
        return
    _RUNTIME["_last_run"] = now
    _RUNTIME["tick"] += 1
    _RUNTIME["last_tick"] = now_iso()
    tick = _RUNTIME["tick"]
    try:
        # 1. external targets
        externals = [t for t in _targets_all() if t.get("kind") == "external"]
        for t in externals:
            try: await _apply_external(t)
            except Exception as e: log.debug("external %s: %s", t.get("id"), e)

        # 2. discovery sweep every Nth tick → MACs + new devices
        do_discovery = (tick == 1) or (tick % max(1, int(cfg_.get("discovery_every_n", 5))) == 0)
        swept: set = set()
        if do_discovery:
            for cidr in await _derive_cidrs(cfg_):
                r = await _call("netscan.lan.scan", cidr=cidr, ports="22,80,443,445",
                                ping=True, port_nodes=True, save_to_fabric=False)
                if _is_err(r):
                    continue
                for h in (r.get("alive") or []):
                    ip = h.get("ip")
                    if not ip:
                        continue
                    swept.add(ip)
                    await _handle_lan_host(ip, (h.get("mac") or "").lower(),
                                           h.get("hostname", ""), True,
                                           h.get("open_ports") or [])

        # 3. light liveness for known LAN hosts not just swept
        rows = await _aux_read(
            "MATCH (h:NetHost) WHERE coalesce(h.kind,'') <> 'external' "
            "RETURN h.ip AS ip, h.mac AS mac, h.hostname AS hn LIMIT 1024")
        targets = [(r.get("ip"), (r.get("mac") or "").lower(), r.get("hn") or "")
                   for r in rows if r.get("ip") and r.get("ip") not in swept]
        sem = asyncio.Semaphore(48)
        async def _probe(ip, mac, hn):
            async with sem:
                alive = await _is_alive(ip)
                await _handle_lan_host(ip, mac, hn, alive, None)
        await asyncio.gather(*[_probe(ip, mac, hn) for ip, mac, hn in targets],
                             return_exceptions=True)
        await emit_event({"type": "netmon.tick", "tick": tick,
                          "swept": len(swept), "checked": len(targets),
                          "externals": len(externals)})
    except Exception as e:
        log.debug("netmon presence tick: %s", e)


@capability(
    "netmon.scan_now",
    http_method="POST", http_path="/netmon/scan_now", http_tags=["netmon"],
    description="Run a presence tick immediately (forces a discovery sweep). "
                "Honours the global enabled flag. Output: {ok, ran}.",
)
async def cap_netmon_scan_now(trace_id=None) -> Dict:
    if not _load_config().get("enabled"):
        return {"ok": False, "ran": False, "note": "global monitoring is off"}
    _RUNTIME["_last_run"] = 0
    _RUNTIME["tick"] = max(0, _RUNTIME.get("tick", 0))
    await _presence_tick()
    return {"ok": True, "ran": True}


schedule(_presence_tick, _BASE_TICK, "netmon_presence")


# ─────────────────────────────────────────────────────────────────────────────
# WiFi ingestion (ESP32 mesh.wifi.scan results, serial workers, or direct POST)
# ─────────────────────────────────────────────────────────────────────────────
def _ap_label(ap: Dict) -> str:
    ssid = (ap.get("ssid") or "").strip()
    bssid = (ap.get("bssid") or "").strip()
    return (ssid or "(hidden)") + (f"  {bssid[-8:]}" if bssid else "")


@capability(
    "netscan.wifi.ingest",
    http_method="POST", http_path="/netscan/wifi/ingest",
    http_tags=["netscan", "netmon"],
    description="Ingest a WiFi scan into the network graph. Browsers can't scan "
                "WiFi — feed this from an ESP32 mesh.wifi.scan, a serial/wifi "
                "worker, or any client. Creates :WifiAP {bssid,ssid,channel,rssi,"
                "auth} nodes, links the observing node (:NetHost)-[:SEES_AP]->, and "
                "cross-links an AP to the :NetHost whose MAC == BSSID. "
                "Input: observer (str — node id/ip/mac of the scanner), aps "
                "(list[{ssid,bssid,channel,rssi,auth}]). Output: {ok, aps, new}.",
)
async def cap_netscan_wifi_ingest(observer: str = "", aps=None,
                                  trace_id=None) -> Dict:
    aps = aps or []
    if not isinstance(aps, list):
        return {"error": "aps must be a list"}
    obs = (observer or "").strip()
    obs_id = ""
    if obs:
        # observer may be an IP, a MAC, or a mesh node id
        is_ip = ("." in obs and obs.replace(".", "").isdigit())
        obs_id = f"net:{obs}" if is_ip else f"sensor:{obs}"
        await _aux_run(
            "MERGE (h:NetHost {id:$id}) SET h.ip=coalesce(h.ip,$ip), "
            "h.label=coalesce(h.label,$lbl), h.role=coalesce(h.role,'wifi-sensor')",
            id=obs_id, ip=(obs if is_ip else None), lbl=obs)
    cfg_ = _load_config()
    cur_bssids = set()
    new_count = 0
    for ap in aps:
        if not isinstance(ap, dict):
            continue
        bssid = (ap.get("bssid") or ap.get("BSSID") or "").strip().lower()
        ssid = (ap.get("ssid") or ap.get("SSID") or "").strip()
        if not bssid and not ssid:
            continue
        key = bssid or f"ssid:{ssid}"
        cur_bssids.add(key)
        wid = f"wifi:{key}"
        existed = await _aux_read("MATCH (a:WifiAP {id:$id}) RETURN a.id AS id", id=wid)
        if not existed:
            new_count += 1
        await _aux_run(
            """
            MERGE (a:WifiAP {id:$id})
            SET a.bssid=$bssid, a.ssid=$ssid, a.label=$label, a.channel=$ch,
                a.rssi=$rssi, a.auth=$auth, a.source='wifi', a.updated_at=$ts,
                a.last_seen=$ts
            """,
            id=wid, bssid=bssid, ssid=ssid, label=_ap_label(ap),
            ch=ap.get("channel") or ap.get("ch"), rssi=ap.get("rssi"),
            auth=ap.get("auth") or ap.get("enc") or "", ts=now_iso())
        if obs_id:
            await _aux_run(
                "MATCH (h:NetHost {id:$oid}),(a:WifiAP {id:$wid}) "
                "MERGE (h)-[:SEES_AP]->(a)", oid=obs_id, wid=wid)
        if bssid:
            # cross-link to a host whose MAC is this AP's BSSID
            await _aux_run(
                "MATCH (a:WifiAP {id:$wid}),(h:NetHost) WHERE lower(h.mac)=$bssid "
                "MERGE (a)-[:IS_AP_OF]->(h)", wid=wid, bssid=bssid)
    # appear/leave alerts vs the last scan from this observer
    prev = set(_RUNTIME["wifi_seen"].get(obs, []))
    appeared = cur_bssids - prev
    left = prev - cur_bssids
    _RUNTIME["wifi_seen"][obs] = list(cur_bssids)
    if cfg_.get("enabled") and (appeared or left) and prev:
        # only alert when a watched 'wifi' trigger exists somewhere, or new-device global
        names = {a.get("bssid", "").lower(): (a.get("ssid") or "(hidden)") for a in aps if isinstance(a, dict)}
        if appeared:
            await _emit_alert(
                "wifi", title=f"📶 New WiFi AP near {obs or 'sensor'}",
                body="Appeared:\n" + "\n".join(
                    f"  {names.get(b,'') or b}  ({b})" for b in list(appeared)[:10]),
                channels=_as_list(cfg_.get("new_device_channels")) or ["browser"])
    await emit_event({"type": "netscan.wifi.ingested", "observer": obs,
                      "aps": len(cur_bssids), "new": new_count})
    return {"ok": True, "aps": len(cur_bssids), "new": new_count,
            "appeared": len(appeared), "left": len(left)}


log.info("netmon_capabilities ready — presence tick base=%ss", _BASE_TICK)
