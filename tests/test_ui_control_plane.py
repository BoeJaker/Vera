"""
The control plane (UI redesign, Notes/40 §5.4; the Control and Driven boards):
one vocabulary of directives, one dispatcher (policy → surface → log → event,
undo through the same surface, the room manifest), and the scripted path
(deterministic on · if · do records run by the same dispatcher on events, no
model call).

Both modules are loaded against a stub orchestrator — a fake async Redis, a
fake capability registry whose surfaces record what they were asked — so the
dispatcher and the runner run for real: policy resolution, refused / asked /
applied, the ask answered, undo, the canvas.add rewrite, the manifest text;
the expression language's fence, placeholders, matching, a run, the tick over
a fake stream, and the script capabilities. The wiring is held text-level.
"""
import asyncio
import importlib.util
import json
import os
import sys
import types

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


class _FakeRedis:
    def __init__(self):
        self.d, self.h, self.l, self.s, self.stream = {}, {}, {}, {}, []

    async def get(self, k): return self.d.get(k)
    async def set(self, k, v, ex=None): self.d[k] = v
    async def delete(self, k):
        n = 0
        for store in (self.d, self.h, self.l, self.s):
            if k in store: store.pop(k); n = 1
        return n
    async def scan_iter(self, match="*", count=100):
        for k in list(self.d):
            if k.startswith(match.rstrip("*")): yield k
    async def hgetall(self, k): return dict(self.h.get(k, {}))
    async def hset(self, k, f, v): self.h.setdefault(k, {})[f] = v
    async def hget(self, k, f): return self.h.get(k, {}).get(f)
    async def hdel(self, k, f): return 1 if self.h.get(k, {}).pop(f, None) is not None else 0
    async def rpush(self, k, v): self.l.setdefault(k, []).append(v)
    async def ltrim(self, k, a, b): pass
    async def lrange(self, k, a, b):
        arr = self.l.get(k, [])
        return arr[a:] if a < 0 else arr[a:(b + 1 if b >= 0 else None)]
    async def lset(self, k, i, v): self.l[k][i] = v
    async def sadd(self, k, v): self.s.setdefault(k, set()).add(v)
    async def sismember(self, k, v): return v in self.s.get(k, set())
    async def smembers(self, k): return set(self.s.get(k, set()))
    async def xrevrange(self, stream, count=1): return self.stream[-count:][::-1] if self.stream else []
    async def xread(self, streams, count=100, block=0):
        last = list(streams.values())[0]
        num = lambda i: int(str(i).split("-")[0])
        out = [(eid, f) for eid, f in self.stream if last == "0-0" or num(eid) > num(last)]   # ids compare numerically, as Redis does
        return [(b"vera:events", out[:count])] if out else []


