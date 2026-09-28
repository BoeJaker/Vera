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
    # ctx-graph-5: the send's turn keeps its record ids. Turns overlap, so the capture is a map keyed by the
    # question and the patch goes through _ctxPatchSentIds, which declines to guess when more than one turn is
    # in flight rather than stamping one question's records onto another's frame.
    assert "CTX_NODES=nodes;CTX_EDGES=edges;_ctxQuery=query; _ctxPatchSentIds(nodes); _ctxBroadcast();" in src
    assert "else if(m.act==='ctx'){ CTX_NODES=Array.isArray(a.nodes)?a.nodes:[];" in src
    assert "else if(m.act==='grown') _ctxRemoteSet(a.on);" in src
    assert "document.body.classList.toggle('ctx-remote', !!on)" in src
    assert "body.ctx-grown #chatColumn,body.ctx-remote #chatColumn{padding-left:var(--ctx-gutter,16px)}" in src


def test_the_runs_cross_the_seam_at_shared_heights():
    src = _read("vera", "chat", "chat_panel.html")
    assert "if(_EMBED.only==='menu'){ const b=_ctxRemoteBlock; if(!b||!(b.bottom>b.top)) return null;" in src
    assert "const seam=_EMBED.only==='menu', seamX=window.innerWidth; const lanesOut=[];" in src
    # the post goes through _ctxLanesPost now: standing down has to reach the chat instance too, or it keeps
    # drawing the last lanes it was sent (Notes/42 defect 67)
    assert "if(seam) _ctxLanesPost(lanesOut);" in src
    assert "_lhmPost('lanes',{ lanes:arr||[] });" in src
    assert "if(_EMBED.only==='chat'&&_ctxRemote.on&&!_ctxGrown){" in src
    assert "_lhmPost('block', b);" in src
    assert "function _frameTop(){ try{ const f=window.frameElement; return f?f.getBoundingClientRect().top:0; }catch(_){ return 0; } }" in src


def test_the_mini_graph_follows_the_focus_and_explode_folds_the_remote_graph():
    src = _read("vera", "chat", "chat_panel.html")
    assert "if(mid&&mid!==_ctxMiniMid){ _ctxMiniMid=mid; try{ renderCtxGraph(); }catch(_){} }" in src
    # The focused turn travels chat -> menu. It is broadcast from the FOLLOW itself, not only from the graph
    # column's sync: on the board the column is not open, so the column-only broadcast never fired and the
    # menu's graph could not move. An EMPTY mid is a real value ("the chat is at the bottom with a question
    # being typed, show the live set"), so the handler no longer requires a.mid, and it recomputes the frame
    # and repaints rather than redrawing the legacy SVG alone.
    assert "_lhmPost('focus',{ mid:m })" in src and "_lhmPost('focus',{ mid:mid })" in src
    assert "else if(m.act==='focus'){ _grFocusMid=a.mid||'';" in src
    assert "remote:_ctxRemote.on" in src and "_lhmPost('grow-set',{ on:false })" in src and "_lhmPost('grow-set',{ on:true })" in src
    assert "else if(m.act==='grow-set'){ _ctxGrow(!!a.on); }" in src
