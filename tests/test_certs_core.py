"""One certificate list's rules: dates in the formats FreeIPA, openssl and ISO
use, issuer names, states by days left, only the newest FreeIPA certificate per
name counts, and findings for expired, expiring, unreachable, self-signed and
missing certificates."""
import calendar
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.security import certs_core as core  # noqa: E402

pytestmark = pytest.mark.critical

NOW = float(calendar.timegm(time.strptime("2026-09-13 12:00:00", "%Y-%m-%d %H:%M:%S")))
DAY = 86400


def test_dates_parse_from_every_format_seen():
    assert core.parse_when("Thu Aug 03 09:28:17 2028 UTC") == calendar.timegm((2028, 8, 3, 9, 28, 17, 0, 0, 0))
    assert core.parse_when("Jun 23 10:00:00 2027 GMT") == calendar.timegm((2027, 6, 23, 10, 0, 0, 0, 0, 0))
    assert core.parse_when("2027-06-23T10:00:00Z") == calendar.timegm((2027, 6, 23, 10, 0, 0, 0, 0, 0))
    assert core.parse_when("20270623100000Z") == calendar.timegm((2027, 6, 23, 10, 0, 0, 0, 0, 0))
    assert core.parse_when(123.0) == 123.0 and core.parse_when("") is None and core.parse_when("soon") is None


def test_issuers_are_named():
    assert core.issuer_kind("CN=R11,O=Let's Encrypt,C=US") == "Let's Encrypt"
    assert core.issuer_kind("CN=(STAGING) Riddling Rhubarb R12,O=(STAGING) Let's Encrypt,C=US") == "Let's Encrypt (staging)"
    assert core.issuer_kind("CN=Certificate Authority,O=VERA.INT") == "FreeIPA"
    assert core.issuer_kind("O=PVE Cluster Manager CA,CN=Proxmox Virtual Environment") == "Proxmox"
    assert core.issuer_kind("CN=vera-orchestrator", "CN=vera-orchestrator", True) == "self-signed"
    assert core.issuer_kind("CN=Smallstep Intermediate CA") == "step-ca"
    assert core.issuer_kind("CN=Some CA") == "other"


def test_states_follow_days_left():
    assert [core.state_of(d) for d in (-1, 0, 14, 15, 30, 31, None)] == \
        ["expired", "expiring", "expiring", "renew soon", "renew soon", "ok", "unknown"]
    assert core.state_of(100, revoked=True) == "revoked"


def test_only_the_newest_freeipa_certificate_per_name_counts():
    rows = core.freeipa_rows([
        {"subject": "CN=dc.vera.int,O=VERA.INT", "valid_not_after": "Thu Aug 03 09:28:17 2028 UTC", "serial_number": 9},
        {"subject": "CN=dc.vera.int,O=VERA.INT", "valid_not_after": "Mon Aug 03 09:28:17 2026 UTC", "serial_number": 2},
        {"subject": "CN=old.vera.int,O=VERA.INT", "valid_not_after": "Mon Jan 01 00:00:00 2029 UTC", "revoked": True},
    ], NOW)
    states = {(r["name"], r["serial"]): r["state"] for r in rows}
    assert states == {("dc.vera.int", "9"): "ok", ("dc.vera.int", "2"): "superseded", ("old.vera.int", ""): "revoked"}


def probe(name, days, issuer="CN=Certificate Authority,O=VERA.INT", subject="CN=x", **kw):
    return dict({"name": name, "host": "192.168.0.9", "port": 443, "subject": subject, "issuer": issuer,
                 "names": [name], "not_after": NOW + days * DAY + 60}, **kw)


def test_summary_findings_and_soonest():
    out = core.summarize(
        [probe("Proxmox", 283, issuer="O=PVE Cluster Manager CA"), probe("Gitea", 5),
         probe("old-app", -3), probe("Vera", 3600, issuer="CN=vera", subject="CN=vera", self_signed=True),
         {"name": "PBS", "host": "192.168.0.170", "port": 8007, "error": "timeout"}],
        [], [{"fqdn": "app.vera.int", "issued_at": "2026-09-01"}],
        {"domain": "*.boejaker.duckdns.org", "wildcard": None}, {"FreeIPA": "login HTTP 401"}, NOW)
    msgs = [f["message"] for f in out["findings"]]
    assert out["findings"][0]["severity"] == "error" and msgs[0].startswith("The certificate for old-app")
    assert "The certificate for Gitea (192.168.0.9:443) expires in 5 day(s)." in msgs
    assert any(m.startswith("Could not read the certificate from 1 service(s): PBS") for m in msgs)
    assert any(m.startswith("1 service(s) present a self-signed certificate") for m in msgs)
    assert "No Let's Encrypt certificate has been issued for *.boejaker.duckdns.org yet." in msgs
    assert "Could not read certificates from FreeIPA." in msgs
    assert out["soonest"]["name"] == "old-app"
    assert out["counts"]["recorded"] == 1 and out["counts"]["not issued"] == 1
    assert [r["name"] for r in out["certs"]][:3] == ["old-app", "Gitea", "Proxmox"]


def test_a_trusted_wildcard_counts_like_any_certificate():
    row = core.letsencrypt_row({"domain": "*.x.duckdns.org", "wildcard": {
        "names": ["*.x.duckdns.org"], "issuer": "C=US, O=Let's Encrypt, CN=R11", "not_after": "Dec 12 00:00:00 2026 GMT",
        "days_left": 89, "trusted": True}}, NOW)
    assert (row["issuer_kind"], row["state"], row["days_left"]) == ("Let's Encrypt", "ok", 89)
    assert core.letsencrypt_row(None, NOW) is None
