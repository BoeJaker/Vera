"""
redis_auth_capabilities.py — ACL users for the shared Redis, passwords in OpenBao
================================================================================

See redis_auth_core.py for the model. The order of operations matters, and each
cap is safe to call again:

  1. redis.auth.status      what exists: the ACL file, users, who is connected
                            as whom, which credentials OpenBao holds.
  2. redis.auth.ensure      create a user's password (OpenBao FIRST, then the
                            ACL user, then ACL SAVE; for `vera`, the local
                            sealed copy the host boots from). rotate=true adds a
                            new password BESIDE the old one.
  3. (restart / re-provision / reconfigure each client onto its user)
  4. redis.auth.retire_old  after a rotation, drop every password but the
                            current one.
  5. redis.auth.lock_default  give `default` (vikunja: password, no username)
                            its password - refused while anything unexpected
                            still connects as `default`, because it would be
                            locked out the moment this runs.

No capability returns a password. Operators read one from OpenBao
(secstore.kv.get vera/redis/users/<user>) when configuring a client by hand.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, emit_event
from Vera.vera.security import redis_auth_core as core
from Vera.vera.security import secrets as vsecrets

log = logging.getLogger("vera.security.redis_auth")


def _cap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("raw") or c.get("func") if c else None


def _r():
    return _orch.REDIS


def _s(v) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


async def _bao_get(user: str) -> Dict[str, Any]:
    get = _cap("secstore.kv.get")
    if not get:
        return {"error": "secstore.kv.get unavailable (provisioning module not loaded)"}
    return await get(path=core.BAO_PATH.format(user=user)) or {}


async def credential(user: str) -> str:
    """The user's current password from OpenBao, or '' (never exposed as a cap)."""
    res = await _bao_get(user)
    return str(((res or {}).get("data") or {}).get("password") or "") if res.get("ok") else ""


async def node_redis_url(url: str) -> str:
    """The Redis URL a node worker gets: the host's URL with the HOST's own
    credential removed and `vera-node`'s put in its place (unchanged when
    OpenBao holds no vera-node credential yet)."""
    bare = core.without_credentials(url)
    pw = await credential("vera-node")
    return core.with_credentials(bare, "vera-node", pw) if pw else bare


async def _acl_file() -> str:
    try:
        v = await _r().config_get("aclfile")
        return _s((v or {}).get("aclfile") or (v or {}).get(b"aclfile") or "")
    except Exception as e:
        return "?%s" % e


