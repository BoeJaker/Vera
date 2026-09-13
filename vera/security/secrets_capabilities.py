"""secrets.* - one secrets service over OpenBao and keydrop.

OpenBao is the store of record. vera/security/secrets.py seals into it once Vera
has its address and token, and named secrets (a netctl door token, a backup of
the SSH logins) live under <mount>/vera/named/. Keydrop is the way to a person:
anything someone needs is sealed to their public key in ~/.vera-keydrop, and no
process on the estate can read it back.

No capability here returns a secret's value. Vera's /mcp/call has no
authentication, so secrets go in, and out only to keydrop; Vera's own modules
read named secrets in-process through get_named().

  secrets.status    OpenBao's seal state and storage, Vera's token, keydrop, and
                    how many stored values are still sealed with the file key
  secrets.setup     a token limited to Vera's own paths, an audit log, and the
                    unseal key and root token handed to keydrop (dry run first)
  secrets.put / .list / .delete / .handoff    named secrets
  secrets.migrate   re-seal file-key values in Redis and the SSH login store into
                    OpenBao (dry run first)
  secrets.renew     renew Vera's token now. A watcher renews it every six hours
                    and unseals OpenBao when it comes back sealed.
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import APP, capability, emit_event, now_iso
from Vera.vera.security import secrets as vsecrets
from Vera.vera.security import secret_service_core as core

log = logging.getLogger("vera.security.secrets_service")

KEY_SETUP = "vera:secrets:setup"
BACKUP_PREFIX = "vera:secrets:migrate:backup:"
BACKUP_TTL_S = 30 * 24 * 3600
AUDIT_PATH = "/openbao/logs/audit.log"
COUNT_TTL_S = 300.0
WATCH_EVERY_S = 60
RENEW_EVERY_S = 6 * 3600
_COUNTS: Dict[str, Any] = {"at": 0.0, "value": None}
_WATCH: Dict[str, Any] = {"task": None, "renewed_at": 0.0}
_KEYDROP: Dict[str, Any] = {"path": None, "module": None}


def _redis():
    return getattr(_orch, "REDIS", None)


def _text(v: Any) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return "" if v is None else str(v)


def _flag(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _module_of(cap_name: str) -> Optional[Dict[str, Any]]:
    fn = (_orch.CAPABILITY_REGISTRY.get(cap_name) or {}).get("func")
    return getattr(inspect.unwrap(fn), "__globals__", None) if fn is not None else None


async def _call(cap_name: str, **kwargs: Any) -> Dict[str, Any]:
    fn = (_orch.CAPABILITY_REGISTRY.get(cap_name) or {}).get("func")
    if fn is None:
        return {"error": f"{cap_name} is not loaded"}
    try:
        out = await fn(**kwargs)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return out if isinstance(out, dict) else {"error": f"{cap_name} returned no result"}


async def _thread(fn, *args):
    return await asyncio.get_event_loop().run_in_executor(None, lambda: fn(*args))


# ═════════════════════════════════════════════════════════════════════════════
#  OPENBAO
# ═════════════════════════════════════════════════════════════════════════════
async def _http(method: str, url: str, *, token: str = "", body: Any = None, namespace: str = "",
                verify: bool = False, timeout: float = 8.0) -> Tuple[int, Any, str]:
    headers = {}
    if token:
        headers["X-Vault-Token"] = token
    if namespace:
        headers["X-Vault-Namespace"] = namespace
    try:
        async with httpx.AsyncClient(verify=verify, timeout=timeout) as client:
            r = await client.request(method, url, headers=headers, json=body)
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"
    try:
        data = r.json() if r.content else None
    except ValueError:
        data = None
    return r.status_code, data, ""


def _cfg() -> Dict[str, Any]:
    return vsecrets._bao_cfg() or {}


async def _bao(method: str, path: str, body: Any = None, token: str = "") -> Tuple[int, Any, str]:
    """OpenBao with Vera's own token. path is the part after /v1/."""
    cfg = _cfg()
    if not cfg:
        return 0, None, "Vera has no OpenBao address and token"
    return await _http(method, f"{cfg['addr']}/v1/{path.lstrip('/')}", token=token or cfg["token"],
                       body=body, namespace=cfg.get("namespace", ""), verify=cfg.get("verify", False))


