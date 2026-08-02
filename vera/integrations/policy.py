"""
policy.py — pure, dependency-free logic for the Integrations Hub
===============================================================

Kept free of Redis / the orchestrator / secrets so it can be unit-tested in
isolation (mirrors ``vera.operator.safety``). Holds:

  • KIND_SPECS               — labels + API-auth conventions per service kind
  • guess_kind()             — best-effort kind from port / image / label
  • base_url()               — resolve an integration record to a base URL
  • require_access()         — THE enforced access gate (embed/interact/api/mcp/ssh)
  • auth_header()            — map an auth scheme + token to HTTP header(s)
"""
from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

ACCESS_MODES = ("embed", "interact", "api", "mcp", "ssh")

# Auto-discovered integrations start locked: view-only, no active control.
DEFAULT_ACCESS: Dict[str, bool] = {"embed": True, "interact": False, "api": False,
                                   "mcp": False, "ssh": False}

# Modes a `sensitive` flag forces off regardless of the individual toggle.
SENSITIVE_LOCKED = ("interact", "api", "mcp")

# Labels + API-auth conventions for well-known services. 'generic' covers the rest.
KIND_SPECS: Dict[str, Dict[str, Any]] = {
    "n8n":           {"icon": "🔗", "label": "n8n", "ports": [5678],
                      "api_base": "/api/v1", "auth_scheme": "header",
                      "auth_header": "X-N8N-API-KEY"},
    "homeassistant": {"icon": "🏠", "label": "Home Assistant", "ports": [8123],
                      "api_base": "/api", "auth_scheme": "bearer"},
    "gitea":         {"icon": "🍵", "label": "Gitea", "ports": [3000],
                      "api_base": "/api/v1", "auth_scheme": "token"},
    "github":        {"icon": "🐙", "label": "GitHub", "ports": [443],
                      "api_base": "", "auth_scheme": "token",
                      "default_base": "https://api.github.com"},
    "grafana":       {"icon": "📊", "label": "Grafana", "ports": [3000, 3001],
                      "api_base": "/api", "auth_scheme": "bearer"},
    "wordpress":     {"icon": "📝", "label": "WordPress", "ports": [80, 443],
                      "api_base": "/wp-json", "auth_scheme": "basic"},
    "portainer":     {"icon": "🐳", "label": "Portainer", "ports": [9000, 9443],
                      "api_base": "/api", "auth_scheme": "bearer"},
    "prometheus":    {"icon": "🔥", "label": "Prometheus", "ports": [9090],
                      "api_base": "/api/v1", "auth_scheme": "none"},
    "generic":       {"icon": "🌐", "label": "Service", "ports": [],
                      "api_base": "", "auth_scheme": "bearer"},
}

_PORT_KIND = {5678: "n8n", 8123: "homeassistant", 3000: "gitea", 3001: "grafana",
              9090: "prometheus", 9000: "portainer", 9443: "portainer"}

_IMG_KIND: List[tuple] = [("n8n", "n8n"), ("home-assistant", "homeassistant"),
                          ("homeassistant", "homeassistant"), ("gitea", "gitea"),
                          ("grafana", "grafana"), ("wordpress", "wordpress"),
                          ("portainer", "portainer"), ("prometheus", "prometheus")]


def guess_kind(port: int = 0, image: str = "", label: str = "") -> str:
    """Best-effort service kind. Image/label signal wins over port."""
    hay = f"{image} {label}".lower()
    for needle, kind in _IMG_KIND:
        if needle in hay:
            return kind
    return _PORT_KIND.get(int(port or 0), "generic")


def base_url(rec: Dict) -> str:
    """Resolve an integration record to a base URL (no trailing slash)."""
    if rec.get("base_url"):
        return rec["base_url"].rstrip("/")
    spec = KIND_SPECS.get(rec.get("kind", "generic"), {})
    if spec.get("default_base") and not rec.get("host"):
        return spec["default_base"].rstrip("/")
    host = rec.get("host", "")
    if not host:
        return ""
    scheme = rec.get("scheme") or "http"
    port = rec.get("port")
    return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"


def require_access(rec: Optional[Dict], mode: str) -> Optional[Dict]:
    """THE access gate. Return None if `mode` is permitted for this integration,
    else a 403/404/400 error dict. `sensitive` hard-locks interact/api/mcp."""
    if not rec:
        return {"error": "integration not found", "code": 404}
    if mode not in ACCESS_MODES:
        return {"error": f"unknown access mode: {mode}", "code": 400}
    if rec.get("sensitive") and mode in SENSITIVE_LOCKED:
        return {"error": f"'{mode}' is locked — integration "
                         f"'{rec.get('label')}' is marked sensitive",
                "code": 403, "integration": rec.get("id")}
    if not (rec.get("access") or {}).get(mode):
        return {"error": f"'{mode}' access is disabled for integration "
                         f"'{rec.get('label')}'. Enable it in the Integrations Hub "
                         f"(integration.access.set).",
                "code": 403, "integration": rec.get("id")}
    return None


def auth_header(scheme: str, token: str, header_name: str = "") -> Dict[str, str]:
    """Map an auth scheme + token to the HTTP header(s) to inject (pure)."""
    if not token or scheme in ("none", ""):
        return {}
    if scheme == "bearer":
        return {"Authorization": f"Bearer {token}"}
    if scheme == "token":                         # gitea / github
        return {"Authorization": f"token {token}"}
    if scheme == "header":                         # n8n X-N8N-API-KEY etc.
        return {header_name or "X-API-KEY": token}
    if scheme == "basic":                          # token stored as "user:pass"
        return {"Authorization": "Basic " + base64.b64encode(token.encode()).decode()}
    return {}
