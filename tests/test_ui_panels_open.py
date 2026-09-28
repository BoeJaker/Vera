"""
The one panel set (UI redesign lhm-library, Notes/40 §9): every panel open
anywhere — beside the chat, or as a tab of the harness — whoever opened it, in
one list (ui.panels.open), and one bridge to drive any of them (panel.dispatch /
panel.query with a `panel` target).

These tests hold the contract across the three files that make it one set:
the backend stores what the holders report and returns the union; dispatch and
query carry the target; the chat reports the panel beside it, hands a targeted
dispatch it cannot serve to the harness (or acks that it cannot, standalone),
and lists the harness's tabs in its Open now rows; the harness reports its
tabs under the chat's session, tells the chat, routes a targeted dispatch to
the tab that holds the panel and acks like the chat does, and closes a tab the
chat's row asks for. Text-level, so it runs anywhere.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


PY = _read("vera", "chat", "chat_panels_capabilities.py")
CHAT = _read("vera", "chat", "chat_panel.html")
HARNESS = _read("vera", "capability_orchestration.html")


# ── the backend ──────────────────────────────────────────────────────────────

def test_the_report_route_and_the_capability_exist_and_share_one_key():
    assert '@APP.post("/ui/panels/open/report", include_in_schema=False)' in PY
    assert '"ui.panels.open",' in PY and 'http_method="GET", http_path="/ui/panels/open"' in PY
    assert PY.count('_PANELS_OPEN_KEY.format(sid=sid, host=host)') == 2, "the report writes and the capability reads the same key"
    assert '_PANELS_OPEN_KEY = "vera:ui:panels:open:{sid}:{host}"' in PY
    # never under the prefix the startup loader globs for dynamic panel records
    assert not re.search(r'_PANELS_OPEN_KEY = "vera:ui:panel:', PY)


def test_the_request_alias_is_imported_before_the_report_route_uses_it():
    assert PY.index("from fastapi import Request as _BridgeRequest") < PY.index("async def _panels_open_report(request: _BridgeRequest)")


def test_a_report_expires_and_an_empty_report_clears():
    assert "_PANELS_OPEN_TTL = 90" in PY
    assert "ex=_PANELS_OPEN_TTL" in PY
    assert "await r.delete(key)" in PY


def test_the_capability_returns_the_union_of_both_hosts():
    body = PY[PY.index("async def cap_ui_panels_open("):]
    body = body[:body.index("\n@capability(")]
    assert 'for host in ("chat", "harness"):' in body
    assert 'return {"ok": True, "session_id": sid, "panels": panels, "count": len(panels), "hosts": hosts}' in body


def test_dispatch_and_query_carry_the_panel_target():
    assert re.search(r"async def cap_panel_dispatch\([^)]*panel: str = \"\",", PY, re.S)
    assert '"panel":      str(panel or "").strip(),' in PY
    assert re.search(r"async def cap_panel_query\([^)]*panel: str = \"\",", PY, re.S)
    q = PY[PY.index("async def cap_panel_query("):]
    assert "panel=panel," in q[:q.index("\n@capability(") if "\n@capability(" in q else len(q)]


# ── the chat ─────────────────────────────────────────────────────────────────

def test_the_chat_reports_the_panel_beside_it_under_its_session():
    assert "function _panelsOpenReport(){" in CHAT
    assert "body:JSON.stringify({session_id:SID, host:'chat', panels:mine})" in CHAT
    assert "_panelsOpenTimer=setInterval(_panelsOpenReport, 30000);" in CHAT
    init = CHAT[CHAT.index("try{ _lhmMount(); }catch(e){"):]
    assert "try{ _panelsOpenStart(); }catch(e){" in init[:600]
    close = CHAT[CHAT.index("function panelClose(){"):]
    assert "setTimeout(_panelsOpenReport,0)" in close[:400]


def test_the_chat_hands_a_targeted_dispatch_to_the_harness_or_says_it_cannot():
    h = CHAT[CHAT.index("async function _handleServerDispatch(req){"):]
    h = h[:h.index("if(action==='__cap_activity__')")]
    assert "if(req.panel && req.panel!==_activePanelId && !String(action).startsWith('__')){" in h
    assert "window.parent.postMessage({type:'vera:panel:dispatch', req}, '*'); return;" in h
    assert "error:'panel '+req.panel+' is not open beside this chat'" in h


def test_the_chats_open_now_rows_include_the_harness_tabs():
    assert "const theirs=(_lhmHarnessOpen||[]).map(p=>({id:p.id, label:p.label||p.id, icon:'⧉', origin:p.origin||'you', placement:p.placement||'harness tab'," in CHAT
    assert "window.parent.postMessage({type:'vera:lhm:close', id:p.id}, '*');" in CHAT
    assert "d.type!=='vera:lhm:open') return;" in CHAT
    assert "_lhmHarnessOpen=Array.isArray(d.panels)?d.panels.slice(0,40):[];" in CHAT


# ── the harness ──────────────────────────────────────────────────────────────

def test_the_harness_reports_its_tabs_under_the_chats_session_and_tells_the_chat():
    assert "function _panelsOpenList(){" in HARNESS
    assert "return { id: pid.replace(/^auto-/, ''), label, origin: 'you', placement: 'harness tab' };" in HARNESS
    assert "const list = _panelsOpenList().filter(p => p.id !== 'chat2');" in HARNESS
    assert "api('/ui/panels/open/report', 'POST', { session_id: sid, host: 'harness', panels: list });" in HARNESS
    assert "f.contentWindow.postMessage({ type: 'vera:lhm:open', panels: list }, '*');" in HARNESS
    # the chat frame is known by its LHM and its session id — CH is a top-level const there, not a window property
    assert "return !!(w && w.VeraLHM && w._veraSessionId);" in HARNESS
    render = HARNESS[HARNESS.index("function _tabRender(){"):]
    assert "_panelsOpenStart(); setTimeout(_panelsOpenReport, 400);" in render[:400]


def test_the_harness_routes_a_targeted_dispatch_to_the_tab_and_acks_like_the_chat():
    assert "if(d.type !== 'vera:panel:dispatch' || !d.req || !d.req.request_id) return;" in HARNESS
    assert "api('/ui/panels/dispatch/ack', 'POST', { request_id: req.request_id, session_id: sid, ok: !!ok, result: result === undefined ? null : result, error: error || null, action: req.action || '' })" in HARNESS
    assert "ack(false, null, 'panel ' + req.panel + ' is not open in the harness')" in HARNESS
    assert "f.contentWindow.postMessage({ type: 'vera:panel:action', action: req.action, action_id: aid, payload: req.payload || {} }, '*');" in HARNESS
    assert "ack(false, null, 'timeout: the panel did not answer')" in HARNESS
    assert "if(d.type === 'vera:panel:action_result' && d.action_id && _panelsDispatchWaits[d.action_id]){" in HARNESS


def test_the_harness_closes_and_focuses_a_tab_the_chats_row_asks_for():
    close = HARNESS[HARNESS.index("if(d.type === 'vera:lhm:close' && d.id){"):]
    close = close[:close.index("_panelsOpenReport(); return;")]
    assert "splitTab(pid);" in close and "customTabRemove(d.id);" in close
    assert "if(d.type === 'vera:lhm:focus' && d.id){" in HARNESS
