"""Documentation generation: domain map, scaffold round-trip, gallery (no browser)."""

from vera.operator.docs import doc_scaffold as DS
from vera.operator.docs import domain_map as DM
from vera.operator.docs import gallery as G
from vera.operator.missions import seeds as SEEDS


def test_domain_map_integrity():
    slugs = DM.all_slugs()
    assert len(slugs) == len(set(slugs)), "duplicate slugs"
    docs = [d["doc"] for d in DM.DOMAINS]
    assert len(docs) == len(set(docs)), "duplicate doc files"
    assert "operator" in slugs and "capability-framework" in slugs


def test_every_seed_is_resolvable():
    for d in DM.DOMAINS:
        seed = d.get("seed")
        assert seed is None or seed in SEEDS.SEEDS, f"{d['slug']} → unknown seed {seed}"


def test_resolve_slugs():
    assert DM.resolve_slugs(["markets"]) and DM.resolve_slugs(["markets"])[0]["slug"] == "markets"
    assert len(DM.resolve_slugs(None)) == len(DM.DOMAINS)
    assert DM.resolve_slugs(["nope"]) == []


def test_panel_matching():
    assert DM.domain_for_panel({"id": "markets-studio", "label": "Quant Studio"}) == "markets"
    assert DM.domain_for_panel({"id": "evolve", "label": "Loop Lab"}) == "evolve"
    assert DM.domain_for_panel({"id": "operator-studio", "label": "Operator"}) == "operator"
    assert DM.domain_for_panel({"id": "zzz-unknown", "label": "?"}) is None


def test_scaffold_upsert_round_trip():
    txt = "# Title\n\nAuthored prose.\n"
    out = DS.upsert_block(txt, "screenshots", "IMG-A", heading="## Screenshots")
    assert "Authored prose." in out
    assert "IMG-A" in out
    # replace, not duplicate
    out2 = DS.upsert_block(out, "screenshots", "IMG-B")
    assert "IMG-B" in out2 and "IMG-A" not in out2
    assert out2.count("VERA:AUTO:screenshots START") == 1


def test_build_doc_new_and_existing():
    fresh = DS.build_doc(None, number="34", title="Operator",
                         screenshots_content="shots", caps_content="caps")
    assert "# 34 · Operator" in fresh
    assert "VERA:AUTO:screenshots START" in fresh
    # preserves prose on re-build
    edited = fresh.replace("Draft — author me.", "Real overview.")
    rebuilt = DS.build_doc(edited, number="34", title="Operator",
                           screenshots_content="NEW", caps_content="NEWCAPS")
    assert "Real overview." in rebuilt
    assert "NEW" in rebuilt and "NEWCAPS" in rebuilt


def test_render_caps_and_shots():
    caps = DS.render_caps_block([{"name": "operator.run", "method": "POST",
                                  "path": "/operator/run", "description": "Drive a goal."}])
    assert "`operator.run`" in caps and "POST /operator/run" in caps
    assert "No screenshots" in DS.render_screenshots_block([])
    shots = DS.render_screenshots_block([{"label": "Panel", "rel_path": "assets/x/p.png",
                                          "caption": "Panel", "mode": "seeded"}])
    assert "![Panel](assets/x/p.png)" in shots


def test_panel_capture_url_prefers_real_iframe_route():
    from vera.operator.missions import documentation as M
    p = {"id": "evolve", "html": '<div><iframe src="/evolve/panel"></iframe></div>'}
    r = M.panel_capture_url("http://h:8998", p)
    assert r["via"] == "route"
    assert r["url"] == "http://h:8998/evolve/panel"


def test_panel_capture_url_window_fallback_for_element_panels():
    from vera.operator.missions import documentation as M
    p = {"id": "live-event-stream", "html": "<vera-live-event-stream></vera-live-event-stream>"}
    r = M.panel_capture_url("http://h:8998", p)
    assert r["via"] == "window"
    assert "ui/panel/window?id=live-event-stream" in r["url"]


def test_normalise_panels_keeps_html_and_mode():
    from vera.operator.missions import documentation as M
    n = M._normalise_panels([{"id": "a", "label": "A", "mode": "tab",
                              "html": "<iframe src='/a/panel'></iframe>"}])
    assert n[0]["mode"] == "tab" and n[0]["html"]


def test_capture_states_default_and_named_files():
    from vera.operator.missions import documentation as M
    assert M._capture_states({}, "plain-panel") == [{}]
    domain = {"capture_states": {"fabric-panel": [
        {"name": "graph", "click": "#fnav-graph"},
        {"name": "stats", "click": "#fnav-stats"}]}}
    states = M._capture_states(domain, "fabric-panel")
    assert [M._state_name("fabric-panel", s) for s in states] == [
        "fabric-panel-graph", "fabric-panel-stats"]
    assert states[0]["click"] == "#fnav-graph"


def test_data_fabric_capture_recipe_is_representative():
    fabric = DM.by_slug("data-fabric")
    states = fabric["capture_states"]["fabric-panel"]
    assert states[0]["name"] == "graph"
    assert states[0]["ready_selector"] and states[0]["ready_text"]


def test_gallery_build():
    assert G.OUTPUT_FILE == "GALLERY.md"
    md = G.build_gallery([{"slug": "markets", "title": "Markets", "doc": "15-markets.md",
                           "cover_rel": "assets/markets/s.png", "shot_count": 3, "cap_count": 12}],
                         generated_at="now", total_caps=12)
    assert "Vera — Visual gallery" in md
    assert "15-markets.md" in md
    assert "assets/markets/s.png" in md
