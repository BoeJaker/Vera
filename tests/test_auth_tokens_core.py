"""Caller tokens (pure): format, digest-only records, verification, headers, peer classes; and the bridge sends it."""
import importlib.util
import os
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vera import auth_tokens_core as tc  # noqa: E402

NOW = 1_800_000_000.0


def fresh(**kw):
    token, tid, secret = tc.mint()
    rec = tc.record(tid, secret, kind=kw.pop("kind", "mcp_bridge"), subject=kw.pop("subject", "claude@lenovo"),
                    now=NOW, **kw)
    return token, rec


def test_mint_shape_and_parse():
    token, tid, secret = tc.mint()
    assert token == f"vat_{tid}.{secret}" and len(tid) == 12 and len(secret) == 43
    assert tc.parse(token) == (tid, secret)
    for bad in ("", "vat_", "vat_xyz.abc", "Bearer vat_x", "vat_" + tid, f"vat_{tid}.{secret}x",
                f"vat_{tid.upper()}.{secret}", "sk_" + secret):
        assert tc.parse(bad) is None, bad
    assert tc.mint()[0] != token


def test_the_record_keeps_only_a_digest():
    token, rec = fresh(scopes=["ide.", " fabric. ", ""])
    secret = tc.parse(token)[1]
    assert secret not in str(rec) and rec["digest"] == tc.digest(secret)
    assert rec["scopes"] == ["ide.", "fabric."] and rec["expires"] == NOW + 90 * 86400
    assert "digest" not in tc.public(rec) and tc.public(rec)["subject"] == "claude@lenovo"


def test_record_validation():
    _, tid, secret = tc.mint()
    with pytest.raises(ValueError):
        tc.record(tid, secret, kind="root", subject="x", now=NOW)
    with pytest.raises(ValueError):
        tc.record(tid, secret, kind="tool", subject="  <>  ", now=NOW)
    r = tc.record(tid, secret, kind="tool", subject="census<script>", now=NOW, ttl_days=10_000)
    assert r["subject"] == "censusscript" and r["expires"] == NOW + 365 * 86400


def test_verify():
    token, rec = fresh()
    assert tc.verify(token, rec, NOW + 1) == (True, "")
    other, _ = fresh()
    assert tc.verify(other, rec, NOW) == (False, "wrong secret")
    assert tc.verify(token, None, NOW) == (False, "unknown")
    assert tc.verify("vat_nope", rec, NOW) == (False, "malformed")
    assert tc.verify(token, dict(rec, revoked=True), NOW) == (False, "revoked")
    assert tc.verify(token, rec, rec["expires"] + 1) == (False, "expired")
    tid, secret = tc.parse(token)
    forged = f"vat_{tid}.{'A' * 43}"
    assert tc.verify(forged, rec, NOW) == (False, "wrong secret")


def test_from_headers():
    token, _ = fresh()
    assert tc.from_headers({"authorization": "Bearer " + token}) == token
    assert tc.from_headers({"authorization": "bearer   " + token}) == token
    assert tc.from_headers({"x-vera-token": token}) == token
    assert tc.from_headers({"authorization": "Basic abc"}) == ""
    assert tc.from_headers({"authorization": "Bearer other-token"}) == ""
    assert tc.from_headers({}) == ""


@pytest.mark.parametrize("peer, cls", [("127.0.0.1", "loopback"), ("::1", "loopback"), ("172.18.0.5", "docker"),
                                       ("172.40.1.1", "other"), ("172.x", "other"), ("192.168.0.190", "lan"),
                                       ("10.66.66.2", "lan"), ("8.8.8.8", "other"), ("", "other")])
def test_peer_class(peer, cls):
    assert tc.peer_class(peer) == cls


def test_evidence_key():
    assert tc.evidence_key(has_origin=False, peer="192.168.0.9", result="none") == "program|lan|none"
    assert tc.evidence_key(has_origin=True, peer="127.0.0.1", result="valid") == "browser|loopback|valid"


def test_the_bridge_sends_the_token_and_never_without_one():
    path = os.path.join(ROOT, "vera", "ide", "vera_mcp_bridge.py")
    spec = importlib.util.spec_from_file_location("_bridge_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with_token = mod.Vera("https://h:8999", "", token="vat_0123456789ab." + "A" * 43)
    assert with_token._headers({"Content-Type": "application/json"}) == {
        "Content-Type": "application/json", "Authorization": "Bearer vat_0123456789ab." + "A" * 43}
    assert "Authorization" not in mod.Vera("https://h:8999", "")._headers()