def _load():
    orch = types.ModuleType("Vera.vera.capability_orchestration")
    orch.REDIS = _FakeRedis()
    orch.EVENT_STREAM = "vera:events"
    orch.SCHEDULED_TASKS = []
    orch.CAPABILITY_REGISTRY = {}
    orch.EVENTS = []
    orch.CALLS = []

    def capability(name, **kw):
        def deco(fn):
            orch.CAPABILITY_REGISTRY[name] = {"func": fn, "meta": kw}
            return fn
        return deco

    class _App:
        def get(self, *a, **k): return lambda fn: fn
        def post(self, *a, **k): return lambda fn: fn

    async def emit_event(ev):
        ev.setdefault("ts", "t")
        orch.EVENTS.append(ev)
        orch.REDIS.stream.append(("%d-0" % (len(orch.REDIS.stream) + 1), {b"data": json.dumps(ev).encode()}))

    # the surfaces the dispatcher reaches through the registry
    async def panel_dispatch(session_id="", action="", payload=None, timeout_secs=8.0, panel="", trace_id=None):
        orch.CALLS.append(("panel.dispatch", session_id, action, payload, panel))
        if action == "__ui_directive__":
            name = (payload or {}).get("name")
            if name in ("canvas.pin", "canvas.size"):
                return {"ok": False, "error": "the canvas column (%s) — lands with a later slice" % name}
            return {"ok": True, "result": {"outcome": "applied", "note": "chat applied " + str(name), "prev": "M" if name == "canvas.size" else None}}
        return {"ok": True, "result": {"echo": action}}

    async def panel_query(session_id="", timeout_secs=4.0, panel="", trace_id=None):
        return {"ok": True, "state": {"panel": panel}}

    async def instantiate(id="", where="", host="", config=None, session_id="", trace_id=None):
        orch.CALLS.append(("instantiate", id, where, host))
        return {"ok": True, "instance": {"id": "inst-1", "template": id, "where": where}}

    async def inst_remove(id="", trace_id=None):
        orch.CALLS.append(("inst.remove", id)); return {"ok": True, "id": id}

    async def inst_list(where="", host="", session_id="", template="", trace_id=None):
        return {"ok": True, "instances": [{"name": "GPU + queue", "form": "meter", "where": "dashboard", "template": "gpu-queue"}]}

    async def panels_open(session_id="", trace_id=None):
        return {"ok": True, "panels": [{"id": "exec-panel", "placement": "beside chat", "origin": "aide", "host": "chat"}]}

    orch.CAPABILITY_REGISTRY.update({
        "panel.dispatch": {"func": panel_dispatch}, "panel.query": {"func": panel_query},
        "widget.template.instantiate": {"func": instantiate}, "widget.instance.remove": {"func": inst_remove},
        "widget.instance.list": {"func": inst_list}, "ui.panels.open": {"func": panels_open},
    })
    orch.APP = _App(); orch.capability = capability; orch.emit_event = emit_event
    orch.UI_PANELS = {}
    orch.register_ui = lambda panel_id, label, icon, html, js="", ui_caps=None, mode="inject", tab_order=100, **kw: orch.UI_PANELS.__setitem__(panel_id, {"id": panel_id, "label": label, "icon": icon, "html": html, "ui_caps": ui_caps or [], "mode": mode, "tab_order": tab_order})
    orch.now_iso = lambda: "2026-09-11T00:00:00+00:00"
    orch.schedule = lambda fn, interval, name=None, skip_in_sandbox=False, singleton=False: orch.SCHEDULED_TASKS.append({"fn": fn, "int": interval, "name": name})
    pkg = types.ModuleType("Vera"); pkg.__path__ = []
    sub = types.ModuleType("Vera.vera"); sub.__path__ = []
    sys.modules["Vera"] = pkg; sys.modules["Vera.vera"] = sub; sys.modules["Vera.vera.capability_orchestration"] = orch

    def load(name, rel):
        spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *rel))
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

    d = load("directives_under_test", ("vera", "ui", "directives.py"))
    s = load("scripts_under_test", ("vera", "ui", "scripts.py"))
    return d, s, orch


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


D, S, ORCH = _load()
SID = "chat-test-1"


# ── the vocabulary and the policy ─────────────────────────────────────────────

def test_the_vocabulary_is_the_boards_twenty():
    assert len(D.VOCAB) == 21, "the board's 20 rows, with its 'canvas.pin / park' row as two directives"
    for n in ("panel.open", "panel.dispatch", "panel.query", "panel.close", "canvas.show", "canvas.add", "canvas.pin", "canvas.park", "canvas.size",
              "canvas.ask", "lhm.focus", "lhm.compose", "widget.place", "widget.update", "widget.template.save", "chat.card", "chat.mode",
              "graph.focus", "graph.view", "ui.script.run", "ui.script.save"):
        assert n in D.VOCAB, n
    v = _run(D.cap_ui_directive_vocab())
    assert v["ok"] and len(v["directives"]) == 21 and v["policy_default"]["settings:*"] == "never"


def test_policy_resolves_most_specific_first():
    pol = dict(D.POLICY_DEFAULT)
    assert D._resolve_mode(pol, "panel.open", "panel:ops:ct126:terminal") == "drive"
    assert D._resolve_mode(pol, "panel.open", "panel:notebook:fabric") == "ask"
    assert D._resolve_mode(pol, "panel.open", "panel:settings:theme") == "never"
    assert D._resolve_mode(pol, "panel.open", "panel:exec-panel") == "drive", "no pattern → the panels class"
    assert D._resolve_mode(dict(pol, **{"panel:exec-panel": "ask"}), "panel.open", "panel:exec-panel") == "ask", "an exact target wins"
    assert D._resolve_mode(pol, "lhm.focus", "always") == "drive"
    assert D._resolve_mode(pol, "chat.mode", "chat.mode") == "ask"
    assert D._resolve_mode(pol, "widget.place", "widgets") == "drive"
    assert D._resolve_mode(dict(pol, scripts="off"), "ui.script.run", "scripts") == "off"


