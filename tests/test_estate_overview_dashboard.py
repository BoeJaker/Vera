"""The Estate's Overview is a dashboard: one VeraDash grid over the readers the
estate already has, served as its own panel and mounted in the Overview pane."""
import os
import re
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

pytestmark = pytest.mark.critical

WIDGETS = ("health", "vera", "inference", "resources", "machines", "backups", "storage", "network", "trust",
           "activity", "background")
READERS = ("/estate/health", "/health", "/sysmon/status", "/sysmon/history?limit=60", "/ollama/gate",
           "/estate/registration", "/backup/status", "/docker/disk/breakdown?host_id=local", "/netsec/mesh/members",
           "/certs/list", "/identity/status", "/secrets/list", "/background/status", "/census/control",
           "/proxmox/cluster/list", "/pxstore/inventory")


def read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_the_dashboard_is_one_grid_of_the_estates_readers():
    p = read("vera", "estate", "estate_overview_panel.html")
    for w in WIDGETS:
        assert f'data-wid="{w}"' in p, w
    for r in READERS:
        assert r in p, r
    assert "VeraDash.init($('grid'),{key:'estate-overview',loader:true,popout:true" in p
    for src in ("/ui/vera-dashboard.js", "/ui/elements/sparkline.js", "/ui/elements/activity_timeline.js",
                "/ui/vera-entity-drawer.js", "/ui/vera-estate.js"):
        assert f'<script src="{src}"></script>' in p, src
    assert "document.visibilityState==='visible'" in p, "polls only while visible"
    assert "Date.now()-_invAt<300000" in p, "the pool inventory walks zpool over SSH - five minutes, not every minute"
    assert ".spark vera-sparkline{display:block;height:96px;flex:none}" in p, "a sparkline in a growing box grows forever"


def test_every_name_on_the_dashboard_opens_the_drawer():
    p = read("vera", "estate", "estate_overview_panel.html")
    for ref in ("chip(f.ref,", "chip('backup-job:'+s.id", "chip('pool:'+pl.name", "chip('mesh:'+x.host_id",
                "chip('cert:'+x.name", "chip(s.kind&&s.id?s.kind+':'+s.id:''", "chip(ref,n.label||n.id)"):
        assert ref in p, ref
    assert "veraEstate.find({addr})" in p, "inference nodes resolve to machines by address"
    assert "vera:entity:focus" in p and "vera:entity:focused" in p, "the Estate panel can ask it to focus an entity"
    assert "postMessage({type:'vera:estate:open',pane,sub:sub||''}" in p, "the › buttons open the pane in the Estate panel"


def test_the_overview_pane_mounts_the_dashboard():
    w = read("vera", "workers", "workers_ollama_panel.html")
    assert '<iframe id="overview-frame"' in w
    assert "function ovLoad(){ _mountFrame('overview-frame', BASE+'/estate/overview/panel'); }" in w
    assert "overview:'overview-frame'" in w, "entity focus reaches the frame like the other iframe panes"
    for gone in ("function ovRender(", "function _ovRow(", "function _ovFacts(", 'id="ov-sections"'):
        assert gone not in w, gone
    caps = read("vera", "estate", "estate_health_capabilities.py")
    assert '@_orch.APP.get("/estate/overview/panel"' in caps and "estate_overview_panel.html" in caps
