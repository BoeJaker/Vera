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


def test_theme_picker_is_injected_once_the_card_is_in_the_document():
    # the mirror showed an empty Theme card: injectPicker looks its container up by id, so it must run after the page is built
    src = _read("vera", "chat", "chat_panel.html")
    build =src[src.index("function _spBuild(){"):src.index("function _spShow(id){")]
    assert build.count("injectPicker('spThemePick')") == 1
    assert build.index("cn.appendChild(sec); });") < build.index("injectPicker('spThemePick')") < build.index("_spShow('variant');")


def test_header_tightens_in_a_narrow_frame():
    # the harness's chat frame is 1122–1178 px at 1440: the header must not overflow (⋯ and Aa fell off the edge)
    src = _read("vera", "chat", "chat_panel.html")
    assert "@media (max-width:1300px){" in src and "#topBar{gap:6px;padding:0 10px}" in src
    assert "#topBar .agent #agentMeta,#topBar .agent #capBadge{display:none}" in src
    assert "#topBar #ctxMeterBar{width:56px}" in src


def test_the_page_and_its_index():
    assert '<div id="settingsPage" class="spage" hidden data-w="settings · page">' in HTML
    assert '<input type="search" placeholder="Find a setting" oninput="CH._spFilter(this.value)">' in HTML
    for grp in ("{grp:'Agent loop'", "{grp:'Chat'", "{grp:'Context'", "{grp:'Appearance'"):
        assert grp in HTML, grp
    assert "if((ev.metaKey||ev.ctrlKey)&&ev.key===','){ ev.preventDefault(); _settingsPage(); }" in HTML, "⌘,"


def test_the_group_page_is_a_grid_of_section_cards():
    # the board: an index row selects its GROUP; the group's sections are tiled as cards; a row's cards light up
    assert "const _SP_ROWGRP={};" in HTML and "card.className='sp-card'; card.dataset.row=r.id;" in HTML
    assert "page.querySelectorAll('.sp-card').forEach(c=>c.classList.toggle('on', c.dataset.row===id));" in HTML
    assert "#settingsPage .sp-grid{display:grid;" in HTML and '#settingsPage .sp-card .opt>input[type="checkbox"]{position:static;opacity:1;' in HTML
    # the pack segment in the header; Appearance drawn in place
    assert "w.className='sp-hd-pack'; w.appendChild(veraUI.makeStyleControl());" in HTML and "veraUI.injectPicker('spThemePick')" in HTML


def test_the_fields_move_with_their_ids_and_handlers():
    assert "function _spTake(paneId, title){" in HTML and "els.forEach(el=>card.appendChild(el));" in HTML, "the same elements move; nothing is copied"
    assert "l.innerHTML='<span>'+moved[pid]+' section'+(moved[pid]>1?'s':'')+' moved to <b>Settings</b> — ⌘,</span>" in HTML, "the panes say where their fields went"
    for pair in ("['rpLoop','Agent loop variant']", "['rpCfg','Agent']", "['rpCtx','Sources']", "['rpLoop','Prompt template']", "['rpCfg','Telegram HITL']"):
        assert pair in HTML, pair


def test_the_ways_in():
    assert "cta:{label:'Open Settings ⌘,', run:()=>_settingsPage(true)}}," in HTML
    assert "cta:{label:'Loop settings ⚙', run:()=>_settingsPage(true,'variant')}}," in HTML
    assert "onclick=\"CH._settingsPage(true,'variant')\"" in HTML, "the loop tab's ⚙ Config"
    assert "_settingsPage,_spShow,_spFilter" in HTML
