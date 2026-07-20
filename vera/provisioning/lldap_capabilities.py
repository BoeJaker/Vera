"""
lldap_capabilities.py — Identity directory (lldap) management
=============================================================

lldap is Vera's recommended lightweight identity directory (users + groups that
container-apps bind to for login). It pairs with step-ca (PKI + SSH certs) and
is deployed as a container from Provision → Security (secprov.deploy service=
lldap). This module lets the Identity panel MANAGE that directory — list/create
users & groups, manage membership — instead of only linking out to lldap's own
web admin.

It auto-discovers the deployed instance: the secprov deploy record
(`vera:provisioning:security`) carries the lldap host address and the generated
admin password (sealed, Fernet). So once you've deployed lldap, this connects
with no extra configuration. A manual override (external lldap) can be saved in
`vera:provisioning:lldap`.

Capabilities (group `identity.lldap.*`)
───────────────────────────────────────
  identity.lldap.config (.save)  — manual connection override (admin pw sealed)
  identity.lldap.status          — auto-discover + login probe + counts
  identity.lldap.users           — list users
  identity.lldap.groups          — list groups
  identity.lldap.user.create     — create a user (optionally into a group)
  identity.lldap.group.create    — create a group
  identity.lldap.member.add      — add a user to a group
  identity.lldap.user.delete     — delete a user

lldap API: POST {url}/auth/simple/login {username,password} → {token}; then
GraphQL at {url}/api/graphql with `Authorization: Bearer <token>`. Password
setting uses lldap's OPAQUE flow (not a plain REST call) — the panel links to
lldap's own admin for that; here we manage identities + membership.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, emit_event

try:
    from Vera.vera.security import secrets as vsecrets
except Exception:                                    # pragma: no cover
    vsecrets = None                                  # type: ignore

log = logging.getLogger("vera.identity.lldap")

KEY_SEC = "vera:provisioning:security"       # secprov deploy records
KEY_LLDAP = "vera:provisioning:lldap"        # manual override (admin pw sealed)


def _redis():
    return getattr(_orch, "REDIS", None)


def _open(v: str) -> str:
    if v and vsecrets is not None:
        try:
            return vsecrets.open_secret(v)
        except Exception:
            return v
    return v


def _seal(v: str) -> str:
    if v and vsecrets is not None:
        try:
            return vsecrets.seal(v)
        except Exception:
            return v
    return v


async def _manual_cfg() -> Dict[str, Any]:
    r = _redis()
    if not r:
        return {}
    try:
        raw = await r.hget(KEY_LLDAP, "main")
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


async def _discover(host_id: str = "local") -> Optional[Dict[str, str]]:
    """Resolve {url, user, password} for the lldap directory. Priority: manual
    override → the secprov deploy record for this host → any lldap record."""
    manual = await _manual_cfg()
    if manual.get("url"):
        return {"url": manual["url"].rstrip("/"),
                "user": manual.get("user", "admin"),
                "password": _open(manual.get("password_sealed", ""))}
    r = _redis()
    if not r:
        return None
    try:
        rows = await r.hgetall(KEY_SEC) or {}
    except Exception:
        rows = {}
    # Prefer the requested host; fall back to any lldap deployment.
    chosen = None
    for field, blob in rows.items():
        fkey = field.decode() if isinstance(field, bytes) else field
        if not fkey.endswith(":lldap"):
            continue
        try:
            rec = json.loads(blob)
        except Exception:
            continue
        if fkey == f"{host_id}:lldap":
            chosen = rec
            break
        chosen = chosen or rec
    if not chosen:
        return None
    addr = chosen.get("addr") or "localhost"
    # admin web/API port (17170) — first published port for lldap
    port = "17170"
    for h in (chosen.get("ports") or {}):
        if str(chosen["ports"][h]) == "17170" or str(h) == "17170":
            port = str(h)
            break
    pw = ""
    sealed = (chosen.get("sealed") or {}).get("admin_password", "")
    if sealed:
        pw = _open(sealed)
    return {"url": f"http://{addr}:{port}", "user": "admin", "password": pw}


async def _login(conn: Dict[str, str]) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(conn["url"] + "/auth/simple/login",
                             json={"username": conn["user"],
                                   "password": conn["password"]})
            if r.status_code == 200:
                return (r.json() or {}).get("token", "")
    except Exception as e:
        log.debug("lldap login: %s", e)
    return None


async def _gql(conn: Dict[str, str], token: str, query: str,
               variables: Optional[Dict] = None) -> Dict:
    async with httpx.AsyncClient(timeout=12) as c:
        r = await c.post(conn["url"] + "/api/graphql",
                         headers={"Authorization": f"Bearer {token}"},
                         json={"query": query, "variables": variables or {}})
        try:
            body = r.json()
        except Exception:
            return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
        if body.get("errors"):
            return {"error": "; ".join(e.get("message", "?")
                                       for e in body["errors"])}
        return body.get("data", {})


async def _connect(host_id: str) -> Any:
    """→ (conn, token) or {'error': ...}."""
    conn = await _discover(host_id)
    if not conn:
        return {"error": "no lldap directory found — deploy it from Provision → "
                "Security, or save a manual connection (identity.lldap.config.save)"}
    if not conn.get("password"):
        return {"error": "lldap admin password unavailable — redeploy lldap or "
                "set it via identity.lldap.config.save"}
    token = await _login(conn)
    if not token:
        return {"error": f"lldap login failed at {conn['url']} — is it running "
                "and reachable from Vera?"}
    return conn, token


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────
@capability(
    "identity.lldap.config.save",
    http_method="POST", http_path="/identity/lldap/config/save", http_tags=["identity"],
    memory="off",
    description="Save a manual lldap connection override (for an lldap that Vera "
                "didn't deploy). Normally unnecessary — the deployed instance is "
                "auto-discovered. Inputs: url (str! — http://host:17170), user "
                "(str='admin'), password (str — sealed at rest; blank keeps "
                "existing). Output: {ok}.",
)
async def cap_lldap_config_save(url: str = "", user: str = "admin",
                                password: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"error": "redis unavailable"}
    cur = await _manual_cfg()
    cfg = {"url": url.rstrip("/") or cur.get("url", ""),
           "user": user or cur.get("user", "admin"),
           "password_sealed": _seal(password) if password
           else cur.get("password_sealed", "")}
    await r.hset(KEY_LLDAP, "main", json.dumps(cfg))
    return {"ok": True}


@capability(
    "identity.lldap.status",
    http_method="GET", http_path="/identity/lldap/status", http_tags=["identity"],
    memory="off", silent=True,
    description="Auto-discover the lldap directory (from the secprov deploy record "
                "or a manual override), log in, and report reachability + counts. "
                "Input: host_id (str='local'). Output: {deployed, reachable, url, "
                "users, groups, admin_url}.",
)
async def cap_lldap_status(host_id: str = "local", trace_id=None) -> Dict:
    conn = await _discover(host_id)
    if not conn:
        return {"deployed": False, "reachable": False}
    token = await _login(conn) if conn.get("password") else None
    if not token:
        return {"deployed": True, "reachable": False, "url": conn["url"],
                "admin_url": conn["url"]}
    users = await _gql(conn, token, "{ users { id } }")
    groups = await _gql(conn, token, "{ groups { id } }")
    return {"deployed": True, "reachable": True, "url": conn["url"],
            "admin_url": conn["url"],
            "users": len(users.get("users", []) or []),
            "groups": len(groups.get("groups", []) or [])}


@capability(
    "identity.lldap.users",
    http_method="GET", http_path="/identity/lldap/users", http_tags=["identity"],
    memory="off", silent=True,
    description="List directory users. Input: host_id (str='local'). Output: "
                "{users:[{id,email,display_name,creation_date}]}.",
)
async def cap_lldap_users(host_id: str = "local", trace_id=None) -> Dict:
    res = await _connect(host_id)
    if isinstance(res, dict):
        return res
    conn, token = res
    data = await _gql(conn, token,
                      "{ users { id email displayName creationDate } }")
    if data.get("error"):
        return data
    return {"users": [{"id": u.get("id"), "email": u.get("email"),
                       "display_name": u.get("displayName"),
                       "creation_date": u.get("creationDate")}
                      for u in data.get("users", [])]}


@capability(
    "identity.lldap.groups",
    http_method="GET", http_path="/identity/lldap/groups", http_tags=["identity"],
    memory="off", silent=True,
    description="List directory groups. Input: host_id (str='local'). Output: "
                "{groups:[{id,display_name}]}.",
)
async def cap_lldap_groups(host_id: str = "local", trace_id=None) -> Dict:
    res = await _connect(host_id)
    if isinstance(res, dict):
        return res
    conn, token = res
    data = await _gql(conn, token, "{ groups { id displayName } }")
    if data.get("error"):
        return data
    return {"groups": [{"id": g.get("id"), "display_name": g.get("displayName")}
                       for g in data.get("groups", [])]}


@capability(
    "identity.lldap.user.create",
    http_method="POST", http_path="/identity/lldap/user/create", http_tags=["identity"],
    memory="off",
    description="Create a directory user (optionally add to a group). Password is "
                "NOT set here — lldap uses an OPAQUE flow; set it in lldap's admin "
                "or via a reset. Inputs: id (str! — login), email (str!), "
                "display_name (str), first_name (str), last_name (str), group_id "
                "(int — optional group to join). host_id (str='local'). Output: "
                "{ok, id, added_to_group}.",
)
async def cap_lldap_user_create(id: str = "", email: str = "",
                                display_name: str = "", first_name: str = "",
                                last_name: str = "", group_id: Optional[int] = None,
                                host_id: str = "local", trace_id=None) -> Dict:
    if not id or not email:
        return {"error": "id and email required"}
    res = await _connect(host_id)
    if isinstance(res, dict):
        return res
    conn, token = res
    user_in: Dict[str, Any] = {"id": id, "email": email}
    if display_name:
        user_in["displayName"] = display_name
    if first_name:
        user_in["firstName"] = first_name
    if last_name:
        user_in["lastName"] = last_name
    data = await _gql(
        conn, token,
        "mutation($u: CreateUserInput!) { createUser(user: $u) { id } }",
        {"u": user_in})
    if data.get("error"):
        return data
    added = None
    if group_id is not None:
        g = await _gql(conn, token,
                       "mutation($u:String!,$g:Int!){ addUserToGroup(userId:$u, "
                       "groupId:$g){ ok } }", {"u": id, "g": int(group_id)})
        added = None if g.get("error") else int(group_id)
    await emit_event({"type": "identity.lldap.user.created", "id": id})
    return {"ok": True, "id": id, "added_to_group": added}


@capability(
    "identity.lldap.group.create",
    http_method="POST", http_path="/identity/lldap/group/create", http_tags=["identity"],
    memory="off",
    description="Create a directory group. Inputs: name (str!), host_id "
                "(str='local'). Output: {ok, id}.",
)
async def cap_lldap_group_create(name: str = "", host_id: str = "local",
                                 trace_id=None) -> Dict:
    if not name:
        return {"error": "name required"}
    res = await _connect(host_id)
    if isinstance(res, dict):
        return res
    conn, token = res
    data = await _gql(conn, token,
                      "mutation($n:String!){ createGroup(name:$n){ id } }",
                      {"n": name})
    if data.get("error"):
        return data
    await emit_event({"type": "identity.lldap.group.created", "name": name})
    return {"ok": True, "id": (data.get("createGroup") or {}).get("id")}


@capability(
    "identity.lldap.member.add",
    http_method="POST", http_path="/identity/lldap/member/add", http_tags=["identity"],
    memory="off",
    description="Add a user to a group. Inputs: user_id (str!), group_id (int!), "
                "host_id (str='local'). Output: {ok}.",
)
async def cap_lldap_member_add(user_id: str = "", group_id: Optional[int] = None,
                               host_id: str = "local", trace_id=None) -> Dict:
    if not user_id or group_id is None:
        return {"error": "user_id and group_id required"}
    res = await _connect(host_id)
    if isinstance(res, dict):
        return res
    conn, token = res
    data = await _gql(conn, token,
                      "mutation($u:String!,$g:Int!){ addUserToGroup(userId:$u, "
                      "groupId:$g){ ok } }", {"u": user_id, "g": int(group_id)})
    if data.get("error"):
        return data
    return {"ok": True}


@capability(
    "identity.lldap.user.delete",
    http_method="POST", http_path="/identity/lldap/user/delete", http_tags=["identity"],
    memory="off",
    description="Delete a directory user. Inputs: user_id (str!), host_id "
                "(str='local'). Output: {ok}.",
)
async def cap_lldap_user_delete(user_id: str = "", host_id: str = "local",
                                trace_id=None) -> Dict:
    if not user_id:
        return {"error": "user_id required"}
    res = await _connect(host_id)
    if isinstance(res, dict):
        return res
    conn, token = res
    data = await _gql(conn, token,
                      "mutation($u:String!){ deleteUser(userId:$u){ ok } }",
                      {"u": user_id})
    if data.get("error"):
        return data
    await emit_event({"type": "identity.lldap.user.deleted", "id": user_id})
    return {"ok": True}


log.info("lldap_capabilities ready — identity.lldap.* (directory management)")
