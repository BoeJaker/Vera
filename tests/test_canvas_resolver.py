"""
The session canvas and its resolver (Notes/38 P0–P1, Notes/40 §6; the Canvas board's canvas column):
keyed items on one document per chat session — recall over recreate — with a revision that every write bumps
and an append-only timeline; pin · park · size · remove by key; the room shape the manifest reads.

vera/canvas/canvas_capabilities.py is loaded against a stub orchestrator (a fake async Redis with the ZSET the
index uses, a fake registry, a fake event sink), so the capabilities run for real. The pre-existing keyless
path — create / append / get / list / update / move / remove / delete — is exercised alongside to show it is
unchanged.
"""
import asyncio
import importlib.util
import json
import os
import types

from orchestration_stub import stubbed_orchestration

ROOT = os.path.join(os.path.dirname(__file__), "..")


class _FakeRedis:
    def __init__(self):
        self.d, self.z = {}, {}

    async def get(self, k): return self.d.get(k)
    async def set(self, k, v, ex=None): self.d[k] = v
    async def delete(self, k): return 1 if self.d.pop(k, None) is not None else 0
    async def zadd(self, k, mapping): self.z.setdefault(k, {}).update(mapping)
    async def zrem(self, k, m): self.z.get(k, {}).pop(m, None)
    async def zrevrange(self, k, a, b):
        ids = sorted(self.z.get(k, {}).items(), key=lambda kv: -kv[1])
        return [i for i, _ in ids][a:b + 1]


def _load():
    orch = types.ModuleType("Vera.vera.capability_orchestration")
    orch.REDIS = _FakeRedis()
    orch.CAPABILITY_REGISTRY = {}
    orch.EVENTS = []
    orch.UI = []

    def capability(name, **kw):
        def deco(fn):
            orch.CAPABILITY_REGISTRY[name] = {"func": fn, "meta": kw}
            return fn
        return deco

    class _App:
        def get(self, *a, **k): return lambda fn: fn
        def post(self, *a, **k): return lambda fn: fn

    async def emit_event(ev):
        orch.EVENTS.append(ev)

    orch.APP = _App(); orch.capability = capability; orch.emit_event = emit_event
    orch.now_iso = lambda: "2026-09-11T00:00:%02d+00:00" % (len(orch.EVENTS) % 60)
    orch.register_ui = lambda *a, **k: orch.UI.append((a, k))
    spec = importlib.util.spec_from_file_location("canvas_under_test", os.path.join(ROOT, "vera", "canvas", "canvas_capabilities.py"))
    with stubbed_orchestration(orch):
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod, orch


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


C, ORCH = _load()
SID = "a41c-test"


def _fresh():
    ORCH.REDIS.d.clear(); ORCH.REDIS.z.clear(); ORCH.EVENTS.clear()


def _updates():
    return [e for e in ORCH.EVENTS if e.get("type") == "canvas.updated"]


# ── the session canvas ────────────────────────────────────────────────────────

def test_resolve_makes_the_session_canvas_once():
    _fresh()
    a = _run(C.cap_canvas_session_resolve(session_id=SID))
    assert a["ok"] and a["id"] == "cv_session_" + SID and a["created"] is True and a["count"] == 0
    b = _run(C.cap_canvas_session_resolve(session_id=SID))
    assert b["id"] == a["id"] and b["created"] is False, "resolve is idempotent"
    doc = _run(C.cap_canvas_get(id=a["id"]))
    assert doc["title"] == "Session canvas" and doc["mode"] == "session" and doc["session"] == SID
    assert doc["revision"] == 1 and doc["timeline"][0]["op"] == "create"
    assert _run(C.cap_canvas_session_resolve(session_id="")) ["ok"] is False


def test_the_session_id_is_made_safe_for_a_key():
    assert C._session_canvas_id("a b/c:d") == "cv_session_a_b_c_d"
    assert C._session_canvas_id("x" * 100) == "cv_session_" + "x" * 64


# ── the resolver ──────────────────────────────────────────────────────────────