# ── the dispatcher ────────────────────────────────────────────────────────────

def test_unknown_and_sessionless_are_refused_without_a_log_row():
    r = _run(D.dispatch("panel.explode", {}, SID))
    assert not r["ok"] and r["outcome"] == "refused" and "unknown directive" in r["error"] and "vocabulary" in r
    r2 = _run(D.dispatch("lhm.focus", {"menu": "loop"}, ""))
    assert not r2["ok"] and "session_id" in r2["error"]


def test_never_refuses_and_logs_and_emits():
    before = len(ORCH.EVENTS)
    r = _run(D.dispatch("panel.open", {"id": "settings:theme"}, SID))
    assert r["outcome"] == "refused" and "policy never" in r["error"]
    rows = _run(D._log_rows(SID, 50))
    assert rows[-1]["name"] == "panel.open" and rows[-1]["outcome"] == "refused" and rows[-1]["path"] == "direct"
    assert ORCH.EVENTS[-1]["type"] == "ui.directive.refused" and len(ORCH.EVENTS) == before + 1


def test_drive_applies_through_the_chat_and_records_the_undo():
    r = _run(D.dispatch("panel.open", {"id": "exec-panel"}, SID, issued_by="model"))
    assert r["ok"] and r["outcome"] == "applied"
    assert r["row"]["undo"] == {"name": "panel.close", "args": {"id": "exec-panel"}}
    assert ORCH.CALLS[-1][0] == "panel.dispatch" and ORCH.CALLS[-1][2] == "__ui_directive__" and ORCH.CALLS[-1][3]["name"] == "panel.open"
    assert ORCH.EVENTS[-1]["type"] == "ui.directive.applied" and ORCH.EVENTS[-1]["path"] == "direct"
    r2 = _run(D.dispatch("lhm.focus", {"menu": "loop"}, SID, issued_by="script:loop-waits", run_id="run-1"))
    assert r2["ok"] and r2["row"]["path"] == "script:loop-waits" and r2["row"]["run_id"] == "run-1"
    r3 = _run(D.dispatch("canvas.pin", {"key": "boot-log"}, SID))
    assert not r3["ok"] and r3["outcome"] == "failed" and "later slice" in r3["error"], "what the chat cannot do yet is said, not faked"


def test_ask_holds_the_directive_until_the_chip_answers():
    r = _run(D.dispatch("chat.mode", {"mode": "council", "on": True}, SID))
    assert not r["ok"] and r["outcome"] == "asked" and r["ask_id"]
    log = _run(D.cap_ui_directive_log(session_id=SID))
    assert any(a["id"] == r["ask_id"] for a in log["asks"])
    assert ORCH.CALLS[-1][3]["name"] == "ui.ask", "the chip was sent to the chat"
    ans = _run(D.cap_ui_directive_answer(session_id=SID, ask_id=r["ask_id"], answer="allow_once"))
    assert ans["ok"] and ans["outcome"] == "applied" and ans["row"]["policy"] == "drive"
    assert not any(a["id"] == r["ask_id"] for a in _run(D.cap_ui_directive_log(session_id=SID))["asks"]), "an answered ask leaves the pending set"
    # allow always: the next one drives without asking
    r2 = _run(D.dispatch("chat.mode", {"mode": "council", "on": False}, SID))
    assert r2["outcome"] == "asked"
    _run(D.cap_ui_directive_answer(session_id=SID, ask_id=r2["ask_id"], answer="allow_always"))
    r3 = _run(D.dispatch("chat.mode", {"mode": "council", "on": True}, SID))
    assert r3["ok"] and r3["outcome"] == "applied" and "allowed always" in r3["row"]["note"]
    # refuse
    r4 = _run(D.dispatch("lhm.compose", {"menu": "context", "widgets": []}, SID))
    ref = _run(D.cap_ui_directive_answer(session_id=SID, ask_id=r4["ask_id"], answer="refuse"))
    assert not ref["ok"] and ref["outcome"] == "refused" and ORCH.EVENTS[-1]["type"] == "ui.directive.refused"
    assert not _run(D.cap_ui_directive_answer(session_id=SID, ask_id="nope", answer="refuse"))["ok"]


