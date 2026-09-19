"""An entity in every deep link.

Every mechanism that moved a reader between Estate panes carried only the
pane: `?pane=&sub=`, `vera:estate:open {pane, sub}` and the injected-menu
relay. "Open Machines focused on VM 145" could not be said, so the storage
view listed guests with no way out of the list (review of 19 Sep 2026).

A reference is `<kind>:<id>`. estate_nav_core owns the vocabulary and the
pane each kind opens in; the Estate panel carries it on the URL, in the
shell's message and in a message any embedded panel may send; the Machines
pane focuses one; vera-ui.js gives every panel one call.
"""
import os
import re
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.estate import estate_nav_core as nav  # noqa: E402

pytestmark = pytest.mark.critical

PANEL = os.path.join(ROOT, "vera", "workers", "workers_ollama_panel.html")
STORAGE = os.path.join(ROOT, "vera", "proxmox", "pxstore_panel.html")
SHELL = os.path.join(ROOT, "vera", "capability_orchestration.html")
UI_JS = os.path.join(ROOT, "vera", "vera-ui.js")


def read(p):
    return open(p, encoding="utf-8").read()


# ── the vocabulary ───────────────────────────────────────────────────────────

def test_a_reference_is_kind_colon_id_and_only_the_first_colon_divides():
    assert nav.parse_entity("guest:145") == {"kind": "guest", "id": "145"}
    assert nav.parse_entity("guest:2d83c1b5:145") == {"kind": "guest", "id": "2d83c1b5:145"}
    assert nav.parse_entity("container:host-a/redis-1") == {"kind": "container", "id": "host-a/redis-1"}
    assert nav.parse_entity(" Cert:dc.vera.int ") == {"kind": "cert", "id": "dc.vera.int"}
    for bad in ("", None, "guest", "guest:", ":145", "spaceship:1", 42):
        assert nav.parse_entity(bad) == {}


def test_every_kind_names_the_pane_that_can_focus_it():
    for kind, spec in nav.ENTITY_KINDS.items():
        assert spec["pane"] and "noun" in spec, kind
    assert nav.entity_target("guest:145")["pane"] == "machines"
    assert nav.entity_target("cert:x") == {"kind": "cert", "id": "x", "pane": "provision",
                                           "sub": "certs", "noun": "certificate"}
    assert nav.entity_target("nope:1") == {}
    assert nav.entity_ref("pool", "corp/tank_sdh") == "pool:corp/tank_sdh"
    assert nav.entity_ref("pool", "") == "" and nav.entity_ref("spaceship", "1") == ""


def test_the_panel_map_matches_the_core_vocabulary():
    """The panel keeps its own copy of kind -> pane (it cannot import Python);
    if either side gains or moves a kind, the other must follow."""
    html = read(PANEL)
    pane_map = re.search(r"const _ENTITY_PANE=\{(.+?)\};", html, re.S).group(1)
    sub_map = re.search(r"const _ENTITY_SUB=\{(.+?)\};", html, re.S).group(1)
    panes = dict(re.findall(r"'?([a-z-]+)'?:'([a-z-]+)'", pane_map))
    subs = dict(re.findall(r"'?([a-z-]+)'?:'([a-z-]+)'", sub_map))
    core = {k: v["pane"] for k, v in nav.ENTITY_KINDS.items()}
    assert panes == core, "panel _ENTITY_PANE and estate_nav_core.ENTITY_KINDS disagree"
    assert subs == {k: v["sub"] for k, v in nav.ENTITY_KINDS.items() if v["sub"]}


# ── the three ways a reference arrives ───────────────────────────────────────

def test_the_estate_panel_carries_an_entity_on_the_url_and_in_both_messages():
    html = read(PANEL)
    assert "function estateOpen(pane, sub, entity)" in html
    assert "q.get('entity')" in html, "?entity= is not read at boot"
    assert "e.data.entity?String(e.data.entity):''" in html, "vera:estate:open drops the entity"
    assert "e.data?.type==='vera:entity:open'&&e.data.ref" in html, "no way for an embedded panel to ask"
    assert "vera:entity:opened" in html and "vera:entity:focus" in html and "vera:entity:focused" in html
    exports = re.search(r"termOpen, termClose, provSub, buildGo,([^\n]*)", html).group(1)
    assert "entityOpen" in exports and "mcFocus" in exports


def test_machines_can_focus_a_guest_or_a_host_row():
    html = read(PANEL)
    fn = re.search(r"function mcFocus\(kind, id\)\{(.+?)\n\}", html, re.S).group(1)
    assert "_mcFocusWant" in fn and "mcLoad()" in fn, "a focus before the list is loaded must wait for it"
    match = re.search(r"function _mcMatch\(r, want\)\{(.+?)\n\}", html, re.S).group(1)
    assert "String(r.vmid)===v" in match and "r.ssh_host_id===want.id" in match
    assert "want.id.split(':').pop()" in match, "a cluster:vmid guest id must still match the row"
    assert "tr data-idx=" in html and "mc-focus" in html and "scrollIntoView" in html
    assert "if(_mcFocusWant) _mcApplyFocus();" in html, "a load that finishes must apply a pending focus"


def test_the_shell_redirect_passes_an_entity_through():
    html = read(SHELL)
    assert "function _estateOpen(pid, pane, sub, entity)" in html
    assert "entity: entity || ''" in html


def test_any_panel_can_open_an_entity_with_one_call():
    js = read(UI_JS)
    fn = re.search(r"function openEntity\(ref\)\{(.+?)\n  \}", js, re.S).group(1)
    assert "vera:entity:open" in fn, "inside the Estate panel's tree the request goes up by message"
    assert "/ui/panels/workers-ollama?entity=" in fn, "standalone, the Estate panel is opened with the reference"
    assert "openEntity: openEntity" in js


# ── the first consumer, and the backup names ─────────────────────────────────

def test_storage_links_each_guest_to_machines():
    html = read(STORAGE)
    assert html.count("veraUI.openEntity('guest:${g.vmid}')") == 2, "both guest tables link out"


def test_backup_jobs_are_named_by_what_they_do():
    html = read(STORAGE)
    fn = re.search(r"const jobName=j=>\{(.+?)\};", html, re.S).group(1)
    assert "j.comment" in fn, "a job's own comment comes first"
    assert "' guests'" in fn and "'every guest'" in fn and "(disabled)" in fn
    assert "esc(jobName(j))" in html and "<div class=\"kv\">${esc(j.id)}" in html, "the id is kept, second"
