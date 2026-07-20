"""
web_api_capabilities.py — Platform API providers for web search & fetch.
========================================================================

The problem
───────────
Some platforms (Reddit above all) hard-block scrapers no matter how good the
browser fingerprint is: datacenter-IP bans, mandatory JS, aggressive rate
limits. The reliable way to read those platforms is their own API. This module
lets the operator register platform API providers (Reddit, GitHub, Hacker News,
or any custom REST/JSON API) with their credentials, and then makes web.search
and web.fetch transparently switch over to the API when a request targets a
platform that has one configured.

How the switchover works
────────────────────────
  • web.fetch — before scraping a URL, web_client calls fetch_for_url(url).
    If an enabled provider's domain matches the URL's host, the page content
    comes from that platform's API (via_api="<id>") instead of an HTTP scrape.
    This runs BEFORE the reddit→old.reddit rewrite, so a configured Reddit
    provider always wins; without one, the rewrite is the fallback.
  • web.search — before hitting the general engines, web.search calls
    search_for_query(query, limit, platform). If the query targets a platform
    (an explicit platform="reddit", a `site:reddit.com` filter, or a platform
    trigger keyword) that provider's native search API runs and its results
    lead the merged list.

Storage
───────
Provider records live in Redis hash `vera:web_api` (id → json). Secret fields
are sealed with the shared Fernet/OpenBao vault (Vera.vera.security.secrets),
exactly like the LLM providers module. Non-secret config is stored in the
clear; the UI only ever sees redacted records (has_<field> booleans).

Drivers
───────
A "driver" is the integration for one kind of platform. Built-ins:
  reddit      — OAuth (app-only client_credentials, or password grant for a
                script app); search + thread/subreddit fetch.
  github      — REST v3; repo/issue fetch + repository search. Token optional.
  hackernews  — Algolia HN API; no auth; item fetch + search.
  rest        — generic configurable JSON API (templated search URL + dot-path
                result mapping, optional auth header + fetch template).

Each driver declares the config/secret fields the UI should render, so adding
"an API like Reddit" is: pick a driver, fill the fields, Save, Test.

Register in capability_orchestration.py `_module_files`:
    os.path.join(_here, "web/web_api_capabilities.py"),
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, quote_plus

import httpx

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability, emit_event, now_iso
from Vera.vera.security import secrets as vsecrets
from Vera.vera.web import web_client as _wc

log = logging.getLogger("vera.web_api")

KEY_PROVIDERS = "vera:web_api"            # hash id -> json record
_PANEL_PATH   = Path(__file__).parent / "web_api_panel.html"

# Cached OAuth tokens: provider-id -> (token, expiry_epoch)
_TOKENS: Dict[str, Tuple[str, float]] = {}


def _redis():
    return getattr(_orch, "REDIS", None)


def _ua() -> str:
    return _wc.USER_AGENT


# ─────────────────────────────────────────────────────────────────────────────
#  Driver field specs — drive both the UI form and validation. `secret=True`
#  fields are sealed on write and never returned to the UI.
# ─────────────────────────────────────────────────────────────────────────────
_DRIVERS_META: Dict[str, dict] = {
    "reddit": {
        "label": "Reddit",
        "blurb": "Reddit's OAuth API. Register a 'script' app at "
                 "reddit.com/prefs/apps → use its client id & secret. Add your "
                 "Reddit username/password for higher limits + user context; "
                 "leave them blank for read-only app-only auth.",
        "domains": ["reddit.com", "www.reddit.com", "old.reddit.com"],
        "keywords": ["reddit", "subreddit", "r/"],
        "fields": [
            {"key": "client_id",     "label": "Client ID",       "secret": False},
            {"key": "client_secret", "label": "Client Secret",   "secret": True},
            {"key": "username",      "label": "Username (opt)",  "secret": False},
            {"key": "password",      "label": "Password (opt)",  "secret": True},
        ],
    },
    "github": {
        "label": "GitHub",
        "blurb": "GitHub REST API. A personal access token is optional but "
                 "raises the rate limit from 60 to 5000 requests/hour.",
        "domains": ["github.com", "www.github.com"],
        "keywords": ["github", "repo", "repository"],
        "fields": [
            {"key": "token", "label": "Personal Access Token (opt)", "secret": True},
        ],
    },
    "hackernews": {
        "label": "Hacker News",
        "blurb": "Hacker News via the free Algolia API. No credentials needed.",
        "domains": ["news.ycombinator.com"],
        "keywords": ["hacker news", "hackernews", "hn"],
        "fields": [],
    },
    "rest": {
        "label": "Custom REST/JSON API",
        "blurb": "Any JSON API. Templated search URL with {query} and {limit}; "
                 "dot-path to the results array; field mappings for url/title/"
                 "snippet. Optional auth header + fetch URL template with {url}.",
        "domains": [],
        "keywords": [],
        "fields": [
            {"key": "base_url",     "label": "Base URL",                  "secret": False},
            {"key": "search_path",  "label": "Search path ({query},{limit})", "secret": False},
            {"key": "results_path", "label": "Results dot-path (e.g. data.items)", "secret": False},
            {"key": "map_url",      "label": "Result → url field",        "secret": False},
            {"key": "map_title",    "label": "Result → title field",      "secret": False},
            {"key": "map_snippet",  "label": "Result → snippet field",    "secret": False},
            {"key": "fetch_path",   "label": "Fetch path ({url}) (opt)",  "secret": False},
            {"key": "auth_header",  "label": "Auth header name (opt)",    "secret": False},
            {"key": "auth_value",   "label": "Auth header value (opt)",   "secret": True},
        ],
    },
}


# ─────────────────────────────────────────────────────────────────────────────
#  Record helpers (seal on write, redact on read)
# ─────────────────────────────────────────────────────────────────────────────
async def _stored() -> Dict[str, dict]:
    r = _redis()
    out: Dict[str, dict] = {}
    if not r:
        return out
    try:
        raw = await r.hgetall(KEY_PROVIDERS)
        for k, v in (raw or {}).items():
            pid = k.decode() if isinstance(k, bytes) else k
            try:
                out[pid] = json.loads(v.decode() if isinstance(v, bytes) else v)
            except Exception:
                continue
    except Exception as e:
        log.debug("web_api _stored: %s", e)
    return out


async def _get(pid: str) -> Optional[dict]:
    return (await _stored()).get(pid)


def _secret(rec: dict, key: str) -> str:
    """Decrypt a sealed field of a provider record."""
    return vsecrets.open_secret((rec.get("secrets") or {}).get(key, "") or "")


def _cfg(rec: dict, key: str, default: str = "") -> str:
    return (rec.get("config") or {}).get(key, default) or default


def _domains(rec: dict) -> List[str]:
    ds = rec.get("domains")
    if not ds:
        ds = _DRIVERS_META.get(rec.get("kind", ""), {}).get("domains", [])
    return [d.lower().lstrip(".") for d in ds if d]


def _keywords(rec: dict) -> List[str]:
    kws = rec.get("keywords")
    if not kws:
        kws = _DRIVERS_META.get(rec.get("kind", ""), {}).get("keywords", [])
    return [k.lower() for k in kws if k]


def _redact(rec: dict) -> dict:
    kind = rec.get("kind", "")
    meta = _DRIVERS_META.get(kind, {})
    secrets_present = {k: bool(v) for k, v in (rec.get("secrets") or {}).items()}
    return {
        "id":       rec.get("id", ""),
        "kind":     kind,
        "label":    rec.get("label") or meta.get("label", kind),
        "enabled":  bool(rec.get("enabled", True)),
        "domains":  _domains(rec),
        "keywords": _keywords(rec),
        "config":   dict(rec.get("config") or {}),
        "has_secret": secrets_present,
        "fields":   meta.get("fields", []),
        "blurb":    meta.get("blurb", ""),
    }


def _domain_matches(host: str, domains: List[str]) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


# ─────────────────────────────────────────────────────────────────────────────
#  Shared HTTP
# ─────────────────────────────────────────────────────────────────────────────
async def _get_json(url: str, headers: dict, timeout: float = 12.0,
                    params: dict = None) -> Any:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.get(url, headers=headers, params=params)
        r.raise_for_status()
        return r.json()


# ═════════════════════════════════════════════════════════════════════════════
#  DRIVER: reddit
# ═════════════════════════════════════════════════════════════════════════════
async def _reddit_token(rec: dict) -> str:
    pid = rec.get("id", "reddit")
    tok, exp = _TOKENS.get(pid, ("", 0.0))
    if tok and exp > time.time() + 30:
        return tok
    cid = _cfg(rec, "client_id") or _secret(rec, "client_id")
    csecret = _secret(rec, "client_secret")
    if not cid or not csecret:
        raise RuntimeError("reddit provider needs client_id + client_secret")
    user = _cfg(rec, "username")
    pw = _secret(rec, "password")
    if user and pw:
        data = {"grant_type": "password", "username": user, "password": pw}
    else:
        # App-only auth for confidential (script/web) clients.
        data = {"grant_type": "client_credentials"}
    ua = f"vera/1.0 (by /u/{user})" if user else "vera/1.0"
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post("https://www.reddit.com/api/v1/access_token",
                         data=data, auth=(cid, csecret),
                         headers={"User-Agent": ua})
        r.raise_for_status()
        d = r.json()
    tok = d.get("access_token", "")
    if not tok:
        raise RuntimeError(f"reddit token error: {str(d)[:160]}")
    _TOKENS[pid] = (tok, time.time() + int(d.get("expires_in", 3600)))
    return tok


def _reddit_headers(rec: dict, token: str) -> dict:
    user = _cfg(rec, "username")
    return {"Authorization": f"bearer {token}",
            "User-Agent": f"vera/1.0 (by /u/{user})" if user else "vera/1.0"}


async def _reddit_search(rec: dict, query: str, limit: int) -> List[Dict[str, Any]]:
    token = await _reddit_token(rec)
    hdrs = _reddit_headers(rec, token)
    # Subreddit-scoped search when the query says "in r/<sub>" or "r/<sub>".
    sub = ""
    import re as _re
    m = _re.search(r"\br/([A-Za-z0-9_]+)", query)
    if m:
        sub = m.group(1)
        query = _re.sub(r"\b(in\s+)?r/[A-Za-z0-9_]+", "", query).strip() or sub
    base = f"https://oauth.reddit.com/r/{sub}/search" if sub else "https://oauth.reddit.com/search"
    params = {"q": query, "limit": min(limit, 25), "sort": "relevance",
              "type": "link", "raw_json": 1}
    if sub:
        params["restrict_sr"] = "true"
    data = await _get_json(base, hdrs, params=params)
    out: List[Dict[str, Any]] = []
    for ch in (data.get("data", {}).get("children", []) or [])[:limit]:
        d = ch.get("data", {})
        permalink = d.get("permalink", "")
        url = ("https://www.reddit.com" + permalink) if permalink else d.get("url", "")
        snippet = (d.get("selftext", "") or "")[:300]
        meta = f"r/{d.get('subreddit','')} · ▲{d.get('score',0)} · {d.get('num_comments',0)} comments"
        out.append({"url": url, "title": d.get("title", ""),
                    "snippet": (snippet + "  " + meta).strip(), "engine": "reddit"})
    return out


async def _reddit_fetch(rec: dict, url: str) -> Optional[Dict[str, Any]]:
    token = await _reddit_token(rec)
    hdrs = _reddit_headers(rec, token)
    path = urlparse(url).path.rstrip("/")
    if not path or path == "":
        path = "/hot"
    data = await _get_json("https://oauth.reddit.com" + path, hdrs,
                           params={"raw_json": 1, "limit": 50})
    parts: List[str] = []
    title = ""

    def _post_block(listing) -> Tuple[str, str]:
        try:
            d = listing["data"]["children"][0]["data"]
        except Exception:
            return "", ""
        t = d.get("title", "")
        body = d.get("selftext", "") or ""
        head = f"r/{d.get('subreddit','')} · ▲{d.get('score',0)} · {d.get('num_comments',0)} comments · u/{d.get('author','')}"
        return t, "\n".join(x for x in (f"# {t}", head, body) if x)

    def _comments(listing, depth=0, acc=None) -> None:
        acc = acc if acc is not None else parts
        if depth > 6:
            return
        for ch in listing.get("data", {}).get("children", []):
            if ch.get("kind") != "t1":
                continue
            cd = ch.get("data", {})
            body = (cd.get("body", "") or "").strip()
            if body:
                acc.append(("  " * depth) + f"• u/{cd.get('author','')} (▲{cd.get('score',0)}): {body}")
            replies = cd.get("replies")
            if isinstance(replies, dict):
                _comments(replies, depth + 1, acc)

    if isinstance(data, list) and data:
        title, post = _post_block(data[0])
        if post:
            parts.append(post)
        if len(data) > 1:
            parts.append("\n--- Comments ---")
            _comments(data[1])
    elif isinstance(data, dict):
        # Subreddit / user listing.
        for ch in data.get("data", {}).get("children", [])[:40]:
            cd = ch.get("data", {})
            t = cd.get("title", "") or cd.get("link_title", "")
            if t:
                parts.append(f"• {t}  (r/{cd.get('subreddit','')} ▲{cd.get('score',0)})")
        title = title or f"Reddit: {path}"

    text = "\n".join(parts).strip()
    if not text:
        return None
    return {"text": text[:_wc.MAX_PAGE_CHARS], "title": title, "provider": rec.get("id", "reddit"),
            "url": url}


async def _reddit_test(rec: dict) -> Dict[str, Any]:
    t0 = time.time()
    try:
        token = await _reddit_token(rec)
        hdrs = _reddit_headers(rec, token)
        who = await _get_json("https://oauth.reddit.com/api/v1/me", hdrs) \
            if _cfg(rec, "username") else {"app": "read-only"}
        return {"ok": True, "latency_ms": round((time.time() - t0) * 1000, 1),
                "info": f"authenticated ({'user: ' + who.get('name','') if who.get('name') else 'app-only'})"}
    except Exception as e:
        return {"ok": False, "latency_ms": round((time.time() - t0) * 1000, 1),
                "error": str(e)[:200]}


# ═════════════════════════════════════════════════════════════════════════════
#  DRIVER: github
# ═════════════════════════════════════════════════════════════════════════════
def _gh_headers(rec: dict) -> dict:
    h = {"Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "vera/1.0"}
    tok = _secret(rec, "token")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


async def _github_search(rec: dict, query: str, limit: int) -> List[Dict[str, Any]]:
    data = await _get_json("https://api.github.com/search/repositories",
                           _gh_headers(rec),
                           params={"q": query, "per_page": min(limit, 30)})
    out: List[Dict[str, Any]] = []
    for it in (data.get("items", []) or [])[:limit]:
        out.append({"url": it.get("html_url", ""), "title": it.get("full_name", ""),
                    "snippet": (it.get("description", "") or "") +
                               f"  ★{it.get('stargazers_count',0)} · {it.get('language') or ''}",
                    "engine": "github"})
    return out


async def _github_fetch(rec: dict, url: str) -> Optional[Dict[str, Any]]:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    hdrs = _gh_headers(rec)
    try:
        meta = await _get_json(f"https://api.github.com/repos/{owner}/{repo}", hdrs)
    except Exception:
        return None
    body = [f"# {meta.get('full_name','')}", meta.get("description", "") or "",
            f"★{meta.get('stargazers_count',0)} · forks {meta.get('forks_count',0)} · "
            f"{meta.get('language') or ''} · {meta.get('open_issues_count',0)} open issues"]
    # Specific issue / PR
    if len(parts) >= 4 and parts[2] in ("issues", "pull"):
        num = parts[3]
        kind = "issues" if parts[2] == "issues" else "pulls"
        try:
            iss = await _get_json(f"https://api.github.com/repos/{owner}/{repo}/{kind}/{num}", hdrs)
            body = [f"# {iss.get('title','')} (#{num})", f"state: {iss.get('state','')}",
                    iss.get("body", "") or ""]
        except Exception:
            pass
    else:
        try:
            readme = await _get_json(
                f"https://api.github.com/repos/{owner}/{repo}/readme",
                {**hdrs, "Accept": "application/vnd.github.raw+json"})
            if isinstance(readme, dict) and readme.get("content"):
                import base64
                body.append(base64.b64decode(readme["content"]).decode("utf-8", "ignore"))
        except Exception:
            pass
    text = "\n\n".join(x for x in body if x).strip()
    if not text:
        return None
    return {"text": text[:_wc.MAX_PAGE_CHARS], "title": meta.get("full_name", repo),
            "provider": rec.get("id", "github"), "url": url}


async def _github_test(rec: dict) -> Dict[str, Any]:
    t0 = time.time()
    try:
        d = await _get_json("https://api.github.com/rate_limit", _gh_headers(rec))
        core = d.get("resources", {}).get("core", {})
        return {"ok": True, "latency_ms": round((time.time() - t0) * 1000, 1),
                "info": f"rate limit {core.get('remaining','?')}/{core.get('limit','?')}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ═════════════════════════════════════════════════════════════════════════════
#  DRIVER: hackernews (Algolia — no auth)
# ═════════════════════════════════════════════════════════════════════════════
async def _hn_search(rec: dict, query: str, limit: int) -> List[Dict[str, Any]]:
    data = await _get_json("https://hn.algolia.com/api/v1/search",
                           {"User-Agent": "vera/1.0"},
                           params={"query": query, "hitsPerPage": min(limit, 30),
                                   "tags": "story"})
    out: List[Dict[str, Any]] = []
    for h in (data.get("hits", []) or [])[:limit]:
        oid = h.get("objectID", "")
        out.append({"url": h.get("url") or f"https://news.ycombinator.com/item?id={oid}",
                    "title": h.get("title", "") or h.get("story_title", ""),
                    "snippet": ((h.get("story_text") or h.get("comment_text") or "")[:200] +
                                f"  ▲{h.get('points',0)} · {h.get('num_comments',0)} comments"),
                    "engine": "hackernews"})
    return out


async def _hn_fetch(rec: dict, url: str) -> Optional[Dict[str, Any]]:
    from urllib.parse import parse_qs
    qs = parse_qs(urlparse(url).query)
    item_id = (qs.get("id") or [""])[0]
    if not item_id:
        return None
    data = await _get_json(f"https://hn.algolia.com/api/v1/items/{item_id}",
                           {"User-Agent": "vera/1.0"})
    parts: List[str] = []
    title = data.get("title", "") or "Hacker News item"
    if data.get("title"):
        parts.append(f"# {data['title']}")
    if data.get("text"):
        parts.append(_wc.html_to_text(data["text"]))

    def _walk(node, depth=0):
        for ch in (node.get("children") or []):
            txt = _wc.html_to_text(ch.get("text", "") or "")
            if txt:
                parts.append(("  " * depth) + f"• {ch.get('author','')}: {txt}")
            _walk(ch, depth + 1)
    _walk(data)
    text = "\n".join(parts).strip()
    if not text:
        return None
    return {"text": text[:_wc.MAX_PAGE_CHARS], "title": title,
            "provider": rec.get("id", "hackernews"), "url": url}


async def _hn_test(rec: dict) -> Dict[str, Any]:
    try:
        await _get_json("https://hn.algolia.com/api/v1/search",
                        {"User-Agent": "vera/1.0"}, params={"query": "test", "hitsPerPage": 1})
        return {"ok": True, "info": "Algolia HN API reachable (no auth needed)"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ═════════════════════════════════════════════════════════════════════════════
#  DRIVER: generic REST
# ═════════════════════════════════════════════════════════════════════════════
def _dig(obj: Any, path: str) -> Any:
    if not path:
        return obj
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit():
            obj = obj[int(part)] if int(part) < len(obj) else None
        else:
            return None
    return obj


def _rest_headers(rec: dict) -> dict:
    h = {"User-Agent": "vera/1.0", "Accept": "application/json"}
    name = _cfg(rec, "auth_header")
    val = _secret(rec, "auth_value")
    if name and val:
        h[name] = val
    return h


async def _rest_search(rec: dict, query: str, limit: int) -> List[Dict[str, Any]]:
    base = _cfg(rec, "base_url").rstrip("/")
    path = _cfg(rec, "search_path")
    if not base or not path:
        return []
    path = path.replace("{query}", quote_plus(query)).replace("{limit}", str(limit))
    data = await _get_json(base + path, _rest_headers(rec))
    items = _dig(data, _cfg(rec, "results_path")) or []
    if not isinstance(items, list):
        return []
    mu, mt, ms = _cfg(rec, "map_url", "url"), _cfg(rec, "map_title", "title"), _cfg(rec, "map_snippet", "snippet")
    out: List[Dict[str, Any]] = []
    for it in items[:limit]:
        if not isinstance(it, dict):
            continue
        out.append({"url": str(_dig(it, mu) or ""), "title": str(_dig(it, mt) or ""),
                    "snippet": str(_dig(it, ms) or "")[:300], "engine": rec.get("id", "rest")})
    return [r for r in out if r["url"] or r["title"]]


async def _rest_fetch(rec: dict, url: str) -> Optional[Dict[str, Any]]:
    base = _cfg(rec, "base_url").rstrip("/")
    fpath = _cfg(rec, "fetch_path")
    if not base or not fpath:
        return None
    fpath = fpath.replace("{url}", quote_plus(url))
    data = await _get_json(base + fpath, _rest_headers(rec))
    text = json.dumps(data, indent=2) if not isinstance(data, str) else data
    if not text:
        return None
    return {"text": text[:_wc.MAX_PAGE_CHARS], "title": urlparse(url).path,
            "provider": rec.get("id", "rest"), "url": url}


async def _rest_test(rec: dict) -> Dict[str, Any]:
    try:
        res = await _rest_search(rec, "test", 1)
        return {"ok": True, "info": f"search returned {len(res)} result(s)"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ─────────────────────────────────────────────────────────────────────────────
#  Driver dispatch table
# ─────────────────────────────────────────────────────────────────────────────
_DRIVERS = {
    "reddit":     {"search": _reddit_search, "fetch": _reddit_fetch, "test": _reddit_test},
    "github":     {"search": _github_search, "fetch": _github_fetch, "test": _github_test},
    "hackernews": {"search": _hn_search,     "fetch": _hn_fetch,     "test": _hn_test},
    "rest":       {"search": _rest_search,   "fetch": _rest_fetch,   "test": _rest_test},
}


# ═════════════════════════════════════════════════════════════════════════════
#  INTEGRATION HOOKS  (called by web_client.fetch_page and web.search)
# ═════════════════════════════════════════════════════════════════════════════
async def fetch_for_url(url: str) -> Optional[Dict[str, Any]]:
    """
    web_client hook: if an enabled provider covers this URL's host, return its
    API content {text, title, provider, url}; else None (scrape normally).
    """
    host = urlparse(url).netloc.lower()
    if not host:
        return None
    for rec in (await _stored()).values():
        if not rec.get("enabled", True):
            continue
        drv = _DRIVERS.get(rec.get("kind", ""))
        if not drv or not drv.get("fetch"):
            continue
        if _domain_matches(host, _domains(rec)):
            try:
                return await drv["fetch"](rec, url)
            except Exception as e:
                log.debug("web_api fetch_for_url %s via %s: %s", url[:80], rec.get("id"), e)
                return None
    return None


async def search_for_query(query: str, limit: int, platform: str = ""
                           ) -> Tuple[List[Dict[str, Any]], str, str]:
    """
    web.search hook. Decide whether a platform API should handle this query and,
    if so, run its search. Returns (results, engine_id, cleaned_query) where
    cleaned_query has any `site:` / platform tokens stripped for the general
    engines. (results, "", "") when no platform applies.
    """
    q_lc = query.lower()
    providers = await _stored()

    def _match() -> Optional[dict]:
        # 1. Explicit platform= wins.
        if platform:
            rec = providers.get(platform)
            if rec and rec.get("enabled", True):
                return rec
            for rec in providers.values():           # match by kind too
                if rec.get("kind") == platform and rec.get("enabled", True):
                    return rec
        # 2. site:<domain> filter.
        import re as _re
        m = _re.search(r"site:([^\s]+)", q_lc)
        if m:
            host = m.group(1).lstrip(".")
            for rec in providers.values():
                if rec.get("enabled", True) and _domain_matches(host, _domains(rec)):
                    return rec
        # 3. trigger keyword.
        for rec in providers.values():
            if not rec.get("enabled", True):
                continue
            if any(kw in q_lc for kw in _keywords(rec)):
                if _DRIVERS.get(rec.get("kind", ""), {}).get("search"):
                    return rec
        return None

    rec = _match()
    if not rec:
        return [], "", ""
    drv = _DRIVERS.get(rec.get("kind", ""))
    if not drv or not drv.get("search"):
        return [], "", ""

    # Strip site:/platform tokens + trigger keywords from the query for both the
    # platform search and (via cleaned) the general engines.
    import re as _re
    cleaned = _re.sub(r"site:[^\s]+", "", query)
    for kw in _keywords(rec):
        # Only strip plain-word triggers ("reddit", "hacker news"). Structural
        # tokens like "r/" carry meaning (subreddit scoping) — leave them in.
        if not kw.replace(" ", "").isalnum():
            continue
        cleaned = _re.sub(r"\b" + _re.escape(kw) + r"\b", "", cleaned, flags=_re.I)
    cleaned = _re.sub(r"\s{2,}", " ", cleaned).strip() or query

    try:
        results = await drv["search"](rec, cleaned, limit)
        return results, rec.get("id", rec.get("kind", "api")), cleaned
    except Exception as e:
        log.debug("web_api search_for_query via %s: %s", rec.get("id"), e)
        return [], "", ""


# Register the fetch hook with web_client so web.fetch / crawls switch over.
try:
    _wc.set_api_hook(fetch_for_url)
except Exception as e:  # pragma: no cover
    log.warning("web_api: could not register fetch hook: %s", e)


# ═════════════════════════════════════════════════════════════════════════════
#  CAPABILITIES
# ═════════════════════════════════════════════════════════════════════════════
@capability("web.api.drivers", memory="off", silent=True,
            http_method="GET", http_path="/web/api/drivers", http_tags=["web", "api"],
            description="List the available platform-API driver templates and the "
                        "config/secret fields each one needs, for building the "
                        "'add provider' form. Output: {drivers:[{kind,label,blurb,"
                        "domains,keywords,fields:[{key,label,secret}]}]}.")
async def cap_web_api_drivers(trace_id=None) -> Dict:
    return {"drivers": [{"kind": k, **{kk: vv for kk, vv in meta.items()}}
                        for k, meta in _DRIVERS_META.items()]}


@capability("web.api.list", memory="off", silent=True,
            http_method="GET", http_path="/web/api/list", http_tags=["web", "api"],
            description="List configured platform-API providers (Reddit, GitHub, "
                        "Hacker News, custom). Secrets are redacted to has_secret "
                        "flags. Output: {providers:[...]}.")
async def cap_web_api_list(trace_id=None) -> Dict:
    return {"providers": [_redact(rec) for rec in (await _stored()).values()]}


@capability("web.api.save", memory="off",
            http_method="POST", http_path="/web/api/save", http_tags=["web", "api"],
            description="Create/update a platform-API provider. Inputs: id (str!), "
                        "kind ('reddit'|'github'|'hackernews'|'rest'), label, "
                        "enabled (bool), domains (list[str] — hosts to route via "
                        "this API), keywords (list[str] — query triggers), "
                        "config (dict of non-secret fields), secrets (dict of "
                        "secret fields; blank value keeps the stored one). "
                        "Output: {ok, provider}.")
async def cap_web_api_save(id: str = "", kind: str = "", label: str = "",
                           enabled: bool = True, domains: Optional[List[str]] = None,
                           keywords: Optional[List[str]] = None,
                           config: Optional[Dict[str, Any]] = None,
                           secrets: Optional[Dict[str, str]] = None,
                           trace_id=None) -> Dict:
    if not id:
        return {"error": "id required"}
    r = _redis()
    if not r:
        return {"error": "redis unavailable"}
    existing = (await _stored()).get(id, {})
    kind = kind or existing.get("kind", "")
    if kind not in _DRIVERS_META:
        return {"error": f"unknown driver kind: {kind}"}
    rec = {
        "id": id, "kind": kind,
        "label": label or existing.get("label") or _DRIVERS_META[kind]["label"],
        "enabled": bool(enabled),
        "domains": domains if domains is not None else existing.get("domains", []),
        "keywords": keywords if keywords is not None else existing.get("keywords", []),
        "config": {**(existing.get("config") or {}), **(config or {})},
        "secrets": dict(existing.get("secrets") or {}),
    }
    # Seal only non-blank secret fields; blank keeps the stored value.
    for k, v in (secrets or {}).items():
        if v:
            try:
                rec["secrets"][k] = vsecrets.seal(v)
            except Exception as e:
                return {"error": f"could not seal {k}: {e}"}
    try:
        await r.hset(KEY_PROVIDERS, id, json.dumps(rec))
    except Exception as e:
        return {"error": str(e)}
    _TOKENS.pop(id, None)   # creds may have changed — drop any cached token
    await emit_event({"type": "web.api.saved", "provider": id, "kind": kind})
    return {"ok": True, "provider": _redact(rec)}


@capability("web.api.delete", memory="off",
            http_method="POST", http_path="/web/api/delete", http_tags=["web", "api"],
            description="Delete a platform-API provider. Input: id (str!). Output: {ok}.")
async def cap_web_api_delete(id: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r or not id:
        return {"error": "id required"}
    try:
        await r.hdel(KEY_PROVIDERS, id)
    except Exception as e:
        return {"error": str(e)}
    _TOKENS.pop(id, None)
    await emit_event({"type": "web.api.deleted", "provider": id})
    return {"ok": True}


@capability("web.api.test", memory="off",
            http_method="POST", http_path="/web/api/test", http_tags=["web", "api"],
            description="Test a provider's credentials/connectivity. Input: id "
                        "(str!). Output: {ok, latency_ms, info, error}.")
async def cap_web_api_test(id: str = "", trace_id=None) -> Dict:
    rec = await _get(id)
    if not rec:
        return {"ok": False, "error": f"unknown provider: {id}"}
    drv = _DRIVERS.get(rec.get("kind", ""))
    if not drv or not drv.get("test"):
        return {"ok": False, "error": "driver has no test"}
    return await drv["test"](rec)


@capability("web.api.search", memory="off",
            http_method="POST", http_path="/web/api/search", http_tags=["web", "api"],
            description="Search directly through one platform-API provider. Inputs: "
                        "id (str!), query (str!), limit (int default 8). "
                        "Output: {results:[{url,title,snippet,engine}], count}.")
async def cap_web_api_search(id: str = "", query: str = "", limit: int = 8,
                             trace_id=None) -> Dict:
    rec = await _get(id)
    if not rec:
        return {"error": f"unknown provider: {id}"}
    drv = _DRIVERS.get(rec.get("kind", ""))
    if not drv or not drv.get("search"):
        return {"error": "driver has no search"}
    if not query.strip():
        return {"error": "query required"}
    try:
        results = await drv["search"](rec, query, max(1, min(30, int(limit))))
        return {"results": results, "count": len(results), "provider": id}
    except Exception as e:
        return {"error": str(e)[:200], "provider": id}


@capability("web.api.fetch", memory="off",
            http_method="POST", http_path="/web/api/fetch", http_tags=["web", "api"],
            description="Fetch one URL through the matching platform-API provider "
                        "(or a named one). Inputs: url (str!), id (str — optional, "
                        "else auto-matched by domain). Output: {text, title, "
                        "provider, url} or {error}.")
async def cap_web_api_fetch(url: str = "", id: str = "", trace_id=None) -> Dict:
    if not url.strip():
        return {"error": "url required"}
    if id:
        rec = await _get(id)
        if not rec:
            return {"error": f"unknown provider: {id}"}
        drv = _DRIVERS.get(rec.get("kind", ""))
        if not drv or not drv.get("fetch"):
            return {"error": "driver has no fetch"}
        try:
            res = await drv["fetch"](rec, url)
            return res or {"error": "provider returned no content", "url": url}
        except Exception as e:
            return {"error": str(e)[:200], "url": url}
    res = await fetch_for_url(url)
    return res or {"error": "no platform API matched this URL", "url": url}


# ─────────────────────────────────────────────────────────────────────────────
#  PANEL
# ─────────────────────────────────────────────────────────────────────────────
@APP.get("/web/api/panel", include_in_schema=False)
async def _web_api_panel_html():
    from fastapi.responses import HTMLResponse
    if _PANEL_PATH.exists():
        return HTMLResponse(_PANEL_PATH.read_text(encoding="utf-8"))
    return HTMLResponse("<p style='color:#c96b6b'>web_api_panel.html not found</p>",
                        status_code=404)


try:
    from Vera.vera.capability_orchestration import register_ui
    register_ui(
        "web_api",
        "Web APIs",
        "🔌",
        """<div id="webapi-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/web/api/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#181614)"
          allow="clipboard-read; clipboard-write"></iframe>
</div>""",
        "",
        ui_caps=["web.api.list", "web.api.drivers", "web.api.save", "web.api.delete",
                 "web.api.test", "web.api.search", "web.api.fetch"],
        mode="inject",
    )
except Exception as e:  # pragma: no cover
    log.debug("web_api register_ui: %s", e)
