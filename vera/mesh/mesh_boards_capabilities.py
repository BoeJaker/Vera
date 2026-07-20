# ============================================================================
# mesh_boards_capabilities.py — board pin profiles + display bring-up
# ============================================================================
#
# A display/SD mesh node's pin map is board-specific: an ESP32-S3 Uno board, a
# classic "D1 R32" Uno board and a bare devkit all route the ILI9488 shield's
# parallel bus to different GPIOs. The reference firmware ships one default map;
# this module lets you pick a *named board profile* (firmware/boards.json, plus
# user-saved profiles in boards_custom.json), push its pin map to a live node
# (no reflash — it lands as config.io.tft/sd/neopixel), and fire a on-screen
# **test pattern** so you can see whether the bus is actually alive and dial in
# the pins for a new board, then Save the working map as a new profile.
#
#   • mesh.boards.list    — the profiles (built-in + custom) + each node uses them
#   • mesh.boards.apply   — push a profile's pin map to a node (persist + live)
#   • mesh.boards.save    — save/overwrite a custom profile
#   • mesh.boards.delete  — remove a custom profile
#   • mesh.display.test   — draw the bring-up test pattern (colour bars + pin map)
#
# The same profiles feed the flash-time baker (?board=<id> on GET /mesh/firmware,
# handled in mesh_capabilities._bake_firmware_options) so a freshly flashed node
# already has the right pins.
# ============================================================================

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from Vera.vera.capability_orchestration import capability, emit_event

log = logging.getLogger("vera.mesh.boards")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BOARDS_BUILTIN = os.path.join(_HERE, "firmware", "boards.json")
_BOARDS_CUSTOM = os.path.join(_HERE, "firmware", "boards_custom.json")


# ─────────────────────────────────────────────────────────────────────────────
# Profile storage (built-in JSON shipped with the repo + user-writable custom)
# ─────────────────────────────────────────────────────────────────────────────

def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log.warning("mesh.boards: failed to read %s: %s", path, e)
        return default


def load_boards() -> List[dict]:
    """Built-in profiles, then custom ones (custom overrides built-in by id)."""
    builtin = _load_json(_BOARDS_BUILTIN, []) or []
    custom = _load_json(_BOARDS_CUSTOM, []) or []
    by_id: Dict[str, dict] = {}
    for b in builtin:
        if isinstance(b, dict) and b.get("id"):
            by_id[b["id"]] = {**b, "builtin": True}
    for b in custom:
        if isinstance(b, dict) and b.get("id"):
            by_id[b["id"]] = {**b, "builtin": False}
    return list(by_id.values())


def get_board(board_id: str) -> Optional[dict]:
    for b in load_boards():
        if b.get("id") == board_id:
            return b
    return None


def board_io(board_id: str) -> Optional[dict]:
    """The io block (tft/sd/neopixel) for a profile — used by the firmware baker."""
    b = get_board(board_id)
    return (b or {}).get("io") if b else None


def _save_custom(boards: List[dict]) -> None:
    tmp = _BOARDS_CUSTOM + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(boards, f, indent=2)
    os.replace(tmp, _BOARDS_CUSTOM)


async def _call_cap(name: str, **kwargs) -> Any:
    from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap:
        return {"error": f"unknown_cap:{name}"}
    try:
        return await cap["func"](**kwargs)
    except TypeError:
        accepted = set(cap.get("schema", {}).get("properties", {}).keys())
        return await cap["func"](**{k: v for k, v in kwargs.items() if k in accepted})
    except Exception as e:
        log.warning("mesh.boards _call_cap %s: %s", name, e)
        return {"error": f"{type(e).__name__}: {e}"}


async def _push_io(node_id: str, io_patch: dict, kiosk_patch: dict = None) -> dict:
    """Deep-merge an io patch (tft/sd/neopixel/…) into a node's config so we never
    clobber unrelated pins (led/relay/adc/audio/touch), then persist + push it.
    tft/sd are themselves merged key-by-key; scalars (neopixel) replace."""
    node = await _call_cap("mesh.node", node_id=node_id)
    n = node.get("node") if isinstance(node, dict) else None
    if not n:
        return {"error": "unknown node", "node_id": node_id}
    cur = n.get("config") or {}
    io = dict(cur.get("io") or {})
    for key, val in (io_patch or {}).items():
        if isinstance(val, dict) and isinstance(io.get(key), dict):
            io[key] = {**io[key], **val}
        else:
            io[key] = val
    new_cfg: Dict[str, Any] = {"io": io}
    if isinstance(kiosk_patch, dict) and kiosk_patch:
        new_cfg["kiosk"] = {**(cur.get("kiosk") or {}), **kiosk_patch}
    return await _call_cap("mesh.config", node_id=node_id, config=new_cfg, merge=True)


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "mesh.boards.list", http_method="GET", http_path="/mesh/boards",
    http_tags=["mesh", "boards"], memory="off", silent=True,
    description="List board pin profiles (built-in + user-saved) for display/SD nodes. Each has "
                "{id, label, chip, controller, io:{tft,sd,neopixel}, kiosk:{rotation}, note, builtin}. "
                "Output: {boards:[...], count}.",
)
async def cap_mesh_boards_list(trace_id=None) -> dict:
    boards = load_boards()
    return {"boards": boards, "count": len(boards)}


