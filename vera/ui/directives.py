# -*- coding: utf-8 -*-
"""
The control plane - how the LLM drives the UI (UI redesign, Notes/40 section
5.4; the Control and Driven boards).

Panels, widgets, the chat and the canvas are driven through ONE vocabulary of
directives and ONE dispatcher, by two paths: DIRECT - the model issues a
directive as a capability call (`ui.directive`), in its tool loop or inline in
a reply as [[cap:ui.directive {...}]] - and SCRIPTED (vera/ui/scripts.py) -
deterministic rules run the same directives on events, with no model call.
Policy, the log, the ribbon and undo apply to both, and the model sees the
room it changed on its next turn (`ui.room`, the manifest).

The dispatcher: policy check (per target: drive . ask . never; scripts: run .
ask . off) -> apply on the surface (the panel bridge for panels, wherever they
sit; the widget registry; the chat and the LHM through the chat's dispatch
stream, action __ui_directive__; canvas.show through its own capability) ->
a log row (who, path, outcome, note, the undo) -> an event
(ui.directive.applied | refused | asked). Undo reverses a row through the
same surface; a script's run (one run_id) undoes as one.

Keys: vera:ui:directive:log:<sid> (list), vera:ui:directive:ask:<sid> (hash),
vera:ui:policy:<sid> and :global (hash), vera:ui:policy:allow:<sid> (set).
"""
from __future__ import annotations

import fnmatch
import json
import uuid
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (   # noqa: F401
    APP, CAPABILITY_REGISTRY, capability, emit_event, now_iso, register_ui,
)

# ── the vocabulary ────────────────────────────────────────────────────────────
# name -> args (the shape), surface, policy target (what the policy is looked up
# by: 'panel' = the panel id, or a fixed class), undo (how the dispatcher
# reverses it), event, what it does
VOCAB: Dict[str, Dict[str, Any]] = {
    "panel.open":          {"args": "{id, at}",                  "surface": "panels",   "policy": "panel",    "undo": "close",     "event": "panel.opened",           "doc": "open a panel: beside chat . floating . harness tab . on the canvas . in a reply - one set wherever it lands"},
    "panel.dispatch":      {"args": "{id, action, args}",        "surface": "panels",   "policy": "panel",    "undo": "-",         "event": "panel:state",            "doc": "drive a panel through the bridge (handler:arg keys), whoever opened it"},
    "panel.query":         {"args": "{id, what}",                "surface": "panels",   "policy": "always",   "undo": "-",         "event": "-",                      "doc": "read a panel's state, nav or actions (the panel bridge)"},
    "panel.close":         {"args": "{id}",                      "surface": "panels",   "policy": "panel",    "undo": "reopen",    "event": "panel.closed",           "doc": "close it everywhere it shows"},
    "canvas.show":         {"args": "{key}",                     "surface": "canvas",   "policy": "canvas",   "undo": "-",         "event": "canvas.updated",         "doc": "bring an existing item into the NOW band"},
    "canvas.add":          {"args": "{kind, ref, at}",           "surface": "canvas",   "policy": "canvas",   "undo": "remove",    "event": "canvas.updated",         "doc": "a new item through the resolver; on an existing key it becomes canvas.show"},
    "canvas.pin":          {"args": "{key}",                     "surface": "canvas",   "policy": "canvas",   "undo": "park",      "event": "canvas.updated",         "doc": "keep it above the flow"},
    "canvas.park":         {"args": "{key}",                     "surface": "canvas",   "policy": "canvas",   "undo": "pin",       "event": "canvas.updated",         "doc": "put it below the flow"},
    "canvas.size":         {"args": "{key, size}",               "surface": "canvas",   "policy": "canvas",   "undo": "restore",   "event": "canvas.updated",         "doc": "XS . S . M . L . XL"},
    "canvas.ask":          {"args": "{q, choices}",              "surface": "canvas . chat", "policy": "canvas", "undo": "dismiss", "event": "ask.opened",          "doc": "the ask block on the canvas and the answering strip on the composer"},
    "lhm.focus":           {"args": "{menu}",                    "surface": "LHM",      "policy": "always",   "undo": "-",         "event": "-",                      "doc": "open a quick menu (context . loop . sandbox . ...)"},
    "lhm.compose":         {"args": "{menu, widgets}",           "surface": "LHM",      "policy": "ask",      "undo": "restore",   "event": "lhm.menu.saved",         "doc": "set a menu's widgets - what the edit pencil does, by hand or by rule"},
    "widget.place":        {"args": "{template, into, at}",      "surface": "widgets",  "policy": "widgets",  "undo": "remove",    "event": "widget.placed",          "doc": "instantiate a template into a surface's envelope"},
    "widget.update":       {"args": "{id, config}",              "surface": "widgets",  "policy": "widgets",  "undo": "restore",   "event": "widget.updated",         "doc": "change a placement's config"},
    "widget.template.save": {"args": "{from}",                   "surface": "registry", "policy": "widgets",  "undo": "delete",    "event": "widget.template.saved",  "doc": "a configuration becomes a template"},
    "chat.card":           {"args": "{kind, payload}",           "surface": "chat",     "policy": "always",   "undo": "-",         "event": "chat.card",              "doc": "a reply card: council . artifact . image . ask . attachment"},
    "chat.mode":           {"args": "{mode, on}",                "surface": "chat",     "policy": "ask",      "undo": "off",       "event": "chat.mode",              "doc": "set a composer mode (voice . council . diffuse) - asks unless you allowed it"},
    "graph.focus":         {"args": "{turn|step|record}",        "surface": "graph",    "policy": "always",   "undo": "-",         "event": "graph.focused",          "doc": "focus the context graph on a turn, a loop step or a record"},
    "graph.view":          {"args": "{view}",                    "surface": "graph",    "policy": "always",   "undo": "-",         "event": "-",                      "doc": "switch its layout: galaxy . iso . flow . time"},
    "ui.script.run":       {"args": "{name, args}",              "surface": "all",      "policy": "scripts",  "undo": "the run",   "event": "ui.script.ran",          "doc": "run a script as one composite directive"},
    "ui.script.save":      {"args": "{script}",                  "surface": "registry", "policy": "ask",      "undo": "delete",    "event": "ui.script.saved",        "doc": "the model proposes a rule instead of repeating itself"},
}

