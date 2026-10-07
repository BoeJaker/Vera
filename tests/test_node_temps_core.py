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


# PVE01's `sensors -A -u`, trimmed (2026-10-02): two coretemp chips, each with its own "Core 0"
SENSORS = """tg3-pci-0203
temp1:
  temp1_input: 59.000

coretemp-isa-0000
Package id 0:
  temp1_input: 61.000
Core 0:
  temp2_input: 56.000
Core 8:
  temp10_input: 57.000

coretemp-isa-0001
Package id 1:
  temp1_input: 90.000
Core 0:
  temp2_input: 82.000
Core 13:
  temp15_input: 84.000
"""


def test_every_packages_cores_are_kept_apart():
    assert tc.parse_core_temps(SENSORS) == {"0:0": 56.0, "0:8": 57.0, "1:0": 82.0, "1:13": 84.0}


def test_each_logical_cpu_gets_its_physical_cores_temperature():
    topo = tc.parse_cpu_topo("CPUTOPO|cpu0|0|0\nCPUTOPO|cpu6|0|8\nCPUTOPO|cpu12|1|0\nCPUTOPO|cpu36|1|0\n"
                             "CPUTOPO|cpu47|1|13\nCPUTOPO|cpu5|0|5\nCPUTOPO|bad|x|y\n")
    assert topo["cpu36"] == (1, 0) and "bad" not in topo
    t = tc.cpu_temps(tc.parse_core_temps(SENSORS), topo)
    # hyperthread siblings (cpu12, cpu36) share their core; a core with no reading is left out
    assert t == {"cpu0": 56.0, "cpu6": 57.0, "cpu12": 82.0, "cpu36": 82.0, "cpu47": 84.0}


def test_a_container_moves_its_core_temps_to_the_hosts_too():
    out = tc.attribute(_entry(core_temps={"0:0": 56.0}, cpu_temps={"cpu0": 56.0}), "lxc")
    assert out["core_temps"] == {} and out["cpu_temps"] == {}
    assert out["host_core_temps"] == {"0:0": 56.0} and out["host_cpu_temps"] == {"cpu0": 56.0}


def test_the_input_is_not_changed():
    e = _entry()
    tc.attribute(e, "lxc")
    assert e["temps"] == PVE_TEMPS and e["max_c"] == 91.0
