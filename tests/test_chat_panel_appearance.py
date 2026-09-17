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

from Vera.vera.theme_defs import DENSITY_TIERS, DEFAULT_DENSITY, STYLE_PACKS

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


def test_blocks_off_outranks_the_minimal_views_own_question_rules():
    """The minimal view paints the question with body[data-view]...#msgs > .mwrap.u — more specific than a
    plain html[data-blocks] .mwrap.u — so Blocks off must say it again at that specificity. A pinned question
    keeps an opaque ground (the answer would scroll through it) but loses the wash and the accent bar."""
    css = _appearance_css()
    base = HTML[:HTML.index("Density tiers and Blocks")]
    assert 'body[data-view="minimal"].has-msgs #msgs > .mwrap.u{' in base, "the minimal view moved; re-check the override"
    assert 'html[data-blocks="off"] body[data-view="minimal"].has-msgs #msgs > .mwrap.u{background:transparent;border-left-color:transparent}' in css
    # A pinned question is position:sticky and the answer scrolls underneath, so it has to hide what passes
    # behind it - that is why it keeps a ground where every other message loses one. A FLAT ground hides the
    # background wash as well, and read as a box Blocks off had failed to remove (Notes/42 defect 94). It
    # occludes through a ground at about a third opacity over a blur now: the text under it is unreadable, the
    # gradient carries through. The requirement is the occlusion, not the opacity.
    assert 'html[data-blocks="off"] body[data-view="minimal"].has-msgs #msgs > .mwrap.u.pin{background-color:color-mix(in srgb,var(--bg0) 34%,transparent);background-image:none;border-left-color:transparent;backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}' in css
    assert 'background-color:var(--bg0);background-image:none;border-left-color:transparent}' not in css, "no flat fill over the wash"
    assert 'html[data-blocks="off"] body[data-view="minimal"].has-msgs:not(.has-panel) #inputBar' in css

# ── the chat reads the style-pack tokens (M1a-surfaces) ──────────────────────

def _packs_css():
    i = HTML.index("Style packs on the chat surface")
    return HTML[i:HTML.index("</style>", i)]


def test_the_chats_own_type_variables_derive_from_the_pack():
    """A pack switch changed nothing on the chat while --sans/--mono were its own; now they follow --f-ui/--f-mono,
    with the old stacks as the fallback for a chat opened without vera-ui.js."""
    root = HTML[HTML.index(":root{"):HTML.index("}", HTML.index(":root{"))]
    assert re.search(r"--sans:var\(--f-ui,'Inter'", root)
    assert re.search(r"--mono:var\(--f-mono,'JetBrains Mono'", root)


def test_prose_display_radius_labels_and_the_primary_button_follow_the_pack():
    css = _packs_css()
    assert "html[data-style] .mbody,html[data-style] .msg-body{font-family:var(--f-prose,var(--sans))}" in css
    assert "var(--f-disp,var(--sans))" in css
    assert "font-size:var(--body,13.5px);line-height:var(--lead,1.6)" in css
    assert "text-transform:var(--label-case,none);letter-spacing:var(--label-track,0)" in css
    assert "html[data-style] .cap-inline{border-radius:var(--ui-radius,7px)}" in css
    assert "html[data-style] .ib.send{background:var(--pri-bg,var(--acc))" in css
    # the block comes after the tiers, so it is the last word on these properties
    assert HTML.index("Style packs on the chat surface") > HTML.index("Density tiers and Blocks")


def test_every_token_the_chat_reads_is_one_the_packs_declare():
    """The chat may only lean on tokens every pack carries; a typo here would silently fall back forever."""
    declared = set(STYLE_PACKS["standard"]["vars"])
    for pack in STYLE_PACKS.values():
        declared &= set(pack["vars"])
    used = set(re.findall(r"var\((--(?:f-|ui-radius|r-|label-|body|lead|pri-|fill|card)[\w-]*)", _packs_css() + HTML[HTML.index(":root{"):HTML.index(":root[data-theme=light]")]))
    assert used, "no pack tokens read"
    missing = used - declared
    assert not missing, f"the chat reads tokens no pack declares: {sorted(missing)}"


# ── the stylesheet the panel ships still parses as one ───────────────────────

def test_the_appearance_block_is_balanced_and_inside_the_head():
    css = _appearance_css()
    assert css.count("{") == css.count("}")
    assert HTML.index("Density tiers and Blocks") < HTML.index("</head>")
