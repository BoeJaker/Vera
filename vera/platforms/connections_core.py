"""API connections: the templates for known APIs and how a request is built from a connection. Pure: no I/O.

data-connections plan (shared-planning/data-connections/PLAN.md), owner 2026-10-11: every API Vera talks to is a
Platforms connection - its base URL, how it authenticates, and its credential as an "@secret:<key>" reference into
platform.secrets - and every data source is a fabric source that reaches its API through a connection.

A connection is a Platforms target of kind "api" (platform_core.PLATFORM_SPECS["api"]); its fields, once references
are resolved, go through build_request. The credential is only ever sent to the connection's own host.
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

AUTH_KINDS = ("none", "bearer", "header", "query", "basic")
UA = {"User-Agent": "Vera intelligence/1.0 (https://github.com/BoeJaker/Vera)"}

# Known APIs. key: the platform.secrets record its credential lives in ("" = keyless). sample_path: something cheap
# to test and map. signup: where to get a key.
TEMPLATES: List[Dict[str, Any]] = [
    {"id": "brave", "label": "Brave Search API", "base_url": "https://api.search.brave.com/res/v1",
     "auth": "header:X-Subscription-Token", "key": "brave_search_api_key", "sample_path": "/web/search?q=uk+house+prices",
     "docs": "https://api-dashboard.search.brave.com/app/documentation", "signup": "https://brave.com/search/api/",
     "tags": ["search"]},
    {"id": "github", "label": "GitHub", "base_url": "https://api.github.com", "auth": "bearer", "key": "github_token",
     "sample_path": "/search/repositories?q=model+context+protocol&per_page=5",
     "headers": {"Accept": "application/vnd.github+json"}, "docs": "https://docs.github.com/rest",
     "signup": "https://github.com/settings/personal-access-tokens", "tags": ["code"]},
    {"id": "guardian", "label": "Guardian Open Platform", "base_url": "https://content.guardianapis.com",
     "auth": "query:api-key", "key": "guardian_api_key", "sample_path": "/search?q=house%20prices&order-by=newest",
     "docs": "https://open-platform.theguardian.com/documentation/", "signup": "https://open-platform.theguardian.com/access/",
     "tags": ["news"]},
    {"id": "companies_house", "label": "Companies House", "base_url": "https://api.company-information.service.gov.uk",
     "auth": "basic", "key": "companies_house_api_key", "sample_path": "/search/companies?q=savills",
     "docs": "https://developer-specs.company-information.service.gov.uk/",
     "signup": "https://developer.company-information.service.gov.uk/", "tags": ["companies"]},
    {"id": "epc", "label": "EPC Open Data", "base_url": "https://epc.opendatacommunities.org/api/v1",
     "auth": "basic", "key": "epc_api_key", "sample_path": "/domestic/search?postcode=CB1&size=5",
     "headers": {"Accept": "application/json"}, "username_hint": "the email you registered with",
     "docs": "https://epc.opendatacommunities.org/docs/api", "signup": "https://epc.opendatacommunities.org/",
     "tags": ["property"]},
    {"id": "hackernews", "label": "Hacker News (Algolia)", "base_url": "https://hn.algolia.com/api/v1", "auth": "none",
     "key": "", "sample_path": "/search?query=llm&tags=story", "docs": "https://hn.algolia.com/api", "tags": ["news"]},
    {"id": "polymarket", "label": "Polymarket", "base_url": "https://gamma-api.polymarket.com", "auth": "none", "key": "",
     "sample_path": "/markets?limit=5&active=true", "docs": "https://docs.polymarket.com/", "tags": ["markets"]},
    {"id": "polymarket_clob", "label": "Polymarket prices (CLOB)", "base_url": "https://clob.polymarket.com",
     "auth": "none", "key": "", "sample_path": "/markets?limit=3", "docs": "https://docs.polymarket.com/",
     "tags": ["markets"]},
    {"id": "manifold", "label": "Manifold Markets", "base_url": "https://api.manifold.markets/v0", "auth": "none",
     "key": "", "sample_path": "/search-markets?term=ai&limit=5", "docs": "https://docs.manifold.markets/api",
     "tags": ["markets"]},
    {"id": "wikidata", "label": "Wikidata", "base_url": "https://www.wikidata.org", "auth": "none", "key": "",
     "sample_path": "/w/api.php?action=wbsearchentities&search=Savills&language=en&format=json",
     "headers": UA, "rate_per_min": 40, "docs": "https://www.wikidata.org/wiki/Wikidata:Data_access",
     "tags": ["reference"]},
    {"id": "wikimedia", "label": "Wikimedia (page views)", "base_url": "https://wikimedia.org/api/rest_v1",
     "auth": "none", "key": "", "headers": UA, "rate_per_min": 100,
     "sample_path": "/metrics/pageviews/per-article/en.wikipedia/all-access/user/Bitcoin/daily/{days_ago:30:%Y%m%d}/{today:%Y%m%d}",
     "docs": "https://wikimedia.org/api/rest_v1/", "tags": ["attention"]},
    {"id": "fred", "label": "FRED (St. Louis Fed, public CSV)", "base_url": "https://fred.stlouisfed.org",
     "auth": "none", "key": "", "sample_path": "/graph/fredgraph.csv?id=UNRATE",
     "docs": "https://fred.stlouisfed.org/docs/api/fred/", "tags": ["macro"]},
    {"id": "blockchain_info", "label": "Blockchain.com charts", "base_url": "https://api.blockchain.info",
     "auth": "none", "key": "", "sample_path": "/charts/hash-rate?timespan=30days&format=json",
     "docs": "https://www.blockchain.com/explorer/api/charts_api", "tags": ["macro", "crypto"]},
    {"id": "nominatim", "label": "Nominatim (OpenStreetMap)", "base_url": "https://nominatim.openstreetmap.org",
     "auth": "none", "key": "", "sample_path": "/search?q=Cambridge&format=jsonv2&limit=3",
     "headers": UA, "rate_per_min": 60,                       # the usage policy: one request a second, say who you are
     "docs": "https://nominatim.org/release-docs/latest/api/Overview/", "tags": ["places"]},
]


def template(tid: str) -> Optional[Dict[str, Any]]:
    return next((t for t in TEMPLATES if t["id"] == str(tid or "").strip().lower()), None)


def fields_from_template(t: Dict[str, Any]) -> Dict[str, str]:
    """A connection's fields from a template; the credential is a reference to the template's key."""
    return {"base_url": t["base_url"], "auth": t.get("auth", "none"),
            "credential": ("@secret:" + t["key"]) if t.get("key") else "",
            "username": "", "headers": json.dumps(t["headers"]) if t.get("headers") else "",
            "sample_path": t.get("sample_path", ""), "rate_per_min": str(t.get("rate_per_min") or ""),
            "docs": t.get("docs", "")}


_DATE_TOKEN = re.compile(r"\{(today|days_ago:(\d{1,4})):([^{}]{1,20})\}")


def expand_path(path: str, now: Optional[datetime] = None) -> str:
    """Date tokens in a path, so a stored source can ask for a moving window: {today:%Y%m%d} and
    {days_ago:120:%Y%m%d} (UTC). Anything else is left as it is."""
    now = now or datetime.now(timezone.utc)

    def sub(m):
        when = now - timedelta(days=int(m.group(2))) if m.group(2) else now
        return when.strftime(m.group(3))
    return _DATE_TOKEN.sub(sub, str(path or ""))


def parse_auth(auth: Any) -> Dict[str, str]:
    """"header:X-Key" -> {"kind": "header", "name": "X-Key"}; unknown -> none."""
    a = str(auth or "none").strip()
    kind, _, name = a.partition(":")
    kind = kind.strip().lower()
    if kind not in AUTH_KINDS:
        return {"kind": "none", "name": ""}
    if kind in ("header", "query") and not name.strip():
        return {"kind": "none", "name": ""}
    return {"kind": kind, "name": name.strip()}


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def build_request(fields: Dict[str, Any], path: str = "", params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """{url, headers, params, auth (user, pass) | None} for GETting `path` through a connection whose fields are
    already resolved (the credential is its value). A full URL is accepted only under the connection's base URL - the
    credential never goes to another host. {error} when the connection cannot make the request."""
    base = str(fields.get("base_url") or "").strip().rstrip("/")
    sp = urlsplit(base) if base else None
    if not sp or sp.scheme not in ("http", "https") or not sp.hostname:
        return {"error": "the connection has no valid base URL"}
    p = str(path or "").strip()
    if p.lower().startswith(("http://", "https://")):
        under = p == base or p.startswith((base + "/", base + "?"))
        if _host(p) != sp.hostname.lower() or not under:
            return {"error": "a full URL must be under the connection's base URL (the credential stays on its host)"}
        url = p
    else:
        url = base + ("/" + p.lstrip("/") if p else "")
    try:
        extra = json.loads(fields.get("headers") or "{}") if isinstance(fields.get("headers"), str) else (fields.get("headers") or {})
    except ValueError:
        extra = {}
    headers = {str(k): str(v) for k, v in (extra or {}).items() if v not in (None, "")}
    q = dict(params or {})
    a = parse_auth(fields.get("auth"))
    cred = str(fields.get("credential") or "")
    if a["kind"] != "none" and not cred:
        return {"error": "the connection needs a credential that is not set"}
    basic = None
    if a["kind"] == "bearer":
        headers["Authorization"] = "Bearer " + cred
    elif a["kind"] == "header":
        headers[a["name"]] = cred
    elif a["kind"] == "query":
        q[a["name"]] = cred
    elif a["kind"] == "basic":
        user = str(fields.get("username") or "")
        basic = (user, cred) if user else (cred, "")
    # params given with the path stay in the URL; extra params (and a query credential) are merged in
    if q:
        u = urlsplit(url)
        merged = dict(parse_qsl(u.query, keep_blank_values=True))
        merged.update({k: str(v) for k, v in q.items()})
        url = urlunsplit((u.scheme, u.netloc, u.path, urlencode(merged), u.fragment))
    return {"url": url, "headers": headers, "params": {}, "auth": basic}


def describe(fields: Dict[str, Any]) -> str:
    """How a connection authenticates, in words, without the credential."""
    a = parse_auth(fields.get("auth"))
    return {"none": "no key", "bearer": "bearer token", "header": f"key in header {a['name']}",
            "query": f"key in query parameter {a['name']}", "basic": "basic auth"}[a["kind"]]


def redact_url(url: str, fields: Dict[str, Any]) -> str:
    """A URL safe to show: a query-parameter credential replaced by ***."""
    a = parse_auth(fields.get("auth"))
    if a["kind"] != "query":
        return url
    u = urlsplit(url)
    q = [(k, "***" if k == a["name"] else v) for k, v in parse_qsl(u.query, keep_blank_values=True)]
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), u.fragment))


def basic_header(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
