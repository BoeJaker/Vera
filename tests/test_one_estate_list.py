"""One estate list. Seven panes each grew their own list of hosts - the SSH
store, the enrol store, Proxmox, Docker, the mesh, the directory, nodes.list.
estate.machines is the join of them; vera-estate.js is how a panel reaches it,
picks from it, and runs a change through the one plan-then-confirm flow. The
mesh's candidates, the Map and the host dropdowns must read it."""
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

pytestmark = pytest.mark.critical
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_the_shared_script_offers_the_list_the_picker_and_the_flow():
    js = read("vera", "estate", "vera-estate.js")
    for name in ("machines: machines", "logins: logins", "pick: pick", "fillSelect: fillSelect", "ref: ref",
                 "plan: plan", "confirmRun: confirmRun", "chip: chip"):
        assert name in js, name
    assert "'/estate/machines'" in js, "the list is estate.machines and nothing else"
    assert "TTL = 30000" in js
    fn = js[js.index("function plan("):js.index("function confirmRun(")]
    assert "if (!confirm(text))" in fn and "{confirm: true}" in fn, "the plan is shown before the confirmed run"
    assert "opts.typed" in fn, "layout-changing runs can demand a typed name"
    caps = read("vera", "estate", "estate_entity_capabilities.py")
    assert '"/ui/vera-estate.js"' in caps


@needs_node
def test_ref_and_label_agree_with_the_entity_vocabulary():
    js = read("vera", "estate", "vera-estate.js")
    harness = js + """
const m1={kind:'guest',vmid:145,label:'NWM-02',addr:'192.168.0.221',type:'lxc',status:'running',ssh_host_id:'x'};
const m2={kind:'host',id:'h9',label:'PVE01',addr:'192.168.0.200',ssh_host_id:'h9'};
const m3={kind:'docker-host',id:'d1',label:'local'};
const E=window.veraEstate; console.log(JSON.stringify([E.ref(m1), E.ref(m2), E.ref(m3), E.label(m1)]));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write("var window = {}; var document = {getElementById(){return null}, createElement(){return {style:{}}}, head:{appendChild(){}}, addEventListener(){}};\n"
                "var fetch = () => Promise.resolve({json: () => Promise.resolve({machines: []})});\n" + harness)
        path = f.name
    try:
        r = subprocess.run(["node", path], capture_output=True, timeout=60)
        r = type("R", (), {"returncode": r.returncode, "stderr": r.stderr.decode("utf-8", "replace"), "stdout": r.stdout.decode("utf-8", "replace")})()
        assert r.returncode == 0, r.stderr[:800]
        out = r.stdout.strip().splitlines()[-1]
    finally:
        os.unlink(path)
    assert out == '["guest:145","host:h9","host:d1","NWM-02 · CT 145 · 192.168.0.221 · running"]'


def test_the_mesh_candidates_are_the_estate_machines():
    src = read("vera", "networking", "netsec_capabilities.py")
    fn = src[src.index("async def cap_mesh_candidates"):src.index("async def cap_mesh_members") - 30]
    assert '_cap("estate.machines")' in fn and '"source": "estate.machines"' in fn
    assert '"source": "exec.ssh.hosts"' in fn, "the SSH store alone is the fallback, not the answer"
    assert '"ref":' in fn, "each candidate carries its entity reference"
    panel = read("vera", "networking", "netsec_panel.html")
    assert "veraEstate.chip(c.ref, c.label)" in panel and "veraEstate.chip('mesh:'+x.host_id" in panel
    assert "veraEstate.confirmRun('/netsec/mesh/join'" in panel and "veraEstate.confirmRun('/netsec/mesh/leave'" in panel
    assert '<script src="/ui/vera-estate.js"></script>' in panel


def test_the_map_is_drawn_from_the_registration_table():
    html = read("vera", "interaction", "interaction_panel.html")
    assert "j('/estate/registration')" in html
    for gone in ("j('/enroll/ssh/host/list')", "j('/workers/docker/hosts')", "j('/proxmox/cluster/list')", "j('/identity/host/list')"):
        assert gone not in html, gone + " is a second list of the estate"
    assert "veraEntityDrawer.open(h.ref)" in html, "a host on the map opens the drawer"
    assert "ALL_MACHINES" in html and "every machine" in html
    assert "h.backup==='yes'" in html


def test_host_dropdowns_read_the_one_list():
    storage = read("vera", "proxmox", "pxstore_panel.html")
    assert "veraEstate.logins()" in storage and "veraEstate.label(m)" in storage
    assert "veraEstate.plan(capName, base, title, opts)" in storage, "Storage uses the shared flow, not its own"
    prov = read("vera", "provisioning", "provision_panel.html")
    assert "veraEstate.logins()" in prov and '<script src="/ui/vera-estate.js"></script>' in prov
    estate = read("vera", "workers", "workers_ollama_panel.html")
    assert "veraEstate.confirmRun('/proxmox/guest/action'" in estate
    assert 'data-entity="guest:\'+esc(v.vmid)+\'"' in estate, "Proxmox guest names open the drawer too"


def test_structured_plane_facts_feed_the_picture():
    sys.path.insert(0, ROOT)
    from vera.estate import estate_entity_core as core
    src = core.Sources(
        machines=[{"id": "m", "label": "Ollama-C", "kind": "guest", "status": "running", "addr": "192.168.0.247",
                   "ips": ["192.168.0.247"], "vmid": 130, "type": "lxc", "ssh_host_id": "s1"}],
        ssh_hosts=[{"id": "s1", "label": "Ollama-C", "host": "192.168.0.247", "user": "root", "auth": "cert"}],
        mesh=[{"host_id": "s1", "host": "192.168.0.247", "ip": "10.66.66.2", "state": "up", "connected": True}],
        identity=[{"fqdn": "ollama-c.vera.int"}])
    p = core.machine_planes(src, src.machines[0])
    assert p["ssh"]["auth"] == "cert" and p["ssh"]["host_id"] == "s1"
    assert p["mesh"]["ip"] == "10.66.66.2" and p["mesh"]["connected"] is True
    assert p["directory"]["fqdn"] == "ollama-c.vera.int"
