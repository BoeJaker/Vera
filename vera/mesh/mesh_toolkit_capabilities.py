# ============================================================================
# mesh_toolkit_capabilities.py — ESP32(-S3) "swiss-army-knife" + RF positioning
# ============================================================================
#
# A mesh of headless ESP32-S3 nodes (one has a display, the rest don't) becomes
# a distributed RF sensor grid. This module adds:
#
#   • Typed shortcuts for the extended firmware toolkit — RGB/NeoPixel, BLE
#     scan, Wi-Fi CSI (Channel State Information), promiscuous sniffer, I2C bus
#     scan, capacitive touch, internal temperature, ESP-NOW ranging, deep-sleep,
#     channel survey, detailed sys-info. (Firmware side: vera/mesh/firmware/*.)
#
#   • Netmap bridge — BLE devices ingest into the same aux graph the Wi-Fi
#     scanner feeds (netscan.wifi.ingest), and every mesh node is projected as a
#     :NetHost so the fleet + what each node hears shows on the Network Map.
#
#   • RF positioning — nodes at known coordinates report RSSI to a target
#     (AP/BLE/ESP-NOW peer); the backend converts RSSI→distance (log-distance
#     path-loss) and multilaterates a position. CSI gives per-node motion /
#     presence (device-free human sensing) which overlays onto the same map.
#
# All heavy lifting is queued as ordinary mesh jobs (mesh.send), so it works
# over every transport (HTTP long-poll / WS / MQTT / serial) and is durable.
# ============================================================================

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from Vera.vera.capability_orchestration import (
    capability,
    emit_event,
    now_iso,
    schedule,
)

log = logging.getLogger("vera.mesh.toolkit")

_HERE = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = os.path.join(_HERE, "mesh_rf.db")

# Common on-board WS2812 data pins across ESP32-S3 dev boards, best-guess order.
# The firmware also accepts a pin override; mesh.rgb.probe walks this list.
S3_NEOPIXEL_CANDIDATES = [48, 38, 47, 21, 18, 8]

# Log-distance path-loss defaults (indoor). d = 10 ** ((A - rssi) / (10 * n)).
DEFAULT_RSSI_AT_1M = -45.0     # A — calibrate per environment via mesh.locate(cal=...)
DEFAULT_PATH_LOSS_N = 2.7      # n — 2.0 free space, 2.5-3.5 indoor with walls


# ─────────────────────────────────────────────────────────────────────────────
# Cross-module helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _call_cap(name: str, **kwargs) -> Any:
    from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap:
        return {"error": f"unknown_cap:{name}"}
    try:
        return await cap["func"](**kwargs)
    except TypeError:
        # tolerate signature drift — filter to declared props
        accepted = set(cap.get("schema", {}).get("properties", {}).keys())
        return await cap["func"](**{k: v for k, v in kwargs.items() if k in accepted})
    except Exception as e:
        log.warning("mesh toolkit _call_cap %s: %s", name, e)
        return {"error": f"{type(e).__name__}: {e}"}


async def _send(node_id: str, jtype: str, payload: dict) -> dict:
    """Queue a job on a node via the mesh's own delivery path."""
    if not node_id:
        return {"error": "node_id required"}
    return await _call_cap("mesh.send", node_id=node_id, type=jtype, payload=payload or {})


def _netmon():
    return sys.modules.get("netmon_capabilities")


