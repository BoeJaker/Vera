import asyncio
import copy
import sys

import pytest

from vera.integrations.source_build_plan import (
    build_plan_contract,
    plan_source_build,
)


pytestmark = pytest.mark.critical
_DIGEST = "sha256:" + "a" * 64


def _manifest():
    return {
        "entrypoints": ["tool.inspect"],
        "license": "Apache-2.0",
        "effects": ["none"],
        "resources": {"cpu": "shared", "memory_mb": 256,
                      "accelerator": "none"},
        "secret_refs": ["secretref:external/tool"],
        "network": {"mode": "deny", "allowlist": []},
    }


def _descriptor(kind):
    provenance = {
        "python": {"package": "example-tool", "version": "1.2.3",
                   "artifact_digest": _DIGEST},
        "cli": {"artifact": "example-tool-linux-amd64", "version": "1.2.3",
                "artifact_digest": _DIGEST},
        "oci": {"image": "registry.example/tool@" + _DIGEST},
        "repository": {"url": "https://code.example/team/tool.git",
                       "revision": "b" * 40, "archive_digest": _DIGEST},
    }[kind]
    return {"source_id": "example-" + kind, "kind": kind,
            "provenance": provenance, "manifest": _manifest()}


@pytest.mark.parametrize("kind", ["python", "cli", "oci", "repository"])
def test_each_source_kind_produces_a_stable_inert_proposal(kind):
    before = set(sys.modules)
    first = plan_source_build(_descriptor(kind)).to_dict()
    second = plan_source_build(copy.deepcopy(_descriptor(kind))).to_dict()
    assert first == second
    assert first["source_kind"] == kind
    assert first["lifecycle"] == {"current": "proposed", "next": "built"}
    assert first["provenance_verified"] is False
    assert first["ready_for_build"] is False
    assert first["ready_for_activation"] is False
    assert first["approval"] == {
        "required": True, "granted": False, "scope": "activation"}
    assert first["rollback"]["required_before_activation"] is True
    assert first["upgrade"]["requires_new_plan"] is True
    assert first["teardown"]["required"] is True
    assert all(first[key] is False for key in (
        "network_io", "fetches", "installs", "builds", "activates",
        "registers", "executes"))
    assert set(sys.modules) == before


def test_plan_requires_every_review_and_recovery_evidence_class():
    result = plan_source_build(_descriptor("repository")).to_dict()
    assert set(result["required_evidence"]) == {
        "approval_receipt", "conformance_report", "license_review",
        "malware_scan", "manifest_review", "policy_decision", "rollback_proof",
        "sbom", "teardown_proof", "vulnerability_scan",
    }
    assert [stage["id"] for stage in result["stages"]] == [
        "materialize", "scan", "review", "verify", "approve", "activate",
        "export", "upgrade", "rollback", "teardown"]
    assert all(stage["status"] == "queued" for stage in result["stages"])


@pytest.mark.parametrize("kind,patch,match", [
    ("python", {"version": ">=1"}, "exact package and version"),
    ("cli", {"artifact_digest": "sha256:bad"}, "sha256 digest"),
    ("oci", {"image": "registry.example/tool:latest"}, "image@sha256"),
    ("repository", {"revision": "main"}, "full 40- or 64-hex"),
    ("repository", {"url": "https://user:pass@code.example/tool"},
     "credential-free HTTPS"),
    ("repository", {"url": "https://code.example/tool?token=plaintext"},
     "credential-free HTTPS"),
])
def test_mutable_or_credentialed_provenance_fails_closed(kind, patch, match):
    doc = _descriptor(kind)
    doc["provenance"].update(patch)
    with pytest.raises(ValueError, match=match):
        plan_source_build(doc)


def test_plaintext_secrets_and_unsafe_manifest_policy_fail_closed():
    doc = _descriptor("python")
    doc["manifest"]["api_token"] = "plaintext"
    with pytest.raises(ValueError, match="plaintext credentials"):
        plan_source_build(doc)
    doc = _descriptor("python")
    doc["manifest"]["credentials"] = ["plaintext"]
    with pytest.raises(ValueError, match="plaintext credentials"):
        plan_source_build(doc)
    for field, value, match in (
        ("secret_refs", ["actual-secret"], "opaque secretref"),
        ("effects", ["none", "network.read"], "cannot be combined"),
        ("network", {"mode": "deny", "allowlist": ["https://example.com"]},
         "cannot include"),
        ("network", {"mode": "allowlist", "allowlist": ["http://example.com"]},
         "credential-free HTTPS"),
        ("resources", {"cpu": "shared", "memory_mb": 0,
                       "accelerator": "none"}, "memory_mb"),
    ):
        doc = _descriptor("python")
        doc["manifest"][field] = value
        with pytest.raises(ValueError, match=match):
            plan_source_build(doc)


def test_contract_and_capability_wrappers_are_honest_and_ui_is_visible():
    contract = build_plan_contract()
    assert contract["proposal_contract"] == "implemented"
    assert contract["build_execution"] == "queued_live"
    assert contract["activation_execution"] == "queued_live"
    assert contract["supported_kinds"] == ["cli", "oci", "python", "repository"]
    from vera.integrations import integrations_capabilities as caps
    status = asyncio.run(caps.integration_source_build_status.__wrapped__())
    result = asyncio.run(caps.integration_source_build_plan.__wrapped__(
        document=_descriptor("oci")))
    rejected = asyncio.run(caps.integration_source_build_plan.__wrapped__(
        document={"kind": "unknown"}))
    assert status == contract
    assert result["accepted"] is True and result["executes"] is False
    assert rejected["accepted"] is False
    assert rejected["ready_for_activation"] is False
    panel = caps._HERE.joinpath("integrations_panel.html").read_text(encoding="utf-8")
    assert "/integrations/source/build/status" in panel
    assert "execution queued" in panel
