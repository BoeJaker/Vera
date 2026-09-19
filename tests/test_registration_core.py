"""Who is registered where: every machine against every plane, the entries no
machine answers to, and what to forget when a guest is destroyed. The estate
in the fixture is the one measured on 19 Sep 2026, in miniature."""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.estate import estate_entity_core as ent  # noqa: E402
from vera.estate import registration_core as core  # noqa: E402

pytestmark = pytest.mark.critical
NOW = 1_789_830_000.0

MACHINES = [
    {"id": "m-pve", "label": "PVE01", "kind": "proxmox-node", "status": "", "addr": "192.168.0.200", "ips": [], "ssh_host_id": "ssh-pve"},
    {"id": "m-130", "label": "Ollama-C", "kind": "guest", "status": "running", "addr": "192.168.0.247", "ips": ["192.168.0.247"],
     "cluster_id": "c1", "node": "corp", "vmid": 130, "type": "lxc", "ssh_host_id": "ssh-130"},
    {"id": "m-145", "label": "NWM-02", "kind": "guest", "status": "running", "addr": "192.168.0.221", "ips": ["192.168.0.221"],
     "cluster_id": "c1", "node": "corp", "vmid": 145, "type": "lxc", "ssh_host_id": ""},
    {"id": "m-900", "label": "tmpl", "kind": "guest", "status": "stopped", "template": True, "vmid": 900, "addr": "", "ips": []},
]
SSH = [{"id": "ssh-pve", "label": "PVE01", "host": "192.168.0.200", "user": "root", "auth": "password", "tags": []},
       {"id": "ssh-130", "label": "Ollama-C (cpu-247)", "host": "192.168.0.247", "user": "root", "auth": "key",
        "tags": ["ollama", "guest:c1:130"]},
       {"id": "ssh-local", "label": "localhost", "host": "localhost", "user": "root", "auth": "key", "tags": []},
       {"id": "ssh-dead", "label": "e2e-wkr.vera.int", "host": "192.168.0.97", "user": "root", "auth": "key", "tags": ["guest:c1:141"]}]
IDENTITY = [{"fqdn": "ollama-e.vera.int", "enrolled": True}, {"fqdn": "dc.vera.int", "enrolled": True}]
MESH = [{"host_id": "ssh-130", "label": "Ollama-C (cpu-247)", "host": "192.168.0.247", "ip": "10.66.66.2", "state": "up", "connected": True},
        {"host_id": "ssh-gone", "label": "(none)", "host": "10.22.22.132", "ip": "10.88.0.5", "state": "error"}]
BACKUPS = {"schedules": [], "guests": [{"vmid": 130, "covered_by": [], "excluded_by": ["vera-estate"], "backups": 0},
                                        {"vmid": 145, "covered_by": ["vera-estate"], "excluded_by": [], "backups": 3, "latest_at": NOW - 3600}]}


def src():
    return ent.Sources(machines=MACHINES, backups=BACKUPS, certs=[], mesh=MESH, identity=IDENTITY, ssh_hosts=SSH, now=NOW)


def test_coverage_puts_every_machine_against_every_plane_and_counts_the_gaps():
    out = core.coverage(src())
    assert out["machines"] == 3, "templates are not machines to register"
    assert out["complete"] == 0
    rows = {r["label"]: r for r in out["rows"]}
    c = rows["Ollama-C"]["planes"]
    assert c["ssh"]["state"] == "yes" and c["mesh"]["state"] == "yes" and c["directory"]["state"] == "no"
    assert c["backup"]["state"] == "no" and rows["NWM-02"]["planes"]["backup"]["state"] == "yes"
    assert rows["PVE01"]["planes"]["backup"]["state"] == "n/a"
    assert out["counts"]["ssh"] == {"yes": 2, "no": 1, "unknown": 0, "n/a": 0}
    assert out["rows"][0]["label"] in ("NWM-02", "PVE01"), "the least registered come first"
    assert rows["Ollama-C"]["ref"] == "guest:130" and rows["PVE01"]["ref"] == "host:ssh-pve"


def test_stale_entries_are_the_ones_no_machine_answers_to():
    # the directory's A records: ollama-e's address belongs to no machine, dc's neither
    st = core.stale(src(), {"ollama-e.vera.int": ["192.168.0.249"], "dc.vera.int": ["192.168.0.91"]})
    got = {(s["kind"], s["id"]): s for s in st}
    assert ("identity", "ollama-e.vera.int") in got and ("identity", "dc.vera.int") in got
    assert got[("identity", "ollama-e.vera.int")]["action"] == {"cap": "identity.host.delete",
                                                                 "args": {"fqdn": "ollama-e.vera.int", "updatedns": True}}
    assert ("host", "ssh-dead") in got and got[("host", "ssh-dead")]["action"]["cap"] == "exec.ssh.hosts.delete"
    assert ("host", "ssh-local") not in got, "localhost is never stale"
    assert ("host", "ssh-130") not in got and ("host", "ssh-pve") not in got
    assert ("mesh", "ssh-gone") in got and ("mesh", "ssh-130") not in got


