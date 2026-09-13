"""Pure tests for ollama_node_core — the provisioning rules for an Ollama node.

Each test states the live defect it guards. Imports lowercase
`vera.provisioning.ollama_node_core` with the repo root on sys.path so the
WORKTREE copy is exercised.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.provisioning.ollama_node_core import (  # noqa: E402
    DEFAULT_OLLAMA_PORT, DONE_MARKER, DROPIN_PATH, ct_install_script,
    find_instance_by_url, install_succeeded, normalise_url, ollama_host_dropin,
    registration_plan)

pytestmark = pytest.mark.critical


def test_the_recipe_binds_every_interface_not_loopback():
    """Measured on CT130: the stock unit carries only PATH in its Environment and
    the daemon listens on 127.0.0.1:11434, so a node provisioned by the old
    recipe was registered at an address nothing outside the container reaches."""
    script = ct_install_script(11435)
    assert 'OLLAMA_HOST=0.0.0.0:11435' in script
    assert DROPIN_PATH in script
    assert "127.0.0.1:11435/api/tags" in script       # the reachability check
    assert script.rstrip().endswith(DONE_MARKER)


def test_the_recipe_honours_the_requested_port_everywhere():
    """The old recipe ignored the port completely and always left 11434."""
    script = ct_install_script(11500)
    assert "11434" not in script
    assert script.count("11500") >= 2                 # the bind AND the check


def test_the_recipe_proves_the_node_answers_before_reporting_success():
    """`systemctl is-active` was the old proof, which a loopback-bound daemon
    also passes. Success now requires an answer on the port itself."""
    script = ct_install_script(11435)
    check = script.index("curl -fsS -m 5 http://127.0.0.1:11435/api/tags")
    assert check < script.index("echo " + DONE_MARKER)
    assert install_succeeded("...\n" + DONE_MARKER + "\n")
    assert not install_succeeded("active\n")          # unit up, unreachable


def test_the_recipe_adapts_to_a_login_that_is_not_root():
    root = ct_install_script(11434, sudo="")
    user = ct_install_script(11434, sudo="sudo ")
    assert "sudo " not in root
    assert "sudo systemctl restart ollama" in user
    assert "| sudo tee " + DROPIN_PATH in user


def test_the_dropin_is_a_dropin_not_an_edit_of_the_units_own_file():
    """An Ollama upgrade rewrites /etc/systemd/system/ollama.service; a drop-in
    survives it, an edited unit does not."""
    assert DROPIN_PATH.endswith(".conf")
    assert "/ollama.service.d/" in DROPIN_PATH
    assert ollama_host_dropin(11435).startswith("[Service]")


def test_urls_compare_the_way_a_node_answers_them():
    same = ["http://192.168.0.250:11435", "HTTP://192.168.0.250:11435/",
            "http://192.168.0.250:11435/api", "192.168.0.250:11435"]
    assert len({normalise_url(u) for u in same}) == 1
    assert normalise_url("http://h") == "http://h:%d" % DEFAULT_OLLAMA_PORT
    assert normalise_url("https://h") == "https://h:443"
    assert normalise_url("") == ""
    assert normalise_url("http://h:11435") != normalise_url("http://h:11434")


INSTANCES = {
    "gpu-250": {"url": "http://192.168.0.250:11435", "has_gpu": True},
    "cpu-246": {"url": "http://192.168.0.246:11435/", "has_gpu": False},
}


def test_registration_reuses_the_id_already_serving_that_ollama():
    """add_ollama_instance stores whatever id it is given, so re-provisioning
    gpu-250 as node-192-168-0-250-11435 would add a SECOND id for one server —
    and the gate's capacity is per id, so the card would be booked twice."""
    plan = registration_plan(INSTANCES, "192.168.0.250", 11435, has_gpu=True)
    assert plan["action"] == "reuse"
    assert plan["instance_id"] == "gpu-250"
    assert "twice" in plan["reason"]


def test_registration_creates_an_id_for_a_node_nothing_serves_yet():
    plan = registration_plan(INSTANCES, "192.168.0.251", 11435)
    assert plan["action"] == "create"
    assert plan["instance_id"] == "node-192-168-0-251-11435"
    plan = registration_plan(INSTANCES, "192.168.0.251", 11435, preferred_id="gpu-251")
    assert plan["instance_id"] == "gpu-251"


def test_a_different_port_on_a_registered_host_is_a_different_node():
    """The SSH path installed on 11434 while registering the requested port; the
    two must not be mistaken for each other."""
    assert find_instance_by_url(INSTANCES, "http://192.168.0.250:11434") is None
    assert find_instance_by_url(INSTANCES, "http://192.168.0.250:11435") == "gpu-250"
