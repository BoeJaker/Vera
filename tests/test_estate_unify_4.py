"""Round four: the lists that still built their own machines - Connections,
Enrol, the network graph, the Proxmox panel, the Exec/Netmap/Provisioning host
selects, the storage setup selects, Remote IDE targets, System Monitor rows -
label and link through the one estate list."""
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

EST = '<script src="/ui/vera-estate.js"></script>'
DRW = '<script src="/ui/vera-entity-drawer.js"></script>'


def read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_the_one_list_can_be_looked_up_synchronously():
    js = read("vera", "estate", "vera-estate.js")
    for name in ("find: find", "refFor: refFor", "labelMap: labelMap"):
        assert name in js, name
    fn = js[js.index("function find("):js.index("function refFor(")]
    for key in ("q.ssh_host_id", "q.vmid", "q.docker_host_id", "q.node", "q.addr"):
        assert key in fn, key
    assert "(m.ips || []).indexOf(q.addr)" in fn, "any address the machine answers on"


@needs_node
def test_find_matches_by_login_vmid_docker_host_node_and_address():
    js = read("vera", "estate", "vera-estate.js")
    harness = js + """
const E=window.veraEstate;
E.machines().then(function(){
  console.log(JSON.stringify([E.refFor({vmid:145}), E.refFor({ssh_host_id:'h9'}), E.refFor({node:'corp'}),
    E.refFor({addr:'10.66.66.4'}), E.refFor({docker_host_id:'local'}), E.refFor({addr:'1.2.3.4'}), E.refFor({vmid:''})]));
  return E.labelMap();
}).then(function(m){ console.log(JSON.stringify(m)); });
"""
    rows = ("[{kind:'guest',vmid:145,label:'NWM-02',addr:'192.168.0.221',ssh_host_id:''},"
            "{kind:'proxmox-node',id:'h9',node:'corp',label:'PVE01',addr:'192.168.0.200',ssh_host_id:'h9'},"
            "{kind:'guest',vmid:126,label:'Ollama',addr:'192.168.0.250',ips:['192.168.0.250','10.66.66.4'],ssh_host_id:'g126'},"
            "{kind:'host',id:'l1',label:'localhost',addr:'localhost',ssh_host_id:'l1',docker_host_id:'local'}]")
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
    assert out[-2] == '["guest:145","host:h9","host:h9","guest:126","host:l1","",""]'
    assert out[-1] == '{"h9":"PVE01 · node · 192.168.0.200","g126":"Ollama · CT 126 · 192.168.0.250","l1":"localhost · host · localhost"}'


def test_connections_enrol_and_the_graph_open_the_machine():
    w = read("vera", "workers", "workers_ollama_panel.html")
    assert "<td><b>${veraEstate.chip('host:'+h.id, h.label||'')}</b></td>" in w, "Connections rows"
    e = read("vera", "provisioning", "enroll_panel.html")
    assert "veraEstate.chip('guest:'+g.vmid, g.name||'?')" in e and "veraEstate.chip(veraEstate.refFor({addr:h.host}), h.label||'')" in e
    assert EST in e and DRW in e
    g = read("vera", "networking", "netgraph_panel.html")
    fn = g[g.index("function entityRefFor("):g.index("function showInfo(")]
    assert "'guest:'+d.vmid" in fn and "'container:'+(d.host_id||'local')+'/'+d.label" in fn
    assert "'docker-host:'+d.host_id" in fn and "veraEstate.refFor({node:d.label})" in fn
    assert 'data-entity="${esc(ref)}"' in g and "Open in the estate" in g
    assert "veraEstate.find({docker_host_id:h.id})" in g, "the Docker host filter reads as the estate names it"
    assert EST in g and DRW in g


def test_the_proxmox_panel_cards_open_the_machine():
    p = read("vera", "proxmox", "proxmox_panel.html")
    assert "veraEstate.chip(veraEstate.refFor({node:n.node}), n.node)" in p
    assert "veraEstate.chip('guest:'+g.vmid, g.name||('vm'+g.vmid))" in p
    assert "await veraEstate.machines()" in p, "the list is loaded before the cards draw"
    assert EST in p and DRW in p


def test_host_selects_read_as_the_estate_names_them():
    for f in (("execution", "exec_panel.html"), ("execution", "netmap_panel.html")):
        s = read("vera", *f)
        assert "names=await veraEstate.labelMap()" in s and "names[h.id] ?" in s, f
        assert EST in s, f
        assert "_hosts = j.hosts||[]" in s, "the rows themselves stay: the pane needs user/port from them"
    pr = read("vera", "provisioning", "provisioning_panel.html")
    assert "veraEstate.fillSelect($('#cSsh'), {blank:'— none —', selected:c.default_ssh_host_id||''})" in pr
    assert "/exec/ssh/hosts/list" not in pr and EST in pr
    px = read("vera", "proxmox", "pxstore_panel.html")
    assert "veraEstate.fillSelect($('sxHost')" in px
    assert "veraEstate.fillSelect($('setWriterHost'), {blank:'VFS-02 (found by its label)', selected:st.store_writer_host||''})" in px
    assert "veraEstate.fillSelect($('setNwm'), {blank:'— not set —', selected:st.nwm_host_id||''})" in px
    assert "(P.state.sshHosts||[]).map(h=>`<option" not in px, "no private copy of the SSH store renders a select"


def test_remote_ide_and_system_monitor_rows_open_the_machine():
    i = read("vera", "ide", "ide_remote_panel.html")
    assert "veraEstate.chip('guest:'+t.vmid, t.name)" in i and "veraEstate.chip('host:'+i.host_id" in i
    assert DRW in i
    m = read("vera", "monitor", "system_monitor_panel.html")
    assert "veraEstate.chip(h.id?'docker-host:'+h.id:'', h.label)" in m
    assert "veraEstate.chip(veraEstate.refFor({addr}), n.label)" in m
    assert "veraEstate.machines().catch(()=>[])" in m, "the list arrives with the status"
    assert EST in m and DRW in m
