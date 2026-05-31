"""
cap_tracking_config.py  —  Capability Chain Tracking Configuration
====================================================================
Runtime-mutable configuration for which capabilities are tracked into
the FOLLOWS_ACTIVITY chain in the memory graph.

Bugs fixed vs v1
────────────────
  1. Unknown groups now default to TRACKED (open allowlist) instead of
     silently dropped (closed allowlist). Any group not in config.groups
     falls through to default_track=True.
  2. _ACT_QUEUE None-check bypass: install() pre-creates the queue so
     the wrapper's `_ACT_QUEUE is not None` guard doesn't block everything.
  3. record_chain_step() wired in via _TrackingCursor — a drop-in dict
     subclass replacing _ACT_SESSION_CURSOR that notifies us on every write.
  4. chain_stats reads _ACT_SESSION_CURSOR from the orch module (the live
     cursor) so the panel shows active sessions immediately, not only after
     chain_step fires.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from copy import deepcopy
from typing import Any, Dict, Optional

log = logging.getLogger("vera.cap_tracking")

# ─────────────────────────────────────────────────────────────────────────────
# HARD-WIRED SAFETY SETS
# ─────────────────────────────────────────────────────────────────────────────

_HARD_SKIP_GROUPS = frozenset({
    "fabric", "memory", "obs", "health", "ui", "mcp",
    "syslog", "cluster", "db", "stream", "caps", "session",
})

_ALWAYS_SKIP_CAPS = frozenset({
    "memory.stats", "memory.backends", "obs.health", "caps.list",
    "ui.panels", "syslog.feed", "cluster.health",
    "cap_tracking.chain_stats", "cap_tracking.get_config",
})

# ─────────────────────────────────────────────────────────────────────────────
# FACTORY DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────

_FACTORY_DEFAULTS: Dict[str, Any] = {
    "enabled":                 True,
    "max_chain_length":        200,
    "max_queue_size":          2000,
    "max_per_session_per_min": 60,
    # Open allowlist — unknown groups track by default
    "default_track":           True,
    "groups": {
        # Streaming endpoints (raw FastAPI routes wrapped via
        # record_stream_activity): bash/ps/ssh/ide/chat/dag-plan-stream.
        # These are the things the user explicitly asked to be tracked.
        "exec":     {"track": True,  "budget": 200, "note": "bash/ps/ssh streams"},
        "ide":      {"track": True,  "budget": 100, "note": "IDE generate stream"},
        "chat":     {"track": True,  "budget": 100, "note": "agent chat stream"},
        "dag":      {"track": True,  "budget": 100, "note": "DAG plan/run"},
        "research": {"track": True,  "budget": 100, "note": "research jobs"},
        "nlp":      {"track": True,  "budget": 100, "note": "NLP analysis"},
        "llm":      {"track": True,  "budget": 200, "note": "LLM generation"},
        # Agent infrastructure (heartbeats, list/get/etc.) is too noisy by
        # default. Specific agent caps that matter (chat.stream above) are
        # tracked under the chat group instead.
        "agent":    {"track": False, "budget": 0,   "note": "high-frequency infra: off by default"},
        # Hard-skip groups (loop-safety / pure infrastructure). Shown
        # greyed-out in the UI. Mirrors capability_orchestration._ACT_SKIP_GROUPS.
        "fabric":   {"track": False, "budget": 0,   "note": "loop-safety: hard off"},
        "memory":   {"track": False, "budget": 0,   "note": "loop-safety: hard off"},
        "obs":      {"track": False, "budget": 0,   "note": "loop-safety: hard off"},
        "health":   {"track": False, "budget": 0,   "note": "loop-safety: hard off"},
        "ui":       {"track": False, "budget": 0,   "note": "loop-safety: hard off"},
        # All other groups (skills, data, pipeline, http, text, system…)
        # are NOT listed → fall through to default_track=True
    },
    "caps":        {},
    "always_skip": sorted(_ALWAYS_SKIP_CAPS),
}

# ─────────────────────────────────────────────────────────────────────────────
# LIVE STATE
# ─────────────────────────────────────────────────────────────────────────────

_CAP_TRACKING: Dict[str, Any] = deepcopy(_FACTORY_DEFAULTS)
_SESSION_RATE:  Dict[str, list] = {}
_CHAIN_LEN:     Dict[str, int]  = {}
REDIS_CONFIG_KEY = "vera:cap_tracking:config"
_INSTALLED_ORCH  = None


# ─────────────────────────────────────────────────────────────────────────────
# GATE
# ─────────────────────────────────────────────────────────────────────────────

def is_tracked(cap_name: str, group: str, session_id: str) -> bool:
    """
    Gate check — synchronous, dict-lookups only.

    Precedence:
      1. Hard safety sets   → always False
      2. Per-cap override   → explicit True/False
      3. Group setting      → config["groups"][group]["track"]
      4. default_track      → True by default (open allowlist)
    """
    cfg = _CAP_TRACKING

    if not cfg.get("enabled", True):
        return False

    if cap_name in _ALWAYS_SKIP_CAPS or cap_name in cfg.get("always_skip", []):
        return False
    if group in _HARD_SKIP_GROUPS:
        return False

    # Per-cap override
    cap_override = cfg["caps"].get(cap_name)
    if cap_override is not None:
        if not cap_override.get("track", True):
            return False
    else:
        # Group setting — FIX 1: absent group → default_track
        group_cfg = cfg["groups"].get(group)
        if group_cfg is not None:
            if not group_cfg.get("track", False):
                return False
        else:
            if not cfg.get("default_track", True):
                return False

    # Chain length guard
    max_chain = cfg.get("max_chain_length", 0)
    if max_chain > 0 and _CHAIN_LEN.get(session_id, 0) >= max_chain:
        return False

    # Flood guard
    max_rate = cfg.get("max_per_session_per_min", 0)
    if max_rate > 0:
        now = time.time()
        window = _SESSION_RATE.setdefault(session_id, [])
        _SESSION_RATE[session_id] = [t for t in window if now - t < 60]
        if len(_SESSION_RATE[session_id]) >= max_rate:
            return False
        _SESSION_RATE[session_id].append(now)

    return True


def record_chain_step(session_id: str):
    """Increment per-session chain counter. Called via _TrackingCursor."""
    _CHAIN_LEN[session_id] = _CHAIN_LEN.get(session_id, 0) + 1


def reset_chain(session_id: str):
    _CHAIN_LEN.pop(session_id, None)
    _SESSION_RATE.pop(session_id, None)


# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENCE
# ─────────────────────────────────────────────────────────────────────────────

async def _save_to_redis():
    orch = _INSTALLED_ORCH
    if not orch or not getattr(orch, "REDIS", None):
        return
    try:
        await orch.REDIS.set(REDIS_CONFIG_KEY, json.dumps(_CAP_TRACKING))
    except Exception as e:
        log.warning("cap_tracking: redis save: %s", e)


async def _load_from_redis():
    orch = _INSTALLED_ORCH
    if not orch or not getattr(orch, "REDIS", None):
        return
    try:
        raw = await orch.REDIS.get(REDIS_CONFIG_KEY)
        if raw:
            loaded = json.loads(raw)
            _CAP_TRACKING.update(loaded)
            _CAP_TRACKING["always_skip"] = sorted(_ALWAYS_SKIP_CAPS)
            log.info("cap_tracking: config loaded from Redis")
    except Exception as e:
        log.warning("cap_tracking: redis load: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# INSTALL
# ─────────────────────────────────────────────────────────────────────────────

class _TrackingCursor(dict):
    """
    FIX 3: Drop-in replacement for _ACT_SESSION_CURSOR.
    Calls record_chain_step() whenever the activity worker advances a cursor,
    keeping _CHAIN_LEN accurate without touching _activity_worker's source.
    """
    def __setitem__(self, session_id, node_id):
        super().__setitem__(session_id, node_id)
        record_chain_step(session_id)


def install(orch_module):
    """
    Patch orch_module in-place. Idempotent.

      • Replaces _act_enqueue with a gated version
      • Replaces _ACT_SESSION_CURSOR with _TrackingCursor
      • FIX 2: pre-creates _ACT_QUEUE if missing
      • Schedules deferred Redis config load
    """
    global _INSTALLED_ORCH
    if getattr(orch_module, "_cap_tracking_installed", False):
        log.debug("cap_tracking: already installed, skipping")
        return
    _INSTALLED_ORCH = orch_module

    # FIX 2: pre-create the queue so the `_ACT_QUEUE is not None` guard
    # in the capability wrapper doesn't silently drop all enqueues.
    if orch_module._ACT_QUEUE is None:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                orch_module._ACT_QUEUE = asyncio.Queue(
                    maxsize=_CAP_TRACKING.get("max_queue_size", 2000)
                )
                log.info("cap_tracking: pre-created _ACT_QUEUE (size=%d)",
                         _CAP_TRACKING.get("max_queue_size", 2000))
        except Exception as e:
            log.warning("cap_tracking: queue pre-create failed: %s", e)

    # Gate patch
    _orig = orch_module._act_enqueue

    def _gated(cap_name, group, session_id, trace_id, kw, result,
               elapsed_ms, trigger_id="", trigger_cap=""):
        # Signature mirrors the new rich _act_enqueue. is_tracked() is the
        # cheap synchronous gate — if the cap fails the gate we drop it
        # without enqueueing (the recording cost lives downstream in
        # _activity_worker). If it passes, we hand off to the original
        # function which builds the queue item with the full payload.
        if is_tracked(cap_name, group, session_id):
            _orig(cap_name, group, session_id, trace_id, kw, result,
                  elapsed_ms, trigger_id=trigger_id, trigger_cap=trigger_cap)

    orch_module._act_enqueue = _gated

    # FIX 3: swap in the tracking cursor
    existing = dict(orch_module._ACT_SESSION_CURSOR)
    orch_module._ACT_SESSION_CURSOR = _TrackingCursor(existing)

    # Deferred Redis load
    async def _deferred_load():
        await asyncio.sleep(3)
        await _load_from_redis()

    try:
        if asyncio.get_event_loop().is_running():
            asyncio.ensure_future(_deferred_load())
    except Exception:
        pass

    orch_module._cap_tracking_installed = True
    log.info("cap_tracking: installed (gate + tracking cursor + queue pre-create)")


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITY ENDPOINTS + PANEL REGISTRATION
# ─────────────────────────────────────────────────────────────────────────────

try:
    from Vera.Orchestration.capability_orchestration import (
        APP, CAPABILITY_REGISTRY, capability, emit_event,
    )
    from pathlib import Path as _Path
    _CAP_AVAILABLE = True
except ImportError:
    _CAP_AVAILABLE = False

if _CAP_AVAILABLE:

    _HERE = _Path(__file__).parent

    # @APP.get("/cap_tracking/panel", include_in_schema=False)
    # async def _panel_route():
    #     from fastapi.responses import HTMLResponse
    #     p = _HERE / "cap_tracking_panel.html"
    #     return HTMLResponse(
    #         p.read_text(encoding="utf-8") if p.exists()
    #         else "<p style='color:red'>cap_tracking_panel.html not found</p>"
    #     )

    # try:
    #     from Vera.Orchestration.capability_orchestration import register_ui
        
    #     register_ui(
    #         "cap_tracking",
    #         "Activity",
    #         "",
    #         """<div id="cap_tracking-panel-mount" style="height:100%;display:flex;flex-direction:column;">
    #     <iframe src="/cap_tracking/panel"
    #             style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
    #             allow="clipboard-read; clipboard-write">
    #     </iframe>
    #     </div>""",
    #     "",
    #         ui_caps=[

    #         ],
    #         mode="tab",
    #         tab_order=2,
    #     )
    # except Exception as _e:
    #     log.warning("cap_tracking: register_ui: %s", _e)

    @capability(
        "cap_tracking.get_config",
        http_method="GET", http_path="/cap_tracking/config",
        http_tags=["memory", "tracking"],
        memory="off", silent=True,
        description="Return the full capability tracking configuration.",
    )
    async def cap_tracking_get_config(trace_id=None):
        orch = _INSTALLED_ORCH
        all_groups: Dict[str, int] = {}
        all_caps:   Dict[str, str] = {}
        if orch and hasattr(orch, "CAPABILITY_REGISTRY"):
            for cname in orch.CAPABILITY_REGISTRY:
                g = cname.split(".")[0]
                all_groups[g] = all_groups.get(g, 0) + 1
                all_caps[cname] = g
        return {
            "config":           _CAP_TRACKING,
            "all_groups":       all_groups,
            "all_caps":         all_caps,
            "hard_skip_groups": sorted(_HARD_SKIP_GROUPS),
            "always_skip_caps": sorted(_ALWAYS_SKIP_CAPS),
        }

    @capability(
        "cap_tracking.set_group",
        http_method="POST", http_path="/cap_tracking/group",
        http_tags=["memory", "tracking"],
        memory="off",
        description="Enable or disable tracking for a whole group. "
                    "Hard-safety groups cannot be enabled.",
    )
    async def cap_tracking_set_group(
        group: str, track: bool, budget: int = -1, trace_id=None,
    ):
        if group in _HARD_SKIP_GROUPS and track:
            return {"error": f"Group '{group}' is hard-locked off for loop safety."}
        cfg = _CAP_TRACKING["groups"].setdefault(
            group, {"track": False, "budget": 100, "note": ""}
        )
        cfg["track"] = bool(track)
        if budget >= 0:
            cfg["budget"] = int(budget)
        await _save_to_redis()
        await emit_event({"type": "cap_tracking.updated", "scope": "group",
                          "group": group, "track": track})
        return {"ok": True, "group": group, "track": track, "budget": cfg["budget"]}

    @capability(
        "cap_tracking.set_cap",
        http_method="POST", http_path="/cap_tracking/cap",
        http_tags=["memory", "tracking"],
        memory="off",
        description="Override tracking for a single capability. "
                    "Pass track=null to remove override and inherit group.",
    )
    async def cap_tracking_set_cap(
        cap_name: str, track: Optional[bool] = None, note: str = "", trace_id=None,
    ):
        if cap_name in _ALWAYS_SKIP_CAPS:
            return {"error": f"'{cap_name}' is hard-locked off (loop-safety)."}
        if track is None:
            _CAP_TRACKING["caps"].pop(cap_name, None)
            action = "removed override"
        else:
            _CAP_TRACKING["caps"][cap_name] = {"track": bool(track), "note": note}
            action = "override set"
        await _save_to_redis()
        await emit_event({"type": "cap_tracking.updated", "scope": "cap",
                          "cap_name": cap_name, "track": track})
        return {"ok": True, "cap_name": cap_name, "action": action, "track": track}

    @capability(
        "cap_tracking.set_limits",
        http_method="POST", http_path="/cap_tracking/limits",
        http_tags=["memory", "tracking"],
        memory="off",
        description="Adjust tracking limits: enabled, default_track, "
                    "max_chain_length, max_queue_size, max_per_session_per_min.",
    )
    async def cap_tracking_set_limits(
        enabled:                 Optional[bool] = None,
        default_track:           Optional[bool] = None,
        max_chain_length:        Optional[int]  = None,
        max_queue_size:          Optional[int]  = None,
        max_per_session_per_min: Optional[int]  = None,
        trace_id=None,
    ):
        changed = {}
        if enabled is not None:
            _CAP_TRACKING["enabled"] = bool(enabled); changed["enabled"] = bool(enabled)
        if default_track is not None:
            _CAP_TRACKING["default_track"] = bool(default_track)
            changed["default_track"] = bool(default_track)
        if max_chain_length is not None:
            _CAP_TRACKING["max_chain_length"] = max(0, int(max_chain_length))
            changed["max_chain_length"] = _CAP_TRACKING["max_chain_length"]
        if max_queue_size is not None:
            _CAP_TRACKING["max_queue_size"] = max(100, int(max_queue_size))
            changed["max_queue_size"] = _CAP_TRACKING["max_queue_size"]
        if max_per_session_per_min is not None:
            _CAP_TRACKING["max_per_session_per_min"] = max(0, int(max_per_session_per_min))
            changed["max_per_session_per_min"] = _CAP_TRACKING["max_per_session_per_min"]
        await _save_to_redis()
        await emit_event({"type": "cap_tracking.updated", "scope": "limits",
                          "changed": changed})
        return {"ok": True, "changed": changed, "config": _CAP_TRACKING}

    @capability(
        "cap_tracking.reset",
        http_method="POST", http_path="/cap_tracking/reset",
        http_tags=["memory", "tracking"],
        memory="off",
        description="Reset capability tracking configuration to factory defaults.",
    )
    async def cap_tracking_reset(trace_id=None):
        _CAP_TRACKING.clear()
        _CAP_TRACKING.update(deepcopy(_FACTORY_DEFAULTS))
        await _save_to_redis()
        await emit_event({"type": "cap_tracking.updated", "scope": "reset"})
        return {"ok": True, "config": _CAP_TRACKING}

    @capability(
        "cap_tracking.set_session",
        http_method="POST", http_path="/cap_tracking/session",
        http_tags=["memory", "tracking"],
        memory="off", silent=True,
        description="Register the current UI session_id for activity recording.",
    )
    async def cap_tracking_set_session(session_id: str, trace_id=None):
        import Vera.Orchestration.capability_orchestration as _co
        if session_id:
            _co._CURRENT_SESSION = session_id
        return {"ok": True, "session_id": _co._CURRENT_SESSION}

    @capability(
        "cap_tracking.chain_stats",
        http_method="GET", http_path="/cap_tracking/stats",
        http_tags=["memory", "tracking"],
        memory="off", silent=True,
        description="Live stats: queue depth, per-session chain lengths, rate counters.",
    )
    async def cap_tracking_chain_stats(trace_id=None):
        orch = _INSTALLED_ORCH
        q_depth = 0
        if orch and getattr(orch, "_ACT_QUEUE", None):
            q_depth = orch._ACT_QUEUE.qsize()

        # FIX 4: live cursor from orch — shows sessions even before chain_step fires
        live_cursors: Dict[str, str] = {}
        if orch:
            live_cursors = dict(getattr(orch, "_ACT_SESSION_CURSOR", {}))

        now = time.time()
        rate_now = {
            sid: len([t for t in ts if now - t < 60])
            for sid, ts in _SESSION_RATE.items()
        }

        all_sessions = set(live_cursors.keys()) | set(_CHAIN_LEN.keys())
        chain_info = {
            sid: {
                "chain_len":    _CHAIN_LEN.get(sid, 0),
                "last_node":    live_cursors.get(sid, "")[:8],
                "rate_per_min": rate_now.get(sid, 0),
            }
            for sid in all_sessions
        }

        return {
            "queue_depth":     q_depth,
            "queue_max":       _CAP_TRACKING.get("max_queue_size", 2000),
            "chain_lengths":   {sid: v["chain_len"] for sid, v in chain_info.items()},
            "chain_info":      chain_info,
            "max_chain_length": _CAP_TRACKING.get("max_chain_length", 200),
            "rate_per_min":    rate_now,
            "max_per_min":     _CAP_TRACKING.get("max_per_session_per_min", 60),
            "enabled":         _CAP_TRACKING.get("enabled", True),
            "default_track":   _CAP_TRACKING.get("default_track", True),
            "active_sessions": len(all_sessions),
        }