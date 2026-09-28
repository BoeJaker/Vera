# -*- coding: utf-8 -*-
"""
UI scripts - the SCRIPTED path of the control plane (UI redesign, Notes/40
section 5.4; the Control board). A script is a deterministic record:

    on <event> [match {k: v}]  if <expression over the event and the room>
    do [directives]  else [directives]

versioned, enabled or not, run by the same dispatcher as the model's own
directives (vera/ui/directives.py) with no model call, marked `script . name`
in the log and on the ribbon. Triggers are the events on the stream
(vera:events): cap.ok / cap.error, the UI's own reports (ui.loop.step.waiting,
ui.panel.opened, ui.canvas.item.added, ui.widget.value, ui.session.start -
see ui.event), ui.directive.refused, and a timer (HH:MM, once a day).

Authored three ways: shipped (loop-waits, image-to-card, refused-to-ask),
yours (recorded from your own moves), or proposed by the model
(ui.script.save through the dispatcher lands as an ask). The model can call a
script as a composite directive (ui.script.run).

Records live in Redis (vera:ui:script:<name>); the shipped ones are built in
and can be shadowed. A runner tick (every 2 s, one instance) reads new events
and fires the armed scripts. The expression language is deliberately small: a
Python expression restricted to comparisons, and/or/not, arithmetic, names
(event, room, value, result, args), attribute and item access, literals, and
the helpers open(panel id) / has(x).
"""
from __future__ import annotations

import ast
import datetime as _dt
import json
import re
import uuid
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (   # noqa: F401
    APP, CAPABILITY_REGISTRY, capability, emit_event, now_iso, schedule,
)

_KEY = "vera:ui:script:{name}"
_GLOB = "vera:ui:script:*"
STATES = ("on", "off", "asked")
TRIGGERS = ("cap.ok", "cap.error", "ui.loop.step.waiting", "ui.panel.opened", "ui.canvas.item.added", "ui.widget.value",
            "ui.session.start", "ui.directive.refused", "ui.directive.applied", "timer")

SHIPPED: List[Dict[str, Any]] = [
    {"name": "loop-waits", "version": 3, "by": "vera", "state": "on",
     "on": {"event": "ui.loop.step.waiting", "match": {}}, "if": "True",
     "do": [{"name": "canvas.ask", "args": {"q": "{event.question}", "choices": "{event.choices}", "run": "{event.run}"}},
            {"name": "lhm.focus", "args": {"menu": "loop"}}],
     "else": [], "doc": "step waits on you: the ask block on the canvas, the loop menu - no model call"},
    {"name": "image-to-card", "version": 1, "by": "vera", "state": "on",
     "on": {"event": "cap.ok", "match": {"group": "media"}}, "if": "'image' in str(event.preview or '') or 'png' in str(event.preview or '')",
     "do": [{"name": "chat.card", "args": {"kind": "image", "payload": {"cap": "{event.name}", "preview": "{event.preview}"}}}],
     "else": [], "doc": "an image result became an artifact card in the reply"},
    {"name": "refused-to-ask", "version": 1, "by": "vera", "state": "on",
     "on": {"event": "ui.directive.refused", "match": {}}, "if": "event.by != 'undo'",
     "do": [{"name": "chat.card", "args": {"kind": "ask", "payload": {"what": "{event.name} was refused by policy for {event.target}", "row": "{event.row}"}}}],
     "else": [], "doc": "a refused directive is told to you as an ask card, not lost"},
]


def _redis():
    return _orch.REDIS


def _cap(name: str):
    return (CAPABILITY_REGISTRY.get(name) or {}).get("func")


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")
    return s[:48]