def test_the_users_own_moves_are_logged_and_never_asked():
    r = _run(D.dispatch("chat.mode", {"mode": "diffuse", "on": True}, SID, issued_by="user"))
    assert r["ok"] and r["row"]["path"] == "you"
    r2 = _run(D.dispatch("panel.open", {"id": "settings:x"}, SID, issued_by="you"))
    assert r2["ok"], "policy holds the model, not the user"


def test_undo_reverses_through_the_same_surface():
    r = _run(D.dispatch("widget.place", {"template": "gpu-queue", "into": "dashboard"}, SID))
    assert r["ok"] and r["row"]["undo"] == {"name": "widget.remove", "args": {"id": "inst-1"}}
    u = _run(D.cap_ui_directive_undo(session_id=SID, row_id=r["row"]["id"]))
    assert u["ok"] and u["undone"][0]["via"] == "widget.remove" and ORCH.CALLS[-1] == ("inst.remove", "inst-1")
    rows = _run(D._log_rows(SID, 300))
    assert next(x for x in rows if x["id"] == r["row"]["id"]).get("undone") is True
    assert rows[-1]["by"] == "undo" and rows[-1]["path"] == "undo"
    assert not _run(D.cap_ui_directive_undo(session_id=SID, row_id="d-nope"))["ok"]


def test_canvas_add_on_a_known_key_becomes_canvas_show_and_says_so():
    _run(D.dispatch("canvas.show", {"key": "boot-log"}, SID))
    r = _run(D.dispatch("canvas.add", {"key": "boot-log", "kind": "table", "ref": "x"}, SID))
    assert r["row"]["name"] == "canvas.show" and "canvas.add on an existing key" in r["row"]["note"]


def test_policy_set_and_get():
    r = _run(D.cap_ui_policy_set(target="panel:exec-panel", mode="never", session_id=SID))
    assert r["ok"] and r["policy"]["panel:exec-panel"] == "never"
    g = _run(D.cap_ui_policy_get(session_id=SID))
    assert g["policy"]["panel:exec-panel"] == "never" and "chat.mode" in g["allowed_always"]
    assert _run(D.dispatch("panel.open", {"id": "exec-panel"}, SID))["outcome"] == "refused"
    assert not _run(D.cap_ui_policy_set(target="x", mode="maybe"))["ok"]
    _run(D.cap_ui_policy_set(target="panel:exec-panel", mode="drive", session_id=SID))


def test_the_room_manifest_reads_every_surface():
    r = _run(D.cap_ui_room(session_id=SID))
    assert r["ok"] and r["text"].startswith("## the room")
    assert "exec-panel (beside chat . aide . drivable)" in r["text"]
    assert "widgets: GPU + queue (meter . dashboard)" in r["text"]
    assert "scripts armed:" in r["text"] and "loop-waits" in r["text"]
    assert "policy:" in r["text"] and "last directives:" in r["text"]
    assert "[[cap:ui.directive" in r["text"]


def test_ui_event_reports_a_moment_the_scripts_hear():
    r = _run(D.cap_ui_event(type="panel.opened", payload={"id": "exec-panel"}, session_id=SID))
    assert r["ok"] and r["type"] == "ui.panel.opened" and ORCH.EVENTS[-1]["id"] == "exec-panel" and ORCH.EVENTS[-1]["session_id"] == SID


# ── the scripted path ─────────────────────────────────────────────────────────

