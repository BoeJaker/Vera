"""
The Context menu grows INTO the full graph in place — in the harness too, where the chat is two instances of one page:
the MENU instance grows its own rail (and the harness widens the slot), the CHAT instance never grows a rail while the
menu holds the graph; the chat instance feeds the menu its context and its turn in focus; the runs cross the seam
between the frames at shared heights; Explode folds the remote graph too. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def test_menu_instance_grows_in_place_and_the_harness_widens():
    src = _read("vera", "chat", "chat_panel.html")
    assert "window.parent.postMessage({ type:'vera:lhm:grow', on:on, width:on?706:318 }, '*')" in src
    assert "_lhmPost('grown',{ on:on })" in src
    assert "_lhmPost('grow',{ on:(on==null)?true:!!on }); return _ctxGrown;" not in src
    harness = _read("vera", "capability_orchestration.html")
    assert "#lhmNav.lhm-nav.absorbed.chatmenu.grown{width:706px!important}" in harness
    assert "d.type!=='vera:lhm:grow'" in harness and "nav.classList.toggle('grown', !!d.on)" in harness


def test_chat_instance_feeds_the_menu_and_holds_no_rail():
    src = _read("vera", "chat", "chat_panel.html")
    assert "function _ctxBroadcast(){ if(_EMBED.only==='menu') return;" in src
    assert "renderCtxGraph();renderCtxList();renderFabPane(); _ctxBroadcast();" in src
    assert "CTX_NODES=nodes;CTX_EDGES=edges;_ctxQuery=query; _ctxBroadcast();" in src
    assert "else if(m.act==='ctx'){ CTX_NODES=Array.isArray(a.nodes)?a.nodes:[];" in src
    assert "else if(m.act==='grown') _ctxRemoteSet(a.on);" in src
    assert "document.body.classList.toggle('ctx-remote', !!on)" in src
    assert "body.ctx-grown #chatColumn,body.ctx-remote #chatColumn{padding-left:var(--ctx-gutter,16px)}" in src


def test_the_runs_cross_the_seam_at_shared_heights():
    src = _read("vera", "chat", "chat_panel.html")
    assert "if(_EMBED.only==='menu'){ const b=_ctxRemoteBlock; if(!b||!(b.bottom>b.top)) return null;" in src
    assert "const seam=_EMBED.only==='menu', seamX=window.innerWidth; const lanesOut=[];" in src
    assert "if(seam) _lhmPost('lanes',{ lanes:lanesOut });" in src
    assert "if(_EMBED.only==='chat'&&_ctxRemote.on&&!_ctxGrown){" in src
    assert "_lhmPost('block', b);" in src
    assert "function _frameTop(){ try{ const f=window.frameElement; return f?f.getBoundingClientRect().top:0; }catch(_){ return 0; } }" in src


def test_the_mini_graph_follows_the_focus_and_explode_folds_the_remote_graph():
    src = _read("vera", "chat", "chat_panel.html")
    assert "if(mid&&mid!==_ctxMiniMid){ _ctxMiniMid=mid; try{ renderCtxGraph(); }catch(_){} }" in src
    assert "_lhmPost('focus',{ mid:mid })" in src and "else if(m.act==='focus'&&a.mid)" in src
    assert "remote:_ctxRemote.on" in src and "_lhmPost('grow-set',{ on:false })" in src and "_lhmPost('grow-set',{ on:true })" in src
    assert "else if(m.act==='grow-set'){ _ctxGrow(!!a.on); }" in src