def test_add_is_keyed_and_the_same_key_is_shown_not_added_again():
    _fresh()
    r = _run(C.cap_canvas_add(session_id=SID, kind="session", content={"host": "ct126", "command": "df"},
                              key="ssh:ct126", anchor={"turn": "m2", "mid": "m2"}))
    assert r["ok"] and r["resolved"] == "added" and r["existing"] is False and r["key"] == "ssh:ct126"
    assert r["item"]["state"] == "now" and r["item"]["size"] == "m" and r["item"]["anchors"] == [{"turn": "m2", "mid": "m2"}]
    cid = r["id"]
    park = _run(C.cap_canvas_park(key="ssh:ct126", session_id=SID))
    assert park["ok"] and park["state"] == "parked" and park["prev"] == "now"
    # turn 10 asks for the same terminal: it comes back, it does not double
    again = _run(C.cap_canvas_add(session_id=SID, kind="session", content={"host": "ct126"}, key="ssh:ct126",
                                  anchor={"turn": "m10", "mid": "m10"}))
    assert again["ok"] and again["resolved"] == "shown" and again["existing"] is True
    assert again["item"]["state"] == "now" and again["item"]["id"] == r["item"]["id"]
    assert again["item"]["anchors"] == [{"turn": "m2", "mid": "m2"}, {"turn": "m10", "mid": "m10"}], "anchors only grow"
    doc = _run(C.cap_canvas_get(id=cid))
    assert len(doc["blocks"]) == 1, "no second terminal exists"
    ops = [t["op"] for t in doc["timeline"]]
    assert ops == ["create", "add", "park", "show"]


def test_a_bare_key_shows_but_never_invents_an_item():
    _fresh()
    r = _run(C.cap_canvas_add(session_id=SID, key="widget:nope"))
    assert r["ok"] is False and "unknown key" in r["error"]
    assert len(_run(C.cap_canvas_get(id="cv_session_" + SID))["blocks"]) == 0
    _run(C.cap_canvas_add(session_id=SID, kind="note", content="hi", key="note:a"))
    shown = _run(C.cap_canvas_add(session_id=SID, key="note:a"))
    assert shown["resolved"] == "shown"


def test_a_key_is_generated_when_none_is_given_and_content_json_text_is_accepted():
    _fresh()
    r = _run(C.cap_canvas_add(session_id=SID, kind="table", content='{"columns":["a"],"rows":[[1]]}', at="pinned", size="l"))
    assert r["ok"] and r["key"].startswith("table:bk_") and r["item"]["state"] == "pinned" and r["item"]["size"] == "l"
    doc = _run(C.cap_canvas_get(id=r["id"]))
    assert doc["blocks"][0]["content"] == {"columns": ["a"], "rows": [[1]]}
    bad = _run(C.cap_canvas_add(session_id=SID, kind="note", content="x", at="sideways", size="huge"))
    assert bad["item"]["state"] == "now" and bad["item"]["size"] == "m", "unknown state/size fall back"


def test_add_targets_an_explicit_canvas_id_too():
    _fresh()
    cv = _run(C.cap_canvas_create(title="Board", mode="static"))
    r = _run(C.cap_canvas_add(id=cv["id"], kind="note", content="n", key="note:n"))
    assert r["ok"] and r["id"] == cv["id"]
    assert _run(C.cap_canvas_add(id="cv_missing", kind="note", content="n"))["ok"] is False
    assert _run(C.cap_canvas_add(kind="note", content="n"))["ok"] is False, "no id and no session"


# ── pin · park · size · remove ────────────────────────────────────────────────