def test_the_expression_language_is_fenced():
    assert S.evaluate("event.result.re_embeds > 0", {"result": {"re_embeds": 3}}, {})
    assert not S.evaluate("event.result.re_embeds > 0", {"result": {}}, {}), "an absent field is None, never an error"
    assert S.evaluate("value >= 80 and not open('ops:ct126')", {"value": 84}, {"panels": [{"id": "ops:ct121"}]})
    assert not S.evaluate("value >= 80 and not open('ops:*')", {"value": 84}, {"panels": [{"id": "ops:ct121"}]})
    assert S.evaluate("'image' in str(event.preview or '')", {"preview": "an image of a cat"}, {})
    assert S.evaluate("event.by != 'undo' and args.node == 'ct126'", {"by": "model"}, {}, {"node": "ct126"})
    for bad in ("__import__('os')", "event.__class__", "open.__globals__", "(lambda: 1)()", "[x for x in event]", "print(1)"):
        try:
            S.compile_expr(bad); raise AssertionError("allowed: " + bad)
        except ValueError:
            pass


def test_normalise_problems_and_placeholders():
    s = S.normalise({"name": "GPU threshold", "on": {"event": "ui.widget.value", "match": {"template": "gpu-queue"}}, "if": "value >= 80",
                     "do": [{"name": "widget.pulse", "args": {"id": "{event.id}"}}, "lhm.focus"]})
    assert s["name"] == "gpu-threshold" and s["do"][1] == {"name": "lhm.focus", "args": {}} and S.problems(s) == []
    assert "needs a name" in " ".join(S.problems(S.normalise({"on": {"event": "cap.ok"}, "do": [{"name": "x"}]})))
    assert "needs an event" in " ".join(S.problems(S.normalise({"name": "x", "do": [{"name": "x"}]})))
    assert "does not parse" in " ".join(S.problems(S.normalise({"name": "x", "on": {"event": "cap.ok"}, "if": "import os", "do": [{"name": "x"}]})))
    assert "HH:MM" in " ".join(S.problems(S.normalise({"name": "x", "on": {"event": "timer"}, "do": [{"name": "x"}]})))
    assert S._fill({"q": "{event.question}", "n": "run {event.run} on {args.node}", "k": 1}, {"question": "full or boot?", "run": "2e22"}, {"node": "ct126"}) == {"q": "full or boot?", "n": "run 2e22 on ct126", "k": 1}
    assert S._matches(s, {"type": "ui.widget.value", "template": "gpu-queue"}) and not S._matches(s, {"type": "ui.widget.value", "template": "other"})


def test_a_run_dispatches_its_steps_as_one_run_marked_script():
    s = S.normalise({"name": "gpu-threshold", "on": {"event": "ui.widget.value"}, "if": "value >= 80 and not open('ops:ct126')",
                     "do": [{"name": "lhm.focus", "args": {"menu": "activity"}}, {"name": "panel.open", "args": {"id": "ops:{args.node}:metrics"}}], "else": [{"name": "lhm.focus", "args": {"menu": "loop"}}]})
    dry = _run(S.run_script(s, {"type": "ui.widget.value", "value": 84, "session_id": SID}, SID, args={"node": "ct126"}, dry_run=True))
    assert dry["ok"] and dry["branch"] == "do" and [x["outcome"] for x in dry["steps"]] == ["dry-run", "dry-run"] and dry["steps"][1]["args"] == {"id": "ops:ct126:metrics"}
    r = _run(S.run_script(s, {"type": "ui.widget.value", "value": 84, "session_id": SID}, SID, args={"node": "ct126"}))
    assert r["ok"] and r["branch"] == "do" and all(x["outcome"] == "applied" for x in r["steps"])
    rows = _run(D._log_rows(SID, 300))
    last = [x for x in rows if x.get("run_id") == r["run_id"]]
    assert len(last) == 2 and all(x["path"] == "script:gpu-threshold" and x["by"] == "script:gpu-threshold" for x in last)
    assert ORCH.EVENTS[-1]["type"] == "ui.script.ran" and ORCH.EVENTS[-1]["applied"] == 2
    e = _run(S.run_script(s, {"type": "ui.widget.value", "value": 10, "session_id": SID}, SID))
    assert e["branch"] == "else" and e["steps"][0]["args"] == {"menu": "loop"}