# the policy in force by default: target pattern -> mode (drive . ask . never; scripts: run . ask . off)
POLICY_DEFAULT: Dict[str, str] = {
    "ops:*": "drive", "notebook:*": "ask", "settings:*": "never",
    "panels": "drive", "canvas": "drive", "widgets": "drive", "scripts": "run",
    "chat.mode": "ask", "lhm.compose": "ask", "ui.script.save": "ask",
}
MODES = ("drive", "ask", "never", "run", "off")
_LOG_KEY = "vera:ui:directive:log:{sid}"
_ASK_KEY = "vera:ui:directive:ask:{sid}"
_POLICY_KEY = "vera:ui:policy:{sid}"
_ALLOW_KEY = "vera:ui:policy:allow:{sid}"
_LOG_MAX = 300
# what the chat applies itself, through its dispatch stream (the pseudo-action __ui_directive__)
_CHAT_APPLIED = ("panel.open", "panel.close", "canvas.add", "canvas.pin", "canvas.park", "canvas.size", "canvas.ask",
                 "lhm.focus", "lhm.compose", "chat.card", "chat.mode", "graph.focus", "graph.view")


def _redis():
    return _orch.REDIS


def _cap(name: str):
    return (CAPABILITY_REGISTRY.get(name) or {}).get("func")


def _to_json_dict(raw) -> Optional[dict]:
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    try:
        v = json.loads(raw)
    except Exception:
        return None
    return v if isinstance(v, dict) else None


# ── policy ────────────────────────────────────────────────────────────────────
async def _policy_for(sid: str) -> Dict[str, str]:
    """global defaults <- the global overrides <- the session's overrides."""
    out = dict(POLICY_DEFAULT)
    r = _redis()
    if not r:
        return out
    for key in (_POLICY_KEY.format(sid="global"), _POLICY_KEY.format(sid=sid)):
        try:
            h = await r.hgetall(key)
        except Exception:
            h = {}
        for k, v in (h or {}).items():
            kk = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
            vv = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
            if vv in MODES:
                out[kk] = vv
    return out


def _policy_target(name: str, args: Dict[str, Any]) -> str:
    v = VOCAB.get(name) or {}
    p = v.get("policy", "always")
    if p == "panel":
        return "panel:" + str(args.get("id") or "")
    if p == "ask":
        return name
    return p


def _resolve_mode(policy: Dict[str, str], name: str, target: str) -> str:
    """The most specific rule wins: an exact target, then a pattern, then the class, then the directive's own class."""
    v = VOCAB.get(name) or {}
    if target.startswith("panel:"):
        pid = target[6:]
        if "panel:" + pid in policy:
            return policy["panel:" + pid]
        for pat, mode in policy.items():
            if pat not in ("panels", "canvas", "widgets", "scripts") and fnmatch.fnmatch(pid, pat):
                return mode
        return policy.get("panels", "drive")
    if v.get("policy") == "ask":
        return policy.get(name, "ask")
    if v.get("policy") == "always":
        return "drive"
    return policy.get(target, "drive")


# ── the log ───────────────────────────────────────────────────────────────────
async def _log(sid: str, row: Dict[str, Any]) -> Dict[str, Any]:
    row.setdefault("id", "d-" + uuid.uuid4().hex[:10])
    row.setdefault("ts", now_iso())
    r = _redis()
    if r:
        try:
            key = _LOG_KEY.format(sid=sid)
            await r.rpush(key, json.dumps(row))
            await r.ltrim(key, -_LOG_MAX, -1)
        except Exception:
            pass
    return row


async def _log_rows(sid: str, limit: int = 100) -> List[Dict[str, Any]]:
    r = _redis()
    if not r:
        return []
    try:
        raw = await r.lrange(_LOG_KEY.format(sid=sid), -max(1, int(limit)), -1)
    except Exception:
        return []
    out = []
    for x in raw or []:
        d = _to_json_dict(x)
        if d:
            out.append(d)
    return out


