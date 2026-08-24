"""Mesh now completes on provisioned CT/VM via the node self-enrol feature (curl to
netsec.mesh.enroll) rather than the server-side enroll.guest join that timed out."""
from vera.foundry.features_core import feature_script


def test_mesh_feature_self_enrols():
    sc = feature_script("mesh", {"vera_url": "https://10.0.0.1:8999", "mesh_token": "TOK123"})
    assert "/netsec/mesh/enroll" in sc
    assert "TOK123" in sc                        # presents the enrol token
    assert "https://10.0.0.1:8999" in sc          # to the LAN-reachable Vera URL
    assert "wg genkey" in sc and "wg pubkey" in sc
    assert "wg-quick up vera0" in sc
    # the node's PRIVATE key is generated locally and never sent (only the pubkey)
    assert "PUB=$(wg pubkey" in sc


def test_mesh_feature_empty_ctx_still_renders():
    sc = feature_script("mesh", {})
    assert sc.startswith("#!/bin/sh")
    assert "/netsec/mesh/enroll" in sc
