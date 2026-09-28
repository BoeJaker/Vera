"""
The relational edge routing at parity with the Canvas board's router (live defects 9 · 10 · 11 of Notes/42):
the context's runs are an ORDERED set — one lane per type in the graph|chat channel, one arrival per type fanned down
the block, ordered by where they are going; exploded, they end on the station in focus (its left edge, the Iso plate's
own slanted edge), in the one screen space; the chat→canvas runs take ordered lanes in their own channel; a hand-added
canvas item carries no turn anchor and gets no run. The channels are the columns' own insets. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")
EL = _read("vera", "canvas", "canvas_element.js")


def test_the_context_runs_take_ordered_lanes_and_arrivals():
    # grouped by type, ordered by where they are going: the lanes (dn then up) and the arrivals (fanned in plot order)
    assert "const dn=G.filter(g=>g.my<=g.ty).sort((a,b)=>b.my-a.my), up=G.filter(g=>g.my>g.ty).sort((a,b)=>a.my-b.my);" in HTML
    assert "dn.concat(up).forEach((g,li)=>{ g.lane=li; });" in HTML, "the lane is the type's place in the ordered set"
    assert "const lx=G0+(leftSide?1:-1)*u*Math.min(HWG, 9*(rn-1));" in HTML, "one lane per type about the channel's middle"
    assert "Gs.forEach((g,j)=>{ g.ty=mTop+mH/2+(j-(n-1)/2)*fan; }); });" in HTML, "one arrival per type, fanned down the block (per turn since ctx-graph-5)"
    assert "if(fan*(n-1)>mH-8) fan=Math.max(4, (mH-8)/Math.max(1,n-1));" in HTML, "never past the block's edges"
    # the branches are hairlines, the trunk carries the colour; a type crosses the plot once
    assert "out+=L(cx, ye, lx, ye, 'tr');" in HTML and "#ctxRunsOverlay line.br{stroke-opacity:.35;stroke-width:1}" in HTML
    assert "out+=L(lx, g.ty, edgeAt(g.ty, g.side), g.ty, 'tr');" in HTML, "the arrival ends on ITS block's edge (the question or the response — ctx-graph-5)"


def test_exploded_the_runs_end_on_the_station_in_the_one_screen_space():
    assert "function _ctxRunsTarget(){" in HTML and "if(_xpl.on&&_xplEl&&document.body.classList.contains('exploded')){" in HTML
    assert "return {kind:'station', mid:sel, left:Math.max(r.left,H.left), right:Math.min(r.right,H.right)" in HTML, "the station's visible edge"
    assert "function _xplStationEl(mid){" in HTML and "function _xplStationEdgeAt(st, y, side){" in HTML, "the Iso plate's slanted edge is the edge"
    assert "svg.dataset.target=T.kind;" in HTML
    # the runs follow the scene's renders, pans and zooms, and re-aim when the scene opens or closes
    assert "_ctxRunsFollowScene(); _xplEl.addEventListener('vera:xpl:close', ()=>_xplClose());" in HTML
    assert "try{ _ctxRunsDraw(); _cvRunsDraw(); }catch(_){}   // the runs end on the station now, or on the block again" in HTML
    # the canvas runs leave the station too
    assert "function _cvRunsSource(mid, msgs){" in HTML and "rightAt:(y)=>Math.min(H.right, _xplStationEdgeAt(st, y, 'right'))" in HTML


def test_the_channels_are_the_geometrys_own_insets():
    assert "body.ctx-grown #chatColumn,body.ctx-remote #chatColumn{padding-left:var(--ctx-gutter,16px)}" in HTML   # the gutter is the same whether the graph grew here or in the harness's menu
    assert 'body[data-cols~="canvas"] #canvasColumnBody{padding-left:var(--cv-gutter,18px)}' in HTML
    assert "const px=Math.min(44, 16+18*Math.max(0, rn-1));" in HTML, "the design's graph|chat channel, 44 px at most"
    assert "const px=Math.min(76, 18+18*Math.max(0, n-1));" in HTML, "the design's chat|canvas channel, 76 px at most"


def test_the_canvas_runs_take_ordered_lanes_from_the_blocks_clipped_edge():
    assert "dn.slice().reverse().concat(up).forEach((r,li)=>{ r.lane=li; });" in HTML, "the runs going down, the furthest first, then up"
    assert "const gx=G1+u*Math.min(HW, cap);   // its lane in the channel" in HTML
    assert "let pts=[{x:M.right, y:my},{x:gx, y:my},{x:gx, y:iy},{x:it.rect.left, y:iy}];" in HTML, "down the gutter, never diagonally"
    assert "right:Math.min(r.right,MB.right-1)" in HTML, "a run leaves the block where the transcript shows it"
    assert "window.VeraCanvas.checkRoutes(drawn, rects), {tier:den, lanes:routes.map(r=>r.lane)}" in HTML


def test_a_hand_added_item_relates_to_no_turn():
    # the add bar and the panel picker: yours, beside the turn in view, no turn anchor — so no run
    assert "args.anchor = { origin: 'you', beside: focusMid };" in EL
    assert EL.count("origin: 'you', beside: focusMid") == 4, "the add bar, the terminal picker, the widget sheet and the panel picker"
    assert "if (focusMid) args.anchor = { turn: focusMid, mid: focusMid };" not in EL
    # the placer levels it with the turn it was added beside; the head says whose it is
    assert "const levelOf = (it) => it.mid || it.beside || '';" in EL and "beside: c.dataset.beside || ''" in EL
    assert "added by you — it relates to no turn" in EL
    # what a reply made, and what a loop wrote, keep their turn anchors
    assert "anchor:{mid, turn:mid}" in HTML and "anchor:{mid:R.mid, turn:R.mid}" in HTML
    # the router draws only from real anchors
    assert "const items=its.filter(it=>it.mid&&!it.out&&it.state==='now'" in HTML
