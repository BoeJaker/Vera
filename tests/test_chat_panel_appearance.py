"""
The chat draws itself by the appearance the whole UI shares (UI redesign M1b,
Notes/40 §1): the density tier (data-den = full | hover | zen) and Blocks
(data-blocks = on | off) live on <html>, painted by vera-ui.js (M1a); the chat
panel styles its transcript by them and offers the controls in its top bar.

These tests hold the chain together: the tier names the panel styles are the
ones theme_defs declares; the controls call exported handlers; the handlers ask
vera-ui.js (and fall back to the very same localStorage keys it reads); rendered
output is never hidden by a tier — only chrome is; Blocks off reaches every
surface, not just the transcript. chat_panel.html is text, so this runs anywhere.
"""
import os
import re

from Vera.vera.theme_defs import DENSITY_TIERS, DEFAULT_DENSITY

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")
UI_JS = _read("vera", "vera-ui.js")


def _appearance_css():
    i = HTML.index("Density tiers and Blocks")
    return HTML[i:HTML.index("</style>", i)]


# ── the tiers the panel styles are the tiers the theme system declares ───────

def test_every_declared_tier_has_rules_and_a_button():
    css = _appearance_css()
    for tier in DENSITY_TIERS:
        assert f'html[data-den="{tier}"]' in css, f"no rules for the {tier} tier"
        assert re.search(rf'<button class="tb-btn" data-den="{tier}"\s+onclick="CH\.setDensity\(\'{tier}\'\)"', HTML), f"no button for {tier}"
    # and no tier the theme system does not know
    named = set(re.findall(r'html\[data-den="(\w+)"\]', css))
    assert named == set(DENSITY_TIERS)
    assert DEFAULT_DENSITY in named


def test_the_controls_sit_in_the_top_bar_beside_the_layout_toggles():
    i = HTML.index('id="denGroup"')
    assert HTML.index('id="blocksBtn"') > i
    assert HTML.index('<!-- Layout toggles -->') > i
    assert 'onclick="CH.toggleBlocks()"' in HTML
    # the group appears once: one set of controls, not one per re-render
    assert HTML.count('id="denGroup"') == 1


# ── the handlers exist, are exported, and run from init ─────────────────────

def test_handlers_are_exported_and_initialised():
    assert re.search(r"^\s*function setDensity\(id\)", HTML, re.M)
    assert re.search(r"^\s*function toggleBlocks\(\)", HTML, re.M)
    assert re.search(r"^\s*setDensity,toggleBlocks,\s*$", HTML, re.M), "not exported on CH"
    # painted from the start, after the view mode is restored and before the rails are unified
    init = HTML[HTML.index("setViewMode(savedMode);"):]
    assert init.index("_appearanceInit();") < init.index("_unifyRails();")


def test_the_panel_asks_vera_ui_and_falls_back_to_its_keys():
    """One setting, not two: the fallback must store under the keys vera-ui.js reads."""
    assert "veraUI.setAppearance(patch)" in HTML
    den_key = re.search(r"DEN_KEY\s*=\s*'([^']+)'", UI_JS).group(1)
    blocks_key = re.search(r"BLOCKS_KEY\s*=\s*'([^']+)'", UI_JS).group(1)
    assert f"localStorage.setItem('{den_key}'" in HTML
    assert f"localStorage.setItem('{blocks_key}'" in HTML
    assert "function setAppearance(patch)" in UI_JS, "M1a's runtime is the one the panel talks to"


def test_buttons_mirror_the_root_not_their_own_memory():
    """A change made elsewhere (the harness, another frame) must show here too."""
    assert "new MutationObserver(_appearanceSync)" in HTML
    assert "attributeFilter:['data-den','data-blocks']" in HTML
    assert "b.dataset.den===den" in HTML


# ── what a tier may hide: chrome, never the answer ───────────────────────────

def test_hover_folds_records_to_a_peek_and_opens_them_on_click():
    css = _appearance_css()
    assert 'html[data-den="hover"] .cap-inline:not(.open) .cap-result{display:none!important}' in css
    assert 'html[data-den="hover"] .cap-inline:not(.open) .cap-peek{display:block!important}' in css
    assert 'html[data-den="hover"] .cap-inline.open .cap-result{display:block!important}' in css
    # the click that opens a folded record lives in the panel's logic, gated to the hover tier
    assert "getAttribute('data-den')!=='hover') return;" in HTML
    assert "card.classList.toggle('open')" in HTML


def test_hover_opens_room_for_the_chrome_instead_of_covering_the_words():
    css = _appearance_css()
    assert 'html[data-den="hover"] .mwrap:hover{padding-bottom:34px}' in css
    assert re.search(r'html\[data-den="hover"\] \.msg-actions\{position:absolute;[^}]*bottom:6px', css)


def test_zen_keeps_rendered_output_and_narrows_the_column():
    css = _appearance_css()
    # a record that rendered a report keeps its result; images are never hidden
    assert 'html[data-den="zen"] .cap-inline:has(.cap-md) > .cap-result{display:block!important}' in css
    assert 'html[data-den="zen"] .cap-inline:not(:has(.cap-md)) > :not(.cap-imgs){display:none!important}' in css
    assert re.search(r'html\[data-den="zen"\] \.mwrap\{max-width:820px', css)
    # what zen removes is chrome
    assert 'html[data-den="zen"] .msg-actions,html[data-den="zen"] .think-box{display:none!important}' in css


def test_full_shows_the_chrome_at_rest():
    assert 'html[data-den="full"] .msg-actions{opacity:1}' in _appearance_css()


# ── Blocks off reaches every surface ─────────────────────────────────────────

def test_blocks_off_strips_backgrounds_from_every_surface():
    css = _appearance_css()
    for sel in (".mwrap.u", ".cap-inline", ".cap-result", "#inputBar", "#topBar", "#rightRail", ".ctx-tab-bar", ".think-box"):
        assert f'html[data-blocks="off"] {sel}' in css or f',html[data-blocks="off"] {sel}' in css, f"Blocks off misses {sel}"


# ── the stylesheet the panel ships still parses as one ───────────────────────

def test_the_appearance_block_is_balanced_and_inside_the_head():
    css = _appearance_css()
    assert css.count("{") == css.count("}")
    assert HTML.index("Density tiers and Blocks") < HTML.index("</head>")