def test_the_tick_fires_armed_scripts_on_new_events_only():
    fired_before = S._STATE["fired"]
    _run(S._tick())                                   # first tick: takes the newest id, fires nothing old
    assert S._STATE["last_id"] != "$" and S._STATE["fired"] == fired_before
    _run(D.cap_ui_event(type="loop.step.waiting", payload={"question": "full suite or boot path?", "choices": "full,boot", "run": "2e22"}, session_id=SID))
    _run(S._tick())
    assert S._STATE["fired"] == fired_before + 1, "the shipped loop-waits script fired"
    rows = _run(D._log_rows(SID, 300))
    ask = [x for x in rows if x["name"] == "canvas.ask" and x["path"] == "script:loop-waits"]
    assert ask and ask[-1]["args"]["q"] == "full suite or boot path?" and ask[-1]["args"]["choices"] == "full,boot"
    _run(S._tick())
    assert S._STATE["fired"] == fired_before + 1, "an event fires once"


def test_script_capabilities_list_save_enable_run_delete():
    lst = _run(S.cap_ui_script_list())
    assert {s["name"] for s in lst["scripts"]} >= {"loop-waits", "image-to-card", "refused-to-ask"} and all(s.get("shipped") for s in lst["scripts"] if s["by"] == "vera")
    proposed = _run(S.cap_ui_script_save(script={"name": "re-embed-watch", "on": {"event": "cap.ok", "match": {"name": "fabric.status"}}, "if": "event.result.re_embeds > 0", "do": [{"name": "canvas.show", "args": {"key": "digest-24h"}}]}, issued_by="model"))
    assert proposed["ok"] and proposed["created"] and proposed["script"]["by"] == "aide" and proposed["script"]["state"] == "asked"
    en = _run(S.cap_ui_script_enable(name="re-embed-watch", on=True))
    assert en["ok"] and en["script"]["state"] == "on"
    again = _run(S.cap_ui_script_save(script={"name": "re-embed-watch", "on": {"event": "cap.ok"}, "do": [{"name": "canvas.show", "args": {"key": "x"}}]}))
    assert again["ok"] and not again["created"] and again["script"]["version"] == 2
    bad = _run(S.cap_ui_script_save(script={"name": "nope", "on": {"event": "cap.ok"}, "do": []}))
    assert not bad["ok"] and bad["problems"]
    run = _run(S.cap_ui_script_run(name="loop-waits", session_id=SID, event={"type": "ui.loop.step.waiting", "question": "q?", "choices": "a,b"}, dry_run=True))
    assert run["ok"] and run["dry_run"] and run["steps"][0]["args"]["q"] == "q?"
    off = _run(S.cap_ui_script_enable(name="image-to-card", on=False))
    assert off["ok"] and off["script"]["state"] == "off" and "shipped" not in off["script"]
    assert _run(S.cap_ui_script_delete(name="image-to-card"))["ok"], "a disarmed shipped script goes back to shipped"
    assert _run(S.cap_ui_script_get(name="image-to-card"))["script"]["state"] == "on"
    assert not _run(S.cap_ui_script_delete(name="nope"))["ok"]
    assert len([t for t in ORCH.SCHEDULED_TASKS if t["name"] == "ui.scripts.tick"]) == 1


def test_a_script_is_a_composite_directive_the_dispatcher_can_run_and_undo():
    r = _run(D.dispatch("ui.script.run", {"name": "loop-waits", "args": {}}, SID))
    assert r["ok"] and r["result"]["run_id"] and r["row"]["undo"]["name"] == "ui.script.undo"
    rows = _run(D._log_rows(SID, 300))
    run_rows = [x for x in rows if x.get("run_id") == r["result"]["run_id"] and x["name"] != "ui.script.run"]
    assert len(run_rows) == 2
    prop = _run(D.dispatch("ui.script.save", {"script": {"name": "quiet-hours", "on": {"event": "timer", "at": "22:00"}, "do": [{"name": "lhm.focus", "args": {"menu": "context"}}]}}, SID))
    assert prop["outcome"] == "asked", "a proposed rule is an ask"


# ── the wiring ───────────────────────────────────────────────────────────────

