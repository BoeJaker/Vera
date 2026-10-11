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

#: any JSON API source can carry a key too: a header value "@secret:<key>" is resolved at pull time
JSON_API_HINT = ("Any other API with a key: add it on the Platforms page, then add a JSON API source whose header "
                 "names it, e.g. Authorization: Bearer @secret:<key> - the source holds the reference, never the key.")


def view(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """The catalog with each key's state from platform.secrets.list rows ({key, set, ...})."""
    have = {str(r.get("key")): bool(r.get("set")) for r in records or []}
    rows = [dict(a, set=have.get(a["key"], False), ref="@secret:" + a["key"]) for a in APIS]
    return {"apis": rows, "keyless": KEYLESS, "json_api_hint": JSON_API_HINT,
            "missing_used": [r["key"] for r in rows if r["status"] == "used" and not r["set"]]}