def test_pin_park_size_remove_by_key():
    _fresh()
    _run(C.cap_canvas_add(session_id=SID, kind="note", content="n", key="note:n"))
    assert _run(C.cap_canvas_pin(key="note:n", session_id=SID))["state"] == "pinned"
    assert _run(C.cap_canvas_park(key="note:n", session_id=SID))["state"] == "parked"
    s = _run(C.cap_canvas_size(key="note:n", size="xl", session_id=SID))
    assert s["ok"] and s["size"] == "xl" and s["prev"] == "m"
    assert _run(C.cap_canvas_size(key="note:n", size="huge", session_id=SID))["ok"] is False
    assert _run(C.cap_canvas_pin(key="note:zz", session_id=SID))["ok"] is False
    rm = _run(C.cap_canvas_remove(key="note:n", session_id=SID))
    assert rm["ok"] and rm["key"] == "note:n"
    assert _run(C.cap_canvas_get(id="cv_session_" + SID))["blocks"] == []
    assert _run(C.cap_canvas_remove(key="note:n", session_id=SID))["ok"] is False


# ── revision · timeline · events ──────────────────────────────────────────────

def test_every_write_bumps_the_revision_and_appends_to_the_timeline_and_emits():
    _fresh()
    _run(C.cap_canvas_add(session_id=SID, kind="note", content="n", key="note:n"))       # create + add
    _run(C.cap_canvas_pin(key="note:n", session_id=SID))
    _run(C.cap_canvas_size(key="note:n", size="s", session_id=SID))
    _run(C.cap_canvas_ask(key="note:n", question="why is this here?", session_id=SID))
    tl = _run(C.cap_canvas_timeline(id="cv_session_" + SID))
    assert tl["ok"] and tl["revision"] == 5
    assert [t["rev"] for t in tl["timeline"]] == [1, 2, 3, 4, 5], "one entry per write, numbered by revision"
    assert [t["op"] for t in tl["timeline"]] == ["create", "add", "pin", "size", "ask"]
    assert tl["timeline"][3]["size"] == "s" and tl["timeline"][4]["q"] == "why is this here?"
    assert all("ts" in t and "key" in t for t in tl["timeline"])
    ups = _updates()
    assert [e["op"] for e in ups] == ["create", "add", "pin", "size", "ask"]
    assert ups[-1]["id"] == "cv_session_" + SID and ups[-1]["revision"] == 5 and ups[-1]["key"] == "note:n"
    assert ups[-1]["canvas_id"] == ups[-1]["id"], "the old field name is still there for the element"


def test_ask_records_only_and_needs_a_real_item():
    _fresh()
    _run(C.cap_canvas_add(session_id=SID, kind="note", content="n", key="note:n"))
    assert _run(C.cap_canvas_ask(key="note:q", question="?", session_id=SID))["ok"] is False
    r = _run(C.cap_canvas_ask(key="note:n", question="?", session_id=SID))
    assert r["ok"] and r["revision"] == 3
    doc = _run(C.cap_canvas_get(id="cv_session_" + SID))
    assert doc["blocks"][0]["state"] == "now", "asking changes nothing on the item"


# ── update by key · the loop type (P7: loops write to the canvas) ─────────────

def test_update_by_key_on_the_session_canvas_is_a_write_like_any_other():
    _fresh()
    _run(C.cap_canvas_add(session_id=SID, kind="loop", content={"goal": "fix boot", "status": "running", "steps": []}, key="loop:r1"))
    r = _run(C.cap_canvas_update(session_id=SID, key="loop:r1", content={"goal": "fix boot", "status": "ok", "steps": [{"n": "recall", "cap": "memory.select", "status": "ok"}]}))
    assert r["ok"] and r["key"] == "loop:r1" and r["revision"] == 3 and r["item"]["key"] == "loop:r1"
    doc = _run(C.cap_canvas_get(id="cv_session_" + SID))
    b = doc["blocks"][0]
    assert b["type"] == "loop" and b["content"]["status"] == "ok" and b["content"]["steps"][0]["cap"] == "memory.select"
    assert _run(C.cap_canvas_update(session_id=SID, key="loop:zz", content={}))["ok"] is False
    assert _run(C.cap_canvas_update(session_id="never-made", key="loop:r1", content={}))["ok"] is False, "no canvas is made by an update"
    tl = _run(C.cap_canvas_timeline(id="cv_session_" + SID))
    assert tl["timeline"][-1]["op"] == "update" and tl["timeline"][-1]["key"] == "loop:r1"
    assert _updates()[-1]["op"] == "update" and _updates()[-1]["revision"] == 3


