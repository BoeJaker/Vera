"""The secrets service's rules: named-secret paths, Vera's scoped policy, finding
file-key seals in plain values and JSON documents while leaving OpenBao's own
bootstrap secrets pinned, the SSH store plan, the keydrop payload and findings."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.security import secret_service_core as core  # noqa: E402

pytestmark = pytest.mark.critical

F1, F2 = "fernet:gAAAAone", "fernet:gAAAAtwo"
B1 = "bao:v1:secret:vera-secrets/abc"


def test_paths_are_checked_and_mapped_under_veras_own_prefix():
    assert core.check_path("/Netctl/door-files/") == "netctl/door-files"
    assert core.named_data("secret", "pve01/root") == "secret/data/vera/named/pve01/root"
    assert core.named_metadata("secret/", "pve01/root") == "secret/metadata/vera/named/pve01/root"
    assert core.named_folder("secret") == "secret/metadata/vera/named/"
    assert core.named_folder("secret", "netctl") == "secret/metadata/vera/named/netctl/"
    for bad in ("", "../etc", "a/../b", "has space", "UPPER/../x", "a/" * 8 + "b", "-lead"):
        with pytest.raises(ValueError):
            core.check_path(bad)


def test_the_policy_reaches_only_veras_paths_and_its_own_token():
    hcl = core.policy_hcl("secret")
    paths = [line.split('"')[1] for line in hcl.splitlines() if line.startswith("path ")]
    assert paths == ["secret/data/vera/*", "secret/metadata/vera/*", "secret/data/vera-secrets/*",
                     "secret/metadata/vera-secrets/*", "auth/token/renew-self", "auth/token/lookup-self"]
    assert "sys/" not in hcl and "root" not in hcl


def test_sealed_values_are_found_in_plain_strings_and_json_documents():
    assert core.sealed_in(F1) == [((), "fernet")]
    doc = json.dumps({"imap": {"password": F1, "user": "me"}, "tokens": [B1, F2], "n": 3})
    assert sorted(core.sealed_in(doc)) == [(("imap", "password"), "fernet"), (("tokens", 0), "bao"),
                                           (("tokens", 1), "fernet")]
    assert core.sealed_in("plain text") == [] and core.sealed_in("{not json") == [] and core.sealed_in(7) == []
    assert sorted(core.sealed_strings(doc)) == sorted([F1, F2])
    assert core.sealed_strings(B1, "bao") == [B1]


def test_replacing_keeps_the_documents_shape():
    doc = json.dumps({"imap": {"password": F1, "user": "me"}, "tokens": [B1, F2]})
    new = json.loads(core.replace_sealed(doc, {F1: "bao:v1:secret:vera-secrets/1", F2: "bao:v1:secret:vera-secrets/2"}))
    assert new == {"imap": {"password": "bao:v1:secret:vera-secrets/1", "user": "me"},
                   "tokens": [B1, "bao:v1:secret:vera-secrets/2"]}
    assert core.replace_sealed(F1, {F1: "bao:v1:x:y"}) == "bao:v1:x:y"


def test_the_migration_plan_skips_caches_and_pins_openbaos_own_secrets():
    entries = [
        {"key": "vera:tg:config", "field": "", "value": json.dumps({"bot_token": F1})},
        {"key": "vera:accounts", "field": "a1", "value": json.dumps({"password": F1, "refresh": F2})},
        {"key": "vera:provisioning:state", "field": "main", "value": json.dumps({"openbao_token": F1})},
        {"key": "vera:cap:result:accounts.list", "field": "", "value": json.dumps({"x": F1})},
        {"key": "vera:proxmox:clusters", "field": "c1", "value": json.dumps({"token": B1})},
        {"key": "vera:notes", "field": "", "value": "nothing sealed"},
    ]
    plan = core.migration_plan(entries)
    assert plan["items"] == [{"key": "vera:tg:config", "field": "", "count": 1},
                             {"key": "vera:accounts", "field": "a1", "count": 2}]
    assert (plan["fernet"], plan["pinned"], plan["bao"], plan["keys"]) == (3, 1, 1, 2)


def test_the_ssh_store_plan_counts_every_kind_of_stored_password():
    records = {"pve01": {"label": "PVE01", "password_obf": F1},
               "llm": {"label": "LLM", "password_obf": "bGVnYWN5"},
               "vfs": {"label": "VFS-02", "passphrase_obf": B1},
               "key": {"label": "Ollama-B"}}
    plan = core.ssh_store_plan(records)
    assert plan["counts"] == {"fernet": 1, "bao": 1, "legacy": 1}
    assert [(i["id"], i["kind"]) for i in plan["items"]] == [("pve01", "fernet"), ("llm", "legacy")]


def test_the_keydrop_payload_matches_the_pick_tool():
    p = core.keydrop_payload("OpenBao (Vera)", password="k", notes="n", tags=["vera"], extra={"root_token": "t"})
    assert p == {"title": "OpenBao (Vera)", "username": "", "password": "k", "url": "", "notes": "n",
                 "tags": ["vera"], "extra": {"root_token": "t"}}
    assert "extra" not in core.keydrop_payload("x")


def test_findings_describe_the_state_most_serious_first():
    dev = core.findings({"reachable": True, "storage": "inmem", "initialized": True, "sealed": False},
                        {"openbao_configured": False}, {}, {"available": True, "chain_ok": True}, 12)
    assert [f["severity"] for f in dev] == ["error", "warn", "info"]
    assert dev[0]["message"].startswith("OpenBao keeps its data in memory")
    sealed = core.findings({"reachable": True, "storage": "file", "initialized": True, "sealed": True},
                           {"openbao_configured": True, "openbao_active": False}, {"root": True},
                           {"available": False, "error": "no recipient key"}, 0)
    assert [f["message"][:30] for f in sealed] == ["OpenBao is sealed, so Vera can", "Vera is configured for OpenBao",
                                                   "Vera is using OpenBao's root t", "Keydrop is not available here,"]
    healthy = core.findings({"reachable": True, "storage": "file", "initialized": True, "sealed": False},
                            {"openbao_configured": True, "openbao_active": True},
                            {"root": False, "ttl_s": 2_000_000, "renewable": True},
                            {"available": True, "chain_ok": True}, 0)
    assert healthy == []
    down = core.findings({"reachable": False, "error": "refused"}, {"openbao_configured": True, "openbao_active": False},
                         {}, {"available": True, "chain_ok": False, "message": "entry 3"}, 0)
    assert [f["severity"] for f in down] == ["error", "error", "warn"]