@capability(
    "mesh.boards.apply", http_method="POST", http_path="/mesh/boards/apply",
    http_tags=["mesh", "boards"], memory="on",
    description="Push a board profile's pin map to a node — persisted and applied live (no reflash; "
                "lands as config.io.tft/sd/neopixel + kiosk.rotation). Use this when a display node "
                "shows a blank white screen to try a different pinout. Input: node_id (str!), "
                "board_id (str!). Output: {ok, board_id, config, job_id}.",
)
async def cap_mesh_boards_apply(node_id: str = "", board_id: str = "", trace_id=None) -> dict:
    if not node_id or not board_id:
        return {"error": "node_id and board_id required"}
    b = get_board(board_id)
    if not b:
        return {"error": "unknown board", "board_id": board_id}
    bio = b.get("io") or {}
    io_patch = {k: bio[k] for k in ("tft", "sd") if isinstance(bio.get(k), dict)}
    if "neopixel" in bio:
        io_patch["neopixel"] = bio["neopixel"]
    kiosk = b.get("kiosk") if isinstance(b.get("kiosk"), dict) else None
    r = await _push_io(node_id, io_patch, kiosk)
    if isinstance(r, dict) and r.get("error"):
        return r
    await emit_event({"type": "mesh.board", "stage": "apply", "node_id": node_id,
                      "board_id": board_id})
    return {"ok": bool(isinstance(r, dict) and r.get("ok")), "board_id": board_id,
            "config": (r or {}).get("config"), "job_id": (r or {}).get("job_id")}


