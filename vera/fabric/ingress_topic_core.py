"""Topic maps (shared-planning web-ingress WI-8; PLAN §3.5). Pure.

One map per subject, keyed on the intel topic id (owner, 11 Oct: intel topics are canonical). It links the
artefacts that already exist for the subject - discovery `topic.*` datasets, KB ids, topic models, worldview
concepts - and holds the subject's members: the URLs that matter, each with a role, a relevance and a novelty.
From the members and the hosts' novel yield it says whether the topic is still yielding (saturation) and what
kinds it has none of (gaps).
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

try:
    from Vera.vera.fabric import ingress_core as ic
except ImportError:  # pragma: no cover
    from vera.fabric import ingress_core as ic

ROLES = ("seed", "hub", "authority", "source", "page", "file", "dataset", "api", "feed", "entity")
KINDS_FOR_GAPS = ("page", "file", "dataset", "api", "feed")
SATURATED_BELOW = 0.1       # a topic whose recent novel yield falls under this stops crawling (PLAN §3.5)
MIN_FETCHES_FOR_SATURATION = 20


def topic_key(topic_id: str = "", name: str = "") -> str:
    """The intel topic id as the map's key; a bare name becomes an id the way intel slugs one."""
    t = str(topic_id or "").strip().lower()
    if t:
        return re.sub(r"[^a-z0-9_.-]+", "-", t).strip("-")[:80]
    n = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return n[:48]


def member_role(kind: str, *, seed: bool = False, inbound: int = 0, outbound: int = 0,
                host_yield: Optional[float] = None) -> str:
    """What a URL is to the topic: a seed the owner or intel gave; a hub (links out to many members); an authority
    (many members link to it); else its kind (page, file, dataset, api, feed)."""
    if seed:
        return "seed"
    if inbound >= 5:
        return "authority"
    if outbound >= 10:
        return "hub"
    k = str(kind or "page")
    return k if k in ROLES else "page"


def saturation(novel_yield: Optional[float], fetches: int, recent_new: int, recent_total: int) -> Dict[str, Any]:
    """Whether the topic has stopped yielding: the hosts' novel-yield EWMA and the recent share of new members.
    Needs a minimum of fetches to say anything."""
    if fetches < MIN_FETCHES_FOR_SATURATION:
        return {"state": "young", "yield": novel_yield, "recent_new_share": None}
    share = (recent_new / recent_total) if recent_total else None
    y = novel_yield if novel_yield is not None else share
    if y is None:
        return {"state": "unknown", "yield": None, "recent_new_share": share}
    return {"state": "saturated" if y < SATURATED_BELOW else "yielding", "yield": round(float(y), 3),
            "recent_new_share": None if share is None else round(share, 3)}


def gaps(kind_counts: Mapping[str, int]) -> List[str]:
    """The kinds a topic has none of (so the next crawl or search knows what to look for)."""
    return [k for k in KINDS_FOR_GAPS if int(kind_counts.get(k, 0) or 0) == 0]


def seeds_from_intel_topic(topic: Mapping[str, Any]) -> Dict[str, List[str]]:
    """What an intel topic gives the map to start from: its queries, the sites it names, and its entities."""
    def clean(xs: Any) -> List[str]:
        out: List[str] = []
        for x in xs or ():
            s = str(x or "").strip()
            if s and s not in out:
                out.append(s)
        return out[:50]
    sites = []
    for s in clean(topic.get("sites")):
        u = s if s.startswith("http") else "https://" + s
        k = ic.url_key(u)
        if k and k not in sites:
            sites.append(k)
    return {"queries": clean(topic.get("queries")) + clean(topic.get("questions"))[:10],
            "seeds": sites, "entities": clean(topic.get("entities"))}


def summary(topic: Mapping[str, Any], members: Iterable[Mapping[str, Any]], host_yields: Mapping[str, float]) -> Dict[str, Any]:
    """The map in one object: counts by kind and host, growth, saturation and gaps."""
    ms = list(members or ())
    by_kind: Dict[str, int] = {}
    by_host: Dict[str, int] = {}
    by_role: Dict[str, int] = {}
    for m in ms:
        by_kind[str(m.get("kind") or "page")] = by_kind.get(str(m.get("kind") or "page"), 0) + 1
        h = ic.host_of(str(m.get("url_key") or ""))
        if h:
            by_host[h] = by_host.get(h, 0) + 1
        by_role[str(m.get("role") or "page")] = by_role.get(str(m.get("role") or "page"), 0) + 1
    yields = [host_yields[h] for h in by_host if host_yields.get(h) is not None]
    y = (sum(yields) / len(yields)) if yields else None
    recent = [m for m in ms if m.get("recent")]
    recent_new = sum(1 for m in recent if float(m.get("novelty") or 0) >= 0.5)
    top_hosts = sorted(by_host.items(), key=lambda kv: -kv[1])[:12]
    return {"topic_key": topic.get("topic_key") or topic_key(str(topic.get("intel_topic_id") or ""), str(topic.get("name") or "")),
            "name": topic.get("name") or "", "intel_topic_id": topic.get("intel_topic_id") or "",
            "members": len(ms), "by_kind": by_kind, "by_role": by_role, "hosts": len(by_host), "top_hosts": top_hosts,
            "linked": {"datasets": list(topic.get("dataset_ids") or []), "kbs": list(topic.get("kb_ids") or []),
                       "worldview_concepts": list(topic.get("worldview_concepts") or [])},
            "saturation": saturation(y, len(ms), recent_new, len(recent)), "gaps": gaps(by_kind)}