# ── records ───────────────────────────────────────────────────────────────────
def normalise(s: Dict[str, Any]) -> Dict[str, Any]:
    on = s.get("on") if isinstance(s.get("on"), dict) else {"event": str(s.get("on") or "")}
    do = s.get("do") if isinstance(s.get("do"), list) else []
    els = s.get("else") if isinstance(s.get("else"), list) else []
    step = lambda d: {"name": str(d.get("name") or ""), "args": d.get("args") if isinstance(d.get("args"), dict) else {}} if isinstance(d, dict) else {"name": str(d), "args": {}}
    return {
        "name": _slug(s.get("name") or ""),
        "version": int(s.get("version") or 1),
        "by": str(s.get("by") or "you")[:24],
        "state": str(s.get("state") or "on") if str(s.get("state") or "on") in STATES else "on",
        "on": {"event": str(on.get("event") or "")[:64], "match": on.get("match") if isinstance(on.get("match"), dict) else {}, "at": str(on.get("at") or "")[:8]},
        "if": str(s.get("if") or "True")[:400],
        "do": [step(d) for d in do][:12],
        "else": [step(d) for d in els][:12],
        "doc": str(s.get("doc") or "")[:300],
        "created_at": str(s.get("created_at") or ""), "updated_at": str(s.get("updated_at") or ""),
        "last_run": str(s.get("last_run") or ""), "runs": int(s.get("runs") or 0),
    }


def problems(s: Dict[str, Any]) -> List[str]:
    out = []
    if not s.get("name"):
        out.append("a script needs a name")
    ev = (s.get("on") or {}).get("event")
    if not ev:
        out.append("a script needs an event to run on (on.event)")
    elif ev not in TRIGGERS and not ev.startswith("ui.") and not ev.startswith("cap."):
        out.append("unknown trigger %r (one of: %s, or any ui.* / cap.* event)" % (ev, ", ".join(TRIGGERS)))
    if ev == "timer" and not re.match(r"^\d{2}:\d{2}$", (s.get("on") or {}).get("at") or ""):
        out.append("a timer script needs on.at as HH:MM")
    if not s.get("do"):
        out.append("a script needs at least one directive to do")
    try:
        compile_expr(s.get("if") or "True")
    except Exception as e:
        out.append("the if-expression does not parse: %s" % e)
    for d in (s.get("do") or []) + (s.get("else") or []):
        if not d.get("name"):
            out.append("a do-step needs a directive name")
    return out


# ── the expression language ───────────────────────────────────────────────────
_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
            ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot, ast.Name, ast.Load, ast.Constant, ast.Attribute, ast.Subscript,
            ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Call, ast.List, ast.Tuple, ast.Dict, ast.IfExp)
_NAMES = {"event", "room", "value", "result", "args", "True", "False", "None", "open", "has", "str", "int", "float", "len", "lower"}


class _Box:
    """dict-or-value with attribute access that never raises: event.result.re_embeds -> None when absent."""
    def __init__(self, v):
        self._v = v

    def __getattr__(self, k):
        v = self._v
        if isinstance(v, dict):
            return _Box(v.get(k))
        return _Box(None)

    def __getitem__(self, k):
        v = self._v
        try:
            return _Box(v[k])
        except Exception:
            return _Box(None)

    def _raw(self):
        return self._v

    def __bool__(self):
        return bool(self._v)

    def __eq__(self, o):
        return self._v == (o._v if isinstance(o, _Box) else o)

    def __ne__(self, o):
        return not self.__eq__(o)

    def __lt__(self, o):
        return _num(self._v) < _num(o._v if isinstance(o, _Box) else o)

    def __le__(self, o):
        return _num(self._v) <= _num(o._v if isinstance(o, _Box) else o)

    def __gt__(self, o):
        return _num(self._v) > _num(o._v if isinstance(o, _Box) else o)

    def __ge__(self, o):
        return _num(self._v) >= _num(o._v if isinstance(o, _Box) else o)

    def __contains__(self, o):
        v = self._v
        try:
            return (o._v if isinstance(o, _Box) else o) in v
        except Exception:
            return False

    def __str__(self):
        return "" if self._v is None else str(self._v)

    def __hash__(self):
        return hash(json.dumps(self._v, default=str))

    def __add__(self, o): return _num(self._v) + _num(o._v if isinstance(o, _Box) else o)
    def __sub__(self, o): return _num(self._v) - _num(o._v if isinstance(o, _Box) else o)
    def __mul__(self, o): return _num(self._v) * _num(o._v if isinstance(o, _Box) else o)
    def __truediv__(self, o): return _num(self._v) / (_num(o._v if isinstance(o, _Box) else o) or 1)
    def __radd__(self, o): return _num(o) + _num(self._v)
    def __rsub__(self, o): return _num(o) - _num(self._v)


