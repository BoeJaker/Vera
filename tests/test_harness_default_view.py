"""
The harness default view as the Harness board draws it (design landing, step 4 / B1): the header (wordmark · meters ·
activity ticker · Search ⌘K · style segment · theme swatches · Aa · ⋯ with every old control), the tab bar (+N ⌘K chip,
controls at the right, ◫ for live events), the strips, live events folded by default, the dashboard toolbar and the
widgets' surfaces. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "capability_orchestration.html")


def test_the_design_stylesheet_is_last_in_the_head_and_carries_the_boards_rules():
    i = HTML.index('<style id="designHarness">')
    assert "<style" not in HTML[i + 10:HTML.index("</head>")], "the last stylesheet"
    css = HTML[i:HTML.index("</style>", i)]
    for s in ("header{height:48px", "header .vao{", "header .seg button.on{background:var(--pri-bg", "header .swp{", "#hdrSheet{display:none;position:absolute", ".tabs{height:40px;align-items:flex-end", ".tab.active::after{content:'';position:absolute;left:11px;right:11px;bottom:0;height:2px;background:var(--ac",
              ".tab.more{", ".tab-controls{order:10;margin-left:auto", "#tabSubtabs:not(:empty){display:flex", ".dash-title{font-size:15px;font-weight:600", ".dash-toolbar .live{", ".widget{background:var(--s1", ".w-head{height:34px", ".w-head .vd-rec{", "#evExpandBtn{display:none!important}"):
        assert s in css, s
    assert "var(--t1,var(--text,#d8dce4))" in css, "every board token falls back to the page's own token"


def test_the_header_and_tab_bar_carry_the_boards_pieces_and_every_old_control():
    for s in ('id="hdrAct" type="button" onclick="_hdrActToggle()"', 'onclick="openTabPicker()" title="Find a panel, a setting, a capability"', 'id="hdrStyleSeg"', 'id="hdrSwatches"', 'onclick="openThemeMenu(event)" title="Theme, style pack and UI size"', 'id="hdrMore" onclick="_hdrSheet()"', '<div id="hdrSheet"></div>',
              'id="tabEvBtn">◫</button>', 'id="tabLhmBtn">☰</button>', 'onclick="tabToggleAutohide()"', 'onclick="tabAddPane()"', 'onclick="openTabPicker()" id="tabPickerBtn"', 'onclick="openActiveStandalone()"', 'onclick="openStandalonePicker()"',
              'id="backendUrl"', 'onclick="connect()">Connect</button>', 'onclick="refreshAll()">↻</button>', 'id="hdrMetricBtn"', 'id="evSidebar"', 'id="evFeed"', 'id="dashHealthStrip"'):
        assert s in HTML, s
    fn = HTML[HTML.index("function _harnessChromeMount(){"):HTML.index("if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', ()=>{ try{ _harnessChromeMount(); }")]
    assert "while(right.firstChild){ r1.appendChild(right.firstChild); } sheet.appendChild(r1); right.style.display='none';" in fn, "the connection controls (the theme button too) move as the same elements"
    assert "if(!evOpen&&!_evSidebarCollapsed) toggleEvSidebar();" in fn, "live events fold away by default"
    assert "localStorage.setItem('vera:harness:events'" in fn


def test_the_dashboard_toolbar_is_the_boards():
    assert '<div class="dash-title">Cluster overview</div>' in HTML and 'id="dashLive"' in HTML and 'id="dashRecLbl"' in HTML
    assert "' widgets · every one a record · grid 12 × '" in HTML
    assert "function _tabsMoreChip(){" in HTML and "m.className='tab more';" in HTML
    assert "const _HDR_STYLES = [['standard','Standard'],['newspaper','News'],['terminal','Term'],['pixel','Pixel']];" in HTML
    assert "veraUI.setAppearance({style:b.dataset.style})" in HTML and "_applyThemeById(s.dataset.theme)" in HTML