@capability(
    "mesh.io.pins", http_method="POST", http_path="/mesh/io/pins",
    http_tags=["mesh", "boards"], memory="on",
    description="Set a display/SD node's pin map directly (hand-tuned pins) — deep-merged into "
                "config.io so unrelated pins are preserved, then applied live (the firmware re-inits "
                "the bus, no reflash). Input: node_id (str!), tft (dict — {rst,cs,dc,wr,rd,d:[8]}), "
                "sd (dict — {clk,miso,mosi,cs}), neopixel (int), rotation (int 0-3). Output: {ok, config, job_id}.",
)
async def cap_mesh_io_pins(node_id: str = "", tft=None, sd=None, neopixel=None,
                           rotation=None, trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    tft = json.loads(tft) if isinstance(tft, str) else tft
    sd = json.loads(sd) if isinstance(sd, str) else sd

    def _clean(d: dict) -> dict:
        # Drop blank/None scalar pins so a left-blank field never nulls out a good
        # pin on merge; only accept a data array if it's a full set of 8 ints.
        out = {}
        for k, v in (d or {}).items():
            if k == "d":
                if isinstance(v, list) and len(v) == 8 and all(isinstance(x, int) for x in v):
                    out["d"] = v
            elif isinstance(v, int):
                out[k] = v
        return out

    io_patch: Dict[str, Any] = {}
    if isinstance(tft, dict):
        ct = _clean(tft)
        if ct:
            io_patch["tft"] = ct
    if isinstance(sd, dict):
        cs = _clean(sd)
        if cs:
            io_patch["sd"] = cs
    if neopixel is not None and neopixel != "":
        io_patch["neopixel"] = int(neopixel)
    if not io_patch:
        return {"error": "nothing to set"}
    kiosk = {"rotation": int(rotation)} if rotation is not None and rotation != "" else None
    r = await _push_io(node_id, io_patch, kiosk)
    if isinstance(r, dict) and r.get("error"):
        return r
    return {"ok": bool(isinstance(r, dict) and r.get("ok")),
            "config": (r or {}).get("config"), "job_id": (r or {}).get("job_id")}


@capability(
    "mesh.boards.save", http_method="POST", http_path="/mesh/boards/save",
    http_tags=["mesh", "boards"], memory="on",
    description="Save (or overwrite) a custom board pin profile so a working pin map is never lost. "
                "Input: id (str!), label (str), chip (str='any'), controller (str='ili9488'), "
                "io (dict! — {tft:{rst,cs,dc,wr,rd,d:[8]}, sd:{clk,miso,mosi,cs}, neopixel:int}), "
                "kiosk (dict — {rotation}), note (str). Output: {ok, id, boards}.",
)
async def cap_mesh_boards_save(id: str = "", label: str = "", chip: str = "any",
                               controller: str = "ili9488", io=None, kiosk=None,
                               note: str = "", trace_id=None) -> dict:
    if not id:
        return {"error": "id required"}
    io = json.loads(io) if isinstance(io, str) else io
    if not isinstance(io, dict) or not isinstance(io.get("tft"), dict):
        return {"error": "io.tft (pin map) required"}
    kiosk = json.loads(kiosk) if isinstance(kiosk, str) else (kiosk or {})
    if get_board(id) and (get_board(id) or {}).get("builtin"):
        return {"error": f"'{id}' is a built-in profile — choose a different id"}
    profile = {"id": id, "label": label or id, "chip": chip, "bus": "parallel8",
               "controller": controller, "io": io,
               "kiosk": kiosk if isinstance(kiosk, dict) else {}, "note": note}
    custom = _load_json(_BOARDS_CUSTOM, []) or []
    custom = [b for b in custom if isinstance(b, dict) and b.get("id") != id]
    custom.append(profile)
    try:
        _save_custom(custom)
    except Exception as e:
        return {"error": f"save failed: {e}"}
    await emit_event({"type": "mesh.board", "stage": "save", "board_id": id})
    return {"ok": True, "id": id, "boards": load_boards()}


@capability(
    "mesh.boards.delete", http_method="POST", http_path="/mesh/boards/delete",
    http_tags=["mesh", "boards"], memory="on",
    description="Delete a custom board profile (built-ins can't be deleted). Input: id (str!). "
                "Output: {ok, boards}.",
)
async def cap_mesh_boards_delete(id: str = "", trace_id=None) -> dict:
    if not id:
        return {"error": "id required"}
    if (get_board(id) or {}).get("builtin"):
        return {"error": "cannot delete a built-in profile"}
    custom = _load_json(_BOARDS_CUSTOM, []) or []
    new = [b for b in custom if isinstance(b, dict) and b.get("id") != id]
    if len(new) == len(custom):
        return {"error": "unknown custom board", "id": id}
    try:
        _save_custom(new)
    except Exception as e:
        return {"error": f"delete failed: {e}"}
    return {"ok": True, "boards": load_boards()}


@capability(
    "mesh.display.test", http_method="POST", http_path="/mesh/display/test",
    http_tags=["mesh", "boards"], memory="on",
    description="Draw the display bring-up test pattern on a node (colour bars + border + the live "
                "pin map). ANY output other than a blank white screen proves the parallel bus is "
                "alive — use it to verify/adjust pins for a new board. Input: node_id (str!), "
                "rotation (int optional 0-3). Output: {ok, job_id}.",
)
async def cap_mesh_display_test(node_id: str = "", rotation: int = None, trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    payload: Dict[str, Any] = {"mode": "test"}
    if rotation is not None:
        payload["rotation"] = int(rotation)
    return await _call_cap("mesh.send", node_id=node_id, type="kiosk_set", payload=payload)


@capability(
    "mesh.display.probe", http_method="POST", http_path="/mesh/display/probe",
    http_tags=["mesh", "boards"], memory="on",
    description="Read the display controller's ID over the parallel bus (needs RD wired) to tell "
                "whether the bus is talking at all and which controller it is. The result (via "
                "mesh.jobs) has id_D3/id_04 (Arduino) or ids (MicroPython): 0x..9488=ILI9488, "
                "0x..9486=ILI9486 (different init!), 0x..9341=ILI9341; all 00/FF ⇒ bus not reading. "
                "Input: node_id (str!). Output: {ok, job_id}.",
)
async def cap_mesh_display_probe(node_id: str = "", trace_id=None) -> dict:
    if not node_id:
        return {"error": "node_id required"}
    return await _call_cap("mesh.send", node_id=node_id, type="display_probe", payload={})


log.info("mesh_boards_capabilities ready — %d board profiles", len(load_boards()))
