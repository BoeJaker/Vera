"""Round five: guest power on the Proxmox panel and the Workers Proxmox pane,
and the network graph's connect / firewall / cut, go through the one confirmRun
flow and name the machine the way the estate does."""
import os
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


@needs_node
def test_a_confirmation_calls_a_guest_by_its_estate_name():
    js = read("vera", "estate", "vera-estate.js")
    harness = js + """
const E=window.veraEstate;
E.machines().then(function(){ console.log(JSON.stringify([E.guestName(145,'lxc'), E.guestName(145), E.guestName(999,'qemu'), E.guestName(999)])); });
"""
    rows = "[{kind:'guest',vmid:145,type:'lxc',label:'NWM-02',addr:'192.168.0.221'}]"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write("var window = {}; var document = {getElementById(){return null}, createElement(){return {style:{}}}, head:{appendChild(){}}, addEventListener(){}};\n"
                "var fetch = () => Promise.resolve({json: () => Promise.resolve({machines: " + rows + "})});\n" + harness)
        path = f.name
    try:
        r = subprocess.run(["node", path], capture_output=True, timeout=60)
        out = r.stdout.decode("utf-8", "replace").strip().splitlines()
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:800]
    finally:
        os.unlink(path)
    assert out[-1] == '["NWM-02 (CT 145)","NWM-02 (CT 145)","VM 999","CT 999"]'


def test_guest_power_goes_through_the_one_flow_everywhere():
    p = read("vera", "proxmox", "proxmox_panel.html")
    fn = p[p.index("async function guest("):p.index("setTimeout(refresh,1500)", p.index("async function guest("))]
    assert "veraEstate.confirmRun('/proxmox/guest/action'" in fn and "veraEstate.guestName(vmid,type)" in fn
    assert "POWER_VERB[action]" in fn and "if(r.cancelled||r.error) return;" in fn
    assert "&&!confirm(" not in fn, "no private confirm, and start is confirmed like the rest"
    assert "'Stop (power off, no shutdown)'" in p
    w = read("vera", "workers", "workers_ollama_panel.html")
    fn = w[w.index("async function pmxPower("):w.index("function pmxStopVm(")]
    assert "veraEstate.confirmRun('/proxmox/guest/action'" in fn and "veraEstate.guestName(vmid,type)" in fn
    assert "function pmxStartVm(node,vmid,type){ return pmxPower(node,vmid,type,'start'); }" in w
    assert "function pmxStopVm(node,vmid,type){ return pmxPower(node,vmid,type,'shutdown'); }" in w


def test_network_edges_say_what_changes_before_they_change_it():
    g = read("vera", "networking", "netgraph_panel.html")
    assert "await api('/netgraph/edge/allow'" not in g and "await api('/netgraph/edge/deny'" not in g
    assert g.count("veraEstate.confirmRun('/netgraph/edge/allow'") == 2
    assert g.count("veraEstate.confirmRun('/netgraph/edge/deny'") == 2
    assert "'Connect container '+cnode.data('label')+' to Docker network '+nnode.data('label')" in g
    assert "'Disconnect container '+e.source().data('label')+' from Docker network '" in g
    assert "'Add an ACCEPT firewall rule on '+veraEstate.guestName(g.vmid,g.gtype)+': from '+(source||'anywhere')" in g
    assert "'Delete firewall rule '+m.pos+' ('+(m.proto||'any')+' · dport '+(m.dport||'any')+') on '+veraEstate.guestName(m.vmid,m.gtype)" in g
    assert "if(!confirm(" not in g, "no private confirm remains on the graph"