async def _prov_state(opened: bool = False) -> Dict[str, Any]:
    g = _module_of("prov.config.get") or {}
    fn = g.get("_state_opened" if opened else "_state_raw")
    return await fn() if fn else {}


async def _openbao() -> Dict[str, Any]:
    st = await _prov_state()
    addr = (os.getenv("BAO_ADDR") or st.get("openbao_addr") or "").rstrip("/")
    if not addr:
        return {"configured": False, "reachable": False, "error": "no OpenBao address is configured"}
    code, seal, err = await _http("GET", f"{addr}/v1/sys/seal-status", timeout=4.0)
    if code != 200 or not isinstance(seal, dict):
        return {"configured": True, "addr": addr, "reachable": False, "error": err or f"HTTP {code}"}
    return {"configured": True, "addr": addr, "reachable": True,
            "initialized": bool(seal.get("initialized")), "sealed": bool(seal.get("sealed")),
            "storage": seal.get("storage_type", ""), "version": seal.get("version", "")}


async def _token() -> Dict[str, Any]:
    if not _cfg():
        return {}
    code, data, err = await _bao("GET", "auth/token/lookup-self")
    if code != 200:
        return {"error": err or f"HTTP {code}"}
    d = (data or {}).get("data") or {}
    policies = list(d.get("policies") or [])
    return {"root": "root" in policies, "policies": policies, "ttl_s": d.get("ttl"),
            "renewable": bool(d.get("renewable")), "period_s": d.get("period") or 0}


async def _renew() -> Dict[str, Any]:
    code, body, err = await _bao("POST", "auth/token/renew-self", {})
    if code != 200:
        return {"error": err or f"HTTP {code}"}
    return {"ok": True, "ttl_s": ((body or {}).get("auth") or {}).get("lease_duration")}