async def _log_update(sid: str, row_id: str, patch: Dict[str, Any]) -> bool:
    r = _redis()
    if not r:
        return False
    key = _LOG_KEY.format(sid=sid)
    try:
        raw = await r.lrange(key, 0, -1)
    except Exception:
        return False
    for i, x in enumerate(raw or []):
        d = _to_json_dict(x)
        if d and d.get("id") == row_id:
            d.update(patch)
            try:
                await r.lset(key, i, json.dumps(d))
            except Exception:
                return False
            return True
    return False


# ── the surfaces ──────────────────────────────────────────────────────────────
async def _to_chat(sid: str, name: str, args: Dict[str, Any], by: str, row_id: str, timeout: float = 6.0) -> Dict[str, Any]:
    """The chat (and the LHM, the canvas column, the graph in it) applies what only it can; it answers with the outcome."""
    disp = _cap("panel.dispatch")
    if not disp:
        return {"ok": False, "outcome": "unsupported", "note": "panel.dispatch unavailable"}
    try:
        reply = await disp(session_id=sid, action="__ui_directive__",
                           payload={"name": name, "args": args, "by": by, "row": row_id}, timeout_secs=timeout)
    except Exception as e:
        return {"ok": False, "outcome": "failed", "note": "dispatch failed: %s" % e}
    if not isinstance(reply, dict):
        return {"ok": False, "outcome": "failed", "note": "no reply"}
    if not reply.get("ok"):
        err = str(reply.get("error") or "")
        return {"ok": False, "outcome": "failed" if err != "timeout" else "unreachable",
                "note": err or "the chat did not apply it"}
    res = reply.get("result") if isinstance(reply.get("result"), dict) else {}
    return {"ok": True, "outcome": str(res.get("outcome") or "applied"), "note": str(res.get("note") or ""), "result": res}


