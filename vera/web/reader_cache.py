"""The reader cache: a page read once, as the article it is, kept for a week (canvas-enrichment plan, slice 2).

Every page the estate reads (web.research, web.fetch, the canvas's reader mode) goes through reader.page, which
keeps its reader version here: Redis for the record (title, byline, the article as markdown, its entities), seven
days; a page whose markdown is over BIG characters keeps its body in the object store and a pointer here. Keyed by
the URL as a reader would name it - no fragment, no tracking parameters, the host in lower case - so the same page
reached two ways is one entry.

Owner, 2026-10-03: "reader mode should be cached as the site is crawled - ideally this is the output the LLM should
see to cut out rubbish from its input - the reader mode should not be processed as the user clicks it"; decision:
"Redis 7 days + store".
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

KEY = "vera:reader:"
TTL_S = 7 * 24 * 3600
BIG = 200_000
_TRACKING = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref_src", "igshid")


def canonical(url: str) -> str:
    """The page as a reader would name it: no fragment, no tracking parameters, scheme and host in lower case."""
    try:
        p = urlsplit(str(url or "").strip())
    except ValueError:
        return str(url or "").strip()
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not any(k.lower().startswith(t) for t in _TRACKING)]
    path = p.path or "/"
    return urlunsplit(((p.scheme or "https").lower(), p.netloc.lower(), path, urlencode(q), ""))


def key_of(url: str) -> str:
    return KEY + hashlib.sha1(canonical(url).encode("utf-8")).hexdigest()


def object_key(url: str) -> str:
    return "reader/" + hashlib.sha1(canonical(url).encode("utf-8")).hexdigest() + ".md"


ObjPut = Callable[..., Awaitable[Any]]
ObjGet = Callable[..., Awaitable[Any]]


async def get(redis, url: str, obj_get: Optional[ObjGet] = None) -> Optional[Dict[str, Any]]:
    """The cached reader record for a page, or None. A body kept in the object store is fetched back in."""
    if redis is None:
        return None
    try:
        raw = await redis.get(key_of(url))
    except Exception:
        return None
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if rec.get("body_key") and not rec.get("markdown") and obj_get:
        try:
            r = await obj_get(key=rec["body_key"])
            if isinstance(r, dict) and r.get("content_base64"):
                rec["markdown"] = base64.b64decode(r["content_base64"]).decode("utf-8", "replace")
        except Exception:
            pass
    return rec


async def put(redis, url: str, rec: Dict[str, Any], obj_put: Optional[ObjPut] = None) -> bool:
    """Keep a page's reader record for TTL_S. A body over BIG characters goes to the object store when it can."""
    if redis is None or not isinstance(rec, dict):
        return False
    rec = dict(rec, url=rec.get("url") or url, canonical=canonical(url))
    md = str(rec.get("markdown") or "")
    if len(md) > BIG and obj_put:
        k = object_key(url)
        try:
            r = await obj_put(key=k, content_base64=base64.b64encode(md.encode("utf-8")).decode("ascii"),
                              content_type="text/markdown; charset=utf-8")
            if isinstance(r, dict) and r.get("ok"):
                rec["body_key"], rec["markdown"] = k, ""
        except Exception:
            pass
    try:
        await redis.set(key_of(url), json.dumps(rec, default=str), ex=TTL_S)
        return True
    except Exception:
        return False


async def update(redis, url: str, **fields) -> bool:
    """Add fields (its entities, say) to a page already kept, leaving its expiry where it was."""
    if redis is None:
        return False
    k = key_of(url)
    try:
        raw = await redis.get(k)
        if not raw:
            return False
        rec = json.loads(raw)
        rec.update(fields)
        ttl = await redis.ttl(k)
        await redis.set(k, json.dumps(rec, default=str), ex=ttl if isinstance(ttl, int) and ttl > 0 else TTL_S)
        return True
    except Exception:
        return False


def trimmed(rec: Dict[str, Any], max_chars: int) -> Dict[str, Any]:
    """A copy whose markdown is at most max_chars (the cache keeps the whole article)."""
    out = dict(rec)
    md = str(out.get("markdown") or "")
    if max_chars and len(md) > max_chars:
        out["markdown"], out["truncated"] = md[:max_chars], True
    return out
