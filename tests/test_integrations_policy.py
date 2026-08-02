"""Integrations Hub — access-gate + resolution logic (pure, no Redis/orchestrator).

Mirrors tests/test_operator_safety.py: exercises vera.integrations.policy directly.
The gate here is security-critical, so the matrix is explicit.
"""
from vera.integrations import policy as P


def _rec(**over):
    rec = {"id": "abc", "label": "svc", "kind": "generic", "host": "10.0.0.5",
           "port": 8080, "scheme": "http",
           "access": dict(P.DEFAULT_ACCESS), "sensitive": False}
    rec.update(over)
    return rec


# ── require_access: the enforced gate ─────────────────────────────────────────
def test_default_access_is_embed_only():
    r = _rec()
    assert P.require_access(r, "embed") is None                 # allowed
    for mode in ("interact", "api", "mcp", "ssh"):
        g = P.require_access(r, mode)
        assert g and g["code"] == 403                           # denied by default


def test_enabling_a_mode_allows_it():
    r = _rec(access={**P.DEFAULT_ACCESS, "api": True})
    assert P.require_access(r, "api") is None
    assert P.require_access(r, "mcp")["code"] == 403            # others still denied


def test_sensitive_hard_locks_active_modes_even_if_toggled_on():
    r = _rec(access={m: True for m in P.ACCESS_MODES}, sensitive=True)
    for mode in ("interact", "api", "mcp"):
        g = P.require_access(r, mode)
        assert g and g["code"] == 403 and "sensitive" in g["error"]
    # embed + ssh are not hard-locked by `sensitive`
    assert P.require_access(r, "embed") is None
    assert P.require_access(r, "ssh") is None


def test_missing_record_is_404_and_unknown_mode_400():
    assert P.require_access(None, "embed")["code"] == 404
    assert P.require_access(_rec(), "telepathy")["code"] == 400


def test_gate_error_names_the_integration():
    g = P.require_access(_rec(label="Grafana"), "interact")
    assert "Grafana" in g["error"] and g["integration"] == "abc"


# ── guess_kind ────────────────────────────────────────────────────────────────
def test_guess_kind_prefers_image_over_port():
    assert P.guess_kind(port=3000, image="gitea/gitea:latest") == "gitea"
    assert P.guess_kind(port=3000, image="grafana/grafana", label="graf") == "grafana"
    assert P.guess_kind(port=5678) == "n8n"
    assert P.guess_kind(port=8123) == "homeassistant"
    assert P.guess_kind(port=12345) == "generic"


# ── base_url ──────────────────────────────────────────────────────────────────
def test_base_url_from_host_port_and_override():
    assert P.base_url(_rec()) == "http://10.0.0.5:8080"
    assert P.base_url(_rec(scheme="https", port=443)) == "https://10.0.0.5:443"
    assert P.base_url(_rec(base_url="https://x.example/")) == "https://x.example"
    # github: no host → falls back to the kind's default_base
    assert P.base_url({"kind": "github"}) == "https://api.github.com"
    assert P.base_url({"kind": "generic"}) == ""


# ── auth_header ───────────────────────────────────────────────────────────────
def test_auth_header_schemes():
    assert P.auth_header("bearer", "T") == {"Authorization": "Bearer T"}
    assert P.auth_header("token", "T") == {"Authorization": "token T"}
    assert P.auth_header("header", "K", "X-N8N-API-KEY") == {"X-N8N-API-KEY": "K"}
    assert P.auth_header("basic", "u:p")["Authorization"].startswith("Basic ")
    assert P.auth_header("none", "T") == {}
    assert P.auth_header("bearer", "") == {}                    # no token → nothing