async def _apply(sid: str, name: str, args: Dict[str, Any], by: str, row_id: str) -> Dict[str, Any]:
    """Apply one directive on its surface. Returns {ok, outcome, note, result?, undo?}."""
    a = args or {}
    if name == "panel.query":
        fn = _cap("panel.query")
        if not fn:
            return {"ok": False, "outcome": "unsupported", "note": "panel.query unavailable"}
        res = await fn(session_id=sid, panel=str(a.get("id") or ""))
        return {"ok": bool(isinstance(res, dict) and res.get("ok", True)), "outcome": "applied", "note": "", "result": res}
    if name == "panel.dispatch":
        fn = _cap("panel.dispatch")
        if not fn:
            return {"ok": False, "outcome": "unsupported", "note": "panel.dispatch unavailable"}
        res = await fn(session_id=sid, action=str(a.get("action") or ""), payload=a.get("args") if isinstance(a.get("args"), dict) else {},
                       panel=str(a.get("id") or ""))
        ok = bool(isinstance(res, dict) and res.get("ok"))
        return {"ok": ok, "outcome": "applied" if ok else "failed", "note": "" if ok else str((res or {}).get("error") or ""), "result": res}
    if name == "canvas.show":
        fn = _cap("canvas.show")
        if fn and str(a.get("key") or "").startswith("cv_"):
            res = await fn(id=str(a.get("key")), session_id=sid, title=str(a.get("title") or ""), pinned=bool(a.get("pinned")))
            ok = bool(isinstance(res, dict) and res.get("ok"))
            return {"ok": ok, "outcome": "applied" if ok else "failed", "note": "" if ok else str((res or {}).get("error") or ""), "result": res}
        # a key on the session canvas: the resolver shows it (canvas.add on an existing key IS show)
        add = _cap("canvas.add")
        if add and a.get("key"):
            res = await add(session_id=sid, key=str(a.get("key")), kind=str(a.get("kind") or ""), content=a.get("content"))
            ok = bool(isinstance(res, dict) and res.get("ok"))
            return {"ok": ok, "outcome": "applied" if ok else "failed", "note": "" if ok else str((res or {}).get("error") or ""), "result": res}
        return await _to_chat(sid, name, a, by, row_id)
    if name in ("canvas.add", "canvas.pin", "canvas.park", "canvas.size", "canvas.remove"):
        # the session canvas's own capabilities (canvas_capabilities.py) when they are registered; the chat applies
        # them otherwise (a page without the resolver still gets the column's answer)
        fn = _cap(name)
        if fn:
            if name == "canvas.add":
                res = await fn(session_id=sid, kind=str(a.get("kind") or ""), content=a.get("content") if a.get("content") is not None else a.get("ref"),
                               key=str(a.get("key") or ""), at=a.get("at"), size=str(a.get("size") or ""), anchor=a.get("anchor") if isinstance(a.get("anchor"), dict) else None)
            elif name == "canvas.size":
                res = await fn(session_id=sid, key=str(a.get("key") or ""), size=str(a.get("size") or ""))
            else:
                res = await fn(session_id=sid, key=str(a.get("key") or ""))
            ok = bool(isinstance(res, dict) and res.get("ok"))
            undo = None
            if ok and name == "canvas.add":
                undo = {"name": "canvas.remove", "args": {"key": (res.get("key") or a.get("key"))}} if not res.get("existing") else None
            elif ok and name == "canvas.pin":
                undo = {"name": "canvas.park", "args": {"key": a.get("key")}}
            elif ok and name == "canvas.park":
                undo = {"name": "canvas.pin", "args": {"key": a.get("key")}}
            elif ok and name == "canvas.size" and res.get("prev"):
                undo = {"name": "canvas.size", "args": {"key": a.get("key"), "size": res.get("prev")}}
            note = "" if ok else str((res or {}).get("error") or "")
            if ok and res.get("resolved") == "shown":
                note = "the key was on the canvas already - shown"
            return {"ok": ok, "outcome": "applied" if ok else "failed", "note": note, "result": res, "undo": undo}
        return await _to_chat(sid, name, a, by, row_id)
    if name == "widget.place":
        fn = _cap("widget.template.instantiate")
        if not fn:
            return {"ok": False, "outcome": "unsupported", "note": "widget registry unavailable"}
        res = await fn(id=str(a.get("template") or ""), where=str(a.get("into") or "dashboard"), host=str(a.get("host") or ("main" if a.get("into", "dashboard") == "dashboard" else "")),
                       config=a.get("config") if isinstance(a.get("config"), dict) else {}, session_id=sid)
        ok = bool(isinstance(res, dict) and res.get("ok"))
        undo = {"name": "widget.remove", "args": {"id": (res.get("instance") or {}).get("id")}} if ok else None
        return {"ok": ok, "outcome": "applied" if ok else "failed", "note": "" if ok else str((res or {}).get("error") or ""), "result": res, "undo": undo}
    if name == "widget.remove":
        fn = _cap("widget.instance.remove")
        res = await fn(id=str(a.get("id") or "")) if fn else {"ok": False, "error": "widget registry unavailable"}
        ok = bool(isinstance(res, dict) and res.get("ok"))
        return {"ok": ok, "outcome": "applied" if ok else "failed", "note": "" if ok else str(res.get("error") or ""), "result": res}
    if name == "widget.update":
        r = _redis()
        iid = str(a.get("id") or "")
        if not r or not iid:
            return {"ok": False, "outcome": "failed", "note": "instance id required"}
        key = "vera:ui:widget:inst:" + iid
        inst = _to_json_dict(await r.get(key))
        if not inst:
            return {"ok": False, "outcome": "failed", "note": "no instance %s" % iid}
        prev = dict(inst.get("config") or {})
        inst["config"] = dict(prev, **(a.get("config") if isinstance(a.get("config"), dict) else {}))
        await r.set(key, json.dumps(inst))
        return {"ok": True, "outcome": "applied", "note": "", "result": {"instance": inst}, "undo": {"name": "widget.update", "args": {"id": iid, "config": prev, "_replace": True}}}
    if name == "widget.template.save":
        fn = _cap("widget.template.save")
        res = await fn(template=a.get("from") if isinstance(a.get("from"), dict) else a, force=bool(a.get("force"))) if fn else {"ok": False, "error": "widget registry unavailable"}
        ok = bool(isinstance(res, dict) and res.get("ok"))
        undo = {"name": "widget.template.delete", "args": {"id": (res.get("template") or {}).get("id")}} if ok else None
        return {"ok": ok, "outcome": "applied" if ok else "failed", "note": "" if ok else str((res or {}).get("error") or "; ".join(res.get("problems") or [])), "result": res, "undo": undo}
    if name == "widget.template.delete":
        fn = _cap("widget.template.delete")
        res = await fn(id=str(a.get("id") or "")) if fn else {"ok": False, "error": "widget registry unavailable"}
        ok = bool(isinstance(res, dict) and res.get("ok"))
        return {"ok": ok, "outcome": "applied" if ok else "failed", "note": "" if ok else str(res.get("error") or ""), "result": res}
    if name == "ui.script.run":
        fn = _cap("ui.script.run")
        if not fn:
            return {"ok": False, "outcome": "unsupported", "note": "scripts unavailable"}
        res = await fn(name=str(a.get("name") or ""), args=a.get("args") if isinstance(a.get("args"), dict) else {}, session_id=sid, issued_by=by)
        ok = bool(isinstance(res, dict) and res.get("ok"))
        return {"ok": ok, "outcome": "applied" if ok else "failed", "note": "" if ok else str((res or {}).get("error") or ""), "result": res,
                "undo": {"name": "ui.script.undo", "args": {"run_id": (res or {}).get("run_id")}} if ok else None}
    if name == "ui.script.save":
        fn = _cap("ui.script.save")
        res = await fn(script=a.get("script") if isinstance(a.get("script"), dict) else a, issued_by=by) if fn else {"ok": False, "error": "scripts unavailable"}
        ok = bool(isinstance(res, dict) and res.get("ok"))
        undo = {"name": "ui.script.delete", "args": {"name": (res.get("script") or {}).get("name")}} if ok else None
        return {"ok": ok, "outcome": "applied" if ok else "failed", "note": "" if ok else str((res or {}).get("error") or ""), "result": res, "undo": undo}
    if name == "ui.script.delete":
        fn = _cap("ui.script.delete")
        res = await fn(name=str(a.get("name") or "")) if fn else {"ok": False, "error": "scripts unavailable"}
        ok = bool(isinstance(res, dict) and res.get("ok"))
        return {"ok": ok, "outcome": "applied" if ok else "failed", "note": "" if ok else str(res.get("error") or ""), "result": res}
    if name in _CHAT_APPLIED:
        res = await _to_chat(sid, name, a, by, row_id)
        if res.get("ok"):
            undo = None
            if name == "panel.open":
                undo = {"name": "panel.close", "args": {"id": a.get("id")}}
            elif name == "panel.close":
                undo = {"name": "panel.open", "args": {"id": a.get("id")}}
            elif name == "canvas.pin":
                undo = {"name": "canvas.park", "args": {"key": a.get("key")}}
            elif name == "canvas.park":
                undo = {"name": "canvas.pin", "args": {"key": a.get("key")}}
            elif name == "canvas.size" and (res.get("result") or {}).get("prev"):
                undo = {"name": "canvas.size", "args": {"key": a.get("key"), "size": (res.get("result") or {}).get("prev")}}
            elif name == "chat.mode":
                undo = {"name": "chat.mode", "args": {"mode": a.get("mode"), "on": not bool(a.get("on", True))}}
            res["undo"] = undo
        return res
    return {"ok": False, "outcome": "unsupported", "note": "no surface applies %s" % name}


