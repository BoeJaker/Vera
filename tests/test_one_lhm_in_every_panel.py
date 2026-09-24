"""One left-hand menu, in every panel that has one.

A panel's menu used to be whatever that panel's author wrote: three of them
(the Estate, Capabilities, Agents) rotated their section names ninety degrees
in a 40px rail; five tagged themselves `data-vera-lhm` for the nav bridge but
kept a full copy of the shared chrome in their own <style>; Business and the
Store did not have a left-hand menu at all — Business scrolled its sections
sideways along the header line.

They are one thing now:

    <aside id="sidebar" data-vera-lhm>
      <div id="side-head"><span class="ico">…</span>
           <div><h1>Title</h1><div class="sub">…</div></div></div>
      <nav id="nav">
        <div class="nav-grp">Group</div>                     (optional)
        <button class="nav-btn active" data-…="x" title="X">
          <span class="gl">…</span>X</button>
      </nav>
      <div id="status-tile">…</div>                          (optional)
    </aside>

with the chrome in /ui/vera-panel.css and the behaviour — the collapse
chevron, publishing the menu to whichever shell hosts the panel, hiding it
once that shell says it is showing the same menu — in /ui/vera-panel.js.

These are text-level checks: they read the shipped files, so they fail if a
panel starts styling a menu of its own again.
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


# Every panel page whose menu is a LIST OF SECTIONS — host, header and body
# all shared.
PANELS = {
    "Agents": ("vera", "agents_skills_ontologies_panel.html"),
    "Capabilities": ("vera", "capabilities", "cap_hub.html"),
    "Estate": ("vera", "workers", "workers_ollama_panel.html"),
    "Comms": ("vera", "accounts", "comms_panel.html"),
    "Automations": ("vera", "automations", "automations_panel.html"),
    "Data Fabric": ("vera", "fabric", "fabric_panel.html"),
    "Markets": ("vera", "markets", "markets_studio_panel.html"),
    "Store": ("vera", "commerce", "commerce_uk_panel.html"),
    "Commerce": ("vera", "commerce", "commerce_panel.html"),
    "Business": ("vera", "business", "business_panel.html"),
    "Email": ("vera", "email", "email_panel.html"),
    "Dream": ("vera", "dream", "dream_panel.html"),
    "DAG Workshop": ("vera", "dag", "dag_workshop_panel.html"),
    "UI Builder": ("vera", "ui builder", "ui_builder_panel.html"),
    "Telegram": ("vera", "telegram", "telegram_panel.html"),
    "Image Studio": ("vera", "images", "image_studio_panel.html"),
    "Pxstore": ("vera", "proxmox", "pxstore_panel.html"),
    "Netops": ("vera", "proxmox", "netops_panel.html"),
}

# The Calendar's menu is not a list of sections: below the shared header it is
# a working surface (tabs over layers, the selected day's events, the agent).
# Host, header and stylesheet are the shared ones; the body is its own. It is
# held to everything below except the #nav list.
OWN_BODY = {
    "Calendar": ("vera", "calendar", "calendar_panel.html"),
}
ALL = dict(PANELS, **OWN_BODY)

CSS = _read("vera", "vera-panel.css")
JS = _read("vera", "vera-panel.js")


def test_every_panel_with_a_menu_uses_the_canonical_host():
    """One id, one marker — so one stylesheet reaches all of them."""
    for name, parts in ALL.items():
        src = _read(*parts)
        assert '<aside id="sidebar" data-vera-lhm>' in src, f"{name}: not the canonical menu host"
        assert 'id="side-head"' in src, f"{name}: no menu header"
    for name, parts in PANELS.items():
        assert 'id="nav"' in _read(*parts), f"{name}: no menu body"


def test_no_panel_styles_a_menu_of_its_own():
    """The classes each deviating panel used for its own rail are gone. A menu
    that styles itself is how they drifted apart in the first place."""
    gone = {
        "#secNav": "the rotated 40px rail",
        ".snav": "the rotated rail's items",
        ".fab-nb": "Data Fabric's own nav buttons",
        ".railBtn": "the Markets rail",
        ".subtab": "Comms / Automations' own nav buttons",
        "nav.side": "the Business / Store / Commerce rails",
    }
    for name, parts in ALL.items():
        src = _read(*parts)
        for token, what in gone.items():
            assert token not in src, f"{name} still carries {token} ({what})"


def test_no_panel_rotates_its_menu_text():
    """The Estate, Capabilities and Agents printed their section names
    bottom-to-top. Nothing does now."""
    for name, parts in ALL.items():
        src = _read(*parts)
        assert "writing-mode" not in src, f"{name} still rotates text"


def test_every_panel_loads_the_shared_stylesheet_and_behaviour():
    for name, parts in ALL.items():
        src = _read(*parts)
        assert "/ui/vera-panel.css" in src, f"{name}: no shared menu stylesheet"
        assert "/ui/vera-panel.js" in src, f"{name}: no shared menu behaviour"
        assert "/ui/vera-panel-bridge.js" in src, f"{name}: cannot publish its menu to the shell"


def test_the_shared_stylesheet_carries_the_whole_menu():
    """Groups, counts and the collapsed rail belong to the shared file, so a
    panel that needs them does not have to invent them."""
    for rule in (
        "#sidebar[data-vera-lhm] #side-head",
        "#sidebar[data-vera-lhm] .nav-btn",
        "#sidebar[data-vera-lhm] .nav-btn .gl",
        "#sidebar[data-vera-lhm] .nav-btn .ct",
        "#sidebar[data-vera-lhm] #nav .nav-grp",
        "#sidebar[data-vera-lhm] #nav .nav-div",
        "#sidebar[data-vera-lhm] #status-tile",
        "body.lhm-collapsed #sidebar[data-vera-lhm]",
        "html.vpb-nav-hosted [data-vera-lhm]",
    ):
        assert rule in CSS, rule


def test_the_shared_stylesheet_does_not_require_vars_a_panel_may_not_define():
    """Markets has no --fg, Estate has no --fg2. A bare var() with no fallback
    silently paints the menu wrong on whichever panel is missing it. The knobs
    this file declares itself are the exception."""
    own = ("--lhm-width", "--lhm-collapsed-width", "--ui-radius", "--ui-density")
    bare = [v for v in re.findall(r"var\(--[a-z0-9-]+\)", CSS) if v[4:-1] not in own]
    assert not bare, f"no fallback on: {sorted(set(bare))}"


def test_the_stylesheet_parses_as_css():
    """Comment markers balance and every block closes. A stray `*/` silently
    kills the rest of the file, and the menu loses its chrome everywhere."""
    assert CSS.count("/*") == CSS.count("*/"), "unbalanced comments"
    stripped = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    assert "*/" not in stripped and "/*" not in stripped, "a stray comment marker"
    assert stripped.count("{") == stripped.count("}"), "unbalanced braces"


def test_a_hosted_panel_hides_its_own_menu():
    """When the shell says it is showing this menu, the panel must not show it
    too. `#sidebar[data-vera-lhm]` is an ID selector and sets display:flex, so
    an attribute-only hiding rule loses to it and the page ends up with the
    menu twice, side by side — which is what the Estate did."""
    rule = re.search(r"(html\.vpb-nav-hosted[^{]*)\{\s*display:\s*none", CSS)
    assert rule, "nothing hides a hosted panel's own menu"
    selectors = [s.strip() for s in rule.group(1).split(",")]
    assert any("#sidebar[data-vera-lhm]" in s for s in selectors), \
        "the hiding rule cannot outrank #sidebar[data-vera-lhm]; it will not hide"
    assert any(s.endswith("[data-vera-lhm]") and "#" not in s for s in selectors), \
        "the differently-id'd hosts are no longer covered"


def test_a_panel_rule_that_must_beat_the_shared_one_carries_the_id():
    """The shared chrome is written as `#sidebar[data-vera-lhm] .nav-btn`,
    which is (1,2,0). A panel rule meant to override it — the Estate hiding
    the view it is not showing — has to be at least that specific or it
    silently does nothing, which is how the Estate menu came to list its
    Models pages as well as its own."""
    estate = _read(*PANELS["Estate"])
    filters = re.findall(r"^[^\n{]*\[data-view=\"(?:models|estate)\"\][^\n{]*\{", estate, re.M)
    assert filters, "the Estate's view filter is gone"
    for sel in filters:
        assert "#sidebar[data-vera-lhm]" in sel, \
            f"too weak to beat the shared .nav-btn rule: {sel.strip()}"


def test_the_shared_behaviour_publishes_only_real_menu_items():
    """A heading or a rule that carries data-view (the Estate groups its items
    that way) is not a target, and a button the panel's own rules hide is not
    part of the menu it is publishing."""
    assert "var btns = nav.querySelectorAll('.nav-btn');" in JS
    assert "if (!btns.length) btns = nav.querySelectorAll(SEL);" in JS
    assert "try { return getComputedStyle(b).display !== 'none'; } catch (e) { return true; }" in JS


def test_hidden_items_are_judged_by_their_own_style_not_by_layout():
    """offsetParent is null for EVERY element while the panel sits in a hidden
    container, which the shell routinely does before it shows a tab — so a
    layout-based test lets the whole menu through. The Estate published its
    Models pages (Ollama, Model Routing, Mimic, vLLM, API) into its Estate
    menu exactly that way. (Comments may name it; code may not.)"""
    code = re.sub(r"/\*.*?\*/", "", JS, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    assert "offsetParent" not in code, \
        "the visible-item filter is back on layout, which is null before the tab is shown"


def test_the_shared_behaviour_hands_the_bridge_the_button_itself():
    """Two items can carry the same value on different attributes — the
    Estate's data-pane="estate" map beside every data-view="estate" item — and
    a lookup by attribute picked the wrong one, leaving a dead menu entry."""
    assert "window.VeraPanelBridge.registerNav(items, function (id) {" in JS
    assert "if (idOf(btns[i]) === String(id)) { btns[i].click(); return; }" in JS