def _num(v):
    if isinstance(v, _Box):
        v = v._v
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v))
    except Exception:
        return 0


def compile_expr(expr: str):
    tree = ast.parse(expr or "True", mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError("not allowed in a script expression: %s" % type(node).__name__)
        if isinstance(node, ast.Name) and node.id not in _NAMES:
            raise ValueError("unknown name %r (use event, room, value, result, args)" % node.id)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in ("open", "has", "str", "int", "float", "len", "lower"):
                raise ValueError("only open(), has(), str(), int(), float(), len(), lower() may be called")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("private attributes are not allowed")
    return compile(tree, "<script>", "eval")


def evaluate(expr: str, event: Dict[str, Any], room: Dict[str, Any], args: Optional[Dict[str, Any]] = None) -> bool:
    code = compile_expr(expr)
    panels = [p for p in (room or {}).get("panels") or []]
    ns = {
        "event": _Box(event), "room": _Box(room), "value": _Box((event or {}).get("value")), "result": _Box((event or {}).get("result")),
        "args": _Box(args or {}),
        "open": lambda pid: any(str(p.get("id")) == str(pid) or (str(pid).endswith("*") and str(p.get("id")).startswith(str(pid)[:-1])) for p in panels),
        "has": lambda x: bool(x._v if isinstance(x, _Box) else x),
        "str": lambda x: str(x._v if isinstance(x, _Box) else x), "int": lambda x: int(_num(x)), "float": lambda x: float(_num(x)),
        "len": lambda x: len(x._v if isinstance(x, _Box) else x) if (x._v if isinstance(x, _Box) else x) is not None else 0,
        "lower": lambda x: str(x._v if isinstance(x, _Box) else x).lower(),
        "True": True, "False": False, "None": None, "__builtins__": {},
    }
    return bool(eval(code, ns, {}))


def _fill(v: Any, event: Dict[str, Any], args: Dict[str, Any]) -> Any:
    """'{event.question}' / '{args.node}' placeholders in a directive's args, filled from the event and the call."""
    if isinstance(v, str):
        m = re.fullmatch(r"\{(event|args)\.([a-zA-Z0-9_.]+)\}", v.strip())
        if m:
            src = event if m.group(1) == "event" else args
            cur: Any = src
            for part in m.group(2).split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            return cur
        return re.sub(r"\{(event|args)\.([a-zA-Z0-9_.]+)\}", lambda mm: str(_dig(event if mm.group(1) == "event" else args, mm.group(2))), v)
    if isinstance(v, dict):
        return {k: _fill(x, event, args) for k, x in v.items()}
    if isinstance(v, list):
        return [_fill(x, event, args) for x in v]
    return v


def _dig(d: Any, path: str) -> Any:
    cur = d
    for part in path.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return "" if cur is None else cur


# ── storage ───────────────────────────────────────────────────────────────────
async def _saved() -> Dict[str, Dict[str, Any]]:
    r = _redis()
    out: Dict[str, Dict[str, Any]] = {}
    if not r:
        return out
    try:
        async for key in r.scan_iter(match=_GLOB, count=200):
            raw = await r.get(key)
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "replace")
            try:
                s = normalise(json.loads(raw))
                out[s["name"]] = s
            except Exception:
                continue
    except Exception:
        return out
    return out


async def _all() -> List[Dict[str, Any]]:
    saved = await _saved()
    out = list(saved.values())
    for s in SHIPPED:
        if s["name"] not in saved:
            n = normalise(dict(s))
            n["shipped"] = True
            out.append(n)
    out.sort(key=lambda s: (0 if s.get("by") == "vera" else 1, s["name"]))
    return out