# ── the dispatcher ────────────────────────────────────────────────────────────
async def dispatch(name: str, args: Optional[dict], session_id: str, issued_by: str = "model",
                   run_id: str = "", policy_override: str = "") -> Dict[str, Any]:
    name = str(name or "").strip()
    a = dict(args) if isinstance(args, dict) else {}
    sid = str(session_id or "").strip()
    if name not in VOCAB and name not in ("widget.remove", "widget.template.delete", "ui.script.delete", "ui.script.undo"):
        return {"ok": False, "outcome": "refused", "error": "unknown directive %r" % name, "vocabulary": sorted(VOCAB)}
    if not sid:
        return {"ok": False, "outcome": "refused", "error": "session_id is required"}
    by = str(issued_by or "model")
    path = "script:" + by[7:] if by.startswith("script:") else ("undo" if by == "undo" else ("you" if by in ("user", "you") else "direct"))
    # the manifest grounds the direct path: canvas.add on a key that exists becomes canvas.show, and is reported
    rewrite = ""
    if name == "canvas.add" and a.get("key") and a.get("kind") not in (None, "", "new"):
        rows = [x for x in await _log_rows(sid, 200) if x.get("name") in ("canvas.add", "canvas.show") and (x.get("args") or {}).get("key") == a.get("key") and x.get("outcome") == "applied"]
        if rows:
            rewrite = "canvas.add on an existing key -> canvas.show"
            name = "canvas.show"
    policy = await _policy_for(sid)
    target = _policy_target(name, a)
    mode = policy_override or _resolve_mode(policy, name, target)
    row: Dict[str, Any] = {"id": "d-" + uuid.uuid4().hex[:10], "ts": now_iso(), "by": by, "path": path, "name": name, "args": a,
                           "target": target, "policy": mode, "run_id": run_id or "", "outcome": "", "note": "", "event": VOCAB.get(name, {}).get("event", "-")}
    if rewrite:
        row["note"] = rewrite
    # ask: has the user allowed this always?
    if mode == "ask" and by not in ("user", "you", "undo"):
        r = _redis()
        allowed = False
        if r:
            try:
                allowed = bool(await r.sismember(_ALLOW_KEY.format(sid=sid), target)) or bool(await r.sismember(_ALLOW_KEY.format(sid=sid), name))
            except Exception:
                allowed = False
        if allowed:
            mode = "drive"
            row["note"] = (row["note"] + " . " if row["note"] else "") + "allowed always"
    if mode in ("never", "off") and by not in ("user", "you", "undo"):
        row["outcome"] = "refused"
        row["note"] = (row["note"] + " . " if row["note"] else "") + "policy %s for %s" % (mode, target)
        await _log(sid, row)
        await emit_event({"type": "ui.directive.refused", "session_id": sid, "name": name, "by": by, "target": target, "row": row["id"]})
        return {"ok": False, "outcome": "refused", "error": row["note"], "row": row}
    if mode == "ask" and by not in ("user", "you", "undo"):
        row["outcome"] = "asked"
        row["note"] = (row["note"] + " . " if row["note"] else "") + "policy ask -> the ask chip"
        await _log(sid, row)
        r = _redis()
        if r:
            try:
                await r.hset(_ASK_KEY.format(sid=sid), row["id"], json.dumps(row))
            except Exception:
                pass
        await emit_event({"type": "ui.directive.asked", "session_id": sid, "name": name, "by": by, "target": target, "row": row["id"]})
        # the chip lands in the chat (best effort; the chat also polls the asks)
        try:
            await _to_chat(sid, "ui.ask", {"row": row}, by, row["id"], timeout=2.0)
        except Exception:
            pass
        return {"ok": False, "outcome": "asked", "ask_id": row["id"], "row": row,
                "hint": "waiting for the user: ui.directive.answer(ask_id, allow_once | allow_always | refuse)"}
    res = await _apply(sid, name, a, by, row["id"])
    row["outcome"] = res.get("outcome") or ("applied" if res.get("ok") else "failed")
    row["note"] = (row["note"] + " . " if row["note"] else "") + str(res.get("note") or "")
    if res.get("undo"):
        row["undo"] = res["undo"]
    await _log(sid, row)
    ev = "ui.directive.applied" if res.get("ok") else "ui.directive.failed"
    await emit_event({"type": ev, "session_id": sid, "name": name, "by": by, "path": path, "target": target, "row": row["id"], "outcome": row["outcome"]})
    out = {"ok": bool(res.get("ok")), "outcome": row["outcome"], "row": row}
    if res.get("result") is not None:
        out["result"] = res["result"]
    if not res.get("ok"):
        out["error"] = row["note"] or row["outcome"]
    return out


