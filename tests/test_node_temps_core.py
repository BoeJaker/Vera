"""A container's temperatures are its host's: obs.node_temps must not report
PVE01's sensors as every LXC guest's own (owner, 2026-09-28: the cpu-cores
matrix showed 82-85 °C for every host; on prod 2026-10-01 ollama126, Ollama-C
and VFS-02 each carried PVE01's "Core 0..13" and drives sda..sdj)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.workers import node_temps_core as tc  # noqa: E402

PVE_TEMPS = {"Package id 0": 64.0, "Core 0": 82.0, "Core 9": 91.0, "drive sda": 34.0}


def _entry(**kw):
    e = {"host_id": "h", "label": "ollama126.vera.int", "pve": False, "temps": dict(PVE_TEMPS), "max_c": 91.0,
         "percpu": {"cpu12": 31.7}, "health": {"fan": {"Fan 1": 2200.0}, "voltage": {}, "power": {}},
         "drives": {"sda": {"temp_c": 34.0}}, "disk_usage": [{"mount": "/", "used_pct": 40.0}], "error": ""}
    e.update(kw)
    return e


def test_parse_virt():
    assert tc.parse_virt("VIRT|lxc\nSENSORS_BEGIN\n") == "lxc"
    assert tc.parse_virt("x\nVIRT|none\n") == ""
    assert tc.parse_virt("VIRT|\n") == ""
    assert tc.parse_virt("SENSORS_BEGIN") == ""          # a probe from before the line
    assert tc.parse_virt("VIRT|Docker \n") == "docker"


def test_a_container_carries_its_hosts_readings_as_the_hosts():
    out = tc.attribute(_entry(), "lxc")
    assert out["temps"] == {} and out["max_c"] is None and out["drives"] == {}
    assert out["host_temps"] == PVE_TEMPS and out["host_drives"] == {"sda": {"temp_c": 34.0}}
    assert out["host_health"]["fan"] == {"Fan 1": 2200.0} and out["health"]["fan"] == {}
    assert out["temps_from"] == "host" and out["virt"] == "lxc"


def test_load_and_disk_stay_the_containers_own():
    out = tc.attribute(_entry(), "lxc")
    assert out["percpu"] == {"cpu12": 31.7}
    assert out["disk_usage"] == [{"mount": "/", "used_pct": 40.0}]


def test_a_machine_keeps_its_own():
    out = tc.attribute(_entry(label="PVE01", pve=True), "")
    assert out["temps"] == PVE_TEMPS and out["max_c"] == 91.0 and out["temps_from"] == "own"
    assert "host_temps" not in out


def test_no_sensors_in_a_container_is_not_a_fault():
    out = tc.attribute(_entry(temps={}, max_c=None, error="no sensors available (lm-sensors not installed)"), "lxc")
    assert out["error"] == ""


def test_a_real_failure_in_a_container_is_kept():
    out = tc.attribute(_entry(error="PermissionDenied"), "lxc")
    assert out["error"] == "PermissionDenied"


def test_the_input_is_not_changed():
    e = _entry()
    tc.attribute(e, "lxc")
    assert e["temps"] == PVE_TEMPS and e["max_c"] == 91.0
