"""
A removed canvas item can be put back (owner 2026-10-11: "removed canvas element should be restorable").

canvas.remove and canvas.clear keep what they took (the last 30, one per key); canvas.removed lists it, newest first;
canvas.restore puts an item back as it was, in view. Runs the real capabilities against the stub orchestrator the
canvas resolver test uses.
"""
import asyncio

from test_canvas_resolver import C, _fresh, _run

SID = "chat-restore-1"


def test_remove_then_restore_puts_the_item_back_as_it_was():
    _fresh()
    _run(C.cap_canvas_add(session_id=SID, kind="note", key="note:a", content={"title": "Groceries", "text": "milk"}))
    _run(C.cap_canvas_add(session_id=SID, kind="note", key="note:b", content={"title": "Other", "text": "x"}))
    rm = _run(C.cap_canvas_remove(key="note:a", session_id=SID))
    assert rm["ok"] and rm.get("restorable") is True
    lst = _run(C.cap_canvas_removed(session_id=SID))
    assert lst["ok"] and [r["key"] for r in lst["removed"]] == ["note:a"] and lst["removed"][0]["title"] == "Groceries"
    back = _run(C.cap_canvas_restore(key="note:a", session_id=SID))
    assert back["ok"] and back["restored"] == "note:a"
    doc = _run(C._target("", SID, create=False))
    a = next(b for b in doc["blocks"] if b.get("key") == "note:a")
    assert a["content"]["text"] == "milk" and a.get("state", "now") == "now"
    assert _run(C.cap_canvas_removed(session_id=SID))["removed"] == []


def test_restore_refuses_twice_and_unknown_keys():
    _fresh()
    _run(C.cap_canvas_add(session_id=SID, kind="note", key="note:c", content={"title": "C"}))
    _run(C.cap_canvas_remove(key="note:c", session_id=SID))
    assert _run(C.cap_canvas_restore(key="note:c", session_id=SID))["ok"]
    again = _run(C.cap_canvas_restore(key="note:c", session_id=SID))
    assert again["ok"] is False
    assert _run(C.cap_canvas_restore(key="note:nope", session_id=SID))["ok"] is False


def test_clear_is_restorable_item_by_item_and_the_list_is_bounded():
    _fresh()
    for i in range(35):
        _run(C.cap_canvas_add(session_id=SID, kind="note", key="note:%d" % i, content={"title": "n%d" % i}))
    _run(C.cap_canvas_clear(session_id=SID))
    lst = _run(C.cap_canvas_removed(session_id=SID))["removed"]
    assert len(lst) == C.REMOVED_KEEP == 30
    assert _run(C.cap_canvas_restore(key="note:34", session_id=SID))["ok"]


def test_a_key_removed_twice_is_listed_once():
    _fresh()
    _run(C.cap_canvas_add(session_id=SID, kind="note", key="note:d", content={"title": "D1"}))
    _run(C.cap_canvas_remove(key="note:d", session_id=SID))
    _run(C.cap_canvas_add(session_id=SID, kind="note", key="note:d", content={"title": "D2"}))
    _run(C.cap_canvas_remove(key="note:d", session_id=SID))
    lst = _run(C.cap_canvas_removed(session_id=SID))["removed"]
    assert [r["title"] for r in lst if r["key"] == "note:d"] == ["D2"]