def test_update_by_block_id_is_unchanged():
    _fresh()
    cv = _run(C.cap_canvas_create(title="t"))
    a = _run(C.cap_canvas_append(id=cv["id"], type="note", content="one"))
    r = _run(C.cap_canvas_update(id=cv["id"], block_id=a["block_id"], content="two"))
    assert r["ok"] and r["block_id"] == a["block_id"]
    assert _run(C.cap_canvas_get(id=cv["id"]))["blocks"][0]["content"]["text"] == "two"


def test_loop_is_a_block_type_with_a_bare_string_as_its_goal():
    assert "loop" in C.BLOCK_TYPES
    assert C._validate_block("loop", "fix boot")["content"] == {"goal": "fix boot"}


# ── recall · room ─────────────────────────────────────────────────────────────

def test_recall_finds_items_by_key_kind_or_content_without_changing_them():
    _fresh()
    _run(C.cap_canvas_add(session_id=SID, kind="session", content={"host": "ct126"}, key="ssh:ct126"))
    _run(C.cap_canvas_add(session_id=SID, kind="note", content="the boot digest", key="note:digest"))
    r = _run(C.cap_canvas_recall(id="cv_session_" + SID, q="ct126"))
    assert r["ok"] and [m["key"] for m in r["matches"]] == ["ssh:ct126"]
    r = _run(C.cap_canvas_recall(id="cv_session_" + SID, q="DIGEST"))
    assert [m["key"] for m in r["matches"]] == ["note:digest"]
    assert len(_run(C.cap_canvas_recall(id="cv_session_" + SID, q=""))["matches"]) == 2
    assert _run(C.cap_canvas_timeline(id="cv_session_" + SID))["revision"] == 3, "recall is a read"


def test_room_shape():
    _fresh()
    empty = _run(C.cap_canvas_session_room(session_id="never-made"))
    assert empty == {"ok": True, "id": "cv_session_never-made", "revision": 0, "now": [], "pinned": [], "parked": [], "sizes": {}, "count": 0}
    assert ORCH.REDIS.d == {}, "room never creates the canvas"
    _run(C.cap_canvas_add(session_id=SID, kind="note", content="a", key="note:a"))
    _run(C.cap_canvas_add(session_id=SID, kind="note", content="b", key="note:b", at="pinned", size="l"))
    _run(C.cap_canvas_add(session_id=SID, kind="note", content="c", key="note:c", at="parked"))
    _run(C.cap_canvas_add(session_id=SID, kind="note", content="h", key="note:h", at="hidden"))
    _run(C.cap_canvas_append(id="cv_session_" + SID, type="note", content="keyless"))
    room = _run(C.cap_canvas_session_room(session_id=SID))
    assert room["id"] == "cv_session_" + SID and room["revision"] == 6
    assert room["now"] == ["note:a"] and room["pinned"] == ["note:b"] and room["parked"] == ["note:c"]
    assert room["sizes"] == {"note:a": "m", "note:b": "l", "note:c": "m", "note:h": "m"} and room["count"] == 4
    assert sorted(room) == ["count", "id", "now", "ok", "parked", "pinned", "revision", "sizes"]


# ── the keyless path is what it was ───────────────────────────────────────────