def test_the_modules_are_loaded_after_the_widget_registry():
    src = _read("vera", "capability_orchestration.py")
    i = src.index('os.path.join(_here, "widgets/widget_registry.py"),')
    assert 'os.path.join(_here, "ui/directives.py"),' in src[i:i + 700] and 'os.path.join(_here, "ui/scripts.py"),' in src[i:i + 800]
    assert os.path.exists(os.path.join(ROOT, "vera", "ui", "__init__.py"))


def test_the_widget_registry_awaits_its_events():
    src = _read("vera", "widgets", "widget_registry.py")
    assert "\n    emit_event({" not in src and src.count("await emit_event({") == 4


def test_the_chat_applies_asks_reports_and_shows_the_room():
    chat = _read("vera", "chat", "chat_panel.html")
    assert "if(action==='__ui_directive__'){" in chat and "res=await _uiDirectiveApply(payload||{});" in chat
    assert "case 'ui.ask': _uiAskChip(a.row||{});" in chat
    assert "case 'panel.open': { const ok=await panelOpen(String(a.id||''), who);" in chat
    assert "case 'lhm.focus':" in chat and "VeraLHM.pick(String(a.menu||'')" in chat
    assert "return later('the canvas column ('+name+')');" in chat, "what the chat cannot do yet says so"
    assert "api('/ui/directive/answer','POST',{session_id:SID, ask_id:askId, answer})" in chat
    assert "const roomText = await _uiRoomText();" in chat and "+_studioHint()+roomText+panelStateBlock+" in chat
    assert "api('/ui/room?session_id='+encodeURIComponent(SID))" in chat
    assert "_uiEvent('panel.opened',{id:p.id, by:_panelOpenedBy||'you', placement:'beside chat'})" in chat
    assert "_uiEvent('session.start',{})" in chat
    assert "_uiAskAnswer,_uiAskPick,_uiDirectiveApply," in chat


# ── the carry-overs: the canvas resolver's caps, the room's canvas, the Driven panel, the loop's wait ─────────

def _stub_canvas():
    async def add(session_id="", kind="", content=None, key="", at=None, size="", anchor=None, trace_id=None, **kw):
        ORCH.CALLS.append(("canvas.add", session_id, kind, key, size))
        if key == "doc:seen":
            return {"ok": True, "resolved": "shown", "existing": True, "key": key, "item": {"key": key}}
        return {"ok": True, "resolved": "added", "existing": False, "key": key or "widget:new", "item": {"key": key}}

    async def pin(session_id="", key="", trace_id=None): ORCH.CALLS.append(("canvas.pin", key)); return {"ok": True, "key": key}
    async def park(session_id="", key="", trace_id=None): ORCH.CALLS.append(("canvas.park", key)); return {"ok": True, "key": key}
    async def size(session_id="", key="", size="", trace_id=None): ORCH.CALLS.append(("canvas.size", key, size)); return {"ok": True, "key": key, "prev": "m"}
    async def remove(session_id="", key="", trace_id=None): ORCH.CALLS.append(("canvas.remove", key)); return {"ok": True, "key": key}
    async def room(session_id="", trace_id=None): return {"ok": True, "id": "cv_session_" + session_id, "revision": 7, "now": ["doc:a"], "pinned": ["widget:b"], "parked": [], "sizes": {"doc:a": "m"}, "count": 2}
    ORCH.CAPABILITY_REGISTRY.update({"canvas.add": {"func": add}, "canvas.pin": {"func": pin}, "canvas.park": {"func": park},
                                     "canvas.size": {"func": size}, "canvas.remove": {"func": remove}, "canvas.session.room": {"func": room}})


def _unstub_canvas():
    for n in ("canvas.add", "canvas.pin", "canvas.park", "canvas.size", "canvas.remove", "canvas.session.room"):
        ORCH.CAPABILITY_REGISTRY.pop(n, None)