def _write_local_copy(user: str, password: str) -> str:
    path = core.local_file()
    token = vsecrets.seal(core.seal_payload(user, password), force_fernet=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    old = os.umask(0o077)
    try:
        with open(path + ".new", "w", encoding="ascii") as fh:
            fh.write(token)
        os.chmod(path + ".new", 0o600)
        os.replace(path + ".new", path)
    finally:
        os.umask(old)
    return path


@capability(
    "redis.auth.status",
    http_method="GET", http_path="/redis/auth/status", http_tags=["security"],
    memory="off", silent=True,
    description="State of Redis authentication (never a password): whether the ACL "
                "file persists users, each ACL user (enabled, nopass, password "
                "count), how many connections each user holds and which addresses "
                "are still on `default`, which users OpenBao holds a credential "
                "for, and whether the host's local sealed copy exists. Output: "
                "{aclfile, users[], clients{}, default_clients[], bao{}, local_copy}.",
)
async def cap_status(trace_id=None) -> Dict:
    r = _r()
    if r is None:
        return {"ok": False, "error": "redis not connected"}
    users = []
    for line in await r.execute_command("ACL", "LIST"):
        parts = _s(line).split()
        users.append({"user": parts[1], "on": "on" in parts, "nopass": "nopass" in parts,
                      "passwords": sum(1 for p in parts if p.startswith("#"))})
    counts: Dict[str, int] = {}
    default_clients = []
    for c in await r.client_list():
        u = c.get("user") or "default"
        counts[u] = counts.get(u, 0) + 1
        if u == "default":
            default_clients.append(c.get("addr"))
    bao = {}
    for u in core.USERS:
        res = await _bao_get(u)
        bao[u] = bool(res.get("ok")) if not res.get("error") else "error: %s" % res["error"]
    return {"ok": True, "aclfile": await _acl_file() or None, "users": users,
            "clients": counts, "default_clients": sorted(set(default_clients)),
            "bao": bao, "local_copy": os.path.isfile(core.local_file()),
            "redis_url_has_credentials": core.has_credentials(_orch.REDIS_URL)}


@capability(
    "redis.auth.ensure",
    http_method="POST", http_path="/redis/auth/ensure", http_tags=["security"],
    memory="off",
    description="Make sure each named Redis user exists with a password held in "
                "OpenBao (vera/redis/users/<user>). A missing credential is created "
                "(OpenBao first, then ACL SETUSER, then ACL SAVE); an existing one is "
                "re-applied. rotate=true writes a NEW password beside the old one "
                "(both work until redis.auth.retire_old). For `vera` it also writes "
                "the host's local sealed copy. Refuses without an ACL file (users "
                "would vanish on a Redis restart). Inputs: users (list — vera, "
                "vera-node, searxng, inspector, default), rotate (bool=false). "
                "Output: {ok, results:{user: created|kept|rotated|error}}.",
)
async def cap_ensure(users: Optional[List[str]] = None, rotate: bool = False,
                     trace_id=None) -> Dict:
    r = _r()
    if r is None:
        return {"ok": False, "error": "redis not connected"}
    want = [u for u in (users or []) if u]
    bad = [u for u in want if u not in core.USERS]
    if not want or bad:
        return {"ok": False, "error": "users must be from %s" % list(core.USERS),
                "unknown": bad}
    if not await _acl_file():
        return {"ok": False, "error": "Redis has no ACL file (start it with "
                "--aclfile) - users set now would vanish on its next restart"}
    put = _cap("secstore.kv.put")
    if not put:
        return {"ok": False, "error": "secstore.kv.put unavailable"}
    out: Dict[str, str] = {}
    for u in want:
        try:
            got = await _bao_get(u)
            if got.get("error"):
                out[u] = "error: openbao read: %s" % got["error"]
                continue
            cur = str(((got.get("data") or {}).get("password")) or "") if got.get("ok") else ""
            if cur and not rotate:
                pw, state = cur, "kept"
            else:
                pw, state = core.new_password(), ("rotated" if cur else "created")
                w = await put(path=core.BAO_PATH.format(user=u),
                              data={"password": pw, "user": u, "who": core.USERS[u]["who"]})
                if not (w or {}).get("ok"):
                    out[u] = "error: openbao write: %s" % (w or {}).get("error")
                    continue
            # `default` keeps nopass until lock_default; everyone else gets the
            # password now (ADDED - an old one keeps working until retire_old).
            if u != "default":
                await r.execute_command("ACL", "SETUSER", *core.setuser_args(u, add=[pw]))
            if u == "vera":
                _write_local_copy(u, pw)
            out[u] = state
        except Exception as e:
            out[u] = "error: %s" % e
    try:
        await r.execute_command("ACL", "SAVE")
    except Exception as e:
        return {"ok": False, "results": out, "error": "ACL SAVE failed: %s" % e}
    await emit_event({"type": "redis.auth.ensure", "results": out})
    return {"ok": not any(v.startswith("error") for v in out.values()), "results": out}


@capability(
    "redis.auth.retire_old",
    http_method="POST", http_path="/redis/auth/retire_old", http_tags=["security"],
    memory="off",
    description="After a rotation, keep only the user's CURRENT password (the one "
                "OpenBao holds) and drop the rest. Run it once every client of that "
                "user has moved to the new password. Input: user (str!). Output: {ok}.",
)
async def cap_retire_old(user: str = "", trace_id=None) -> Dict:
    if user not in core.USERS or user == "default":
        return {"ok": False, "error": "user must be one of %s (default: use lock_default)"
                % [u for u in core.USERS if u != "default"]}
    pw = await credential(user)
    if not pw:
        return {"ok": False, "error": "OpenBao holds no credential for %s" % user}
    await _r().execute_command("ACL", "SETUSER", *core.setuser_args(user, add=[pw], reset=True))
    await _r().execute_command("ACL", "SAVE")
    return {"ok": True, "user": user}


@capability(
    "redis.auth.lock_default",
    http_method="POST", http_path="/redis/auth/lock_default", http_tags=["security"],
    memory="off",
    description="Give the `default` Redis user a password (vikunja's - it can send a "
                "password but no username), ending anonymous access. REFUSES while any "
                "client other than allowed_addrs still connects as `default`, and names "
                "them: they would be locked out at once. Configure vikunja with the "
                "password first (a nopass user accepts any password, so it keeps "
                "working until this runs). Inputs: allowed_addrs (list — IPs expected "
                "on default, e.g. vikunja's container IP), force (bool=false). "
                "Output: {ok, unexpected[]}.",
)
async def cap_lock_default(allowed_addrs: Optional[List[str]] = None, force: bool = False,
                           trace_id=None) -> Dict:
    r = _r()
    if r is None:
        return {"ok": False, "error": "redis not connected"}
    if not await _acl_file():
        return {"ok": False, "error": "Redis has no ACL file"}
    plan = core.default_lock_plan(await r.client_list(), allowed_addrs or [])
    if not plan["ok"] and not force:
        return {"ok": False, "error": "clients still on the open default user",
                "unexpected": plan["unexpected"]}
    pw = await credential("default")
    if not pw:
        return {"ok": False, "error": "no credential for `default` in OpenBao - "
                "run redis.auth.ensure users=[default] first"}
    await r.execute_command("ACL", "SETUSER", *core.setuser_args("default", add=[pw], reset=True))
    await r.execute_command("ACL", "SAVE")
    await emit_event({"type": "redis.auth.lock_default", "forced": bool(force and not plan["ok"])})
    return {"ok": True, "unexpected": plan["unexpected"], "forced": bool(force and not plan["ok"])}


@capability(
    "redis.auth.export_env",
    http_method="POST", http_path="/redis/auth/export_env", http_tags=["security"],
    memory="off",
    description="Write one Redis user's password (from OpenBao) into a client's env "
                "file as VAR=<password> - for stack services configured through a "
                "compose .env (vikunja: VIKUNJA_REDIS_PASSWORD with user `default`; "
                "searxng: a password var its SEARXNG_REDIS_URL references). The file "
                "must be an existing .env under VERA_REDIS_EXPORT_ROOTS "
                "(default /home/boejaker/LLM_Stack); it is left 0600. The password is "
                "never returned. Recreate the service afterwards. Inputs: user (str!), "
                "path (str!), var (str!). Output: {ok, path, var, action}.",
)
async def cap_export_env(user: str = "", path: str = "", var: str = "",
                         trace_id=None) -> Dict:
    if user not in core.USERS:
        return {"ok": False, "error": "user must be one of %s" % list(core.USERS)}
    if not core.env_var_ok(var):
        return {"ok": False, "error": "var must be an UPPER_CASE env name"}
    ok, real = core.export_path_allowed(path, core.export_roots())
    if not ok:
        return {"ok": False, "error": real}
    if not os.path.isfile(real):
        return {"ok": False, "error": "no such file: %s" % real}
    pw = await credential(user)
    if not pw:
        return {"ok": False, "error": "OpenBao holds no credential for %s - run "
                "redis.auth.ensure first" % user}
    with open(real, "rb") as fh:
        text = fh.read().decode("utf-8")
    new, action = core.env_file_update(text, var, pw)
    tmp = real + ".vera-tmp"
    old = os.umask(0o077)
    try:
        with open(tmp, "wb") as fh:
            fh.write(new.encode("utf-8"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, real)
    finally:
        os.umask(old)
    await emit_event({"type": "redis.auth.export_env", "user": user, "var": var,
                      "path": real, "action": action})
    return {"ok": True, "path": real, "var": var, "action": action}


log.info("redis_auth_capabilities ready")