def test_keyless_blocks_are_unchanged():
    _fresh()
    cv = _run(C.cap_canvas_create(title="Notes", mode="dynamic", topic="ai"))
    assert cv["mode"] == "dynamic" and cv["id"].startswith("cv_")
    a = _run(C.cap_canvas_append(id=cv["id"], type="markdown", content="# hi"))
    b = _run(C.cap_canvas_append(id=cv["id"], type="code", content={"code": "x=1", "lang": "py"}, meta={"k": 1}))
    assert a["ok"] and b["ok"] and a["type"] == "markdown"
    doc = _run(C.cap_canvas_get(id=cv["id"]))
    assert [x["type"] for x in doc["blocks"]] == ["markdown", "code"]
    assert all("key" not in x and "state" not in x for x in doc["blocks"]), "keyless blocks stay keyless"
    assert doc["blocks"][0]["content"] == {"md": "# hi"} and doc["blocks"][1]["meta"] == {"k": 1}
    assert doc["blocks"][0]["layout"] == {"order": 0}
    ls = _run(C.cap_canvas_list(limit=10))
    assert ls["canvases"][0]["id"] == cv["id"] and ls["canvases"][0]["blocks"] == 2
    up = _run(C.cap_canvas_update(id=cv["id"], block_id=a["block_id"], content="# bye", meta={"m": 2}))
    assert up["ok"]
    doc = _run(C.cap_canvas_get(id=cv["id"]))
    assert doc["blocks"][0]["content"] == {"md": "# bye"} and doc["blocks"][0]["meta"] == {"m": 2}
    mv = _run(C.cap_canvas_move(id=cv["id"], block_id=b["block_id"], order=0, layout={"x": 1, "y": 2}))
    assert mv["ok"]
    doc = _run(C.cap_canvas_get(id=cv["id"]))
    assert [x["type"] for x in doc["blocks"]] == ["code", "markdown"] and doc["blocks"][0]["layout"] == {"order": 0, "x": 1, "y": 2}
    rm = _run(C.cap_canvas_remove(id=cv["id"], block_id=a["block_id"]))
    assert rm == {"ok": True, "removed": a["block_id"]}
    assert _run(C.cap_canvas_remove(id=cv["id"], block_id="bk_nope")) == {"error": "unknown block: bk_nope"}
    assert len(_run(C.cap_canvas_get(id=cv["id"]))["blocks"]) == 1
    assert _run(C.cap_canvas_get(id=cv["id"]))["revision"] >= 5, "the revision counts every write of any kind"
    assert _run(C.cap_canvas_delete(id=cv["id"]))["ok"]
    assert "error" in _run(C.cap_canvas_get(id=cv["id"]))
    bt = _run(C.cap_canvas_block_types())
    assert "widget" in bt["block_types"] and bt["modes"] == ["dynamic", "static"]


# ── the relevance engine (Notes/38 §3.3, P2) ──
def _rel(sid, turn, text="", **kw):
    return _run(C.cap_canvas_session_relevance(session_id=sid, turn=turn, text=text, **kw))


def test_scenario_b_the_notes_come_back_by_intent_and_park_after_two_turns():
    sid = "rel-b"
    nb = _run(C.cap_canvas_add(session_id=sid, kind="note", key="notebook:rel-b", content={"text": "cell 1 · cell 2 · cell 3"}, anchor={"turn": "m1"}))
    assert nb["resolved"] == "added"
    # m1: an explicit hit this turn (the add) — live; m2, m3: recency keeps it live; m4: it parks
    assert _rel(sid, "m1")["focus"] == ["notebook:rel-b"] and _rel(sid, "m1")["scores"]["notebook:rel-b"]["signals"]["explicit"] == 1.0
    r2 = _rel(sid, "m2", "something else", recent=["m2", "m1"]); assert r2["focus"] == ["notebook:rel-b"] and r2["scores"]["notebook:rel-b"]["signals"]["recency"] == 0.5 and r2["parked"] == []
    r3 = _rel(sid, "m3", "other work", recent=["m3", "m2", "m1"]); assert r3["focus"] == ["notebook:rel-b"]
    r4 = _rel(sid, "m4", "more work", recent=["m4", "m3", "m2", "m1"])
    assert r4["focus"] == [] and r4["parked"] == ["notebook:rel-b"] and r4["scores"]["notebook:rel-b"]["score"] < 0.35
    doc = _run(C.cap_canvas_get(id=nb["id"]))
    assert doc["blocks"][0]["state"] == "parked" and doc["timeline"][-2]["op"] == "park" and doc["timeline"][-1]["op"] == "relevance"
    # m15: "add what we found to my notes" — the intent names the notebook; it is recalled, not recreated
    r15 = _rel(sid, "m15", "add what we found to my notes", recent=["m15", "m14", "m13", "m12"])
    assert r15["recalled"] == ["notebook:rel-b"] and r15["focus"] == ["notebook:rel-b"] and r15["scores"]["notebook:rel-b"]["signals"]["intent"] == 0.7
    doc = _run(C.cap_canvas_get(id=nb["id"]))
    b = doc["blocks"][0]
    assert b["state"] == "now" and [a.get("turn") for a in b["anchors"]] == ["m1", "m15"] and b["anchors"][-1]["role"] == "recalled" and b["score"] == 0.7
    assert [e["op"] for e in doc["timeline"][-2:]] == ["recall", "relevance"] and doc["timeline"][-1]["turn"] == "m15"
    assert len(doc["blocks"]) == 1, "recalled, never a second notebook"


