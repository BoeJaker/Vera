"""The relation runs: the switches, the stand-down, and the tiers.

Two things the user asked for on the design edge (Notes/42 defects 67 and 70):

  * the runs went on being drawn while the page's own top-level menu was open. That menu takes the detail column the
    context graph sits in, and the graph's node positions outlive the graph — so the runs were routed from a column
    that now held a menu, reading as if the MENU were what the turn had read;
  * they could only be switched off from the tools sheet, and only the canvas family read Full / Hover / Zen.

These are source checks: the routing itself is measured on the page (the runs' geometry has its own check in the
element), and what they read here is that each rule is in the code that draws them.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_each_family_has_its_switch_on_its_own_column():
    src = _read("vera", "chat", "chat_panel.html")
    assert 'data-runs="cv" onclick="CH._runsToggle(\'cv\')"' in src
    assert 'data-runs="ctx" onclick="CH._runsToggle(\'ctx\')"' in src
    # …and every switch for a family shows that family's one state
    assert "function _runsSync()" in src
    assert "_runsSync();" in src


def test_the_runs_stand_down_while_a_menu_is_over_them():
    src = _read("vera", "chat", "chat_panel.html")
    assert "function _runsBlocked()" in src
    assert "#rightRail .lhm-rail .lhm-ico.top.on" in src
    assert "#toolsSheet.on" in src
    assert "if(_runsOff.cv||_runsBlocked())" in src
    assert "if(_runsOff.ctx||_runsBlocked())" in src


def test_the_runs_follow_the_transcript_at_frame_rate():
    """Notes/42 defect 66, the half that measures. Sampled while the transcript scrolled: the block moved 14px and the
    arrivals 3; at 33ms the block had moved another 33 and the arrivals none; at 50ms they jumped 60-120 at once. The
    runs were redrawn only from _ctxFollow, which is throttled to 150ms because it also re-feeds the graph element."""
    src = _read("vera", "chat", "chat_panel.html")
    assert "function _runsSoon()" in src
    assert "_runsSoonRaf=(window.requestAnimationFrame||setTimeout)" in src
    # the scroll redraws the runs every frame and leaves the element's re-sync on its own throttle
    assert "_grFocusMid=''; _runsSoon(); _ctxFollow(); }, {passive:true}); }" in src
    assert "if(now||Date.now()-_ctxFollowT>150)" in src


def test_a_surface_opening_or_closing_is_a_redraw():
    # nothing else redraws when a sheet or a menu goes away, so the runs would stay gone until the next scroll
    src = _read("vera", "chat", "chat_panel.html")
    assert "if(b) b.classList.toggle('on', v); try{ _ctxRunsDraw(); _cvRunsDraw(); }catch(_){} }" in src
    assert "function _cmClose(){ if(_cm&&_cm.parentNode) _cm.parentNode.removeChild(_cm); _cm=null; try{ _ctxRunsDraw(); _cvRunsDraw(); }catch(_){} }" in src
    assert "t.closest('#rightRail .lhm-rail .lhm-ico')||t.closest('#hdrMore')||t.closest('.cmw')" in src


def test_a_graph_that_is_not_on_screen_routes_nothing():
    src = _read("vera", "chat", "chat_panel.html")
    assert "function _runsGraphVisible()" in src
    assert "||!_runsGraphVisible()){ _ctxRunsStand(svg); return; }" in src


def test_the_context_runs_read_the_tier_the_canvas_runs_read():
    src = _read("vera", "chat", "chat_panel.html")
    # Zen draws none
    assert "if(_den()==='zen'){ _ctxRunsStand(svg); return; }" in src
    # Hover draws the type under the pointer, or everything when the block in focus is under it
    assert "if(_den()==='hover') G=_ctxHoverBlock?G:(_ctxHoverSrc?G.filter(g=>g.src===_ctxHoverSrc):[]);" in src
    assert "let _ctxHoverSrc='', _ctxHoverBlock=false;" in src
    # …and the tier change redraws them: it only ever redrew the canvas family, so the context runs kept the last tier's
    assert "try{ _cvRunsDraw(); _ctxRunsDraw(); }catch(_){}   // BOTH families read the tier" in src


def test_the_menu_instance_says_when_it_draws_no_lanes():
    # the chat instance draws the channel from lanes the menu instance posts: standing down has to be posted too, or
    # the chat keeps drawing the last set it was sent
    src = _read("vera", "chat", "chat_panel.html")
    assert "function _ctxLanesPost(arr)" in src
    assert "if(seam) _ctxLanesPost(lanesOut);" in src
    assert "if(_EMBED.only==='menu') _ctxLanesPost([]);" in src
