"""
The chat's chrome as the Canvas board draws it (the design landing, step 1): the header (agent · session · tiers ·
Blocks · Explode · Cards/Front/Iso · +Canvas/+Graph · meter · mode · ⋯ · Aa), every old header control alive in the
⋯ tools sheet, the rail's glyphs, the exchanges, the capability card, the chrome row, the composer card. Text-level.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")
LIB = _read("vera", "chat", "vera-lhm.js")


def test_the_design_stylesheet_is_last_in_the_head_and_carries_the_boards_rules_with_fallbacks():
    i = HTML.index('<style id="designChrome">')
    assert HTML.index("</head>") > i and "<style" not in HTML[i + 10:HTML.index("</head>")], "the last stylesheet, so it outranks the old rules"
    css = HTML[i:HTML.index("</style>", i)]
    for s in ("#topBar{height:52px", "#topBar .agent{", "#topBar .sess #sessionName{", "#topBar #denGroup{display:flex;background:var(--surf2", "#topBar #ctxMeter{",
              "#toolsSheet{display:none;position:absolute", "#rightRail.lhm-host .lhm-rail{width:46px", "#rightRail.lhm-host .lhm-det{width:288px",
              "#msgs .mwrap{width:var(--measure", "#msgs .mwrap.u .msg-user-line{background:var(--surf", "#msgs .msg-gutter.a{color:var(--ac",
              "#msgs .cap-inline{border:none;border-radius:var(--ui-radius", "#msgs .msg-actions .mact:nth-child(n+6){display:none}",
              "#inputBar{width:calc(var(--measure", "#inputBar #cmpFt{display:flex", "body[data-view=\"minimal\"].has-msgs #msgs > .mwrap.u{background:transparent;border:none"):
        assert s in css, s
    assert "var(--t1,var(--text,#d8dce4))" in css and "var(--ac,var(--acc,#5a9e8f))" in css, "every board token falls back to the chat's old token"
    assert '<body data-view="minimal">' in HTML


def test_the_header_is_the_boards_and_every_old_control_stays_alive():
    assert '<div class="agent" title="The agent answering' in HTML and 'id="agentBarSel"' in HTML and 'id="agentMeta"' in HTML
    assert '<div class="sess">' in HTML and 'id="sessMeta"' in HTML and 'onclick="CH.autoName()"' in HTML
    assert 'id="hdrMore" onclick="CH._toolsSheet()"' in HTML and 'id="hdrAa" onclick="CH._appearanceSheet(event)"' in HTML and 'id="toolsSheet"' in HTML
    assert '<span class="xmseg pseg"' in HTML and 'id="colCanvasBtn"' in HTML and 'id="colGraphBtn"' in HTML
    for ctl in ("autoActBtn", "autoLoopBtn", "capsBtn", "panelsBtn", "webSearchBtn", "ttsBtn", "micBtn", "councilTopBtn", "sdTopBtn", "podTopBtn", "buddyTopBtn", "viewToggleBtn", "denGroup", "blocksBtn", "xplBtn", "xplSeg"):
        assert 'id="%s"' % ctl in HTML, ctl
    assert 'onclick="CH._chatSaveToNotebook()"' in HTML and 'onclick="CH._chatSaveToIde()"' in HTML and 'onclick="CH.toggleRight()"' in HTML
    fn = HTML[HTML.index("function _chromeMount(){"):HTML.index("function _lhmMount(){")]
    assert "['Execution','Execution behaviour'],['Context · tools','Context and tool integrations'],['Audio','Audio I/O'],['Modes','Special agent modes'],['Save','Save chat'],['Layout','Panel layout']" in fn
    assert "bar.insertBefore(act, g)" in fn, "the mode button (Ask first / Act) stays in the header"
    assert "VeraLHM.pick('context')" in fn, "the meter opens the Context menu"


def test_the_rail_wears_the_boards_glyphs_and_the_exchange_reads_as_the_board():
    assert "if(m.iconHtml) ico.innerHTML = m.iconHtml;" in LIB
    assert "LHM_MENUS.forEach(m=>{ if(_RAIL_SVG[m.id]) m.iconHtml=_RAIL_SVG[m.id]; });" in HTML
    for k in ("sessions", "context", "activity", "loop", "workspace", "sandbox", "ops", "settings"):
        assert re.search(r"\n\s+%s:'<svg" % k, HTML), k
    assert "gutter.textContent=(role==='user'&&(!gutterLabel||gutterLabel==='you'))?_ts():(gutterLabel||_ts());" in HTML, "the question's gutter is the time"
    assert 'class="mact-more" onclick="this.parentElement.classList.toggle(\'all\')"' in HTML
