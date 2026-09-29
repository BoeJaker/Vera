"""
The relational edge routing, live: the context's runs end at the message's surface; what a reply made (capability
cards, code, tables, diagrams, widgets, images) lands on the session canvas as keyed items anchored to the turn through
the resolver, so the canvas routing and the exploded scene's landed layer have items. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")


def test_the_runs_end_at_the_message_surface_and_a_reply_lands_on_the_canvas():
    assert "const surf=w.querySelector('.msg-user-line')||w.querySelector('.msg-body')||w;" in HTML and "M=surf.getBoundingClientRect()" in HTML
    assert "function _cvHarvest(body){" in HTML and "async function _cvLandReply(mid){" in HTML
    for k in ("k:'cap'", "k:'code'", "k:'diagram'", "k:'table'", "k:'widget'", "k:'image'"):
        assert k in HTML, k
    # keyed to the turn, never doubled - and an item may name its OWN key, which a source does: it is keyed by the
    # page rather than by where in the reply it was found, or the same page lands once as the research run
    # announced it, again as the reply cited it, and again on every later turn that mentions it. The turn key is
    # still the default, because for everything else the turn IS the identity.
    assert "const key=m.key||('turn:'+mid+':'+m.k+':'+i); if(_CV_LANDED[key]) continue;" in HTML, "keyed to the turn, never doubled"
    assert "k:'source'" in HTML and "k:'term'" in HTML, "a result lands as what it is" 
    # the anchor keeps the TURN (the station is keyed by it) and records the maker: the reply that produced the
    # item, so its run leaves the reply's block rather than the question above it (Notes/42 defect 87)
    assert "_capCall('canvas.add',{session_id:SID, kind:m.kind, content:m.content, key, at:'now', size:_cvLandSize(m), anchor:{mid, turn:mid, from:(w&&w.dataset&&w.dataset.mid)||mid}})" in HTML   # a diagram lands at m, a widget at its record's size (Notes/42 defect 52)
    assert "try{ _cvLandReply(_cvRelMid).then(()=>{ try{ _cvRelevance(_cvRelMid," in HTML, "land, then the relevance engine"
    assert "d:'canvas · '+(r.resolved==='shown'?'already there':'lifted out of the reply')" in HTML, "the exploded scene's landed layer sees it"


def test_the_runs_end_on_the_block_or_the_station_and_only_real_anchors_draw():
    # the target is measured once, in the one screen space: the block (its content edge), or exploded the station
    assert "function _ctxRunsTarget(){" in HTML and "return {kind:'message', mid:w.dataset.mid||'', left:M.left, right:M.right, content:body?body.getBoundingClientRect().left:M.left" in HTML
    # The overlay draws two families now - what the turn read and what the reply said - so the target is a PAIR,
    # and standing down is a helper both of them share rather than a line repeated at each bail-out.
    assert "const _ctxRunsStand=(svg)=>{ svg.innerHTML=''; svg.removeAttribute('data-lit'); if(_EMBED.only==='menu') _ctxLanesPost([]); };" in HTML
    assert "const T=TT.u||TT.a; if(!T||!rn){ _ctxRunsStand(svg); return; }" in HTML, "the grown graph's draw stands down through the helper"
    # and so do the two inside the remote-menu branch, which kept the old bare clear and left the lit source behind
    assert "const TT=_ctxRunsTargets(); const T=TT.u||TT.a; if(!T){ _ctxRunsStand(svg); return; }" in HTML
    assert "if(Date.now()-_ctxTypingT<900){ _ctxRunsStand(svg); if(!_ctxTypingTimer)" in HTML
    # a reply's items and a loop's item carry the turn; the canvas router draws from real anchors only
    assert "const items=its.filter(it=>it.mid&&!it.out&&it.state==='now'" in HTML and "wanted.forEach(it=>{ const M=_cvRunsSource(_srcMid(it), msgs); if(M) routes.push({it, M}); });" and "const _srcMid=(it)=>it.from||it.mid;" in HTML and "routes.push({it, M})" in HTML
