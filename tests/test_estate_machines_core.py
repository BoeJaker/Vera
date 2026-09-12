"""The Machines pane's one list: how nodes.list rows and Proxmox guests join,
what state and actions each machine gets, and the order they are listed in.
Shapes follow nodes.list and /cluster/resources as seen on 12 Sep 2026."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.estate import estate_machines_core as core  # noqa: E402

pytestmark = pytest.mark.critical

NODES = [
    {"id": "pve01", "label": "PVE01", "addr": "192.168.0.200", "ssh_host_id": "pve01", "docker_host_id": "",
     "proxmox": {"kind": "node", "cluster_id": "home", "node": "corp"},
     "hw": {"cpu_cores": 48, "ram_gb": 251.7, "gpu_name": "Tesla V100-PCIE-12GB", "gpu_count": 1},
     "backends": ["ssh"]},
    {"id": "g126", "label": "pve:126@corp", "addr": "192.168.0.250", "ssh_host_id": "g126",
     "proxmox": {"kind": "guest", "cluster_id": "home", "vmid": 126, "node": "corp"},
     "ollama": [{"id": "gpu-250", "label": "GPU Node"}], "backends": ["ssh"]},
    {"id": "g999", "label": "pve:999@corp", "ssh_host_id": "g999",
     "proxmox": {"kind": "guest", "cluster_id": "home", "vmid": 999, "node": "corp"}, "backends": ["ssh"]},
    {"id": "h1", "label": "ollama126.vera.int", "addr": "192.168.0.99", "ssh_host_id": "h1",
     "proxmox": None, "backends": ["ssh"]},
    {"id": "docker-x", "label": "192.168.0.250 (vera-worker)", "ssh_host_id": "",
     "docker_host_id": "192.168.0.250-(vera-worker)", "proxmox": None, "backends": ["docker"]},
]
GUESTS = [
    {"cluster_id": "corp-id", "vmid": 126, "name": "ollama-gpu", "type": "lxc", "node": "corp",
     "status": "running", "template": False, "maxcpu": 12, "maxmem": 50 * 2 ** 30},
    {"cluster_id": "corp-id", "vmid": 147, "name": "kali-2020-4", "type": "qemu", "node": "corp",
     "status": "running", "template": False},
    {"cluster_id": "corp-id", "vmid": 900, "name": "scratch", "type": "lxc", "node": "corp",
     "status": "stopped", "template": False},
    {"cluster_id": "corp-id", "vmid": 141, "name": "debian-12-tmpl", "type": "qemu", "node": "corp",
     "status": "stopped", "template": True},
]


def machines(nodes=NODES, guests=GUESTS):
    return {r["label"]: r for r in core.build_machines(nodes, guests)}


def ids(row):
    return [a["id"] for a in row["actions"]]


def host(hid, label, addr):
    return {"id": hid, "label": label, "addr": addr, "ssh_host_id": hid, "proxmox": None, "backends": ["ssh"]}


def guest(vmid, name, status="running", ips=(), gtype="lxc"):
    return {"cluster_id": "corp-id", "vmid": vmid, "name": name, "type": gtype, "node": "corp",
            "status": status, "template": False, "ips": list(ips)}


def test_every_machine_is_one_row_in_kind_state_name_order():
    labels = [r["label"] for r in core.build_machines(NODES, GUESTS)]
    assert labels == ["PVE01", "kali-2020-4", "ollama-gpu", "scratch", "debian-12-tmpl",
                      "pve:999@corp", "ollama126.vera.int", "192.168.0.250 (vera-worker)"]


def test_an_enrolled_guest_takes_its_state_and_cluster_from_proxmox():
    row = machines()["ollama-gpu"]
    assert (row["kind"], row["status"], row["type"], row["vmid"]) == ("guest", "running", "lxc", 126)
    assert row["cluster_id"] == "corp-id"
    assert row["ssh_label"] == "pve:126@corp" and row["note"] == ""
    assert row["hardware"] == ["12 cores", "50 GB RAM"]
    assert row["runs"] == ["ollama GPU Node"]
    assert ids(row) == ["console", "shutdown", "reboot", "ssh", "detect"]
    assert row["actions"][0]["mode"] == "term"


def test_a_guest_proxmox_knows_but_ssh_does_not_is_still_listed():
    row = machines()["kali-2020-4"]
    assert row["note"] == "not enrolled for SSH" and row["backends"] == ["proxmox"]
    assert ids(row) == ["console", "shutdown", "reboot", "ssh"]
    assert row["actions"][0]["mode"] == "vnc"


def test_stopped_guests_can_only_be_started_and_templates_offer_nothing():
    rows = machines()
    assert ids(rows["scratch"]) == ["start"]
    assert rows["debian-12-tmpl"]["status"] == "template" and ids(rows["debian-12-tmpl"]) == []


def test_an_enrolled_guest_missing_from_proxmox_keeps_its_terminal():
    row = machines()["pve:999@corp"]
    assert row["status"] == "unknown" and "does not list" in row["note"]
    assert ids(row) == ["ssh", "detect"]


def test_nodes_hosts_and_docker_hosts_get_their_own_actions():
    rows = machines()
    assert ids(rows["PVE01"]) == ["ssh", "cpu", "detect"]
    assert rows["PVE01"]["hardware"] == ["Tesla V100-PCIE-12GB", "48 cores", "252 GB RAM"]
    assert ids(rows["ollama126.vera.int"]) == ["ssh", "detect"]
    assert rows["192.168.0.250 (vera-worker)"]["kind"] == "docker-host"
    assert ids(rows["192.168.0.250 (vera-worker)"]) == ["containers"]


def test_counts():
    assert core.counts(core.build_machines(NODES, GUESTS)) == {
        "proxmox-node": 1, "guest": 5, "host": 1, "docker-host": 1, "machines": 8,
        "running_guests": 2, "stopped_guests": 1, "templates": 1}


def test_no_proxmox_answer_still_lists_what_nodes_list_knows():
    rows = core.build_machines(NODES, [])
    assert len(rows) == 5
    assert all(r["status"] == "unknown" for r in rows if r["kind"] == "guest")


def test_an_ssh_host_on_a_guests_static_ip_is_that_guest():
    rows = machines([host("vfs", "VFS-02", "192.168.0.160")], [guest(160, "VFS-02", ips=["192.168.0.160"])])
    assert list(rows) == ["VFS-02"]
    row = rows["VFS-02"]
    assert (row["kind"], row["ssh_host_id"], row["logins"], row["note"]) == ("guest", "vfs", 1, "")
    assert ids(row) == ["console", "shutdown", "reboot", "ssh", "detect"]


def test_a_guest_without_a_static_ip_joins_the_host_with_its_name():
    rows = machines([host("llm", "LLM", "192.168.0.138")], [guest(104, "LLM", gtype="qemu")])
    assert list(rows) == ["LLM"]
    assert rows["LLM"]["addr"] == "192.168.0.138" and rows["LLM"]["vmid"] == 104


def test_a_login_for_another_address_is_not_merged_by_name():
    rows = machines([host("stale", "Ollama-b", "192.168.0.248")], [guest(129, "Ollama-B", ips=["192.168.0.246"])])
    assert sorted(rows) == ["Ollama-B", "Ollama-b"]
    assert rows["Ollama-b"]["kind"] == "host"


def test_repeated_logins_collapse_into_one_row():
    nodes = [host("d1", "Ollama-D", "192.168.0.248"), host("d2", "Ollama-D", "192.168.0.248"),
             host("d3", "Ollama-D", "192.168.0.248"),
             host("f1", "foundry-ct-test.vera.int", "192.168.0.95"),
             host("f2", "foundry-ct-test.vera.int", "192.168.0.95")]
    rows = machines(nodes, [guest(131, "Ollama-D", status="stopped", ips=["192.168.0.248"])])
    assert sorted(rows) == ["Ollama-D", "foundry-ct-test.vera.int"]
    stopped = rows["Ollama-D"]
    assert (stopped["kind"], stopped["logins"], stopped["note"]) == ("guest", 3, "3 saved logins")
    assert ids(stopped) == ["start"]
    assert rows["foundry-ct-test.vera.int"]["logins"] == 2


def test_static_ips_come_from_net_and_ipconfig_keys():
    config = {"net0": "name=eth0,bridge=vmbr0,gw=192.168.0.1,hwaddr=AA:BB,ip=192.168.0.160/24,type=veth",
              "net1": "name=eth1,bridge=vmbr1,ip=10.33.33.11/24", "ipconfig0": "ip=dhcp",
              "description": "ip=1.2.3.4 is not a network key"}
    assert core.guest_ips(config) == ["10.33.33.11", "192.168.0.160"]
    assert core.guest_ips(None) == [] and core.guest_ips([{"vmid": 1}]) == []


def test_a_login_saved_under_another_name_says_so():
    rows = machines([host("e2e", "e2e-mgr.vera.int", "192.168.0.96")],
                    [guest(146, "homeassistant", ips=["192.168.0.96"])])
    assert list(rows) == ["homeassistant"]
    assert rows["homeassistant"]["note"] == "SSH login saved as e2e-mgr.vera.int"


def test_the_lan_address_is_shown_before_a_storage_network_address():
    rows = machines([], [guest(160, "VFS-02", ips=["10.33.33.11", "192.168.0.160"])])
    assert rows["VFS-02"]["addr"] == "192.168.0.160"
    assert rows["VFS-02"]["ips"] == ["192.168.0.160", "10.33.33.11"]