# ── named secrets, for Vera's own modules ────────────────────────────────────
async def put_named(path: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Store a named secret in OpenBao. fields must include 'value'."""
    try:
        p = core.check_path(path)
    except ValueError as e:
        return {"error": str(e)}
    cfg = _cfg()
    if not (cfg and vsecrets._bao_active()):
        return {"error": "OpenBao is not active for Vera, so named secrets cannot be stored; see secrets.status"}
    data = {k: ("" if v is None else v) for k, v in fields.items()}
    data["updated"] = now_iso()
    code, body, err = await _bao("POST", core.named_data(cfg["mount"], p), {"data": data})
    if code not in (200, 204):
        return {"error": err or f"OpenBao refused the write (HTTP {code})"}
    return {"ok": True, "path": p, "version": ((body or {}).get("data") or {}).get("version")}


async def get_named(path: str) -> Optional[Dict[str, Any]]:
    """A named secret's fields; None when it is absent or cannot be read."""
    try:
        p = core.check_path(path)
    except ValueError:
        return None
    cfg = _cfg()
    if not cfg:
        return None
    code, body, _err = await _bao("GET", core.named_data(cfg["mount"], p))
    if code != 200:
        return None
    return ((body or {}).get("data") or {}).get("data") or None


async def delete_named(path: str) -> Dict[str, Any]:
    """Remove a named secret and all its versions."""
    try:
        p = core.check_path(path)
    except ValueError as e:
        return {"error": str(e)}
    cfg = _cfg()
    if not (cfg and vsecrets._bao_active()):
        return {"error": "OpenBao is not active for Vera; see secrets.status"}
    code, _b, err = await _bao("DELETE", core.named_metadata(cfg["mount"], p))
    if code not in (200, 204):
        return {"error": err or f"OpenBao refused the delete (HTTP {code})"}
    return {"ok": True, "path": p}


# ═════════════════════════════════════════════════════════════════════════════
#  KEYDROP  (the user's own tool, loaded from the drop directory)
# ═════════════════════════════════════════════════════════════════════════════
def _keydrop_dir() -> Path:
    return Path(os.getenv("VERA_KEYDROP_DIR") or (Path.home() / ".vera-keydrop"))


def _keydrop_tool():
    tool = _keydrop_dir() / "bin" / "keydrop.py"
    if not tool.is_file():
        return None
    if _KEYDROP["module"] is None or _KEYDROP["path"] != str(tool):
        spec = importlib.util.spec_from_file_location("vera_keydrop_tool", str(tool))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _KEYDROP.update(path=str(tool), module=module)
    return _KEYDROP["module"]


def _keydrop_info_sync() -> Dict[str, Any]:
    d = _keydrop_dir()
    try:
        tool = _keydrop_tool()
    except Exception as e:
        return {"available": False, "error": f"the keydrop tool did not load: {type(e).__name__}: {e}"}
    if tool is None:
        return {"available": False, "error": f"no keydrop tool at {d / 'bin' / 'keydrop.py'}"}
    if not (d / "recipient.pub").is_file():
        return {"available": False, "error": f"no recipient key at {d / 'recipient.pub'}"}
    try:
        drop = tool.Drop(str(d))
        ok, message = drop.verify()
        return {"available": True, "dir": str(d), "entries": len(drop.lines()), "chain_ok": ok,
                "message": message}
    except SystemExit as e:
        return {"available": False, "error": str(e)}


def _keydrop_put_sync(label: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    info = _keydrop_info_sync()
    if not info.get("available"):
        return {"error": info.get("error")}
    if not info.get("chain_ok"):
        return {"error": f"keydrop refuses new entries: {info.get('message')}"}
    try:
        seq = _keydrop_tool().Drop(info["dir"]).append(
            label, payload, os.getenv("VERA_KEYDROP_AUTHOR") or "vera@secrets-service")
    except SystemExit as e:
        return {"error": str(e)}
    return {"ok": True, "entry": seq, "label": label}


# ═════════════════════════════════════════════════════════════════════════════
#  WHAT IS STILL SEALED WITH THE FILE KEY
# ═════════════════════════════════════════════════════════════════════════════
async def _redis_entries() -> List[Dict[str, Any]]:
    r = _redis()
    out: List[Dict[str, Any]] = []
    if r is None:
        return out
    async for raw in r.scan_iter(count=500):
        key = _text(raw)
        if core.skip_key(key):
            continue
        try:
            kind = _text(await r.type(key))
            if kind == "string":
                out.append({"key": key, "field": "", "value": _text(await r.get(key))})
            elif kind == "hash":
                for f, v in (await r.hgetall(key)).items():
                    out.append({"key": key, "field": _text(f), "value": _text(v)})
        except Exception as e:
            log.debug("secrets scan %s: %s", key, e)
    return out


def _exec_store() -> Dict[str, Any]:
    g = _module_of("exec.ssh.hosts.list") or {}
    return g if all(k in g for k in ("_load_hosts", "_save_hosts", "_deobfuscate")) else {}


async def _ssh_records() -> Dict[str, Dict[str, Any]]:
    ex = _exec_store()
    if not ex:
        return {}
    try:
        return {k: dict(v) for k, v in (await ex["_load_hosts"]()).items()}
    except Exception as e:
        log.debug("secrets: exec store read failed: %s", e)
        return {}


async def _counts(refresh: bool = False) -> Dict[str, Any]:
    if not refresh and _COUNTS["value"] is not None and time.time() - _COUNTS["at"] < COUNT_TTL_S:
        return _COUNTS["value"]
    plan = core.migration_plan(await _redis_entries())
    ssh = core.ssh_store_plan(await _ssh_records())
    value = {"redis": {k: plan[k] for k in ("fernet", "bao", "pinned", "keys")},
             "ssh_store": ssh["counts"], "remaining": plan["fernet"] + len(ssh["items"])}
    _COUNTS.update(at=time.time(), value=value)
    return value


# ═════════════════════════════════════════════════════════════════════════════
#  CAPABILITIES
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "secrets.status",
    http_method="GET", http_path="/secrets/status", http_tags=["security"],
    memory="off", silent=True,
    description="The secrets service at a glance: which backend Vera seals with, OpenBao's "
                "reachability, seal state and storage, whether Vera's token is the root "
                "token or its own limited one and when it expires, keydrop's entries and "
                "chain, and how many stored values (Redis and the SSH login store) are "
                "still sealed with the file key. Never returns a secret. Inputs: refresh "
                "(bool; recount now instead of the 5-minute count). Output: {backend, "
                "openbao, token, keydrop, stored, ssh_store, setup, findings}.",
)
async def cap_secrets_status(refresh: bool = False, trace_id=None) -> Dict[str, Any]:
    openbao = await _openbao()
    backend = vsecrets.backend_status()
    token = await _token() if backend.get("openbao_configured") else {}
    keydrop = await _thread(_keydrop_info_sync)
    counts = await _counts(_flag(refresh))
    setup: Dict[str, Any] = {}
    r = _redis()
    if r is not None:
        raw = await r.get(KEY_SETUP)
        setup = json.loads(_text(raw)) if raw else {}
    return {"backend": backend.get("backend"), "openbao": openbao,
            "token": {k: token[k] for k in ("root", "policies", "ttl_s", "renewable", "period_s", "error")
                      if k in token},
            "keydrop": keydrop, "stored": counts["redis"], "ssh_store": counts["ssh_store"],
            "setup": setup, "findings": core.findings(openbao, backend, token, keydrop, counts["remaining"])}


@capability(
    "secrets.setup",
    http_method="POST", http_path="/secrets/setup", http_tags=["security"],
    memory="on",
    description="Finish OpenBao for Vera after secstore.bootstrap: hand the root token and "
                "unseal key to keydrop FIRST, write a 'vera' policy that reaches only "
                "Vera's own KV paths and its own token, turn on the file audit log, create "
                "a renewable token with only that policy, and store it for Vera in place "
                "of the root token. The root token then exists only in keydrop. A dry run "
                "unless apply=true; stops before changing anything if keydrop does not take "
                "the root token. Inputs: apply (bool). Output: {ok, dry_run, steps} or "
                "{ok, scoped_at, policy, period, audit, keydrop_entry} or {error}.",
)
async def cap_secrets_setup(apply: bool = False, trace_id=None) -> Dict[str, Any]:
    openbao = await _openbao()
    if not openbao.get("reachable"):
        return {"error": f"OpenBao is not reachable: {openbao.get('error')}"}
    if not openbao.get("initialized") or openbao.get("sealed"):
        return {"error": "OpenBao must be initialized and unsealed first (secstore.bootstrap)"}
    st = await _prov_state(opened=True)
    root = st.get("openbao_token") or ""
    if not root:
        return {"error": "Vera holds no OpenBao token; run secstore.bootstrap first"}
    addr, mount = openbao["addr"], st.get("openbao_mount") or "secret"
    ns = st.get("openbao_namespace") or ""
    code, look, err = await _http("GET", f"{addr}/v1/auth/token/lookup-self", token=root, namespace=ns)
    if code != 200:
        return {"error": f"Vera's OpenBao token was refused: {err or 'HTTP %s' % code}"}
    policies = ((look or {}).get("data") or {}).get("policies") or []
    if "root" not in policies:
        return {"ok": True, "already_scoped": True, "policies": policies}
    steps = ["hand the root token and unseal key to keydrop",
             f"write the '{core.POLICY}' policy: {mount}/vera/*, {mount}/vera-secrets/* and its own token",
             f"log every request to {AUDIT_PATH}",
             f"create a renewable token with only that policy (period {core.TOKEN_PERIOD})",
             "store that token for Vera in place of the root token, and switch to it"]
    if not _flag(apply):
        return {"ok": True, "dry_run": True, "steps": steps, "keydrop": await _thread(_keydrop_info_sync)}

    raw_unseal = st.get("openbao_unseal") or ""
    try:
        keys = json.loads(raw_unseal) if raw_unseal else []
    except ValueError:
        keys = [raw_unseal]
    payload = core.keydrop_payload(
        "OpenBao on the Vera host", username="root token", password=root, url=addr,
        notes="The unseal key is in the extra fields. Vera unseals OpenBao itself with its own "
              "stored copy; this is yours. Vera no longer holds the root token.",
        tags=["vera", "openbao"], extra={f"unseal_key_{i + 1}": k for i, k in enumerate(keys)})
    kd = await _thread(_keydrop_put_sync, "OpenBao root and unseal", payload)
    if not kd.get("ok"):
        return {"error": f"stopped before changing anything: keydrop did not take the root token "
                         f"({kd.get('error')})"}
    code, _b, err = await _http("PUT", f"{addr}/v1/sys/policies/acl/{core.POLICY}", token=root,
                                namespace=ns, body={"policy": core.policy_hcl(mount)})
    if code not in (200, 204):
        return {"error": f"the policy was not written: {err or 'HTTP %s' % code}", "keydrop": kd}
    code, audits, _e = await _http("GET", f"{addr}/v1/sys/audit", token=root, namespace=ns)
    devices = ((audits or {}).get("data") if isinstance((audits or {}).get("data"), dict) else audits) or {}
    audit = "already on"
    if not any(str(k).startswith("file") for k in devices):
        code, _b, err = await _http("PUT", f"{addr}/v1/sys/audit/file", token=root, namespace=ns,
                                    body={"type": "file", "options": {"file_path": AUDIT_PATH}})
        audit = "on" if code in (200, 204) else f"not turned on: {err or 'HTTP %s' % code}"
    code, created, err = await _http(
        "POST", f"{addr}/v1/auth/token/create-orphan", token=root, namespace=ns,
        body={"policies": [core.POLICY], "period": core.TOKEN_PERIOD, "display_name": "vera",
              "renewable": True, "meta": {"purpose": "vera secrets backend"}})
    scoped = ((created or {}).get("auth") or {}).get("client_token", "")
    if code != 200 or not scoped:
        return {"error": f"the limited token was not created: {err or 'HTTP %s' % code}", "keydrop": kd}
    saved = await _call("prov.config.save", openbao_token=scoped)
    if saved.get("error"):
        return {"error": f"the limited token was created but not stored: {saved['error']}", "keydrop": kd}
    g = _module_of("prov.config.get") or {}
    if "_export_bao_env" in g:
        g["_export_bao_env"](await _prov_state(opened=True))
    record = {"scoped_at": now_iso(), "policy": core.POLICY, "period": core.TOKEN_PERIOD,
              "audit": audit, "keydrop_entry": kd.get("entry")}
    r = _redis()
    if r is not None:
        await r.set(KEY_SETUP, json.dumps(record))
    await emit_event({"type": "secrets.setup", "keydrop_entry": kd.get("entry")})
    return {"ok": True, "dry_run": False, **record}


@capability(
    "secrets.put",
    http_method="POST", http_path="/secrets/put", http_tags=["security"],
    memory="off",
    description="Store a named secret in OpenBao under vera/named/<path>. Inputs: path (str!; "
                "lowercase, '/'-separated, e.g. netctl/door-files), value (str!), username, url, "
                "notes (str), handoff (bool; also seal it to keydrop for a person), title "
                "(KeePass title for the handoff, default the path), label (cleartext keydrop "
                "label; keep it generic). Output: {ok, path, version, keydrop?}. Never echoes "
                "the value.",
)
async def cap_secrets_put(path: str = "", value: str = "", username: str = "", url: str = "",
                          notes: str = "", handoff: bool = False, title: str = "", label: str = "",
                          trace_id=None) -> Dict[str, Any]:
    if not value:
        return {"error": "value required"}
    out = await put_named(path, {"value": value, "username": username, "url": url, "notes": notes})
    if out.get("error"):
        return out
    if _flag(handoff):
        out["keydrop"] = await _thread(_keydrop_put_sync, label or "Vera secret",
                                       core.keydrop_payload(title or out["path"], username, value, url,
                                                            notes, tags=["vera"]))
    await emit_event({"type": "secrets.put", "path": out["path"], "handoff": _flag(handoff)})
    return out


@capability(
    "secrets.list",
    http_method="POST", http_path="/secrets/list", http_tags=["security"],
    memory="off", silent=True,
    description="Named secrets in OpenBao: paths, when each was last updated and its version "
                "count. Never values. Inputs: prefix (str). Output: {secrets:[{path, updated, "
                "versions}], count}.",
)
async def cap_secrets_list(prefix: str = "", trace_id=None) -> Dict[str, Any]:
    cfg = _cfg()
    if not (cfg and vsecrets._bao_active()):
        return {"error": "OpenBao is not active for Vera; see secrets.status"}
    try:
        start = core.named_folder(cfg["mount"], prefix)
    except ValueError as e:
        return {"error": str(e)}
    base = core.named_folder(cfg["mount"])
    found: List[str] = []
    queue = [start]
    while queue and len(found) < 500:
        folder = queue.pop(0)
        code, body, _e = await _bao("LIST", folder)
        if code != 200:
            continue
        for k in ((body or {}).get("data") or {}).get("keys") or []:
            if k.endswith("/"):
                queue.append(folder + k)
            else:
                found.append((folder + k)[len(base):])
    rows = []
    for p in sorted(found):
        _code, meta, _e = await _bao("GET", core.named_metadata(cfg["mount"], p))
        d = (meta or {}).get("data") or {}
        rows.append({"path": p, "updated": d.get("updated_time", ""), "versions": d.get("current_version")})
    return {"secrets": rows, "count": len(rows)}


@capability(
    "secrets.delete",
    http_method="POST", http_path="/secrets/delete", http_tags=["security"],
    memory="on",
    description="Delete a named secret and all its versions. A dry run unless confirm=true. "
                "Inputs: path (str!), confirm (bool). Output: {ok, dry_run, path}.",
)
async def cap_secrets_delete(path: str = "", confirm: bool = False, trace_id=None) -> Dict[str, Any]:
    try:
        p = core.check_path(path)
    except ValueError as e:
        return {"error": str(e)}
    cfg = _cfg()
    if not (cfg and vsecrets._bao_active()):
        return {"error": "OpenBao is not active for Vera; see secrets.status"}
    if not _flag(confirm):
        return {"ok": True, "dry_run": True, "path": p, "note": "nothing deleted; pass confirm=true"}
    res = await delete_named(p)
    if res.get("error"):
        return res
    await emit_event({"type": "secrets.delete", "path": p})
    return {"ok": True, "dry_run": False, "path": p}


@capability(
    "secrets.handoff",
    http_method="POST", http_path="/secrets/handoff", http_tags=["security"],
    memory="on",
    description="Seal an existing named secret into keydrop so a person can import it into "
                "KeePass. Inputs: path (str!), title (str), label (str; cleartext, keep it "
                "generic). Output: {ok, entry, label} or {error}.",
)
async def cap_secrets_handoff(path: str = "", title: str = "", label: str = "", trace_id=None) -> Dict[str, Any]:
    secret = await get_named(path)
    if not secret or not secret.get("value"):
        return {"error": f"no readable named secret at {path!r}"}
    return await _thread(_keydrop_put_sync, label or "Vera secret",
                         core.keydrop_payload(title or core.check_path(path), secret.get("username", ""),
                                              secret["value"], secret.get("url", ""), secret.get("notes", ""),
                                              tags=["vera"]))


@capability(
    "secrets.migrate",
    http_method="POST", http_path="/secrets/migrate", http_tags=["security"],
    memory="on",
    description="Move stored secrets that are still sealed with Vera's file key into OpenBao: "
                "plain values and JSON documents in Redis, and passwords and key passphrases in "
                "the SSH login store. OpenBao's own bootstrap secrets stay with the file key. "
                "Each original is kept for 30 days in a Redis backup key, and a value that "
                "changed while moving is left alone. A dry run unless apply=true. Inputs: apply "
                "(bool). Output: {dry_run, stored, items, ssh_store, moved?, ssh_moved?, "
                "backup_key?, errors?}.",
)
async def cap_secrets_migrate(apply: bool = False, trace_id=None) -> Dict[str, Any]:
    entries = await _redis_entries()
    plan = core.migration_plan(entries)
    records = await _ssh_records()
    ssh = core.ssh_store_plan(records)
    out: Dict[str, Any] = {"dry_run": not _flag(apply),
                           "stored": {k: plan[k] for k in ("fernet", "bao", "pinned", "keys")},
                           "items": plan["items"][:200],
                           "ssh_store": {"counts": ssh["counts"], "items": ssh["items"]}}
    if not _flag(apply):
        return out
    r = _redis()
    if r is None:
        return {**out, "error": "Vera has no Redis connection"}
    if not vsecrets._bao_active():
        return {**out, "error": "OpenBao is not active for Vera, so nothing was moved"}
    backup_key = BACKUP_PREFIX + time.strftime("%Y%m%d%H%M%S")
    values = {(e["key"], e["field"]): e["value"] for e in entries}
    moved, ssh_moved, errors = 0, 0, []

    def reseal(sealed: str, opener) -> str:
        plain = opener(sealed)
        if not plain:
            return ""
        ref = vsecrets.seal(plain)
        return ref if ref.startswith(core.BAO) else ""

    for item in plan["items"]:
        key, field = item["key"], item["field"]
        where = key + (f"/{field}" if field else "")
        original = values[(key, field)]
        mapping: Dict[str, str] = {}
        for sealed in core.sealed_strings(original, "fernet"):
            ref = reseal(sealed, vsecrets.open_secret)
            if not ref:
                mapping = {}
                break
            mapping[sealed] = ref
        if not mapping:
            errors.append(f"{where}: could not be re-sealed; left as it was")
            continue
        current = _text(await (r.hget(key, field) if field else r.get(key)))
        if current != original:
            errors.append(f"{where}: changed while moving; left as it was")
            continue
        await r.hset(backup_key, f"{key}|{field}", original)
        new_value = core.replace_sealed(original, mapping)
        if field:
            await r.hset(key, field, new_value)
        else:
            await r.set(key, new_value, keepttl=True)
        moved += item["count"]

    ex = _exec_store()
    if ssh["items"] and ex:
        for it in ssh["items"]:
            rec = records.get(it["id"]) or {}
            original = rec.get(it["field"]) or ""
            ref = reseal(original, ex["_deobfuscate"])
            if not ref:
                errors.append(f"SSH login {it['label'] or it['id']}: {it['field']} could not be re-sealed")
                continue
            await r.hset(backup_key, f"ssh|{it['id']}|{it['field']}",
                         vsecrets.seal(ex["_deobfuscate"](original), force_fernet=True))
            rec[it["field"]] = ref
            ssh_moved += 1
        if ssh_moved:
            await ex["_save_hosts"](records)
    if moved or ssh_moved:
        await r.expire(backup_key, BACKUP_TTL_S)
    _COUNTS["at"] = 0.0
    await emit_event({"type": "secrets.migrate", "moved": moved, "ssh_store": ssh_moved})
    return {**out, "dry_run": False, "moved": moved, "ssh_moved": ssh_moved,
            "backup_key": backup_key if (moved or ssh_moved) else "", "errors": errors}


@capability(
    "secrets.renew",
    http_method="POST", http_path="/secrets/renew", http_tags=["security"],
    memory="off",
    description="Renew Vera's OpenBao token now (the watcher does this every six hours). "
                "Output: {ok, ttl_s} or {error}.",
)
async def cap_secrets_renew(trace_id=None) -> Dict[str, Any]:
    tok = await _token()
    if tok.get("root"):
        return {"error": "Vera is using the root token, which does not expire; run secrets.setup"}
    return await _renew()


_PANEL = Path(__file__).parent / "secrets_panel.html"


@APP.get("/secrets/panel", include_in_schema=False)
async def _secrets_panel():
    """Estate > Trust > Secrets: the service's state, what is still on the file key,
    named secrets (never values), and the SSH login cleanup."""
    return HTMLResponse(_PANEL.read_text(encoding="utf-8") if _PANEL.exists()
                        else "<p style='color:red'>secrets_panel.html not found</p>")


# ═════════════════════════════════════════════════════════════════════════════
#  WATCHER  (renew Vera's token; unseal OpenBao when it restarts sealed)
# ═════════════════════════════════════════════════════════════════════════════
async def _watch_tick() -> Dict[str, Any]:
    did: Dict[str, Any] = {}
    st = await _prov_state()
    if not st.get("openbao_addr"):
        return did
    ob = await _openbao()
    if ob.get("reachable") and ob.get("initialized") and ob.get("sealed") and st.get("openbao_unseal"):
        res = await _call("secstore.unseal")
        did["unsealed"] = not res.get("error")
        log.warning("secrets: OpenBao came back sealed; unseal %s",
                    "succeeded" if did["unsealed"] else res.get("error"))
        await emit_event({"type": "secrets.unsealed", "ok": did["unsealed"]})
    elif ob.get("reachable") and ob.get("initialized") and not ob.get("sealed") and not _cfg():
        g = _module_of("prov.config.get") or {}
        if "_export_bao_env" in g:
            g["_export_bao_env"](await _prov_state(opened=True))
            did["exported"] = True
    if _cfg() and time.time() - _WATCH["renewed_at"] > RENEW_EVERY_S:
        tok = await _token()
        if tok.get("renewable") and not tok.get("root"):
            did["renewed"] = bool((await _renew()).get("ok"))
        _WATCH["renewed_at"] = time.time()
    return did


async def _watch_loop():
    while True:
        try:
            await _watch_tick()
        except Exception as e:
            log.debug("secrets watcher: %s", e)
        await asyncio.sleep(WATCH_EVERY_S)


def _watch_start():
    task = _WATCH.get("task")
    if task and not task.done():
        return
    _WATCH["task"] = asyncio.ensure_future(_watch_loop())


try:
    _loop = asyncio.get_event_loop()
    if _loop.is_running():
        _loop.call_soon(_watch_start)
except Exception:
    pass

log.info("secrets_capabilities ready - status/setup/put/list/delete/handoff/migrate/renew")
