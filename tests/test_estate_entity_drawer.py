"""The drawer sits in front of the rows that should open it, and the resolver
module is loaded. Structural: the joins are tested in test_estate_entity_core."""
import os
import re

import pytest

pytestmark = pytest.mark.critical
ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_the_resolver_module_is_loaded_and_serves_the_drawer():
    orch = read("vera", "capability_orchestration.py")
    assert 'estate/estate_entity_capabilities.py' in orch
    caps = read("vera", "estate", "estate_entity_capabilities.py")
    assert '"estate.entity.resolve"' in caps and '"/ui/vera-entity-drawer.js"' in caps
    assert os.path.exists(os.path.join(ROOT, "vera", "estate", "vera-entity-drawer.js"))


def test_the_drawer_opens_from_a_data_entity_attribute_and_links_through_open_entity():
    js = read("vera", "estate", "vera-entity-drawer.js")
    assert "closest('[data-entity]')" in js
    assert "window.veraEntityDrawer = {open: open, close: close}" in js
    assert "veraUI.openEntity" in js, "links leave the drawer through the one shared call"
    assert "/estate/entity/resolve?ref=" in js
    assert "prefers-reduced-motion" in js


def test_machines_storage_and_certificates_offer_the_drawer():
    panel = read("vera", "workers", "workers_ollama_panel.html")
    assert '<script src="/ui/vera-entity-drawer.js"></script>' in panel
    assert "data-entity=\"'+esc(eref)+'\"" in panel and "'guest:'+r.vmid" in panel and "'host:'+r.ssh_host_id" in panel
    storage = read("vera", "proxmox", "pxstore_panel.html")
    assert '<script src="/ui/vera-entity-drawer.js"></script>' in storage
    assert storage.count('data-entity="guest:${g.vmid}"') == 2
    assert 'data-entity="pool:${esc(p.name)}"' in storage
    certs = read("vera", "security", "certs_panel.html")
    assert '<script src="/ui/vera-entity-drawer.js"></script>' in certs
    assert 'data-entity="cert:${esc(c.name)}"' in certs


def test_every_kind_the_panel_can_open_has_a_reader_or_says_so():
    """resolve() must answer every kind in the vocabulary - with a record or with
    'no reader joined yet' - never with a KeyError."""
    import sys
    sys.path.insert(0, ROOT)
    from vera.estate import estate_entity_core as core
    from vera.estate.estate_nav_core import ENTITY_KINDS
    for kind in ENTITY_KINDS:
        out = core.resolve(f"{kind}:x", core.Sources())
        assert "found" in out and (out["found"] or out.get("error")), kind