# ── capabilities ──────────────────────────────────────────────────────────────
@capability(
    "ui.directive", memory="off",
    http_method="POST", http_path="/ui/directive", http_tags=["ui", "control"],
    description="Drive the UI with one directive - the DIRECT path of the control plane. Inputs: name "
                "(str! - one of the vocabulary: panel.open/dispatch/query/close, canvas.show/add/pin/park/"
                "size/ask, lhm.focus/compose, widget.place/update/template.save, chat.card/mode, graph.focus/"
                "view, ui.script.run/save), args (object - the directive's args, e.g. panel.open {id, at}; "
                "lhm.focus {menu}; widget.place {template, into, at}), session_id (str! - the chat session; "
                "usually the trace_id of the calling turn). Policy is checked first (drive . ask . never): an "
                "ask returns {outcome:'asked', ask_id} and the user's chip decides; never returns refused. "
                "Output: {ok, outcome: applied|asked|refused|failed|unsupported, row:{id, by, path, name, "
                "args, policy, outcome, note, undo}, result?}. Every applied directive shows in ui.room on "
                "the next turn; undo with ui.directive.undo(row id).")
async def cap_ui_directive(name: str = "", args: Optional[dict] = None, session_id: str = "", issued_by: str = "model", run_id: str = "", trace_id=None):
    return await dispatch(name, args, session_id or trace_id or "", issued_by=issued_by or "model", run_id=run_id or "")


@capability(
    "ui.directive.vocab", memory="off", silent=True,
    http_method="GET", http_path="/ui/directive/vocab", http_tags=["ui", "control"],
    description="The control plane's vocabulary: every directive with its args, surface, policy target, undo, "
                "event and what it does; and the default policy. Output: {ok, directives:[...], policy_default}.")
async def cap_ui_directive_vocab(trace_id=None):
    return {"ok": True, "directives": [dict(v, name=k) for k, v in VOCAB.items()], "policy_default": POLICY_DEFAULT, "modes": MODES}


@capability(
    "ui.directive.log", memory="off", silent=True,
    http_method="GET", http_path="/ui/directive/log", http_tags=["ui", "control"],
    description="The directive log of a session - the model's, a script's and the user's own moves, one log: "
                "who, path (direct . script:name . you . undo), directive, policy, outcome, note, undo. Inputs: "
                "session_id (str!), limit (int, default 100). Output: {ok, rows:[...], asks:[pending asks]}.")
async def cap_ui_directive_log(session_id: str = "", limit: int = 100, trace_id=None):
    sid = (session_id or trace_id or "").strip()
    rows = await _log_rows(sid, limit)
    asks = []
    r = _redis()
    if r:
        try:
            h = await r.hgetall(_ASK_KEY.format(sid=sid))
            for v in (h or {}).values():
                d = _to_json_dict(v)
                if d:
                    asks.append(d)
        except Exception:
            pass
    asks.sort(key=lambda d: d.get("ts") or "")
    return {"ok": True, "session_id": sid, "rows": rows, "asks": asks}


@capability(
    "ui.directive.answer", memory="off",
    http_method="POST", http_path="/ui/directive/answer", http_tags=["ui", "control"],
    description="Answer an ask: the user's chip on a directive the policy held. Inputs: session_id (str!), "
                "ask_id (str! - the log row), answer (str! - allow_once | allow_always | refuse). allow_always "
                "also drives the same target without asking for the rest of the session. Output: the dispatch "
                "result for an allow, {ok, outcome:'refused'} for a refusal.")
async def cap_ui_directive_answer(session_id: str = "", ask_id: str = "", answer: str = "", trace_id=None):
    sid = (session_id or trace_id or "").strip()
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}
    raw = await r.hget(_ASK_KEY.format(sid=sid), ask_id)
    row = _to_json_dict(raw)
    if not row:
        return {"ok": False, "error": "no pending ask %r" % ask_id}
    await r.hdel(_ASK_KEY.format(sid=sid), ask_id)
    ans = str(answer or "").strip().lower()
    if ans == "refuse":
        await _log_update(sid, ask_id, {"outcome": "refused", "note": (row.get("note") or "") + " . you refused"})
        await emit_event({"type": "ui.directive.refused", "session_id": sid, "name": row.get("name"), "by": row.get("by"), "row": ask_id, "answer": "refuse"})
        return {"ok": False, "outcome": "refused", "row": row}
    if ans == "allow_always":
        try:
            await r.sadd(_ALLOW_KEY.format(sid=sid), row.get("target") or row.get("name"))
        except Exception:
            pass
    await _log_update(sid, ask_id, {"outcome": "allowed", "note": (row.get("note") or "") + " . you allowed " + ("always" if ans == "allow_always" else "once")})
    return await dispatch(row.get("name"), row.get("args"), sid, issued_by=row.get("by") or "model", run_id=row.get("run_id") or "", policy_override="drive")


