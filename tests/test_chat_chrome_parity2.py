"""
Three parity findings of the review (Notes/43, Canvas rows 11 · 5 · 15): the header's agent button reads model · node
beneath the name; the Sessions menu groups by day and titles an unnamed session by its first question; the ☰ top-level
list carries every panel (searchable; a row opens it beside the chat), the menus beneath. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")
LHM = _read("vera", "chat", "vera-lhm.js")


def test_the_agent_button_reads_model_and_node():
    assert "const node=String(a.node||a.host||a.route_node||(a.routing&&(a.routing.node||a.routing.host))||'');" in HTML
    assert '(node?`<span class="node"> · ${esc(node)}</span>`:\'\')' in HTML, "model · node beneath the name (the board)"


def test_sessions_are_grouped_by_day_and_titled_by_their_first_question():
    assert "return t===today?'Today':t===yest?'Yesterday':(t||'Earlier'); };" in HTML
    assert "const titleOf=s=>String(s.displayName||s.name||(s.preview?String(s.preview)" in HTML, "a session is its first question until it is named"
    assert "(i===0||day(rows[i-1])!==day(s)?'<div class=\"grp\">'+esc(day(s))" in HTML, "a group head per day"


def test_the_top_level_list_is_every_panel():
    assert "panels:()=>{ try{ return (UI_PANELS||[]).map(p=>({ id:p.id, label:p.label||p.id, icon:p.icon||'▭', open:()=>{ try{ _slashOpenPanel(p.id); }catch(_){} } })); }catch(_){ return []; } }," in HTML
    assert "q.placeholder = 'find a panel, a setting, a capability';" in LHM
    assert "_top.appendChild(_el('div', 'lhm-sec', 'Panels · ' + panels.length + (qq ? ' · ' + shown.length + ' match' : '') + ' · ⌘K'));" in LHM
    assert "meta.textContent = (np ? np + ' panels · ⌘K' : (_cfg.menus || []).length + ' menus') + ' · ' + _openNow().length + ' open';" in LHM
    assert "_top.appendChild(_el('div', 'lhm-sec', 'Menus · ' + (_cfg.menus || []).length));" in LHM, "the menus stay listed beneath"
