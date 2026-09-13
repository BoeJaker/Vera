"""
The Settings page (the Settings board): ⌘, opens a page — the index (Agent loop · Chat · Context · Appearance, Find a
setting) beside the content, one section per index row; the fields of the Config, Loop and Context panes MOVE into
their sections the first time it opens (the same elements, ids and handlers untouched) and the panes keep a line saying
where they went; the Settings menu's CTA, the Loop menu's CTA and the loop tab's ⚙ open it. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")


def test_the_page_and_its_index():
    assert '<div id="settingsPage" class="spage" hidden data-w="settings · page">' in HTML
    assert '<input type="search" placeholder="Find a setting" oninput="CH._spFilter(this.value)">' in HTML
    for grp in ("{grp:'Agent loop'", "{grp:'Chat'", "{grp:'Context'", "{grp:'Appearance'"):
        assert grp in HTML, grp
    assert "if((ev.metaKey||ev.ctrlKey)&&ev.key===','){ ev.preventDefault(); _settingsPage(); }" in HTML, "⌘,"


def test_the_fields_move_with_their_ids_and_handlers():
    assert "function _spTake(paneId, title){" in HTML and "els.forEach(el=>block.appendChild(el));" in HTML, "the same elements move; nothing is copied"
    assert "l.innerHTML='<span>'+moved[pid]+' section'+(moved[pid]>1?'s':'')+' moved to <b>Settings</b> — ⌘,</span>" in HTML, "the panes say where their fields went"
    for pair in ("['rpLoop','Agent loop variant']", "['rpCfg','Agent']", "['rpCtx','Sources']", "['rpLoop','Prompt template']", "['rpCfg','Telegram HITL']"):
        assert pair in HTML, pair


def test_the_ways_in():
    assert "cta:{label:'Open Settings ⌘,', run:()=>_settingsPage(true)}}," in HTML
    assert "cta:{label:'Loop settings ⚙', run:()=>_settingsPage(true,'variant')}}," in HTML
    assert "onclick=\"CH._settingsPage(true,'variant')\"" in HTML, "the loop tab's ⚙ Config"
    assert "_settingsPage,_spShow,_spFilter" in HTML
