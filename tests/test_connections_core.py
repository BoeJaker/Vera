"""API connections (data-connections plan): templates, auth parsing and request building.

The weight is on the credential: it must go only where the connection's auth says, only to the connection's own host,
and never show in a URL we display.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.platforms import connections_core as cc  # noqa: E402
from vera.platforms import platform_core as pc      # noqa: E402


def test_api_kind_is_a_platform_kind_with_a_secret_credential():
    assert "api" in pc.PLATFORM_SPECS
    assert pc.secret_field_keys("api") == ["credential"]
    p = pc.new_platform("api", "guardian", "Guardian")
    assert p["fields"]["auth"] == "none"


def test_templates_are_well_formed_and_unique():
    ids = [t["id"] for t in cc.TEMPLATES]
    assert len(ids) == len(set(ids))
    for t in cc.TEMPLATES:
        assert t["base_url"].startswith("https://")
        assert cc.parse_auth(t["auth"])["kind"] in cc.AUTH_KINDS
        # a keyed template names its key; a keyless one has none
        assert bool(t["key"]) == (cc.parse_auth(t["auth"])["kind"] != "none"), t["id"]
        assert pc.valid_key(t["key"]) or not t["key"]


def test_fields_from_template_reference_the_key_never_hold_it():
    f = cc.fields_from_template(cc.template("guardian"))
    assert f["credential"] == "@secret:guardian_api_key"
    assert f["auth"] == "query:api-key"
    assert cc.fields_from_template(cc.template("hackernews"))["credential"] == ""
    assert json.loads(cc.fields_from_template(cc.template("github"))["headers"])["Accept"]


def test_parse_auth():
    assert cc.parse_auth("header:X-Subscription-Token") == {"kind": "header", "name": "X-Subscription-Token"}
    assert cc.parse_auth("Bearer") == {"kind": "bearer", "name": ""}
    assert cc.parse_auth("header:")["kind"] == "none"
    assert cc.parse_auth("cookie:x")["kind"] == "none"
    assert cc.parse_auth(None)["kind"] == "none"


def _f(**kw):
    base = {"base_url": "https://api.example.com/v1", "auth": "none", "credential": "", "headers": ""}
    base.update(kw)
    return base


def test_build_request_places_the_credential_by_auth_kind():
    r = cc.build_request(_f(auth="bearer", credential="tok"), "/items")
    assert r["url"] == "https://api.example.com/v1/items" and r["headers"]["Authorization"] == "Bearer tok"
    r = cc.build_request(_f(auth="header:X-Key", credential="k"), "items")
    assert r["headers"] == {"X-Key": "k"} and r["auth"] is None
    r = cc.build_request(_f(auth="query:api-key", credential="k"), "/search?q=a b")
    assert "api-key=k" in r["url"] and "q=a" in r["url"]
    r = cc.build_request(_f(auth="basic", credential="k"), "/x")
    assert r["auth"] == ("k", "")
    r = cc.build_request(_f(auth="basic", credential="k", username="me@x.org"), "/x")
    assert r["auth"] == ("me@x.org", "k")


def test_build_request_keeps_extra_headers_and_params():
    r = cc.build_request(_f(headers='{"Accept": "application/json", "Empty": ""}'), "/x?a=1", {"b": 2})
    assert r["headers"] == {"Accept": "application/json"}
    assert "a=1" in r["url"] and "b=2" in r["url"]
    assert cc.build_request(_f(headers="not json"), "/x")["headers"] == {}


def test_credential_never_leaves_the_connections_host():
    f = _f(auth="bearer", credential="tok")
    assert "error" in cc.build_request(f, "https://evil.example.net/v1/items")
    assert "error" in cc.build_request(f, "https://api.example.com.evil.net/v1")
    assert "error" in cc.build_request(f, "https://api.example.com/other")          # same host, outside base
    assert "error" in cc.build_request(f, "https://api.example.com/v1x/items")      # a sibling of the base path
    assert cc.build_request(f, "https://api.example.com/v1/items?x=1")["url"].endswith("/v1/items?x=1")


def test_build_request_refuses_what_it_cannot_do():
    assert "error" in cc.build_request(_f(base_url="ftp://x"), "/")
    assert "error" in cc.build_request(_f(base_url=""), "/")
    assert "error" in cc.build_request(_f(auth="bearer", credential=""), "/")       # key not set yet


def test_redact_and_describe_hide_the_credential():
    f = _f(auth="query:api-key", credential="SECRET")
    url = cc.build_request(f, "/search?q=x")["url"]
    shown = cc.redact_url(url, f)
    assert "SECRET" not in shown and "api-key=%2A%2A%2A" in shown
    assert "SECRET" not in cc.describe(f)
    assert cc.describe(_f()) == "no key"


def test_date_tokens_make_a_moving_window():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)
    p = "/daily/{days_ago:120:%Y%m%d}/{today:%Y%m%d}?x={notatoken}"
    assert cc.expand_path(p, now) == "/daily/20260613/20261011?x={notatoken}"
    assert cc.expand_path("/plain", now) == "/plain"
    r = cc.build_request(_f(), cc.expand_path("/a/{today:%Y}", now))
    assert r["url"].endswith("/a/2026")


def test_keyless_templates_identify_themselves_where_policy_asks():
    for tid in ("wikidata", "nominatim", "wikimedia"):
        f = cc.fields_from_template(cc.template(tid))
        assert "Vera" in json.loads(f["headers"])["User-Agent"] and f["rate_per_min"], tid
    for tid in ("polymarket_clob", "fred", "blockchain_info"):
        assert cc.template(tid)["auth"] == "none"