@capability(
    "ui.directive.undo", memory="off",
    http_method="POST", http_path="/ui/directive/undo", http_tags=["ui", "control"],
    description="Undo a directive for the session: the dispatcher reverses the row's effect through the same "
                "surface (close what it opened, park what it pinned, restore the size, remove the placement); a "
                "script's whole run undoes as one when the row is its run. Inputs: session_id (str!), row_id "
                "(str!). Output: {ok, undone:[rows]}.")
async def cap_ui_directive_undo(session_id: str = "", row_id: str = "", trace_id=None):
    sid = (session_id or trace_id or "").strip()
    rows = await _log_rows(sid, _LOG_MAX)
    row = next((x for x in rows if x.get("id") == row_id), None)
    if not row:
        return {"ok": False, "error": "no row %r" % row_id}
    targets = [row]
    if row.get("run_id") and row.get("name") == "ui.script.run":
        targets = [x for x in rows if x.get("run_id") == row.get("run_id") and x is not row and x.get("outcome") == "applied"]
    undone = []
    for t in reversed(targets):
        u = t.get("undo")
        if not u or t.get("undone"):
            continue
        if u.get("name") == "ui.script.undo":
            continue
        res = await dispatch(u["name"], u.get("args") or {}, sid, issued_by="undo", run_id=t.get("run_id") or "")
        if res.get("ok"):
            await _log_update(sid, t["id"], {"undone": True})
            undone.append({"id": t["id"], "name": t["name"], "via": u["name"]})
    if row.get("name") == "ui.script.run" and undone:
        await _log_update(sid, row["id"], {"undone": True})
    return {"ok": bool(undone), "undone": undone, "error": None if undone else "nothing to undo on that row"}


@capability(
    "ui.policy.get", memory="off", silent=True,
    http_method="GET", http_path="/ui/policy", http_tags=["ui", "control"],
    description="The policy in force for a session: target pattern -> drive . ask . never (scripts: run . ask . off); "
                "defaults, then the global overrides, then the session's. Inputs: session_id (str). Output: {ok, policy, allowed_always}.")
async def cap_ui_policy_get(session_id: str = "", trace_id=None):
    sid = (session_id or trace_id or "").strip()
    pol = await _policy_for(sid)
    allowed = []
    r = _redis()
    if r and sid:
        try:
            allowed = sorted(x.decode() if isinstance(x, (bytes, bytearray)) else str(x) for x in await r.smembers(_ALLOW_KEY.format(sid=sid)))
        except Exception:
            allowed = []
    return {"ok": True, "policy": pol, "allowed_always": allowed, "modes": MODES}


@capability(
    "ui.policy.set", memory="off",
    http_method="POST", http_path="/ui/policy/set", http_tags=["ui", "control"],
    description="Set a policy rule. Inputs: target (str! - 'ops:*', 'notebook:*', 'panel:<id>', 'canvas', 'widgets', "
                "'scripts', 'chat.mode', ...), mode (str! - drive . ask . never; for scripts run . ask . off), "
                "session_id (str - the session; omit for the global rule). Output: {ok, policy}.")
async def cap_ui_policy_set(target: str = "", mode: str = "", session_id: str = "", trace_id=None):
    t, m = str(target or "").strip(), str(mode or "").strip().lower()
    if not t or m not in MODES:
        return {"ok": False, "error": "target and a mode (%s) are required" % ", ".join(MODES)}
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}
    sid = (session_id or "").strip() or "global"
    await r.hset(_POLICY_KEY.format(sid=sid), t, m)
    await emit_event({"type": "ui.policy.set", "session_id": sid, "target": t, "mode": m})
    return {"ok": True, "policy": await _policy_for(sid if sid != "global" else "")}


@capability(
    "ui.event", memory="off", silent=True,
    http_method="POST", http_path="/ui/event", http_tags=["ui", "control"],
    description="A UI surface reports a moment the scripts can act on: loop.step.waiting, panel.opened, "
                "canvas.item.added, widget.value, session.start. Inputs: type (str!), payload (object), "
                "session_id (str). Emitted as 'ui.<type>' on the event stream. Output: {ok, type}.")
async def cap_ui_event(type: str = "", payload: Optional[dict] = None, session_id: str = "", trace_id=None):
    t = str(type or "").strip()
    if not t:
        return {"ok": False, "error": "type is required"}
    ev = {"type": "ui." + t if not t.startswith("ui.") else t, "session_id": (session_id or trace_id or "").strip()}
    ev.update({k: v for k, v in (payload or {}).items() if k not in ("type", "ts")} if isinstance(payload, dict) else {})
    await emit_event(ev)
    return {"ok": True, "type": ev["type"]}


