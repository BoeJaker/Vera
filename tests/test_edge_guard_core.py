"""The edge guard's rules (vera/edge_guard_core.py): browser requests from
another origin are refused; Vera's own pages and non-browser callers pass.

These call decide() with the headers real clients send; nothing here reads
the source.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera import edge_guard_core as g  # noqa: E402

HOSTS = {"llm.int", "192.168.0.138", "localhost", "127.0.0.1", "::1", "host.docker.internal"}


def d(method="POST", headers=None, kind="http", path="/mcp/call", origins=(), proxy=False, port=8999):
    h = {"host": "192.168.0.138:8999"}
    h.update(headers or {})
    return g.decide(kind=kind, method=method, path=path, headers=h, allowed_hosts=HOSTS,
                    allowed_origins=origins, trusted_proxy=proxy, default_port=port)


# ── refused: a page on another site ──────────────────────────────────────────
def test_foreign_origin_post_refused():
    v = d(headers={"origin": "https://example.org", "sec-fetch-site": "cross-site"})
    assert not v.allow and v.rule == "origin"


def test_no_origin_but_cross_site_fetch_refused():
    # a no-cors text/plain POST from a foreign page: the CSRF shape
    v = d(headers={"sec-fetch-site": "cross-site"})
    assert not v.allow and v.rule == "fetch"


def test_same_site_other_port_refused():
    # a sandbox preview on another port of the same host is same-site but not same-origin
    v = d(headers={"origin": "https://192.168.0.138:8981", "sec-fetch-site": "same-site"})
    assert not v.allow and v.rule == "origin"
    v = d(headers={"sec-fetch-site": "same-site"})
    assert not v.allow and v.rule == "fetch"


def test_null_origin_refused():
    v = d(headers={"origin": "null"})
    assert not v.allow and v.rule == "origin"


def test_foreign_websocket_refused():
    v = d(kind="websocket", method="GET", path="/ws/mcp",
          headers={"origin": "https://evil.example"})
    assert not v.allow and v.rule == "origin"


def test_cross_site_api_read_refused_but_navigation_allowed():
    v = d(method="GET", path="/certs/list", headers={"sec-fetch-site": "cross-site", "sec-fetch-mode": "cors"})
    assert not v.allow and v.rule == "read"
    v = d(method="GET", path="/chat_panel", headers={"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"})
    assert v.allow


def test_cross_site_static_asset_allowed():
    v = d(method="GET", path="/ui/bootstrap.css", headers={"sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors"})
    assert v.allow


def test_foreign_host_refused_as_host_rule():
    v = d(headers={"host": "attacker.example:8999", "origin": "https://attacker.example:8999"})
    assert not v.allow and v.rule == "host"


# ── allowed: Vera's own pages and non-browser callers ────────────────────────
def test_same_origin_post_allowed_by_ip_and_by_name():
    assert d(headers={"origin": "https://192.168.0.138:8999", "sec-fetch-site": "same-origin"}).allow
    assert d(headers={"host": "llm.int:8999", "origin": "https://llm.int:8999"}).allow


def test_same_origin_websocket_allowed():
    assert d(kind="websocket", method="GET", path="/ws/mcp",
             headers={"origin": "https://192.168.0.138:8999"}).allow


def test_non_browser_callers_allowed():
    # MCP bridge, n8n, census tools, sandboxes: no Origin, no Sec-Fetch-*
    assert d(headers={}).allow
    assert d(headers={"host": "host.docker.internal:8999", "x-vera-read-through": "1"}).allow
    assert d(kind="websocket", method="GET", path="/ws/mcp", headers={}).allow


def test_default_port_host_header_matches_default_port_origin():
    # through a front on 443: Host carries no port, Origin is https://name
    assert d(headers={"host": "llm.int", "origin": "https://llm.int"}).allow


def test_listed_extra_origin_allowed():
    v = d(headers={"origin": "https://portal.example.net", "sec-fetch-site": "cross-site"},
          origins=["https://portal.example.net"])
    assert v.allow


def test_forwarded_host_only_from_trusted_proxy():
    hdr = {"host": "127.0.0.1:8999", "x-forwarded-host": "vera.example.net",
           "origin": "https://vera.example.net"}
    hosts = HOSTS | {"vera.example.net"}
    ok = g.decide(kind="http", method="POST", path="/mcp/call", headers=hdr,
                  allowed_hosts=hosts, trusted_proxy=True, default_port=8999)
    assert ok.allow
    not_trusted = g.decide(kind="http", method="POST", path="/mcp/call", headers=hdr,
                           allowed_hosts=hosts, trusted_proxy=False, default_port=8999)
    assert not not_trusted.allow and not_trusted.rule == "origin"


def test_own_host_other_port_read_allowed_but_write_refused():
    # the Loop Lab panel served by a sandbox on :8998 reads prod's /ci/branch (observed 17 Oct)
    hdr = {"host": "llm.int:8999", "origin": "https://llm.int:8998",
           "sec-fetch-site": "same-site", "sec-fetch-mode": "cors"}
    assert d(method="GET", path="/ci/branch", headers=hdr).allow
    w = d(method="POST", path="/mcp/call", headers=hdr)
    assert not w.allow and w.rule == "origin"


def test_foreign_host_read_still_refused():
    hdr = {"origin": "https://evil.example", "sec-fetch-site": "cross-site", "sec-fetch-mode": "cors"}
    v = d(method="GET", path="/ci/branch", headers=hdr)
    assert not v.allow and v.rule == "read"


def test_plain_get_without_fetch_metadata_allowed():
    assert d(method="GET", path="/health", headers={}).allow
