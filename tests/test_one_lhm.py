"""
ONE LHM (the Harness board: "a UI open: its menu IS this menu — the chat board embedded menu-only; nothing here is a
copy"; the Canvas board: "embedded in parts: a host's LHM slot takes the menu alone; the main area takes the chat
without a rail of its own"; "inside the harness: ☰ at the top of the rail" is the host's ☰). The chat page has the
two embed modes (?only=menu · ?only=chat&harness=yes) joined by the session and a BroadcastChannel; the harness hosts
the chat's MENU instance in its LHM slot while the chat is open and loads the chat's tab chat-only; the LHM's
"+ Add a widget" opens the shared widget surface when it exists and a record added draws its live face. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


CHAT = _read("vera", "chat", "chat_panel.html")
HARNESS = _read("vera", "capability_orchestration.html")
LHM = _read("vera", "chat", "vera-lhm.js")


def test_the_chat_has_the_two_embed_modes():
    assert "document.documentElement.setAttribute('data-only', o)" in CHAT and "setAttribute('data-harness','yes')" in CHAT, "the modes are on the root before the CSS parses"
    assert 'html[data-only="menu"] body > :not(:has(#rightRail)){display:none!important}' in CHAT, "menu-only: the rail + quick menus alone"
    assert 'html[data-only="menu"] #rightRail .lhm-det{display:flex!important}' in CHAT, "the panel is always open — the frame IS the menu"
    assert 'html[data-only="chat"] #rightRail{display:none!important}' in CHAT, "chat-only: no rail of its own"
    assert 'html[data-only="chat"] body.ctx-grown #rightRail{display:flex!important}' in CHAT, "the grown context still opens as the graph column"
    assert 'html[data-harness="yes"] #rightRail .lhm-rail .lhm-ico.top{display:none!important}' in CHAT, "the host's ☰ does the top-level menu"


def test_the_two_instances_are_one_chat():
    assert "const _LHMBC=(function(){ try{ return new BroadcastChannel('vera:chat:lhm'); }" in CHAT
    assert "if(_EMBED.only==='menu'){ _lhmPost('grow',{ on:(on==null)?true:!!on }); return _ctxGrown; }" in CHAT, "grow goes to the chat instance"
    assert "if(_EMBED.only==='menu'&&name!=='graph'){ _lhmPost('page',{ name }); return true; }" in CHAT
    assert "if(_EMBED.only==='menu'){ _lhmPost('explode',{ on }); return; }" in CHAT
    assert "if(_EMBED.only==='menu'){ _lhmPost('newSession',{}); return; }" in CHAT
    assert "if(_EMBED.only!=='menu') _lhmPost('session',{ sid:SID });" in CHAT, "the chat instance announces its session"
    assert "window.addEventListener('storage', ev=>{ if(ev.key==='vera_session_id'&&ev.newValue&&ev.newValue!==SID){ try{ loadSession(ev.newValue); }catch(_){} } });" in CHAT, "the menu instance follows the session"
    assert "if(m.act==='grow') _ctxGrow(a.on!==false); else if(m.act==='page') togglePage(a.name); else if(m.act==='explode') explode(a.on);" in CHAT, "the chat instance answers"
    assert "try{ _embedMount(); }catch(e){ console.warn('embed', e); }" in CHAT


def test_the_harness_hosts_the_chats_menu_instance():
    assert "function _chatFrameFor(pid){" in HARNESS and "function _lhmMenuFrame(nav, src){" in HARNESS
    assert "const want = String(src || '').split('?')[0] + '?only=menu&harness=yes';" in HARNESS, "the chat board embedded menu-only"
    assert "if(chatFrame){ const navEl = document.getElementById('lhmNav'); if(navEl) navEl.classList.add('chatmenu'); tt.textContent = 'Chat · menu';" in HARNESS, "its menu IS this menu; the ☰ bar stays above it"
    assert "f.setAttribute('src', _hostFrameSrc(src));" in HARNESS and "'only=chat&harness=yes'" in HARNESS, "the chat's tab loads chat-only"
    assert "#lhmNav.lhm-nav.absorbed.chatmenu{width:318px!important}" in HARNESS, "the board's 318 px"
    assert "document.getElementById('lhmMenuHost')?.classList.toggle('on', chatOpen);" in HARNESS, "one persistent frame, shown while the chat is the open UI"
    assert '<script src="/ui/widgets/widget_element.js"></script>' in HARNESS, "the widget surface opens at harness level for the menu instance"
    assert "VeraLHM.absorb(abs, nav.lhm, id => {" in HARNESS, "the spec-absorb for other panels stays"


def test_the_lhm_adds_widgets_through_the_shared_surface():
    assert "function _surface(){" in LHM and "p.VeraWidgetConfig" in LHM, "the surface here, or in the host that embeds this menu"
    assert "S.open({ mode:'add', into:'lhm', title:'Add to ' + (m.title || m.label), templates:true, menuItems:items })" in LHM
    assert "qs0.added.push({ label:rec.title || rec.form || 'widget', c:rec.source || rec.form || '', form:rec.form, record:rec });" in LHM
    assert "var vw = document.createElement('vera-widget'); try{ vw.setAttribute('record', JSON.stringify(a.record)); }catch(e){}" in LHM, "a record added draws its live face"
    assert "foot.setAttribute('data-w', 'edit foot · controls')" not in LHM, "the foot is the editor's, not a widget (its tag sat over the button)"