async def _aux_run(cypher: str, **params) -> List[Dict]:
    """Reuse netmon's aux-graph (FABRIC_NEO) session so BLE/mesh nodes land in
    the same graph the Network Map renders."""
    nm = _netmon()
    fn = getattr(nm, "_aux_run", None) if nm else None
    if not fn:
        return []
    try:
        return await fn(cypher, **params)
    except Exception as e:
        log.debug("mesh toolkit aux_run: %s", e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# SQLite — node positions + RF observations (self-contained; no fabric dep)
# ─────────────────────────────────────────────────────────────────────────────

_DB: Optional[sqlite3.Connection] = None


def _db() -> sqlite3.Connection:
    global _DB
    if _DB is None:
        _DB = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _DB.row_factory = sqlite3.Row
        _DB.executescript(
            """
            CREATE TABLE IF NOT EXISTS node_pos (
                node_id TEXT PRIMARY KEY,
                x REAL, y REAL, z REAL,
                label TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS rf_obs (
                observer TEXT, target TEXT, kind TEXT,
                rssi REAL, ts REAL,
                PRIMARY KEY (observer, target, kind)
            );
            CREATE TABLE IF NOT EXISTS presence (
                node_id TEXT PRIMARY KEY,
                motion REAL, present INTEGER, metric REAL, ts REAL
            );
            """
        )
        _DB.commit()
    return _DB


def _pos_set_sync(node_id: str, x: float, y: float, z: float, label: str):
    c = _db()
    c.execute(
        "INSERT INTO node_pos(node_id,x,y,z,label,updated_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(node_id) DO UPDATE SET x=?,y=?,z=?,label=?,updated_at=?",
        (node_id, x, y, z, label, now_iso(), x, y, z, label, now_iso()))
    c.commit()


def _pos_all_sync() -> List[dict]:
    return [dict(r) for r in _db().execute("SELECT * FROM node_pos").fetchall()]


def _obs_put_sync(observer: str, target: str, kind: str, rssi: float):
    c = _db()
    c.execute(
        "INSERT INTO rf_obs(observer,target,kind,rssi,ts) VALUES(?,?,?,?,?) "
        "ON CONFLICT(observer,target,kind) DO UPDATE SET rssi=?,ts=?",
        (observer, target, kind, rssi, time.time(), rssi, time.time()))
    c.commit()


def _obs_for_target_sync(target: str, max_age_s: float) -> List[dict]:
    cutoff = time.time() - max_age_s
    return [dict(r) for r in _db().execute(
        "SELECT observer,rssi,kind,ts FROM rf_obs WHERE target=? AND ts>=?",
        (target.lower(), cutoff)).fetchall()]


def _obs_targets_sync(max_age_s: float, kind: str = "") -> List[dict]:
    cutoff = time.time() - max_age_s
    q = ("SELECT target, kind, COUNT(*) AS observers, MAX(ts) AS ts "
         "FROM rf_obs WHERE ts>=? ")
    args: list = [cutoff]
    if kind:
        q += "AND kind=? "
        args.append(kind)
    q += "GROUP BY target, kind ORDER BY observers DESC, ts DESC LIMIT 200"
    return [dict(r) for r in _db().execute(q, args).fetchall()]


def _presence_set_sync(node_id: str, motion: float, present: int, metric: float):
    c = _db()
    c.execute(
        "INSERT INTO presence(node_id,motion,present,metric,ts) VALUES(?,?,?,?,?) "
        "ON CONFLICT(node_id) DO UPDATE SET motion=?,present=?,metric=?,ts=?",
        (node_id, motion, present, metric, time.time(),
         motion, present, metric, time.time()))
    c.commit()


def _presence_all_sync() -> List[dict]:
    return [dict(r) for r in _db().execute(
        "SELECT * FROM presence ORDER BY ts DESC").fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
# Positioning math — RSSI → distance → multilateration (pure Python)
# ─────────────────────────────────────────────────────────────────────────────

def _rssi_to_distance(rssi: float, a: float, n: float) -> float:
    try:
        return float(10.0 ** ((a - rssi) / (10.0 * n)))
    except Exception:
        return 1e9


def _multilaterate(points: List[Tuple[float, float, float]]) -> Optional[Tuple[float, float, float]]:
    """Weighted linear least-squares trilateration in 2-D.
    points: [(x, y, distance)]. Weight ~ 1/d (near anchors dominate).
    Returns (x, y, residual_rms) or None if under-determined/degenerate."""
    pts = [(x, y, d) for (x, y, d) in points if d and d > 0]
    if len(pts) < 3:
        return None
    # Reference = the nearest anchor (smallest distance → most reliable).
    ref = min(pts, key=lambda p: p[2])
    xr, yr, dr = ref
    # Normal equations for A·[x,y] = b built from (pt - ref) row differences.
    saa = sab = sbb = sac = sbc = 0.0
    used = 0
    for (xi, yi, di) in pts:
        if (xi, yi, di) == ref:
            continue
        A = 2.0 * (xi - xr)
        B = 2.0 * (yi - yr)
        C = (di * di - dr * dr) - (xi * xi - xr * xr) - (yi * yi - yr * yr)
        C = -C  # move to RHS
        w = 1.0 / max(0.5, di)
        saa += w * A * A
        sab += w * A * B
        sbb += w * B * B
        sac += w * A * C
        sbc += w * B * C
        used += 1
    if used < 2:
        return None
    det = saa * sbb - sab * sab
    if abs(det) < 1e-9:
        return None
    x = (sac * sbb - sbc * sab) / det
    y = (saa * sbc - sab * sac) / det
    # residual RMS between measured and geometric distances
    err2 = 0.0
    for (xi, yi, di) in pts:
        gd = math.hypot(x - xi, y - yi)
        err2 += (gd - di) ** 2
    rms = math.sqrt(err2 / len(pts))
    return (x, y, rms)


# ─────────────────────────────────────────────────────────────────────────────
# Toolkit typed shortcuts (queue jobs the extended firmware understands)
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "mesh.rgb", http_method="POST", http_path="/mesh/rgb", http_tags=["mesh", "toolkit"],
    memory="on",
    description="Set a node's on-board RGB/NeoPixel LED (WS2812). "
                "Input: node_id (str!), r/g/b (int 0-255), pin (int — override the LED GPIO; "
                "S3 boards vary, commonly 48 or 38), n (int — LEDs in the strip, default 1), "
                "effect (str: solid|blink|breathe|rainbow|off), brightness (int 0-255). "
                "If you don't know the pin, run mesh.rgb.probe first. Output: {ok, job_id}.",
)
async def cap_mesh_rgb(node_id: str = "", r: int = 0, g: int = 0, b: int = 0,
                       pin: int = -1, n: int = 1, effect: str = "solid",
                       brightness: int = 255, trace_id=None) -> dict:
    p: Dict[str, Any] = {"r": int(r), "g": int(g), "b": int(b), "n": int(n),
                         "effect": effect, "brightness": int(brightness)}
    if pin is not None and pin >= 0:
        p["pin"] = int(pin)
    return await _send(node_id, "neopixel_set", p)


@capability(
    "mesh.rgb.probe", http_method="POST", http_path="/mesh/rgb/probe", http_tags=["mesh", "toolkit"],
    memory="on",
    description="Find an unknown on-board RGB/NeoPixel pin: the node lights each candidate GPIO "
                "in turn (default S3 set 48,38,47,21,18,8) with a colour and a 1s gap, announcing "
                "which pin it's driving — watch the board and note when the LED lights. "
                "Input: node_id (str!), pins (list[int] — override candidates), dwell_ms (int=1000). "
                "Output: {ok, job_id, pins}.",
)
async def cap_mesh_rgb_probe(node_id: str = "", pins=None, dwell_ms: int = 1000,
                             trace_id=None) -> dict:
    if isinstance(pins, str):
        try:
            pins = json.loads(pins)
        except Exception:
            pins = [int(x) for x in pins.replace(",", " ").split() if x.strip().lstrip("-").isdigit()]
    cand = [int(x) for x in (pins or S3_NEOPIXEL_CANDIDATES)]
    res = await _send(node_id, "neo_probe", {"pins": cand, "dwell_ms": int(dwell_ms)})
    res["pins"] = cand
    return res


@capability(
    "mesh.ble.scan", http_method="POST", http_path="/mesh/ble/scan", http_tags=["mesh", "toolkit"],
    memory="on",
    description="Ask a node to scan for BLE devices (name, MAC, RSSI, manufacturer). Results come "
                "back as the job result and are auto-ingested into the Network Map as :BleDevice "
                "observed by this node, and as RF observations for positioning. "
                "Input: node_id (str!), seconds (int=5), active (bool=False — active scan reads names). "
                "Output: {ok, job_id}.",
)
async def cap_mesh_ble_scan(node_id: str = "", seconds: int = 5, active: bool = False,
                            trace_id=None) -> dict:
    return await _send(node_id, "ble_scan", {"seconds": int(seconds), "active": bool(active)})


@capability(
    "mesh.sniff", http_method="POST", http_path="/mesh/sniff", http_tags=["mesh", "toolkit"],
    memory="on",
    description="Wi-Fi promiscuous capture on a node (Arduino/IDF firmware only): counts frames by "
                "type and tallies unique device MACs on a channel — an anonymous RF density / "
                "foot-traffic sensor (no payloads captured). "
                "Input: node_id (str!), channel (int 1-13, 0=hop all), seconds (int=8). "
                "Output: {ok, job_id}.",
)
async def cap_mesh_sniff(node_id: str = "", channel: int = 0, seconds: int = 8,
                         trace_id=None) -> dict:
    return await _send(node_id, "sniff", {"channel": int(channel), "seconds": int(seconds)})


@capability(
    "mesh.csi.start", http_method="POST", http_path="/mesh/csi/start", http_tags=["mesh", "toolkit"],
    memory="on",
    description="Start Wi-Fi CSI (Channel State Information) sensing on a node (Arduino/IDF firmware "
                "only). The node computes a per-window amplitude-variance motion metric and reports "
                "device-free presence/motion as telemetry (csi_motion, csi_present) — human movement "
                "disturbs the RF channel even with no device on the person. "
                "Input: node_id (str!), interval_s (int=2 — reporting cadence), "
                "threshold (float — motion trip level, default auto), source (str — optional BSSID to "
                "lock onto, else ambient). Output: {ok, job_id}.",
)
async def cap_mesh_csi_start(node_id: str = "", interval_s: int = 2,
                             threshold: float = 0.0, source: str = "",
                             trace_id=None) -> dict:
    p: Dict[str, Any] = {"interval_s": int(interval_s)}
    if threshold:
        p["threshold"] = float(threshold)
    if source:
        p["source"] = source
    return await _send(node_id, "csi_start", p)


@capability(
    "mesh.csi.stop", http_method="POST", http_path="/mesh/csi/stop", http_tags=["mesh", "toolkit"],
    memory="on",
    description="Stop CSI sensing on a node. Input: node_id (str!). Output: {ok, job_id}.",
)
async def cap_mesh_csi_stop(node_id: str = "", trace_id=None) -> dict:
    return await _send(node_id, "csi_stop", {})


@capability(
    "mesh.i2c.scan", http_method="POST", http_path="/mesh/i2c/scan", http_tags=["mesh", "toolkit"],
    memory="on",
    description="Scan a node's I2C bus for device addresses. Input: node_id (str!), "
                "sda (int), scl (int), freq (int=100000). Output: {ok, job_id} (addresses come back "
                "as the job result: {addresses:[0x..]}).",
)
async def cap_mesh_i2c_scan(node_id: str = "", sda: int = -1, scl: int = -1,
                            freq: int = 100000, trace_id=None) -> dict:
    p: Dict[str, Any] = {"freq": int(freq)}
    if sda >= 0:
        p["sda"] = int(sda)
    if scl >= 0:
        p["scl"] = int(scl)
    return await _send(node_id, "i2c_scan", p)


@capability(
    "mesh.touch", http_method="POST", http_path="/mesh/touch", http_tags=["mesh", "toolkit"],
    memory="on",
    description="Read a capacitive touch pad on a node (ESP32/S2/S3 touch GPIOs). "
                "Input: node_id (str!), pin (int!). Output: {ok, job_id} (value returns via telemetry).",
)
async def cap_mesh_touch(node_id: str = "", pin: int = -1, trace_id=None) -> dict:
    if pin is None or pin < 0:
        return {"error": "pin required"}
    return await _send(node_id, "touch_read", {"pin": int(pin)})


@capability(
    "mesh.espnow.ping", http_method="POST", http_path="/mesh/espnow/ping", http_tags=["mesh", "toolkit"],
    memory="on",
    description="ESP-NOW ranging: node sends connectionless frames to a peer MAC (or broadcast) and "
                "reports the reply RSSI — inter-node distance estimate independent of the AP. "
                "Feeds positioning. Input: node_id (str!), peer (str — 'AA:BB:..' or 'broadcast'), "
                "count (int=10). Output: {ok, job_id}.",
)
async def cap_mesh_espnow_ping(node_id: str = "", peer: str = "broadcast", count: int = 10,
                               trace_id=None) -> dict:
    return await _send(node_id, "espnow_ping", {"peer": peer, "count": int(count)})


@capability(
    "mesh.channel.survey", http_method="POST", http_path="/mesh/channel/survey", http_tags=["mesh", "toolkit"],
    memory="on",
    description="Per-channel Wi-Fi occupancy survey on a node (AP count + strongest RSSI per 2.4GHz "
                "channel) to pick the clearest channel. Input: node_id (str!). Output: {ok, job_id}.",
)
async def cap_mesh_channel_survey(node_id: str = "", trace_id=None) -> dict:
    return await _send(node_id, "channel_survey", {})


@capability(
    "mesh.sysinfo", http_method="POST", http_path="/mesh/sysinfo", http_tags=["mesh", "toolkit"],
    memory="on",
    description="Ask a node for detailed chip info (model, cores, revision, flash/PSRAM size, MAC, "
                "reset reason, free heap, features). Returns via the job result. "
                "Input: node_id (str!). Output: {ok, job_id}.",
)
async def cap_mesh_sysinfo(node_id: str = "", trace_id=None) -> dict:
    return await _send(node_id, "sysinfo", {})


@capability(
    "mesh.deep_sleep", http_method="POST", http_path="/mesh/deep_sleep", http_tags=["mesh", "toolkit"],
    memory="on",
    description="Put a node into deep sleep to save power; it wakes and re-enrolls after the timer. "
                "Input: node_id (str!), seconds (int!). Output: {ok, job_id}.",
)
async def cap_mesh_deep_sleep(node_id: str = "", seconds: int = 0, trace_id=None) -> dict:
    if not seconds or seconds <= 0:
        return {"error": "seconds required (>0)"}
    return await _send(node_id, "deep_sleep", {"seconds": int(seconds)})


@capability(
    "mesh.rf.range", http_method="POST", http_path="/mesh/rf/range", http_tags=["mesh", "toolkit"],
    memory="on",
    description="Ask a node to measure and report RSSI to a target (an AP BSSID or BLE MAC) for "
                "positioning. Broadcast this to several positioned nodes, then call mesh.locate. "
                "Input: node_id (str!), target (str — MAC/BSSID), kind (wifi|ble), samples (int=4). "
                "Output: {ok, job_id}.",
)
async def cap_mesh_rf_range(node_id: str = "", target: str = "", kind: str = "wifi",
                            samples: int = 4, trace_id=None) -> dict:
    if not target:
        return {"error": "target required"}
    return await _send(node_id, "rf_range",
                       {"target": target, "kind": kind, "samples": int(samples)})


# ─────────────────────────────────────────────────────────────────────────────
# Netmap bridge — BLE ingest + project mesh nodes as :NetHost
# ─────────────────────────────────────────────────────────────────────────────

def _dev_label(d: dict) -> str:
    name = (d.get("name") or "").strip()
    mac = (d.get("mac") or d.get("bssid") or "").strip()
    return (name or "(ble)") + (f"  {mac[-8:]}" if mac else "")


@capability(
    "netscan.ble.ingest", http_method="POST", http_path="/netscan/ble/ingest",
    http_tags=["netscan", "netmon", "mesh"], memory="off",
    description="Ingest a BLE scan into the Network Map (companion to netscan.wifi.ingest). Creates "
                ":BleDevice {mac,name,rssi,company} nodes and links the observing mesh node "
                "(:NetHost)-[:SEES_BLE]->. Also records RF observations for mesh.locate. "
                "Input: observer (str — node id/mac), devices (list[{mac,name,rssi,company}]). "
                "Output: {ok, devices, new}.",
)
async def cap_netscan_ble_ingest(observer: str = "", devices=None, trace_id=None) -> dict:
    if isinstance(devices, str):
        try:
            devices = json.loads(devices)
        except Exception:
            devices = []
    devices = devices or []
    if not isinstance(devices, list):
        return {"error": "devices must be a list"}
    obs = (observer or "").strip()
    obs_id = f"mesh:{obs}" if obs else ""
    if obs_id:
        await _aux_run(
            "MERGE (h:NetHost {id:$id}) SET h.label=coalesce(h.label,$lbl), "
            "h.role=coalesce(h.role,'mesh-node'), h.kind='mesh', h.last_seen=$ts",
            id=obs_id, lbl=obs, ts=now_iso())
    new = 0
    loop = asyncio.get_running_loop()
    for d in devices:
        if not isinstance(d, dict):
            continue
        mac = (d.get("mac") or d.get("bssid") or d.get("addr") or "").strip().lower()
        if not mac:
            continue
        bid = f"ble:{mac}"
        existed = await _aux_run("MATCH (b:BleDevice {id:$id}) RETURN b.id AS id", id=bid)
        if not existed:
            new += 1
        await _aux_run(
            "MERGE (b:BleDevice {id:$id}) SET b.mac=$mac, b.name=$name, b.label=$label, "
            "b.rssi=$rssi, b.company=$company, b.source='ble', b.updated_at=$ts, b.last_seen=$ts",
            id=bid, mac=mac, name=(d.get("name") or ""), label=_dev_label(d),
            rssi=d.get("rssi"), company=(d.get("company") or d.get("manufacturer") or ""),
            ts=now_iso())
        if obs_id:
            await _aux_run(
                "MATCH (h:NetHost {id:$oid}),(b:BleDevice {id:$bid}) MERGE (h)-[:SEES_BLE]->(b)",
                oid=obs_id, bid=bid)
        if obs and d.get("rssi") is not None:
            try:
                await loop.run_in_executor(None, _obs_put_sync, obs, mac, "ble", float(d["rssi"]))
            except Exception:
                pass
    await emit_event({"type": "netscan.ble.ingested", "observer": obs,
                      "devices": len(devices), "new": new})
    return {"ok": True, "devices": len(devices), "new": new}


async def _sync_mesh_nodes_to_graph() -> int:
    """Project every enrolled mesh node into the aux graph as a :NetHost so the
    fleet shows on the Network Map next to what each node hears."""
    nodes = await _call_cap("mesh.nodes")
    lst = (nodes or {}).get("nodes") if isinstance(nodes, dict) else None
    if not lst:
        return 0
    n = 0
    for nd in lst:
        nid = nd.get("node_id") or nd.get("id")
        if not nid:
            continue
        await _aux_run(
            "MERGE (h:NetHost {id:$id}) SET h.label=$lbl, h.kind='mesh', "
            "h.role='mesh-node', h.mac=$mac, h.ip=$ip, h.status=$st, h.last_seen=$ts",
            id=f"mesh:{nid}", lbl=(nd.get("name") or nid),
            mac=(nd.get("mac") or "").lower(), ip=nd.get("ip") or "",
            st=nd.get("status") or "", ts=now_iso())
        n += 1
    return n


@capability(
    "mesh.netmap.sync", http_method="POST", http_path="/mesh/netmap/sync",
    http_tags=["mesh", "netmon"], memory="on",
    description="Project the mesh fleet into the Network Map: every enrolled node becomes a "
                ":NetHost (kind=mesh) so it renders alongside the Wi-Fi APs and BLE devices it "
                "observes. Runs automatically every few minutes; call to force it now. "
                "Output: {ok, nodes}.",
)
async def cap_mesh_netmap_sync(trace_id=None) -> dict:
    n = await _sync_mesh_nodes_to_graph()
    await emit_event({"type": "mesh.netmap.sync", "nodes": n})
    return {"ok": True, "nodes": n}


# ─────────────────────────────────────────────────────────────────────────────
# Positioning caps
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "mesh.node.position", http_method="POST", http_path="/mesh/node/position",
    http_tags=["mesh", "position"], memory="on",
    description="Set (or clear) a node's known physical coordinates for RF positioning. Place your "
                "anchor nodes at surveyed points (metres, any consistent origin). "
                "Input: node_id (str!), x (float!), y (float!), z (float=0), label (str). "
                "Output: {ok, node_id, x, y}.",
)
async def cap_mesh_node_position(node_id: str = "", x: float = 0.0, y: float = 0.0,
                                 z: float = 0.0, label: str = "", trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    await asyncio.get_running_loop().run_in_executor(
        None, _pos_set_sync, node_id, float(x), float(y), float(z), label)
    # annotate the graph node too, so netmap can lay it out
    await _aux_run("MERGE (h:NetHost {id:$id}) SET h.x=$x, h.y=$y, h.z=$z",
                   id=f"mesh:{node_id}", x=float(x), y=float(y), z=float(z))
    return {"ok": True, "node_id": node_id, "x": x, "y": y, "z": z}


@capability(
    "mesh.node.position.list", http_method="GET", http_path="/mesh/node/position/list",
    http_tags=["mesh", "position"], memory="off", silent=True,
    description="List anchor nodes with known coordinates. Output: {anchors:[{node_id,x,y,z,label}]}.",
)
async def cap_mesh_node_position_list(trace_id=None) -> dict:
    rows = await asyncio.get_running_loop().run_in_executor(None, _pos_all_sync)
    return {"anchors": rows, "count": len(rows)}


@capability(
    "mesh.rf.ingest", http_method="POST", http_path="/mesh/rf/ingest",
    http_tags=["mesh", "position"], memory="off",
    description="Record an RSSI observation of a target seen by an observing node (usually called "
                "automatically from rf_range / wifi / ble results, but exposed for external feeds). "
                "Input: observer (str!), target (str! — MAC/BSSID), rssi (float!), kind (wifi|ble|espnow). "
                "Output: {ok}.",
)
async def cap_mesh_rf_ingest(observer: str = "", target: str = "", rssi: float = 0.0,
                             kind: str = "wifi", trace_id=None) -> dict:
    if not observer or not target:
        return {"error": "observer and target required"}
    await asyncio.get_running_loop().run_in_executor(
        None, _obs_put_sync, observer, target.lower(), kind, float(rssi))
    return {"ok": True}


@capability(
    "mesh.rf.targets", http_method="GET", http_path="/mesh/rf/targets",
    http_tags=["mesh", "position"], memory="off", silent=True,
    description="List targets currently observed by 2+ mesh nodes (locatable candidates). "
                "Input: max_age_s (int=120), kind (wifi|ble optional). "
                "Output: {targets:[{target,kind,observers,ts}]}.",
)
async def cap_mesh_rf_targets(max_age_s: int = 120, kind: str = "", trace_id=None) -> dict:
    rows = await asyncio.get_running_loop().run_in_executor(
        None, _obs_targets_sync, float(max_age_s), kind)
    return {"targets": rows, "count": len(rows)}


@capability(
    "mesh.locate", http_method="POST", http_path="/mesh/locate",
    http_tags=["mesh", "position"], memory="on",
    description="Estimate the position of a target (AP BSSID / BLE MAC) from RSSI observations by "
                "3+ positioned mesh nodes (multilateration with a log-distance path-loss model). "
                "Set anchor coords first with mesh.node.position, then have nodes report RSSI "
                "(mesh.rf.range broadcast, or ongoing wifi/ble scans). "
                "Input: target (str!), max_age_s (int=120 — freshness of observations), "
                "rssi_at_1m (float — path-loss A, default -45), path_loss_n (float — exponent, default "
                "2.7). Output: {ok, target, x, y, accuracy_m, anchors_used, observations:[...]}.",
)
async def cap_mesh_locate(target: str = "", max_age_s: int = 120,
                          rssi_at_1m: float = 0.0, path_loss_n: float = 0.0,
                          trace_id=None) -> dict:
    if not target:
        return {"error": "target required"}
    a = rssi_at_1m or DEFAULT_RSSI_AT_1M
    n = path_loss_n or DEFAULT_PATH_LOSS_N
    loop = asyncio.get_running_loop()
    obs = await loop.run_in_executor(None, _obs_for_target_sync, target, float(max_age_s))
    anchors = {r["node_id"]: r for r in await loop.run_in_executor(None, _pos_all_sync)}
    points: List[Tuple[float, float, float]] = []
    used = []
    for o in obs:
        anc = anchors.get(o["observer"])
        if not anc:
            continue
        d = _rssi_to_distance(float(o["rssi"]), a, n)
        points.append((anc["x"], anc["y"], d))
        used.append({"node_id": o["observer"], "rssi": o["rssi"],
                     "distance_m": round(d, 2), "x": anc["x"], "y": anc["y"]})
    if len(points) < 3:
        return {"error": "need RSSI from >=3 positioned nodes",
                "target": target, "anchors_used": len(points),
                "hint": "set coords with mesh.node.position and broadcast mesh.rf.range",
                "observations": used}
    fix = _multilaterate(points)
    if not fix:
        return {"error": "multilateration failed (degenerate anchor geometry)",
                "target": target, "observations": used}
    x, y, rms = fix
    result = {"ok": True, "target": target.lower(), "x": round(x, 2), "y": round(y, 2),
              "accuracy_m": round(rms, 2), "anchors_used": len(points),
              "model": {"rssi_at_1m": a, "path_loss_n": n},
              "observations": used, "ts": now_iso()}
    # drop an estimate node onto the map
    await _aux_run(
        "MERGE (t:LocatedTarget {id:$id}) SET t.label=$lbl, t.x=$x, t.y=$y, "
        "t.accuracy_m=$acc, t.updated_at=$ts",
        id=f"loc:{target.lower()}", lbl=target, x=round(x, 2), y=round(y, 2),
        acc=round(rms, 2), ts=now_iso())
    await emit_event({"type": "mesh.locate", "target": target.lower(),
                      "x": result["x"], "y": result["y"], "accuracy_m": result["accuracy_m"]})
    return result


@capability(
    "mesh.presence", http_method="GET", http_path="/mesh/presence",
    http_tags=["mesh", "position"], memory="off", silent=True,
    description="Current CSI-derived device-free presence/motion per node (from nodes running "
                "mesh.csi.start). Output: {nodes:[{node_id,motion,present,metric,ts}]}.",
)
async def cap_mesh_presence(trace_id=None) -> dict:
    rows = await asyncio.get_running_loop().run_in_executor(None, _presence_all_sync)
    return {"nodes": rows, "count": len(rows)}


# ─────────────────────────────────────────────────────────────────────────────
# Result / telemetry bridges — called from mesh_capabilities._handle_result and
# the telemetry path (shape-detected, so firmware just reports naturally).
# ─────────────────────────────────────────────────────────────────────────────

async def ingest_ble_result(node_id: str, result: Any) -> bool:
    """Detect a BLE device list in a job result and feed netscan.ble.ingest."""
    devs = None
    if isinstance(result, dict):
        devs = result.get("devices") or result.get("ble")
    elif isinstance(result, list):
        devs = result
    if not (isinstance(devs, list) and any(
            isinstance(d, dict) and (d.get("mac") or d.get("addr")) for d in devs)):
        return False
    await cap_netscan_ble_ingest(observer=node_id, devices=devs)
    return True


async def ingest_rf_range_result(node_id: str, result: Any) -> bool:
    """Detect an rf_range result ({target, rssi, kind}) and store the observation."""
    if not isinstance(result, dict):
        return False
    target = result.get("target")
    rssi = result.get("rssi")
    if not target or rssi is None:
        return False
    await asyncio.get_running_loop().run_in_executor(
        None, _obs_put_sync, node_id, str(target).lower(),
        result.get("kind") or "wifi", float(rssi))
    return True


async def ingest_csi_presence(node_id: str, metrics: dict) -> bool:
    """CSI presence arrives as telemetry metrics (csi_motion / csi_present)."""
    if not isinstance(metrics, dict) or "csi_motion" not in metrics:
        return False
    try:
        motion = float(metrics.get("csi_motion") or 0.0)
        present = int(bool(metrics.get("csi_present")))
        metric = float(metrics.get("csi_metric") or motion)
    except Exception:
        return False
    await asyncio.get_running_loop().run_in_executor(
        None, _presence_set_sync, node_id, motion, present, metric)
    await emit_event({"type": "mesh.presence", "node_id": node_id,
                      "motion": motion, "present": present})
    return True


# Periodic fleet → graph projection so the mesh always shows on the Network Map.
schedule(_sync_mesh_nodes_to_graph, 240, "mesh_netmap_sync")

log.info("mesh_toolkit_capabilities ready — RF positioning db=%s", _DB_PATH)