def test_forgetting_a_destroyed_guest_takes_its_login_directory_host_and_mesh_in_the_safe_order():
    plan = core.forget_plan(src(), vmid=130, name="Ollama-C")
    assert [(p["kind"], p["id"]) for p in plan] == [("mesh", "ssh-130"), ("host", "ssh-130")]
    assert plan[0]["action"]["cap"] == "netsec.mesh.leave", "the mesh leaves over the login before the login goes"
    # by name alone: the directory host of a guest whose login was never made
    plan = core.forget_plan(src(), name="ollama-e")
    assert [(p["kind"], p["id"]) for p in plan] == [("identity", "ollama-e.vera.int")]
    # by the pve:<vmid>@ label and by address
    s = src()
    s.ssh_hosts.append({"id": "ssh-106", "label": "pve:106@corp", "host": "10.33.33.1", "tags": ["proxmox"]})
    assert [p["id"] for p in core.forget_plan(s, vmid=106)] == ["ssh-106"]
    assert [p["id"] for p in core.forget_plan(s, addrs=["10.33.33.1"])] == ["ssh-106"]
    assert core.forget_plan(s, addrs=["localhost"]) == []


def test_the_capabilities_and_the_destroy_hook_are_wired():
    caps = open(os.path.join(ROOT, "vera", "estate", "registration_capabilities.py"), encoding="utf-8").read()
    for name in ('"estate.registration"', '"estate.registration.prune"', '"estate.registration.forget"'):
        assert name in caps
    assert "if not _flag(confirm):\n        return {\"dry_run\": True" in caps, "prune is a dry run unless confirmed"
    orch = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    assert orch.index("estate/estate_entity_capabilities.py") < orch.index("estate/registration_capabilities.py"), \
        "the registration module joins through the entity resolver, which must load first"
    px = open(os.path.join(ROOT, "vera", "proxmox", "proxmox_capabilities.py"), encoding="utf-8").read()
    body = px[px.index("async def cap_guest_destroy"):px.index("async def cap_guest_destroy") + 3000]
    assert "/config\")" in body and body.index("/config\")") < body.index('_pve(rec, "DELETE", path)'), \
        "the name is read while the config still exists"
    assert '_cap("estate.registration.forget")' in body and body.index("DELETE") < body.index("estate.registration.forget")
    panel = open(os.path.join(ROOT, "vera", "workers", "workers_ollama_panel.html"), encoding="utf-8").read()
    assert "prvs-registration" in panel and "registration:'/estate/registration/panel'" in panel


def test_a_directory_host_is_judged_by_address_never_by_name_alone():
    """19 Sep 2026: CT126 is labelled Ollama but registered as ollama126.vera.int,
    the Vera VM is LLM but vera.vera.int; a name match called both stale."""
    machines = MACHINES + [
        {"id": "m-126", "label": "Ollama", "kind": "guest", "status": "running", "addr": "192.168.0.250", "ips": ["192.168.0.250"],
         "vmid": 126, "type": "lxc", "ssh_host_id": "ssh-126"},
        {"id": "m-104", "label": "LLM", "kind": "guest", "status": "running", "addr": "192.168.0.138", "ips": ["192.168.0.138"],
         "vmid": 104, "type": "qemu", "ssh_host_id": ""}]
    ssh = SSH + [{"id": "ssh-126", "label": "ollama126.vera.int", "host": "192.168.0.250", "tags": []}]
    identity = IDENTITY + [{"fqdn": "ollama126.vera.int"}, {"fqdn": "vera.vera.int"}, {"fqdn": "mystery.vera.int"}]
    s = ent.Sources(machines=machines, backups=BACKUPS, certs=[], mesh=MESH, identity=identity, ssh_hosts=ssh, now=NOW)
    resolved = {"vera.vera.int": ["192.168.0.138"], "ollama-e.vera.int": ["192.168.0.249"], "dc.vera.int": ["192.168.0.91"],
                "ollama126.vera.int": ["192.168.0.250"]}
    st = {s_["id"]: s_ for s_ in core.stale(s, resolved) if s_["kind"] == "identity"}
    assert "ollama126.vera.int" not in st, "its login by that name reaches a machine"
    assert "vera.vera.int" not in st, "its A record is the Vera VM's address"
    assert st["ollama-e.vera.int"]["action"]["cap"] == "identity.host.delete" and "192.168.0.249" in st["ollama-e.vera.int"]["why"]
    assert st["dc.vera.int"]["action"], "positively unclaimed: its address belongs to no machine"
    assert st["mystery.vera.int"]["unverified"] is True and st["mystery.vera.int"]["action"] is None, \
        "an address we could not look up is never pruned on a name alone"
    # without any address evidence at all, nothing in the directory is called stale with an action
    assert all(x.get("action") is None for x in core.stale(s, None) if x["kind"] == "identity")


def test_prune_only_ever_runs_entries_that_carry_an_action():
    caps = open(os.path.join(ROOT, "vera", "estate", "registration_capabilities.py"), encoding="utf-8").read()
    assert 'if p.get("action")]' in caps
    assert '"dnsrecord_find"' in caps, "the directory's own A records are the address evidence"
    panel = open(os.path.join(ROOT, "vera", "estate", "registration_panel.html"), encoding="utf-8").read()
    assert 'class="stale-pick"' in panel and "ids: sel.map(s => String(s.id))" in panel, "prune acts on ticked entries only"
    assert "unverified" in panel
