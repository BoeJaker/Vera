"""Compute load: each ollama instance joined to the machine that owns its
address, that machine's per-core load, the node's GPUs and its Proxmox host -
with a missing link named in `notes`, never invented.

Shapes are the ones prod answered on 2026-10-01 (trimmed)."""
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.estate import compute_load_core as cl  # noqa: E402

NOW = datetime(2026, 10, 1, 18, 30, tzinfo=timezone.utc)
V100 = {"index": 0, "name": "Tesla V100-PCIE-12GB", "total_mb": 12288, "used_mb": 8200, "free_mb": 3853,
        "util_pct": 37, "temp_c": 52}


def _src():
    return {
        "ollama.instances": {
            "gpu-250": {"url": "http://192.168.0.250:11435", "has_gpu": True, "label": "GPU Node", "status": "online"},
            "cpu-246": {"url": "http://192.168.0.246:11435", "has_gpu": False, "label": "CPU Node A"},
            "gpu-250-cpu": {"url": "http://192.168.0.250:11436", "has_gpu": False, "label": "GPU Node (CPU)"},
        },
        "estate.machines": {"machines": [
            {"kind": "proxmox-node", "label": "PVE01", "node": "corp", "addr": "192.168.0.200", "ssh_host_id": "ssh-pve"},
            {"kind": "guest", "label": "Ollama", "vmid": 126, "node": "corp", "type": "lxc", "status": "running",
             "ips": ["192.168.0.250"], "addr": "192.168.0.250", "ssh_host_id": "ssh-126"},
            {"kind": "guest", "label": "Ollama-B", "vmid": 129, "node": "corp", "type": "lxc", "status": "running",
             "ips": ["192.168.0.246"], "ssh_host_id": "ssh-129"},
            {"kind": "host", "label": "Ollama-B (ssh)", "addr": "192.168.0.246", "ssh_host_id": "ssh-other"},
        ]},
        "obs.node_temps": {"hosts": [
            {"host_id": "ssh-126", "label": "ollama126.vera.int", "percpu": {"cpu13": 20.8, "cpu12": 91.0, "x": 1},
             "updated_at": "2026-10-01T18:29:54Z", "error": ""},
            {"host_id": "ssh-129", "label": "Ollama-B (cpu-246)", "percpu": {"cpu0": 14.0, "cpu1": 10.1}, "error": ""},
            {"host_id": "ssh-pve", "label": "PVE01", "pve": True, "percpu": {f"cpu{i}": float(i) for i in range(48)}, "error": ""},
        ]},
        "nodes.agent.status": {"nodes": [
            {"node_id": "gpu-250", "gpu": V100, "gpus": [V100], "load": [3.1, 2.0, 1.5], "runners": 4},
            {"node_id": "gpu-250-cpu", "gpu": V100, "gpus": [V100], "runners": 4},
            {"node_id": "cpu-246", "gpu": None, "gpus": [], "runners": 3},
        ], "unreachable": []},
    }


def _by_id(out):
    return {n["id"]: n for n in out["nodes"]}


def test_instance_joins_guest_cores_gpus_and_host():
    n = _by_id(cl.build(_src(), NOW))["gpu-250"]
    assert n["machine"]["ref"] == "guest:126" and n["machine"]["pve_node"] == "corp"
    assert n["cores"] == [{"cpu": 12, "load": 91.0}, {"cpu": 13, "load": 20.8}]   # sorted, junk key dropped
    assert n["summary"] == {"n": 2, "avg": 55.9, "max": 91.0, "hot": 1}
    assert n["gpus"][0]["util_pct"] == 37
    assert n["host"] == {"label": "PVE01", "pve_node": "corp", "ref": "pve:corp"}
    assert n["notes"] == []


def test_two_instances_on_one_machine_name_each_other():
    by = _by_id(cl.build(_src(), NOW))
    assert by["gpu-250"]["same_machine"] == ["gpu-250-cpu"]
    assert by["gpu-250-cpu"]["same_machine"] == ["gpu-250"]
    assert by["cpu-246"]["same_machine"] == []


def test_guest_owns_its_ip_over_an_ssh_host_row():
    n = _by_id(cl.build(_src(), NOW))["cpu-246"]
    assert n["machine"]["ref"] == "guest:129"
    assert [c["cpu"] for c in n["cores"]] == [0, 1]


def test_host_listed_once_with_every_core_and_its_nodes():
    out = cl.build(_src(), NOW)
    assert len(out["hosts"]) == 1
    h = out["hosts"][0]
    assert h["ref"] == "pve:corp" and h["summary"]["n"] == 48
    assert sorted(h["nodes"]) == ["cpu-246", "gpu-250", "gpu-250-cpu"]


def test_gpu_nodes_first():
    assert [n["id"] for n in cl.build(_src(), NOW)["nodes"]][:2] == ["gpu-250", "gpu-250-cpu"]


def test_agent_from_before_gpus_still_gives_its_card():
    src = _src()
    src["nodes.agent.status"]["nodes"][0] = {"node_id": "gpu-250", "gpu": {"name": "V100", "total_mb": 12288}}
    n = _by_id(cl.build(src, NOW))["gpu-250"]
    assert n["gpus"] == [{"name": "V100", "total_mb": 12288, "index": 0}]


def test_missing_links_are_named_not_invented():
    src = _src()
    src["ollama.instances"]["far"] = {"url": "http://10.9.9.9:11434", "has_gpu": True}
    src["nodes.agent.status"]["unreachable"] = [{"node": "cpu-246"}]
    src["nodes.agent.status"]["nodes"] = [a for a in src["nodes.agent.status"]["nodes"] if a["node_id"] != "cpu-246"]
    by = _by_id(cl.build(src, NOW))
    far = by["far"]
    assert far["machine"] is None and far["host"] is None and far["cores"] == [] and far["gpus"] == []
    assert any("no estate machine owns 10.9.9.9" in x for x in far["notes"])
    assert any("no node agent" in x for x in far["notes"])
    assert any("did not answer" in x for x in by["cpu-246"]["notes"])


def test_probe_error_is_carried():
    src = _src()
    src["obs.node_temps"]["hosts"][1] = {"host_id": "ssh-129", "percpu": {}, "error": "PermissionDenied"}
    n = _by_id(cl.build(src, NOW))["cpu-246"]
    assert n["cores"] == [] and n["summary"]["n"] == 0
    assert "cores: PermissionDenied" in n["notes"]


def test_a_failed_reader_is_named():
    src = _src()
    src["obs.node_temps"] = {"error": "obs.node_temps took longer than 8 s"}
    out = cl.build(src, NOW)
    assert out["sources"]["obs.node_temps"].startswith("obs.node_temps took")
    assert out["sources"]["ollama.instances"] == "ok"
    assert all(n["cores"] == [] for n in out["nodes"])


def test_nothing_read_is_empty_not_an_error():
    out = cl.build({}, NOW)
    assert out["nodes"] == [] and out["hosts"] == []
    assert set(out["sources"].values()) == {"not read"}