async def _get(name: str) -> Optional[Dict[str, Any]]:
    for s in await _all():
        if s["name"] == name:
            return s
    return None


async def _put(s: Dict[str, Any]) -> bool:
    r = _redis()
    if not r:
        return False
    try:
        await r.set(_KEY.format(name=s["name"]), json.dumps(s))
    except Exception:
        return False
    return True


# ── the run ───────────────────────────────────────────────────────────────────
async def run_script(s: Dict[str, Any], event: Dict[str, Any], session_id: str, args: Optional[Dict[str, Any]] = None,
                     issued_by: str = "", dry_run: bool = False) -> Dict[str, Any]:
    """Evaluate the script against the event and the room, then dispatch its do (or else) steps as one run."""
    from importlib import import_module  # noqa: F401  (the dispatcher is reached through the registry, not imported)
    args = args or {}
    sid = session_id or str((event or {}).get("session_id") or "")
    room = {}
    fn = _cap("ui.room")
    if fn and sid:
        try:
            room = ((await fn(session_id=sid)) or {}).get("room") or {}
        except Exception:
            room = {}
    try:
        cond = evaluate(s.get("if") or "True", event or {}, room, args)
    except Exception as e:
        return {"ok": False, "error": "if-expression failed: %s" % e, "script": s["name"]}
    steps = s.get("do") if cond else s.get("else")
    run_id = "run-" + uuid.uuid4().hex[:8]
    by = issued_by or ("script:" + s["name"])
    if not by.startswith("script:"):
        by = "script:" + s["name"]
    out_steps = []
    disp = _cap("ui.directive")
    for st in steps or []:
        a = _fill(st.get("args") or {}, event or {}, args)
        if dry_run:
            out_steps.append({"name": st["name"], "args": a, "outcome": "dry-run"})
            continue
        if not disp:
            out_steps.append({"name": st["name"], "args": a, "outcome": "unsupported", "note": "dispatcher unavailable"})
            continue
        try:
            res = await disp(name=st["name"], args=a, session_id=sid, issued_by=by, run_id=run_id)
        except Exception as e:
            res = {"ok": False, "outcome": "failed", "error": str(e)}
        out_steps.append({"name": st["name"], "args": a, "outcome": (res or {}).get("outcome"), "note": (res or {}).get("error") or "", "row": ((res or {}).get("row") or {}).get("id")})
    if not dry_run:
        s["last_run"] = now_iso()
        s["runs"] = int(s.get("runs") or 0) + 1
        if not s.get("shipped"):
            await _put(s)
        await emit_event({"type": "ui.script.ran", "script": s["name"], "session_id": sid, "run_id": run_id, "branch": "do" if cond else "else",
                          "steps": len(out_steps), "applied": sum(1 for x in out_steps if x.get("outcome") == "applied")})
    return {"ok": True, "script": s["name"], "run_id": run_id, "branch": "do" if cond else "else", "condition": cond, "steps": out_steps, "dry_run": dry_run}


def _matches(s: Dict[str, Any], event: Dict[str, Any]) -> bool:
    on = s.get("on") or {}
    if str(event.get("type") or "") != str(on.get("event") or ""):
        return False
    for k, v in (on.get("match") or {}).items():
        if str(_dig(event, k)) != str(v):
            return False
    return True


# ── the tick: new events -> armed scripts ─────────────────────────────────────
# One state and one tick for the process, however many times this module body runs (the Vera.vera.* import trap
# loads a module body more than once): the state hangs off the orchestrator, the tick is registered once by name.
_STATE: Dict[str, Any] = getattr(_orch, "_UI_SCRIPTS_STATE", None) or {"last_id": "$", "timer_day": "", "fired": 0}
setattr(_orch, "_UI_SCRIPTS_STATE", _STATE)


