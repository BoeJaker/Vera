"""Intelligence - names to stable identities (pure: parsing and scoring).

"JLL", "Jones Lang LaSalle" and "JLL Inc" are one company; "Mercury" is a
planet, an element, a record label and a fund. Sources name things loosely, so
a topic's entities are resolved to Wikidata items (QIDs) - stable ids that
discovery, the worldview and other topics can join on - with a description, the
official website, a stock listing and a place's coordinates where Wikidata has
them.

A candidate is scored on four kinds of evidence, and the resolver ABSTAINS
when the best one is not clearly good enough:

  name      the item's label or an alias IS the name (after legal suffixes) - 0..1
  type      its "instance of" (P31) fits what GLiNER called the entity
            (company -> business/enterprise/organisation; city -> city/town...)
  site      its official website's domain is among the topic's own sources
  context   the NLP reranker's view of its description against the topic

Nothing here does I/O; the capabilities fetch Wikidata and hand the JSON in.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlsplit

API = "https://www.wikidata.org/w/api.php"
# Wikimedia asks every client for a contact in its User-Agent; the project page, no personal details
USER_AGENT = "Vera intelligence/1.0 (https://github.com/BoeJaker/Vera)"

# P31 values that say "an organisation" or "a place" - the common ones; the
# description words below catch the long tail without a P279 walk.
ORG_TYPES = {"Q4830453", "Q6881511", "Q891723", "Q783794", "Q43229", "Q167037", "Q2659904", "Q1058914",
             "Q163740", "Q31855", "Q327333", "Q484652", "Q3918", "Q7210356", "Q15911314", "Q1616075",
             "Q18388277", "Q210167", "Q161726", "Q1137109", "Q11032", "Q1320047", "Q219577", "Q155076"}
PLACE_TYPES = {"Q515", "Q6256", "Q3624078", "Q1549591", "Q5119", "Q486972", "Q3957", "Q532", "Q1637706",
               "Q35657", "Q10864048", "Q34876", "Q82794", "Q23442", "Q8502", "Q4022", "Q23397", "Q165",
               "Q1620908", "Q1093829", "Q15284", "Q180673", "Q208511"}
ORG_WORDS = ("company", "corporation", "firm", "provider", "group", "agency", "organisation", "organization",
             "bank", "fund", "trust", "consultancy", "developer", "manufacturer", "publisher", "laboratory",
             "university", "regulator", "authority", "institute", "startup", "business", "association", "party")
PLACE_WORDS = ("city", "town", "village", "country", "county", "region", "state", "province", "district",
               "borough", "capital", "island", "sea", "ocean", "river", "port", "area of", "metropolitan")
ORG_LABELS = {"company", "organization", "property developer", "real estate investment trust",
              "property agency or consultancy", "investor", "investment fund", "research lab", "government agency",
              "financial regulator", "central bank", "military unit or armed group", "law or regulation"}
PLACE_LABELS = {"location", "city or region", "country", "development site", "military facility", "planning authority"}

ACCEPT = 0.62          # the best candidate's score must reach this ...
MARGIN = 0.12          # ... and beat the runner-up by this much


def _key(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower().replace("&", " and "))
    s = re.sub(r"\b(plc|inc|incorporated|corp|corporation|ltd|limited|group|holdings?|co|company|sa|ag|nv|se|llc|the)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def candidates(search: Dict[str, Any], limit: int = 7) -> List[Dict[str, Any]]:
    """wbsearchentities -> [{qid, label, description, aliases}]."""
    out = []
    for x in (search or {}).get("search") or []:
        if not isinstance(x, dict) or not str(x.get("id", "")).startswith("Q"):
            continue
        out.append({"qid": x["id"], "label": x.get("label") or "", "description": x.get("description") or "",
                    "aliases": [a for a in (x.get("aliases") or []) if isinstance(a, str)],
                    "match": (x.get("match") or {}).get("text") or ""})
    return out[:limit]


def _vals(claims: Dict[str, Any], prop: str) -> List[Any]:
    out = []
    for c in (claims or {}).get(prop) or []:
        v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if v is not None:
            out.append(v)
    return out


def facts(entity: Dict[str, Any]) -> Dict[str, Any]:
    """The few claims that matter here, from a wbgetentities item."""
    claims = (entity or {}).get("claims") or {}
    types = [v.get("id") for v in _vals(claims, "P31") if isinstance(v, dict) and v.get("id")]
    sites = [v for v in _vals(claims, "P856") if isinstance(v, str)]
    tickers = []
    for c in claims.get("P414") or []:                      # stock exchange, ticker as a qualifier
        exch = (((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}).get("id")
        for q in ((c.get("qualifiers") or {}).get("P249") or []):
            tk = ((q.get("datavalue") or {}).get("value"))
            if isinstance(tk, str):
                tickers.append({"exchange": exch, "ticker": tk})
    coord = next((v for v in _vals(claims, "P625") if isinstance(v, dict)), None)
    country = next((v.get("id") for v in _vals(claims, "P17") if isinstance(v, dict)), None)
    wiki = (((entity or {}).get("sitelinks") or {}).get("enwiki") or {}).get("title") or ""
    return {"types": types, "websites": sites[:3], "domains": sorted({_domain(u) for u in sites if _domain(u)}),
            "wiki": wiki,
            "tickers": tickers[:3], "country": country,
            "coords": {"lat": coord.get("latitude"), "lon": coord.get("longitude")} if coord else None}


_SUFFIXES = (".co.uk", ".org.uk", ".ac.uk", ".gov.uk", ".com.au", ".co.jp", ".com", ".org", ".net", ".io", ".ai",
             ".co", ".uk", ".de", ".fr", ".eu", ".us", ".jp", ".cn", ".in", ".global", ".group")


def brand(domain: str) -> str:
    """'www.jll.co.uk' and 'jll.com' -> 'jll': the name a site goes by, across its country domains."""
    d = re.sub(r"^www\d?\.", "", str(domain or "").lower())
    for suf in _SUFFIXES:
        if d.endswith(suf):
            d = d[: -len(suf)]
            break
    return d.rsplit(".", 1)[-1]


def _domain(url: str) -> str:
    try:
        h = (urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return ""
    return re.sub(r"^www\d?\.", "", h)


def kind_of(label: str) -> str:
    """'org' | 'place' | '' from a GLiNER entity type."""
    if label in ORG_LABELS:
        return "org"
    if label in PLACE_LABELS:
        return "place"
    return ""


def type_fit(kind: str, cand: Dict[str, Any], fx: Optional[Dict[str, Any]]) -> float:
    """1 when the item's type fits the entity's, 0 when it clearly does not, 0.5 unknown."""
    if not kind:
        return 0.5
    types = set((fx or {}).get("types") or [])
    desc = (cand.get("description") or "").lower()
    want_t, want_w = (ORG_TYPES, ORG_WORDS) if kind == "org" else (PLACE_TYPES, PLACE_WORDS)
    other_t, other_w = (PLACE_TYPES, PLACE_WORDS) if kind == "org" else (ORG_TYPES, ORG_WORDS)
    if types & want_t or any(w in desc for w in want_w):
        return 1.0
    if types & other_t or any(w in desc for w in other_w):
        return 0.0
    if any(w in desc for w in ("film", "album", "song", "novel", "character", "genus", "species", "given name",
                               "family name", "disambiguation", "episode", "painting", "chemical")):
        return 0.0
    return 0.4


def name_fit(name: str, cand: Dict[str, Any]) -> float:
    k = _key(name)
    if not k:
        return 0.0
    labels = [cand.get("label") or ""] + list(cand.get("aliases") or []) + [cand.get("match") or ""]
    keys = [_key(x) for x in labels if x]
    if k in keys:
        return 1.0
    if k.replace(" ", "") in [x.replace(" ", "") for x in keys]:
        return 0.95
    if any(x.startswith(k + " ") or k.startswith(x + " ") for x in keys if x):
        return 0.6
    return 0.0


def score(name: str, kind: str, cand: Dict[str, Any], fx: Optional[Dict[str, Any]], *,
          source_domains: Iterable[str] = (), context: Optional[float] = None, rank: int = 0,
          of: int = 1) -> Dict[str, Any]:
    """{score, parts} for one candidate. context is the reranker's logit (squashed here)."""
    nf = name_fit(name, cand)
    tf = type_fit(kind, cand, fx)
    brands = {brand(d) for d in (fx or {}).get("domains") or []} - {""}
    site = 1.0 if brands and brands & {brand(d) for d in source_domains} else 0.0
    ctx = None if context is None else 1.0 / (1.0 + 2.718281828 ** (-float(context) / 3.0))
    parts = {"name": nf, "type": tf, "site": site, "context": ctx}
    s = 0.45 * nf + 0.25 * tf + 0.15 * (ctx if ctx is not None else 0.5) + 0.15 * site
    s += 0.04 * (1 - rank / max(1, of))                    # Wikidata's own search order breaks near-ties
    if site:
        s = max(s, 0.8 if nf >= 0.6 else s)              # the topic's own sources link to it: strong
    if nf == 0.0 or tf == 0.0:
        s = min(s, 0.4)                                     # a different name or the wrong kind of thing
    if ctx is not None and ctx < 0.35 and not site:
        s = min(s, 0.55)                                    # right name, wrong world: "Mercury" the car in a property topic
    return {"score": round(s, 3), "parts": parts, "brands": sorted(brands)}


