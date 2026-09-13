"""Folding the enrolment SSH store into the exec store: which exec record is a
login's twin, what a merge would change, and what ssh.host.list returns while
both stores exist. Shapes follow both stores on prod, 12 Sep 2026."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.provisioning import ssh_store_merge_core as core  # noqa: E402

pytestmark = pytest.mark.critical

KEY = "/home/boejaker/.vera/ssh/id_vera"
CLUSTER = "2d83c1b5-b25a-4b47-98b8-50660e24b682"


def exec_rec(xid, label, host, user="root", auth="key", tags=None, updated="2026-08-04T20:00:00Z"):
    return {"id": xid, "label": label, "host": host, "port": 22, "user": user, "auth": auth,
            "key_path": KEY if auth == "key" else "", "tags": tags if tags is not None else ["enrolled", "lxc"],
            "has_password": auth == "password", "updated_at": updated}


def enrol_rec(eid, label, host, user="root", auth="cert", vmid=126, **kw):
    rec = {"id": eid, "label": label, "host": host, "port": "22", "user": user, "auth": auth,
           "guest_ref": f"{CLUSTER}:{vmid}" if vmid else "", "public_key": "", "has_password": False,
           "has_private_key": auth == "key"}
    rec.update(kw)
    return rec


def by_enrol(plan):
    return {s["enrol_id"]: s for s in plan["steps"]}


def test_a_twin_is_the_same_host_port_and_user():
    twin = core.find_twin(enrol_rec("e1", "ollama126.vera.int", "192.168.0.250"),
                          [exec_rec("x1", "ollama126.vera.int", "192.168.0.250"),
                           exec_rec("x2", "other", "192.168.0.250", user="vera")])
    assert twin["id"] == "x1"


def test_among_several_twins_the_same_label_then_the_newest_wins():
    records = [exec_rec("old", "Ollama-D", "192.168.0.248", updated="2026-08-04T20:25:19Z"),
               exec_rec("new", "Ollama-D", "192.168.0.248", updated="2026-08-04T22:11:56Z"),
               exec_rec("pw", "Ollama-b", "192.168.0.248", auth="password", updated="2026-09-01T00:00:00Z")]
    assert core.find_twin(enrol_rec("e", "Ollama-D", "192.168.0.248"), records)["id"] == "new"
    assert core.find_twin(enrol_rec("e", "ct901.vera.int", "192.168.0.248"), records)["id"] == "pw"


def test_merging_links_twins_by_tag_and_never_repeats_a_tag():
    exec_records = [exec_rec("x-fct", "foundry-ct-test.vera.int", "192.168.0.95"),
                    exec_rec("x-vfs", "VFS-02", "192.168.0.160", tags=["vfs"])]
    enrol = [enrol_rec("e1", "foundry-ct-test.vera.int", "192.168.0.95", vmid=144),
             enrol_rec("e2", "foundry-ct-test.vera.int", "192.168.0.95", vmid=144)]
    plan = core.plan_merge(enrol, exec_records, KEY)
    steps = by_enrol(plan)
    assert steps["e1"]["action"] == "link"
    assert steps["e1"]["add_tags"] == [f"guest:{CLUSTER}:144", "enrol:e1"]
    assert steps["e2"]["action"] == "link" and steps["e2"]["add_tags"] == ["enrol:e2"]
    assert plan["exec_records_changed"] == 1 and plan["exec_records_created"] == 0


def test_an_already_linked_twin_needs_nothing():
    exec_records = [exec_rec("x", "Ollama-E", "192.168.0.249", tags=["enrolled", f"guest:{CLUSTER}:132", "enrol:e"])]
    plan = core.plan_merge([enrol_rec("e", "Ollama-E", "192.168.0.249", vmid=132)], exec_records, KEY)
    assert plan["steps"][0]["action"] == "linked" and plan["counts"] == {"linked": 1}


def test_logins_without_a_twin_are_copied_when_they_can_be():
    plan = core.plan_merge([enrol_rec("cert", "new-ct", "192.168.0.170"),
                            enrol_rec("pw", "old-box", "192.168.0.171", auth="password", vmid=None, has_password=True),
                            enrol_rec("key", "laptop", "192.168.0.172", user="vera", auth="key", vmid=None)],
                           [], KEY)
    steps = by_enrol(plan)
    assert (steps["cert"]["action"], steps["cert"]["exec_auth"], steps["cert"]["key_path"]) == ("copy", "key", KEY)
    assert (steps["pw"]["action"], steps["pw"]["exec_auth"]) == ("copy", "password")
    assert steps["key"]["action"] == "attention" and "key file path" in steps["key"]["reason"]
    assert plan["counts"] == {"copy": 2, "attention": 1} and plan["exec_records_created"] == 2


def test_read_through_keeps_enrolment_rows_and_adds_exec_only_logins():
    exec_rows = [exec_rec("x-twin", "ollama126.vera.int", "192.168.0.250"),
                 exec_rec("x-llm", "LLM", "192.168.0.138", user="boejaker", auth="password", tags=[]),
                 exec_rec("x-vm", "VFS-02", "192.168.0.160", tags=["vfs", f"guest:{CLUSTER}:160"])]
    rows = core.read_through([enrol_rec("e1", "ollama126.vera.int", "192.168.0.250")], exec_rows)
    assert [r["id"] for r in rows] == ["e1", "x-llm", "x-vm"]
    assert (rows[0]["source"], rows[0]["exec_id"]) == ("enrol", "x-twin")
    llm = rows[1]
    assert (llm["source"], llm["auth"], llm["has_password"], llm["has_private_key"]) == ("exec", "password", True, False)
    assert rows[2]["guest_ref"] == f"{CLUSTER}:160" and rows[2]["port"] == 22
