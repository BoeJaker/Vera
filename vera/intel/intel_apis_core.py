"""The keyed data sources intelligence can use, and what each needs. Pure: no I/O.

The keys themselves live in the one master store, platform.secrets (the Platforms page; OpenBao, never returned by a
capability) - owner 2026-10-11: "there is already a master api key system that should be used and not re-made".
This module only says which credential name each source reads, what it adds, where to get a key, and whether Vera
uses it yet, so the intel panel can show what is missing and send the owner to the Platforms page to add it.

status: "used" - code reads the key today; "planned" - worth having, the source type is not built yet.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

APIS: List[Dict[str, str]] = [
    {"key": "brave_search_api_key", "name": "Brave Search API", "status": "used",
     "gives": "Search results when SearXNG's engines are rate-limited or blocked - web.search, info.seek and the "
              "nightly sweeps fall back to it (today they fall back to scraping DuckDuckGo).",
     "used_by": "web.search / info.seek / intel sweeps", "get": "https://brave.com/search/api/",
     "cost": "free tier, about 2,000 queries a month"},
    {"key": "github_token", "name": "GitHub token", "status": "used",
     "gives": "GitHub sources in the fabric read 5,000 API requests an hour instead of 60.",
     "used_by": "fabric GitHub sources", "get": "https://github.com/settings/personal-access-tokens",
     "cost": "free; a fine-grained token with no permissions reads public data"},
    {"key": "guardian_api_key", "name": "Guardian Open Platform", "status": "planned",
     "gives": "Search the Guardian by keyword and date with full article text - UK news, property and business, "
              "beyond what its RSS feeds carry.",
     "used_by": "", "get": "https://open-platform.theguardian.com/access/", "cost": "free developer key"},
    {"key": "companies_house_api_key", "name": "Companies House", "status": "planned",
     "gives": "UK company profiles, officers and filings - for competitor watch and the property firms topics follow.",
     "used_by": "", "get": "https://developer.company-information.service.gov.uk/", "cost": "free"},
    {"key": "epc_api_key", "name": "EPC Open Data", "status": "planned",
     "gives": "Energy performance certificates for UK homes and commercial buildings - for the property topics.",
     "used_by": "", "get": "https://epc.opendatacommunities.org/", "cost": "free (register an email)"},
]

#: what intelligence reads without any key - so the list of keys is not mistaken for everything it can see
KEYLESS = [
    "RSS / Atom feeds (trade press, BBC, Guardian sections)", "SearXNG (Bing, Google, Yahoo, DuckDuckGo...)",
    "FRED macro series (public CSV)", "Polymarket and Manifold", "Wikimedia page views and Wikidata",
    "Nominatim (OpenStreetMap)", "USGS earthquakes", "arXiv", "YouTube channel feeds", "GitHub release feeds",
]

#: any other API: a Platforms connection, mapped, then a fabric source that reads through it
JSON_API_HINT = ("Any other API: add it as an API connection on the Platforms page (its key goes to Shared "
                 "credentials), map an endpoint and add it as a fabric source - the source holds no key.")


def view(records: Iterable[Dict[str, Any]], templates: Iterable[Dict[str, Any]] = (),
         connections: Iterable[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """The catalog with each key's state from platform.secrets.list rows ({key, set, ...}), each API's connection
    from platform.connection.templates rows ({id, key, added, ...}), and the API connections themselves."""
    have = {str(r.get("key")): bool(r.get("set")) for r in records or []}
    by_key = {t.get("key"): t for t in templates or () if t.get("key")}
    rows = [dict(a, set=have.get(a["key"], False), ref="@secret:" + a["key"],
                 template=(by_key.get(a["key"]) or {}).get("id", ""),
                 connection=(by_key.get(a["key"]) or {}).get("added", "")) for a in APIS]
    keyless = [{"id": t["id"], "label": t.get("label", t["id"]), "connection": t.get("added", ""),
                "tags": t.get("tags", [])} for t in templates or () if not t.get("key")]
    return {"apis": rows, "keyless": KEYLESS, "keyless_templates": keyless, "connections": list(connections or ()),
            "json_api_hint": JSON_API_HINT,
            "missing_used": [r["key"] for r in rows if r["status"] == "used" and not r["set"]]}