async def _tick():
    r = _redis()
    if not r:
        return
    if _STATE["last_id"] == "$":
        # start from the newest entry that exists, so nothing between two ticks is ever skipped
        try:
            last = await r.xrevrange(_orch.EVENT_STREAM, count=1)
            _STATE["last_id"] = (last[0][0].decode() if isinstance(last[0][0], (bytes, bytearray)) else last[0][0]) if last else "0-0"
        except Exception:
            return
    try:
        res = await r.xread({_orch.EVENT_STREAM: _STATE["last_id"]}, count=100, block=1)
    except Exception:
        return
    scripts = [s for s in await _all() if s.get("state") == "on"]
    for _stream, entries in res or []:
        for eid, fields in entries:
            _STATE["last_id"] = eid.decode() if isinstance(eid, (bytes, bytearray)) else eid
            raw = fields.get(b"data") if isinstance(fields, dict) else None
            if raw is None and isinstance(fields, dict):
                raw = fields.get("data")
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "replace")
            try:
                ev = json.loads(raw) if isinstance(raw, str) else None
            except Exception:
                ev = None
            if not isinstance(ev, dict) or str(ev.get("type") or "").startswith("ui.script."):
                continue
            for s in scripts:
                if _matches(s, ev):
                    sid = str(ev.get("session_id") or ev.get("sid") or "")
                    if not sid:
                        continue
                    try:
                        await run_script(s, ev, sid)
                        _STATE["fired"] += 1
                    except Exception:
                        continue
    # timers: once a day at HH:MM (local time of the process)
    now = _dt.datetime.now()
    hm = now.strftime("%H:%M")
    day = now.strftime("%Y-%m-%d")
    for s in scripts:
        on = s.get("on") or {}
        if on.get("event") == "timer" and on.get("at") == hm and s.get("last_run", "")[:10] != day:
            try:
                await run_script(s, {"type": "timer", "at": hm, "session_id": (s.get("on") or {}).get("session_id") or ""}, (s.get("on") or {}).get("session_id") or "")
            except Exception:
                continue


# One tick per estate: on prod the scheduler leader runs it (singleton). A dev sandbox has its own Redis db -
# its event stream is its own - so there it runs as an ordinary job, or the scripted path could never be seen
# on the design edge (singleton jobs are skipped where no leadership is held, which is every sandbox).
def _is_sandbox() -> bool:
    try:
        return bool(_orch.is_dev_sandbox())
    except Exception:
        return False


if not any(t.get("name") == "ui.scripts.tick" for t in getattr(_orch, "SCHEDULED_TASKS", [])):
    schedule(_tick, 2.0, name="ui.scripts.tick", skip_in_sandbox=False, singleton=not _is_sandbox())


# ── capabilities ──────────────────────────────────────────────────────────────
@capability(
    "ui.script.list", memory="off", silent=True,
    http_method="GET", http_path="/ui/scripts", http_tags=["ui", "control"],
    description="Every UI script - the shipped ones and yours: {name, version, by, state (on . off . asked), on:{event, match}, "
                "if, do:[{name,args}], else, doc, runs, last_run}. Output: {ok, scripts, triggers}.")
async def cap_ui_script_list(trace_id=None):
    return {"ok": True, "scripts": await _all(), "triggers": list(TRIGGERS), "fired": _STATE["fired"]}


@capability(
    "ui.script.get", memory="off", silent=True,
    http_method="GET", http_path="/ui/script", http_tags=["ui", "control"],
    description="One UI script by name. Input: name (str!). Output: {ok, script}.")
async def cap_ui_script_get(name: str = "", trace_id=None):
    s = await _get(_slug(name))
    return {"ok": True, "script": s} if s else {"ok": False, "error": "no script %r" % name}


@capability(
    "ui.script.save", memory="off",
    http_method="POST", http_path="/ui/scripts/save", http_tags=["ui", "control"],
    description="Save a UI script (create or update; versions on update). Pass script: {name!, on:{event!, match, at}, "
                "if, do:[{name, args}]!, else:[...], by, state, doc}. Refuses a script with no name, no event, no "
                "steps or an if-expression that does not parse. A script the model proposes lands with state 'asked' "
                "until the user enables it. Output: {ok, script, problems[], created}.")