@capability(
    "ui.room", memory="off", silent=True,
    http_method="GET", http_path="/ui/room", http_tags=["ui", "control"],
    description="The room manifest - what the model sees in every turn's context: the open panels (the one set - "
                "id, placement, origin, drivable), the placed widgets, the scripts armed, the policy in force, the "
                "asks pending and the last directives. Inputs: session_id (str!). Output: {ok, room:{...}, text}.")
async def cap_ui_room(session_id: str = "", trace_id=None):
    sid = (session_id or trace_id or "").strip()
    room: Dict[str, Any] = {"session_id": sid, "panels": [], "widgets": [], "scripts": [], "policy": {}, "asks": [], "recent": []}
    fn = _cap("ui.panels.open")
    if fn:
        try:
            res = await fn(session_id=sid)
            room["panels"] = [p for p in (res or {}).get("panels") or [] if isinstance(p, dict)]
        except Exception:
            pass
    fn = _cap("widget.instance.list")
    if fn:
        try:
            res = await fn(session_id=sid)
            room["widgets"] = [dict(i) for i in (res or {}).get("instances") or []][:20]
        except Exception:
            pass
    fn = _cap("ui.script.list")
    if fn:
        try:
            res = await fn()
            room["scripts"] = [{"name": s.get("name"), "on": (s.get("on") or {}).get("event"), "state": s.get("state")} for s in (res or {}).get("scripts") or [] if s.get("state") == "on"]
        except Exception:
            pass
    fn = _cap("canvas.session.room")
    if fn:
        try:
            res = await fn(session_id=sid)
            if isinstance(res, dict) and res.get("ok"):
                room["canvas"] = {k: res.get(k) for k in ("id", "revision", "now", "pinned", "parked", "sizes", "count")}
        except Exception:
            pass
    room["policy"] = await _policy_for(sid)
    log = await cap_ui_directive_log(session_id=sid, limit=8)
    room["asks"] = log.get("asks") or []
    room["recent"] = [{"by": x.get("by"), "name": x.get("name"), "outcome": x.get("outcome")} for x in log.get("rows") or []]
    # the text the chat puts in the prompt
    lines = ["## the room"]
    if room["panels"]:
        lines.append("panels: " + " . ".join("%s (%s . %s%s)" % (p.get("id"), p.get("placement") or p.get("host"), p.get("origin") or "you", " . drivable" if p.get("host") in ("chat", "harness") else "") for p in room["panels"]))
    else:
        lines.append("panels: none open")
    if room["widgets"]:
        lines.append("widgets: " + " . ".join("%s (%s . %s)" % (w.get("name") or w.get("template"), w.get("form"), w.get("where")) for w in room["widgets"]))
    if room.get("canvas"):
        cv = room["canvas"]
        lines.append("canvas: %s rev %s . now %s . pinned %s . parked %s" % (
            cv.get("id"), cv.get("revision"),
            " ".join(cv.get("now") or []) or "-", " ".join(cv.get("pinned") or []) or "-", " ".join(cv.get("parked") or []) or "-"))
    if room["scripts"]:
        lines.append("scripts armed: " + " . ".join(str(s.get("name")) for s in room["scripts"]))
    pol = room["policy"]
    lines.append("policy: " + " . ".join("%s %s" % (k, v) for k, v in pol.items() if k in ("ops:*", "notebook:*", "settings:*", "canvas", "widgets", "scripts")))
    if room["asks"]:
        lines.append("waiting on you: " + " . ".join("%s %s" % (a.get("name"), json.dumps(a.get("args") or {})[:60]) for a in room["asks"]))
    if room["recent"]:
        lines.append("last directives: " + " . ".join("%s %s -> %s" % (x["by"], x["name"], x["outcome"]) for x in room["recent"][-5:]))
    lines.append("directives: [[cap:ui.directive {\"name\":\"panel.open\",\"args\":{\"id\":\"<panel id>\"}}]] - one vocabulary, policy drive . ask . never; ui.room shows the room you changed on the next turn")
    return {"ok": True, "room": room, "text": "\n".join(lines)}


# ── the Driven panel (the Driven board): the directive log with its outcomes and undo, the policy in force,
# the scripts armed, the asks pending - the control plane's own window ──────────────────────────────────────
_DRIVEN_HTML = """
<div style="height:100%;display:flex;flex-direction:column">
  <iframe src="/ui/driven" style="flex:1;border:none;width:100%;background:transparent"></iframe>
</div>
"""


@APP.get("/ui/driven", include_in_schema=False)
async def _driven_page():
    """The Driven panel as a standalone document (the harness tab and the chat's beside-panel mount it as an iframe)."""
    from fastapi.responses import HTMLResponse
    from pathlib import Path
    p = Path(__file__).parent / "driven_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:#c96b6b'>driven_panel.html not found</p>")


# mode="element": listed for the picker, the chat's Panels list and a beside-panel; opened where it is needed.
register_ui("driven", "Driven", "\u27e1", _DRIVEN_HTML, js="",
            ui_caps=["ui.directive.log", "ui.directive.undo", "ui.directive.answer", "ui.policy.get", "ui.policy.set",
                     "ui.script.list", "ui.script.enable", "ui.room"],
            mode="element", tab_order=64)
