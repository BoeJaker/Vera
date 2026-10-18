"""Which parts of a fabric source are credentials, and what moving them into the Platforms store looks like. Pure.

Owner 2026-10-11: every credential lives in the store (platform.secrets). A source keeps an "@secret:<key>"
reference in its headers or auth instead of the value; fabric resolves it at pull time. A key written into the URL
itself (?api_key=...) cannot be moved this way - that source wants an API connection - so it is reported, not changed.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List
from urllib.parse import parse_qsl, urlsplit

SECRET_REF = "@secret:"
ENV_REF_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")

# header / auth / query names that carry a credential
SECRET_NAME_RE = re.compile(r"(?i)^(authorization|proxy-authorization|cookie|x-[a-z0-9-]*(key|token|secret|auth)[a-z0-9-]*|"
                            r".*(api[-_]?key|apikey|access[-_]?token|token|secret|password|passwd|subscription)[a-z0-9_-]*)$")
NOT_SECRET = {"username", "user", "login", "email", "account", "token_type", "auth_type", "key_id"}


def _as_dict(v: Any) -> Dict[str, Any]:
    if isinstance(v, dict):
        return dict(v)
    if isinstance(v, str) and v.strip():
        try:
            d = json.loads(v)
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}
    return {}


def is_secret_name(name: str) -> bool:
    n = str(name or "").strip()
    return bool(n) and n.lower() not in NOT_SECRET and bool(SECRET_NAME_RE.match(n))


def _literal(v: Any) -> bool:
    """A value worth moving: a non-empty string that is not already a reference or an ${ENV} placeholder."""
    if not isinstance(v, str):
        return False
    s = v.strip()
    if not s or SECRET_REF in s or ENV_REF_RE.match(s):
        return False
    return s.lower() not in ("bearer", "token", "basic")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")


def key_for(src: Dict[str, Any], where: str, name: str) -> str:
    """The store key a source's credential goes under: fabric_<label or id>_<field>, at most 64 characters."""
    base = _slug(src.get("label") or "")[:28] or _slug(str(src.get("id") or ""))[:12] or "source"
    return f"fabric_{base}_{_slug(name)}"[:64].rstrip("_")


def plan(src: Dict[str, Any]) -> Dict[str, Any]:
    """{headers: {name: key}, auth: {name: key}, url_params: [names]} - what would move, and what cannot."""
    headers = {k: key_for(src, "h", k) for k, v in _as_dict(src.get("headers")).items()
               if is_secret_name(k) and _literal(v)}
    auth = {k: key_for(src, "a", k) for k, v in _as_dict(src.get("auth")).items()
            if is_secret_name(k) and _literal(v)}
    try:
        q = parse_qsl(urlsplit(str(src.get("url") or "")).query, keep_blank_values=False)
    except ValueError:
        q = []
    url_params = sorted({k for k, v in q if is_secret_name(k) and _literal(v)})
    return {"headers": headers, "auth": auth, "url_params": url_params}


def values(src: Dict[str, Any], p: Dict[str, Any]) -> List[Dict[str, str]]:
    """[{key, value, label}] to store for plan `p`. A "Bearer <token>" header stores only the token."""
    h, a, out = _as_dict(src.get("headers")), _as_dict(src.get("auth")), []
    for name, key in p.get("headers", {}).items():
        v = str(h[name]).strip()
        scheme, _, rest = v.partition(" ")
        if scheme.lower() in ("bearer", "token", "basic") and rest.strip():
            v = rest.strip()
        out.append({"key": key, "value": v, "label": f"{src.get('label') or src.get('id')} - header {name}"})
    for name, key in p.get("auth", {}).items():
        out.append({"key": key, "value": str(a[name]).strip(),
                    "label": f"{src.get('label') or src.get('id')} - auth {name}"})
    return out


def apply(src: Dict[str, Any], p: Dict[str, Any]) -> Dict[str, Any]:
    """The source with the planned values replaced by references (headers keep a "Bearer " prefix)."""
    out = dict(src)
    if p.get("headers"):
        h = _as_dict(src.get("headers"))
        for name, key in p["headers"].items():
            scheme, _, rest = str(h[name]).strip().partition(" ")
            prefix = scheme + " " if scheme.lower() in ("bearer", "token", "basic") and rest.strip() else ""
            h[name] = prefix + SECRET_REF + key
        out["headers"] = json.dumps(h)
    if p.get("auth"):
        a = _as_dict(src.get("auth"))
        for name, key in p["auth"].items():
            a[name] = SECRET_REF + key
        out["auth"] = json.dumps(a)
    return out


def anything(p: Dict[str, Any]) -> bool:
    return bool(p.get("headers") or p.get("auth"))