def test_canvas_directives_route_to_the_resolvers_caps_when_registered():
    _stub_canvas()
    try:
        r = _run(D.dispatch("canvas.add", {"kind": "widget", "key": "widget:x", "size": "s"}, SID))
        assert r["ok"] and r["outcome"] == "applied" and ("canvas.add", SID, "widget", "widget:x", "s") in ORCH.CALLS
        assert r["row"].get("undo") == {"name": "canvas.remove", "args": {"key": "widget:x"}}, "the undo of an add is the resolver's remove"
        shown = _run(D.dispatch("canvas.add", {"kind": "doc", "key": "doc:seen"}, SID))
        assert shown["ok"] and "shown" in shown["row"]["note"] and not shown["row"].get("undo"), "a key already on the canvas is shown, and there is nothing to undo"
        p = _run(D.dispatch("canvas.pin", {"key": "doc:a"}, SID))
        assert p["ok"] and p["row"]["undo"] == {"name": "canvas.park", "args": {"key": "doc:a"}} and ("canvas.pin", "doc:a") in ORCH.CALLS
        s = _run(D.dispatch("canvas.size", {"key": "doc:a", "size": "xl"}, SID))
        assert s["ok"] and s["row"]["undo"] == {"name": "canvas.size", "args": {"key": "doc:a", "size": "m"}}
        sh = _run(D.dispatch("canvas.show", {"key": "doc:a"}, SID))
        assert sh["ok"] and ("canvas.add", SID, "", "doc:a", "") in ORCH.CALLS, "show by key goes through the resolver"
        # undo goes through the same caps
        u = _run(D.cap_ui_directive_undo(session_id=SID, row_id=p["row"]["id"]))
        assert u["ok"] and ("canvas.park", "doc:a") in ORCH.CALLS
    finally:
        _unstub_canvas()


def test_canvas_directives_fall_back_to_the_chat_without_the_caps():
    assert not D._cap("canvas.add")
    before = len(ORCH.CALLS)
    r = _run(D.dispatch("canvas.park", {"key": "doc:a"}, SID))   # (the stub chat refuses pin/size, as the pre-column chat did)
    assert r["ok"] and any(c[0] == "panel.dispatch" and c[2] == "__ui_directive__" for c in ORCH.CALLS[before:]), "the chat applies it"


def test_the_room_carries_the_session_canvas_when_the_resolver_is_registered():
    _stub_canvas()
    try:
        r = _run(D.cap_ui_room(session_id=SID))
        assert r["room"]["canvas"]["revision"] == 7 and r["room"]["canvas"]["now"] == ["doc:a"]
        assert "canvas: cv_session_%s rev 7 . now doc:a . pinned widget:b . parked -" % SID in r["text"]
    finally:
        _unstub_canvas()
    r2 = _run(D.cap_ui_room(session_id=SID))
    assert "canvas" not in r2["room"] and "canvas:" not in r2["text"], "no resolver, no canvas line (today's text)"


def test_the_driven_panel_is_registered_with_its_caps():
    assert "driven" in ORCH.UI_PANELS
    p = ORCH.UI_PANELS["driven"]
    assert p["mode"] == "element" and "/ui/driven" in p["html"]
    for c in ("ui.directive.log", "ui.directive.undo", "ui.directive.answer", "ui.policy.get", "ui.policy.set", "ui.script.list", "ui.script.enable", "ui.room"):
        assert c in p["ui_caps"], c
    html = _read("vera", "ui", "driven_panel.html")
    for piece in ("call('ui.directive.log'", "call('ui.policy.get'", "call('ui.script.list'", "call('ui.room'", "drUndo(", "drAnswer(", "drPolicy(", "drScript(", "vera:panel:init"):
        assert piece in html, piece


def test_the_loop_tells_the_control_plane_when_a_step_waits():
    src = _read("vera", "dag", "dag_workshop_capabilities.py")
    i = src.index('"type": "agent_loop_v6.step_question"')
    j = src.index("decision = await _await_hitl_decision(", i)
    seg = src[i:j]
    assert 'type="loop.step.waiting"' in seg and '"question": q' in seg and '"run": stream_id' in seg
    assert "except Exception as _e:" in seg, "never fatal"
    # the shipped script listens for exactly that event
    assert any(s["on"]["event"] == "ui.loop.step.waiting" for s in S.SHIPPED)


def test_the_agent_registrys_emits_are_awaited():
    src = _read("vera", "registry", "registry_capabilities.py")
    assert 'await emit_event({"type": "registry.upsert"' in src and 'await emit_event({"type": "registry.delete"' in src
    assert "\n    emit_event({" not in src
