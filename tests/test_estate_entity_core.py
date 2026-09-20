"""estate.entity.resolve's joins: one record per thing, from the readers'
own outputs. The fixtures are shaped like the live estate on 19 Sep 2026:
Ollama-C on the door and in Storage but not in the directory, NWM-02 fronting
netctl as an Integration, a retired directory host nobody answers to.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.estate import estate_entity_core as core  # noqa: E402

pytestmark = pytest.mark.critical

NOW = 1_789_830_000.0
MACHINES = [
    {"id": "m-pve", "label": "PVE01", "kind": "proxmox-node", "status": "", "addr": "192.168.0.200", "ips": [],
     "ssh_host_id": "ssh-pve", "docker_host_id": "", "hardware": ["Tesla V100"], "runs": []},
    {"id": "m-130", "label": "Ollama-C", "kind": "guest", "status": "running", "addr": "192.168.0.247",
     "ips": ["192.168.0.247"], "cluster_id": "c1", "node": "corp", "vmid": 130, "type": "lxc",
     "ssh_host_id": "ssh-130", "docker_host_id": "", "hardware": [], "runs": ["ollama"], "note": ""},
    {"id": "m-145", "label": "NWM-02", "kind": "guest", "status": "running", "addr": "192.168.0.221",
     "ips": ["192.168.0.221", "10.33.33.254"], "cluster_id": "c1", "node": "corp", "vmid": 145, "type": "lxc",
     "ssh_host_id": "", "docker_host_id": "", "hardware": [], "runs": []},
    {"id": "m-104", "label": "LLM", "kind": "guest", "status": "running", "addr": "192.168.0.138",
     "ips": ["192.168.0.138"], "cluster_id": "c1", "node": "corp", "vmid": 104, "type": "qemu",
     "ssh_host_id": "ssh-llm", "docker_host_id": "dk-local", "hardware": [], "runs": ["vera"]},
]
BACKUPS = {
    "schedules": [{"id": "vera-estate", "owner": "proxmox", "what": "every guest except 104", "schedule": "02:30",
                   "enabled": True, "storage": "pbs-estate", "next_run": NOW + 3600, "last_run": None}],
    "guests": [{"vmid": 130, "name": "Ollama-C", "covered_by": [], "excluded_by": ["vera-estate"], "backups": 0},
               {"vmid": 145, "name": "NWM-02", "covered_by": ["vera-estate"], "excluded_by": [], "backups": 8,
                "latest_at": NOW - 9 * 3600},
               {"vmid": 104, "name": "LLM", "covered_by": [], "excluded_by": ["vera-estate"], "backups": 0}],
}
CERTS = [
    {"source": "service", "name": "Proxmox (Home)", "where": "192.168.0.200:8006", "names": ["corp.vera.int"],
     "issuer_kind": "Proxmox", "days_left": 277, "state": "ok"},
    {"source": "service", "name": "Vera", "where": "127.0.0.1:8999", "names": ["vera.vera.int"], "issuer_kind": "FreeIPA",
     "days_left": 300, "state": "ok"},
    {"source": "freeipa", "name": "dc.vera.int", "where": "issued by FreeIPA", "names": ["dc.vera.int"],
     "issuer_kind": "FreeIPA", "days_left": 700, "state": "ok"},
    {"source": "freeipa", "name": "dc.vera.int", "where": "issued by FreeIPA", "names": ["dc.vera.int"],
     "issuer_kind": "FreeIPA", "days_left": 40, "state": "superseded"},
]
MESH = [{"host_id": "ssh-130", "label": "Ollama-C (cpu-247)", "host": "192.168.0.247", "ip": "10.66.66.2",
         "state": "up", "connected": True, "door_device": "vera-Ollama-C-dev"}]
IDENTITY = [{"fqdn": "dc.vera.int", "enrolled": True}, {"fqdn": "ollama-e.vera.int", "enrolled": True},
            {"fqdn": "vera.vera.int", "enrolled": True}]
SSH = [{"id": "ssh-pve", "label": "PVE01", "host": "192.168.0.200", "user": "root", "auth": "password"},
       {"id": "ssh-130", "label": "Ollama-C (cpu-247)", "host": "192.168.0.247", "user": "root", "auth": "key"},
       {"id": "ssh-llm", "label": "LLM", "host": "192.168.0.138", "user": "boejaker", "auth": "password"}]
INTEGRATIONS = [{"id": "int-netctl", "label": "netctl (NWM-02)", "kind": "generic", "base_url": "http://192.168.0.221:8088",
                 "access": {"embed": True, "api": False}, "sensitive": True}]
DOCKER = [{"id": "dk-local", "label": "local", "kind": "socket", "url": "unix:///var/run/docker.sock", "ssh_host_id": "ssh-llm"}]
INVENTORY = {
    "pools": [{"name": "tank_sdh", "size": 3_800_000_000_000, "alloc": 520_000_000_000, "free": 3_280_000_000_000, "health": "ONLINE"}],
    "datasets": [{"name": "tank_sdh", "used": 520_000_000_000, "avail": 3_280_000_000_000, "quota": 0, "mountpoint": "/tank_sdh", "type": "filesystem"},
                 {"name": "tank_sdh/subvol-130-disk-0", "used": 43_800_000_000, "avail": 171_000_000_000, "quota": 214_748_364_800,
                  "mountpoint": "/tank_sdh/subvol-130-disk-0", "type": "filesystem"},
                 {"name": "tank_sdh/vera-store", "used": 396_000_000_000, "avail": 3_280_000_000_000, "quota": 0, "mountpoint": "/tank_sdh/vera-store", "type": "filesystem"}],
    "guests": [{"vmid": 130, "name": "Ollama-C", "type": "lxc", "status": "running",
                "datasets": [{"name": "tank_sdh/subvol-130-disk-0", "used": 43_800_000_000, "quota": 214_748_364_800}]}],
    "storages": [{"storage": "tank-sdh", "type": "zfspool", "pool": "tank_sdh"}],
}


def src(**over):
    kw = dict(machines=MACHINES, backups=BACKUPS, certs=CERTS, mesh=MESH, identity=IDENTITY, ssh_hosts=SSH,
              integrations=INTEGRATIONS, docker_hosts=DOCKER, inventory=INVENTORY, now=NOW)
    kw.update(over)
    return core.Sources(**kw)


def test_a_guest_record_joins_every_plane_and_what_it_touches():
    rec = core.resolve("guest:130", src())
    assert rec["found"] and rec["title"] == "Ollama-C" and rec["subtitle"] == "CT 130 · running"
    p = rec["planes"]
    assert p["ssh"]["state"] == "yes" and "root@192.168.0.247 (key)" in p["ssh"]["detail"]
    assert p["directory"]["state"] == "no"
    assert p["mesh"]["state"] == "yes" and "10.66.66.2" in p["mesh"]["detail"] and p["mesh"]["ref"] == "mesh:ssh-130"
    assert p["certificate"]["state"] == "no"
    assert p["backup"]["state"] == "no" and "excluded by vera-estate" in p["backup"]["detail"]
    assert p["backup"]["ref"] == "backup-job:vera-estate"
    kinds = [(r["noun"], r["label"]) for r in rec["related"]]
    assert ("dataset", "tank_sdh/subvol-130-disk-0") in kinds
    assert any(r["detail"].startswith("40.8 GB of 200.0 GB") for r in rec["related"] if r["noun"] == "dataset")
    assert rec["links"][0]["ref"] == "guest:130"


def test_a_cluster_qualified_guest_id_still_resolves():
    assert core.resolve("guest:c1:130", src())["found"]


def test_a_guest_reached_only_through_an_integration_shows_it():
    rec = core.resolve("guest:145", src())
    assert rec["planes"]["ssh"]["state"] == "no"
    assert rec["planes"]["backup"]["state"] == "yes" and "8 copies, latest 9 h ago" in rec["planes"]["backup"]["detail"]
    assert any(r["noun"] == "service" and r["label"] == "netctl (NWM-02)" for r in rec["related"])


def test_a_pool_lists_the_guests_and_datasets_on_it():
    rec = core.resolve("pool:tank_sdh", src())
    assert rec["found"] and rec["subtitle"] == "ZFS pool · ONLINE"
    facts = {f["label"]: f["value"] for f in rec["facts"]}
    assert facts["Guests on it"] == 1 and facts["Datasets"] == 3 and facts["Proxmox storages"] == "tank-sdh"
    assert facts["Used"].endswith("(13%)")
    rel = [(r["noun"], r["label"]) for r in rec["related"]]
    assert ("machine", "Ollama-C") in rel and ("dataset", "tank_sdh/vera-store") in rel
    assert ("dataset", "tank_sdh") not in rel, "the pool's own root dataset is not listed as a child"
    assert core.resolve("pool:corp/tank_sdh", src())["found"], "a node-qualified pool id is accepted"


def test_a_dataset_knows_its_pool_and_owner():
    rec = core.resolve("dataset:tank_sdh/subvol-130-disk-0", src())
    assert rec["found"] and rec["subtitle"] == "ZFS dataset · Ollama-C"
    assert {f["label"]: f["value"] for f in rec["facts"]}["Quota"] == "200.0 GB"
    assert [(r["noun"], r["label"]) for r in rec["related"]] == [("pool", "tank_sdh"), ("machine", "Ollama-C")]


def test_a_certificate_prefers_the_current_copy_and_counts_the_old_ones():
    rec = core.resolve("cert:dc.vera.int", src())
    facts = {f["label"]: f["value"] for f in rec["facts"]}
    assert facts["State"] == "ok" and facts["Days left"] == 700 and facts["Older copies"] == 1


def test_an_integration_points_at_its_machine_and_its_certificate():
    rec = core.resolve("integration:int-netctl", src())
    assert rec["found"] and rec["title"] == "netctl (NWM-02)"
    assert any(r["noun"] == "machine" and r["ref"] == "guest:145" for r in rec["related"])
    assert {f["label"]: f["value"] for f in rec["facts"]}["Access"] == "embed"


def test_a_backup_job_counts_who_it_covers_and_excludes():
    rec = core.resolve("backup-job:vera-estate", src())
    facts = {f["label"]: f["value"] for f in rec["facts"]}
    assert facts["Guests covered"] == 1 and facts["Guests excluded"] == 2 and facts["Last run"] == "never"
    assert rec["related"][0]["ref"] == "guest:145" and rec["related"][0]["detail"] == "8 copies"


def test_a_stale_directory_host_says_no_machine_answers_to_it():
    rec = core.resolve("identity:ollama-e.vera.int", src())
    assert rec["found"] and rec["related"] == []
    assert any("stale" in str(f["value"]) for f in rec["facts"])
    assert core.resolve("identity:dc.vera.int", src())["related"] == []   # dc has no machine row here either


def test_a_mesh_member_and_a_docker_host_point_back_at_their_machine():
    assert core.resolve("mesh:ssh-130", src())["related"][0]["ref"] == "guest:130"
    assert core.resolve("docker-host:dk-local", src())["related"][0]["ref"] == "guest:104"


def test_the_unknown_and_the_unreadable_are_said_plainly():
    assert core.resolve("guest:999", src())["error"] == "no machine matches '999'"
    assert "not an entity reference" in core.resolve("spaceship:1", src())["error"]
    rec = core.resolve("guest:999", src(errors={"backup.status": "did not answer within 30 s"}))
    assert "some readers did not answer: backup.status" in rec["error"]
    assert rec["errors"] == {"backup.status": "did not answer within 30 s"}


def test_a_host_login_without_a_machine_row_still_resolves_from_the_store():
    rec = core.resolve("host:ssh-pve", src(machines=[]))
    assert rec["found"] and rec["title"] == "PVE01" and rec["planes"]["backup"]["state"] == "n/a"
