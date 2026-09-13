"""SSH login cleanup rules: exact repeats merge into one login and hand it their
tags, an old password login beside a key login is superseded, a login that does
not answer and matches no guest is stale, and a referenced login always stays."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.execution import ssh_cleanup_core as core  # noqa: E402

pytestmark = pytest.mark.critical

KEY = "/home/boejaker/.vera/ssh/id_vera"


def login(host, label, user="root", auth="key", tags=(), updated="2026-08-01T00:00:00Z"):
    return {"host": host, "port": 22, "user": user, "auth": auth, "label": label,
            "key_path": KEY if auth == "key" else "", "tags": list(tags), "updated_at": updated}


LOGINS = {
    "d1": login("192.168.0.248", "Ollama-D", tags=["enrolled", "enrol:e1"], updated="2026-08-07T00:00:00Z"),
    "d2": login("192.168.0.248", "Ollama-D", tags=["enrolled", "enrol:e2", "guest:c:131"], updated="2026-08-09T00:00:00Z"),
    "d3": login("192.168.0.248", "Ollama-D", tags=["enrolled"], updated="2026-08-08T00:00:00Z"),
    "ob": login("192.168.0.248", "Ollama-b", auth="password"),
    "pve": login("192.168.0.200", "PVE01", auth="password"),
    "gone": login("192.168.0.152", "foundry-mesh-val.vera.lab", tags=["enrolled"]),
    "stopped": login("192.168.0.150", "foundry-test-1.vera.lab"),
    "ha": login("192.168.0.96", "e2e-mgr.vera.int", user="vera"),
    "local": login("localhost", "localhost", user="boejaker", auth="password"),
    "w1": login("192.168.0.97", "e2e-wkr.vera.int", user="vera"),
    "w2": login("192.168.0.97", "foundry-e2e.vera.int", user="vera", updated="2026-08-10T00:00:00Z"),
}
GUESTS = [{"name": "Ollama-D", "ips": ["192.168.0.248"]}, {"name": "PVE01"},
          {"name": "foundry-test-1", "ips": []}, {"name": "homeassistant", "ips": ["192.168.0.96"]}]
REACH = {"gone": False, "stopped": False, "ha": True, "w1": False, "w2": False, "pve": True}


def steps(p):
    return {s["id"]: s for s in p["steps"]}


def test_repeats_merge_into_the_newest_enrolled_login_and_it_gains_their_tags():
    p = core.plan(LOGINS, set(), REACH, GUESTS)
    s = steps(p)
    assert s["d2"]["action"] == "keep"
    assert s["d1"]["action"] == "merge" and s["d1"]["into"] == "d2"
    assert s["d3"]["action"] == "merge" and s["d3"]["into"] == "d2"
    assert p["tag_updates"]["d2"] == ["enrol:e1"]


def test_a_referenced_repeat_is_kept_and_becomes_the_login_that_stays():
    p = core.plan(LOGINS, {"d1"}, REACH, GUESTS)
    s = steps(p)
    assert s["d1"]["action"] == "keep" and s["d2"]["action"] == "merge" and s["d2"]["into"] == "d1"
    assert s["ob"]["into"] in ("d1",)


def test_a_password_login_beside_a_key_login_is_superseded_unless_referenced():
    s = steps(core.plan(LOGINS, set(), REACH, GUESTS))
    assert s["ob"]["action"] == "superseded" and s["ob"]["into"] == "d2"
    assert steps(core.plan(LOGINS, {"ob"}, REACH, GUESTS))["ob"]["action"] == "keep"
    assert s["pve"]["action"] == "keep"          # no key login for PVE01's root


def test_stale_needs_no_answer_and_no_guest_with_that_address_or_name():
    s = steps(core.plan(LOGINS, set(), REACH, GUESTS))
    assert s["gone"]["action"] == "stale"
    assert s["stopped"]["action"] == "keep"     # a guest named foundry-test-1 still exists
    assert s["ha"]["action"] == "keep"          # answers
    assert s["local"]["action"] == "keep"       # never judged stale
    unprobed = steps(core.plan(LOGINS, set(), {}, GUESTS))
    assert unprobed["gone"]["action"] == "keep"


def test_different_labels_on_the_same_login_still_merge_and_counts_add_up():
    p = core.plan(LOGINS, set(), REACH, GUESTS)
    s = steps(p)
    assert s["w1"]["action"] == "merge" and s["w1"]["into"] == "w2"
    assert s["w2"]["action"] == "stale"         # the one that stays still does not answer
    assert sum(p["counts"].values()) == len(LOGINS)
    assert p["counts"] == {"keep": 5, "merge": 3, "superseded": 1, "stale": 2}


def test_names_are_compared_without_domains_or_node_suffixes():
    assert core.norm_name("Ollama-B (cpu-246)") == "ollama-b"
    assert core.norm_name("foundry-test-1.vera.lab") == "foundry-test-1"
    assert core.login_key({"host": "HOST", "port": "x", "user": " root "}) == ("host", 22, "root", "password", "")