def choose(scored: Sequence[Dict[str, Any]], accept: float = ACCEPT, margin: float = MARGIN) -> Dict[str, Any]:
    """The winner, or an abstention with the reason - never a guess."""
    if not scored:
        return {"qid": None, "why": "no candidates"}
    ranked = sorted(scored, key=lambda c: -c["score"])
    best = ranked[0]
    second = ranked[1]["score"] if len(ranked) > 1 else 0.0
    if best["score"] < accept:
        return {"qid": None, "why": f"best candidate scored {best['score']:.2f} < {accept}", "best": best.get("qid")}
    if best["score"] - second < margin and best.get("brands") and set(best["brands"]) & set(ranked[1].get("brands") or []):
        # two items with the same official website are one organisation (a duplicate or a regional
        # entity of it): the higher-ranked one is the answer, not an ambiguity
        return {"qid": best.get("qid"), "score": best["score"], "margin": round(best["score"] - second, 3),
                "note": f"same organisation as {ranked[1].get('qid')}"}
    if best["score"] - second < margin:
        return {"qid": None, "why": f"ambiguous: {best.get('qid')} {best['score']:.2f} vs {second:.2f}",
                "best": best.get("qid")}
    return {"qid": best.get("qid"), "score": best["score"], "margin": round(best["score"] - second, 3)}