def test_entity_similarity_and_pin_signals_and_ask_only():
    sid = "rel-e"
    _run(C.cap_canvas_add(session_id=sid, kind="session", key="ssh:ct126", content={"host": "ct126", "command": "df -h"}, anchor={"turn": "m2"}))
    _run(C.cap_canvas_add(session_id=sid, kind="table", key="table:disk", content={"columns": ["fs", "use"], "rows": [["/", "81%"]], "caption": "disk usage on ct126"}, anchor={"turn": "m2"}))
    _run(C.cap_canvas_add(session_id=sid, kind="note", key="note:pinned", content={"text": "keep me"}, at="pinned", anchor={"turn": "m1"}))
    # far from m2: the entity ct126 in the turn brings the terminal and the table back; the pin never leaves
    r = _rel(sid, "m10", "check that disk again on ct126", entities=["ct126"], recent=["m10", "m9", "m8", "m7", "m6", "m5"])
    assert set(r["focus"]) == {"ssh:ct126", "table:disk", "note:pinned"}
    assert r["scores"]["ssh:ct126"]["signals"]["entity"] == 0.85 and r["scores"]["note:pinned"]["signals"]["pin"] == 1.0
    assert r["scores"]["table:disk"]["signals"]["similarity"] > 0, "the turn's words overlap the caption"
    # nothing of the turn: the terminal and the table park, the pin stays; apply=False only answers
    q = _rel(sid, "m11", "tell me a joke", recent=["m11", "m10"], apply=False)
    assert q["applied"] is False and q["parked"] == [] and q["focus"] == ["note:pinned"]
    doc = _run(C.cap_canvas_get(id=q["id"]))
    assert all(b["state"] in ("now", "pinned") for b in doc["blocks"]), "ask-only moved nothing"
    a = _rel(sid, "m11", "tell me a joke", recent=["m11", "m10"])
    assert set(a["parked"]) == {"ssh:ct126", "table:disk"} and a["focus"] == ["note:pinned"]
    view = _run(C.cap_canvas_recall(id=a["id"]))
    assert all("score" in m for m in view["matches"]), "the item view carries the engine's last score"


def test_the_engine_never_creates_the_canvas_and_the_room_is_untouched():
    r = _rel("rel-none", "m1", "hello")
    assert r["ok"] and r["focus"] == [] and r["applied"] is False and r["revision"] == 0
    assert _run(C.cap_canvas_session_room(session_id="rel-none"))["count"] == 0
    assert "canvas.session.relevance" in ORCH.CAPABILITY_REGISTRY


def test_the_caps_are_registered_and_listed_on_the_panel():
    for n in ("canvas.session.resolve", "canvas.add", "canvas.pin", "canvas.park", "canvas.size", "canvas.remove",
              "canvas.recall", "canvas.timeline", "canvas.session.room", "canvas.ask",
              "canvas.create", "canvas.get", "canvas.list", "canvas.append", "canvas.update", "canvas.move", "canvas.delete", "canvas.show"):
        assert n in ORCH.CAPABILITY_REGISTRY, n
    (args, kw), = [u for u in ORCH.UI if u[0][0] == "canvas"]
    assert "canvas.session.resolve" in kw["ui_caps"] and "canvas.append" in kw["ui_caps"]
