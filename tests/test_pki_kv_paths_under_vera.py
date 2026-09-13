"""Vera's scoped OpenBao token may read and write only secret/vera/* (and the
vera-secrets mount), so every certificate record pki.cert.issue stores and
pki.cert.list reads must live under vera/pki/. A bare pki/ path is what broke
pki.cert.list with HTTP 403 once the root token left Vera (13 Sep 2026)."""
import ast
import os
import re

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
SRC = os.path.join(ROOT, "vera", "provisioning", "provisioning_capabilities.py")
POLICY = os.path.join(ROOT, "vera", "security", "secret_service_core.py")


def test_certificate_records_use_the_vera_prefix_only():
    text = open(SRC, encoding="utf-8").read()
    tree = ast.parse(text)
    prefix = next(n.value.value for n in tree.body if isinstance(n, ast.Assign)
                  and any(getattr(t, "id", "") == "PKI_KV_PREFIX" for t in n.targets))
    assert prefix == "vera/pki"
    kv_paths = re.findall(r"cap_kv_(?:put|get|list)\(path=([^,)]+)", text)
    assert kv_paths, "pki.cert.* no longer stores through secstore.kv"
    for p in kv_paths:
        assert p.startswith("PKI_KV_PREFIX") or p.startswith('f"{PKI_KV_PREFIX}'), p
    assert 'path="pki"' not in text and 'f"pki/' not in text


def test_the_prefix_is_inside_the_scoped_policy():
    from vera.security import secret_service_core as core
    hcl = core.policy_hcl()
    assert 'secret/data/vera/*' in hcl and 'secret/metadata/vera/*' in hcl
