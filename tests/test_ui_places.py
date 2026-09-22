"""Where a reading lives in the UI: one table (vera/ui/places_core.py), pinned to
the pages that exist - every place's panel is one the modules register, every
Estate pane and sub-tab is in the Estate panel's markup, every Automations
view is in that panel, the dashboard's records only name known places, and a
source rule points at a place. A renamed pane fails here, not on a click."""
import json
import os
import re
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.ui import places_core as pc  # noqa: E402
from vera.estate import estate_nav_core as nav  # noqa: E402

pytestmark = pytest.mark.critical

ESTATE_PANEL = os.path.join(ROOT, "vera", "workers", "workers_ollama_panel.html")
AUTOMATIONS = os.path.join(ROOT, "vera", "automations", "automations_panel.html")
SHELL = os.path.join(ROOT, "vera", "capability_orchestration.html")
LAYOUTS = os.path.join(ROOT, "vera", "widgets", "layouts")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _registered_panel_ids():
    """Every panel id a module registers with register_ui(...), by scanning the source."""
    ids = set()
    for base, _, files in os.walk(os.path.join(ROOT, "vera")):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            try:
                src = _read(os.path.join(base, fn))
            except Exception:
                continue
            # register_ui("id", …), register_ui(panel_id="id", …) and the aliased _reg_ui("id", …)
            for m in re.finditer(r'(?:register_ui|_reg_ui)\(\s*\n?\s*(?:panel_id\s*=\s*)?["\']([a-z0-9_-]+)["\']', src):
                ids.add(m.group(1))
    return ids


def test_every_place_names_a_registered_panel_or_a_static_tab():
    ids = _registered_panel_ids()
    assert "workers-ollama" in ids and "models" in ids
    for name, spec in pc.PLACES.items():
        assert ("panel" in spec) != ("tab" in spec), name
        if "panel" in spec:
            assert spec["panel"] in ids, f"{name} -> {spec['panel']} is not a registered panel"
        else:
            assert spec["tab"] in ("dashboard", "media"), name


def test_estate_panes_and_subs_exist_in_the_estate_panel():
    html = _read(ESTATE_PANEL)
    panes = set(re.findall(r'id="pane-([a-z-]+)"', html))
    for name, spec in pc.PLACES.items():
        if spec.get("panel") not in (pc.ESTATE_PANEL, pc.MODELS_PANEL) or not spec.get("pane"):
            continue
        assert spec["pane"] in panes, f"{name}: no pane-{spec['pane']} in the Estate panel"
        sub = spec.get("sub")
        if sub and spec["pane"] == "network":
            assert f'id="net-sub-{sub}"' in html, name
        elif sub and spec["pane"] == "provision":
            assert f'id="prv-sub-{sub}"' in html, name
        elif sub and spec["pane"] == "observe":
            assert f'id="obss-{sub}"' in html, name
    # the Estate panel opens a sub of Observe and mounts the Live ops pane
    assert "if(sub&&pane==='observe') obsSub(sub);" in html
    assert "if(name==='ops'){ _mountFrame('ops-frame', BASE+'/ops/panel'); }" in html
    assert 'id="pane-ops"' in html and "ops:'ops-frame'" in html


def test_automations_views_exist():
    html = _read(AUTOMATIONS)
    ks = set(re.findall(r'data-k="([a-z0-9-]+)"', html))
    for name, spec in pc.PLACES.items():
        if spec.get("panel") == "automations":
            assert spec["nav"] in ks, f"{name}: no data-k={spec['nav']} view in the Automations panel"


def test_retired_tabs_and_entity_kinds_agree_with_the_places():
    """The nav core's own pane vocabulary is a subset of what the places reach."""
    estate_panes = {s["pane"] for s in pc.PLACES.values() if s.get("panel") == pc.ESTATE_PANEL}
    for pid, t in nav.RETIRED_TABS.items():
        if t["panel"] == pc.ESTATE_PANEL:
            assert t["pane"] in estate_panes, pid


def test_source_rules_point_at_known_places_and_cover_the_dashboard():
    for prefix, place in pc.SOURCE_RULES:
        assert place in pc.PLACES, f"rule {prefix} -> {place}"
    main = json.load(open(os.path.join(LAYOUTS, "main.json"), encoding="utf-8"))
    recs = [w["record"] for w in main["widgets"] if isinstance(w.get("record"), dict)]
    assert pc.unknown_places(recs) == []
    missing = [r["id"] for r in recs if r.get("source") and r.get("form") != "section" and not pc.place_for(r.get("source"), r.get("open"))]
    assert missing == [], f"records with no place: {missing}"
    assert sum(1 for r in recs if r.get("open")) >= 40
    assert not any("panel" in r for r in recs), "records name a place with `open`, not a panel id"
    for k in ("main-estate", "main-inference", "main-compute"):
        j = json.load(open(os.path.join(LAYOUTS, k + ".json"), encoding="utf-8"))
        assert pc.unknown_places([w["record"] for w in j["widgets"] if isinstance(w.get("record"), dict)]) == []


def test_place_for_prefers_the_explicit_place_then_the_rule():
    assert pc.place_for("backup.status") == "estate/storage"
    assert pc.place_for("backup.status", "estate/proxmox") == "estate/proxmox"
    assert pc.place_for("backup.status", "no-such-place") == "estate/storage"
    assert pc.place_for("ollama.route_stats") == "models/routing"
    assert pc.place_for("ollama.instances") == "models"
    assert pc.place_for("evolve.sandbox.list") == "estate/sandbox"
    assert pc.place_for("evolve.errors.list") == "evolve"
    assert pc.place_for("obs.events") == "activity"
    assert pc.place_for("sysmon.status") == "estate/ops"
    assert pc.place_for("nothing.known") == ""
    assert pc.resolve("estate/ops") == {"panel": pc.ESTATE_PANEL, "pane": "ops"}
    assert pc.resolve("no") is None
    t = pc.table()
    assert t["estate_panel"] == pc.ESTATE_PANEL and "estate/ops" in t["places"] and t["rules"][0] == ["sysmon.", "estate/ops"]


def test_the_shell_opens_a_place_by_name():
    html = _read(SHELL)
    assert "function openPlace(p, entity){" in html
    assert "fetch(base() + '/ui/places'" in html
    assert "if (p.media) _mediaShow(p.media);" in html
    assert "if (p.pane) _estateOpen(tabName, p.pane, p.sub || '', entity || '');" in html
    assert "if (p.nav) _lhmNavSelect(tabName, p.nav);" in html
    assert "d.type === 'vera:place:open' && d.place" in html
    ui = _read(os.path.join(ROOT, "vera", "vera-ui.js"))
    assert "openPlace: openPlace," in ui and "type:'vera:place:open'" in ui
    dash = _read(os.path.join(ROOT, "vera", "chat", "vera-dashboard.js"))
    assert "function recordPlace(r)" in dash and "window.placeFor(src, r.open || '')" in dash
    assert "window.addEventListener('vera:places'" in dash