async def cap_ui_script_save(script: Optional[dict] = None, issued_by: str = "", trace_id=None):
    s = normalise(script if isinstance(script, dict) else {})
    probs = problems(s)
    if probs:
        return {"ok": False, "problems": probs}
    existing = (await _saved()).get(s["name"])
    if existing:
        s["version"] = int(existing.get("version") or 1) + 1
        s["created_at"] = existing.get("created_at") or now_iso()
        s["runs"] = int(existing.get("runs") or 0)
    else:
        s["created_at"] = now_iso()
    s["updated_at"] = now_iso()
    if issued_by in ("model", "aide") or (script or {}).get("by") in ("aide", "model"):
        s["by"] = "aide"
        s["state"] = "asked"
    if not await _put(s):
        return {"ok": False, "error": "redis unavailable"}
    await emit_event({"type": "ui.script.saved", "script": s["name"], "version": s["version"], "by": s["by"], "state": s["state"]})
    return {"ok": True, "script": s, "problems": [], "created": existing is None}


@capability(
    "ui.script.enable", memory="off",
    http_method="POST", http_path="/ui/scripts/enable", http_tags=["ui", "control"],
    description="Arm or disarm a script. Inputs: name (str!), on (bool, default true). A shipped script can be "
                "disarmed (it is then stored as yours). Output: {ok, script}.")
async def cap_ui_script_enable(name: str = "", on: bool = True, trace_id=None):
    s = await _get(_slug(name))
    if not s:
        return {"ok": False, "error": "no script %r" % name}
    s["state"] = "on" if on else "off"
    s.pop("shipped", None)
    s["updated_at"] = now_iso()
    if not await _put(s):
        return {"ok": False, "error": "redis unavailable"}
    await emit_event({"type": "ui.script.saved", "script": s["name"], "version": s["version"], "by": s["by"], "state": s["state"]})
    return {"ok": True, "script": s}


@capability(
    "ui.script.run", memory="off",
    http_method="POST", http_path="/ui/scripts/run", http_tags=["ui", "control"],
    description="Run a script now as one composite directive, or dry-run it. Inputs: name (str!), args (object - "
                "fills {args.x} placeholders), session_id (str! - the chat session the directives act in), event "
                "(object - a synthetic event for the if-expression), dry_run (bool). Output: {ok, run_id, branch, "
                "steps:[{name, args, outcome, row}]}.")
async def cap_ui_script_run(name: str = "", args: Optional[dict] = None, session_id: str = "", event: Optional[dict] = None,
                            dry_run: bool = False, issued_by: str = "", trace_id=None):
    s = await _get(_slug(name))
    if not s:
        return {"ok": False, "error": "no script %r" % name}
    sid = (session_id or trace_id or "").strip()
    if not sid:
        return {"ok": False, "error": "session_id is required"}
    ev = dict(event) if isinstance(event, dict) else {"type": (s.get("on") or {}).get("event") or "manual", "manual": True}
    ev.setdefault("session_id", sid)
    return await run_script(s, ev, sid, args=args or {}, issued_by=issued_by, dry_run=bool(dry_run))


@capability(
    "ui.script.delete", memory="off",
    http_method="POST", http_path="/ui/scripts/delete", http_tags=["ui", "control"],
    description="Delete a saved script (a shipped one returns to its shipped form). Input: name (str!). Output: {ok, name}.")
async def cap_ui_script_delete(name: str = "", trace_id=None):
    r = _redis()
    if not r:
        return {"ok": False, "error": "redis unavailable"}
    n = await r.delete(_KEY.format(name=_slug(name)))
    if not n:
        return {"ok": False, "error": "no saved script %r" % name}
    await emit_event({"type": "ui.script.deleted", "script": _slug(name)})
    return {"ok": True, "name": _slug(name)}
