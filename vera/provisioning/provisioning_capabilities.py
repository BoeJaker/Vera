"""
provisioning_capabilities.py — Vera Security & Provisioning Suite (Iteration 3)
==============================================================================

Secrets store (OpenBao) + certificate engine (step-ca) + an auto-cert hook so
every asset Vera brings online can be given a TLS cert and have its secrets
managed centrally. Vera *orchestrates* these battle-tested services — it does not
reimplement them.

Run model is "both": each service is **detected-and-adopted** if already running,
or **provisioned** otherwise (provisioning of appliances reuses the Proxmox
module / Docker subsystem and lands in later iterations; this module fully
supports adopt + all day-2 operations).

Capabilities
────────────
  prov.config.get / prov.config.save        — suite config (secrets sealed)
  prov.status                               — aggregate health (OpenBao/step-ca)
  secstore.bootstrap / secstore.unseal      — init + unseal OpenBao
  secstore.kv.put / .get / .list / .delete  — KV v2 secret operations
  pki.bootstrap / pki.root                  — adopt step-ca, fetch root of trust
  pki.cert.issue / pki.cert.list            — issue (SSH-driven) + inventory certs
  provisioning.asset.online                 — the auto-cert hook: issue → store →
                                              install for a freshly-online asset

Bootstrap-secret handling
─────────────────────────
OpenBao's unseal keys + root token and step-ca's provisioner password are the
chicken-and-egg secrets the vault itself can't hold yet. Those are sealed with
Vera's existing Fernet helper (vera/security/secrets.py) into Redis
`vera:provisioning:state`. Everything else (app/service secrets, issued keys)
lives in OpenBao.

Redis layout
────────────
  vera:provisioning:state   hash  field 'main' -> JSON   (bootstrap secrets sealed)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi.responses import HTMLResponse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, capability, emit_event, now_iso, register_ui,
)
from Vera.vera.security import secrets as vsecrets

log = logging.getLogger("vera.provisioning")

_HERE = Path(__file__).parent
KEY_STATE = "vera:provisioning:state"
_STATE_FIELD = "main"

# Sealed (Fernet) before storage, redacted to has_* on read.
_SECRET_FIELDS = ("openbao_token", "openbao_unseal", "stepca_provisioner_password")

_DEFAULTS: Dict[str, Any] = {
    "openbao_addr": "", "openbao_namespace": "", "openbao_mount": "secret",
    "stepca_url": "", "stepca_fingerprint": "", "stepca_provisioner": "",
    "stepca_root_pem": "", "base_domain": "", "default_ssh_host_id": "",
    "verify_tls": False,
}


# ═════════════════════════════════════════════════════════════════════════════
#  STATE  (mirrors accounts/proxmox: seal on write, redact on read)
# ═════════════════════════════════════════════════════════════════════════════
def _redis():
    return getattr(_orch, "REDIS", None)


async def _state_raw() -> Dict[str, Any]:
    r = _redis()
    if not r:
        return dict(_DEFAULTS)
    raw = await r.hget(KEY_STATE, _STATE_FIELD)
    if not raw:
        return dict(_DEFAULTS)
    try:
        return {**_DEFAULTS, **json.loads(raw)}
    except Exception:
        return dict(_DEFAULTS)


async def _state_opened() -> Dict[str, Any]:
    st = await _state_raw()
    for f in _SECRET_FIELDS:
        st[f] = vsecrets.open_secret(st.get(f, ""))
    return st


def _redact(st: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in st.items():
        if k in _SECRET_FIELDS:
            out[f"has_{k}"] = bool(v)
        else:
            out[k] = v
    return out


async def _state_save(patch: Dict[str, Any]) -> Dict[str, Any]:
    r = _redis()
    if not r:
        return {"error": "store unavailable (no Redis)"}
    cur = await _state_raw()
    for k, v in patch.items():
        if k in _SECRET_FIELDS:
            if v:                       # seal only a new truthy value; keep existing
                # force_fernet: these are OpenBao's OWN bootstrap secrets (its
                # token/unseal key + step-ca password) — they must NOT be stored
                # in OpenBao itself (it can't open the vault to read the token
                # that opens the vault). Always Fernet-seal them inline.
                cur[k] = vsecrets.seal(v if isinstance(v, str) else json.dumps(v),
                                       force_fernet=True)
        elif v is not None:
            cur[k] = v
    cur["updated"] = now_iso()
    await r.hset(KEY_STATE, _STATE_FIELD, json.dumps(cur))
    return cur


# ═════════════════════════════════════════════════════════════════════════════
#  OPENBAO CLIENT  (Vault-compatible HTTP API)
# ═════════════════════════════════════════════════════════════════════════════
async def _bao(st: Dict, method: str, path: str, token: Optional[str] = None,
               json_body: Optional[Dict] = None, params: Optional[Dict] = None,
               ) -> Tuple[Optional[Any], str]:
    addr = (st.get("openbao_addr") or "").rstrip("/")
    if not addr:
        return None, "OpenBao address not configured"
    headers = {}
    tok = token if token is not None else st.get("openbao_token", "")
    if tok:
        headers["X-Vault-Token"] = tok
    if st.get("openbao_namespace"):
        headers["X-Vault-Namespace"] = st["openbao_namespace"]
    try:
        async with httpx.AsyncClient(timeout=20, verify=bool(st.get("verify_tls"))) as c:
            r = await c.request(method, addr + path, headers=headers,
                                json=json_body, params=params)
            if r.status_code == 404:
                return {"_status": 404}, ""
            if r.status_code >= 400:
                return None, f"HTTP {r.status_code}: {r.text[:300]}"
            if r.text:
                try:
                    return r.json(), ""
                except Exception:
                    return {"_raw": r.text}, ""
            return {}, ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


async def _bao_health(st: Dict) -> Tuple[Optional[Dict], str]:
    """Health with status-code overrides so we can always read the JSON body."""
    addr = (st.get("openbao_addr") or "").rstrip("/")
    if not addr:
        return None, "OpenBao address not configured"
    try:
        async with httpx.AsyncClient(timeout=10, verify=bool(st.get("verify_tls"))) as c:
            r = await c.get(addr + "/v1/sys/health",
                            params={"standbyok": "true", "sealedcode": "200",
                                    "uninitcode": "200", "standbycode": "200"})
            return r.json(), ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# ═════════════════════════════════════════════════════════════════════════════
#  CAPABILITIES — suite config + aggregate status
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "prov.config.get",
    http_method="GET", http_path="/provisioning/config", http_tags=["provisioning"],
    memory="off", silent=True,
    description="Get the security-suite config (REDACTED — secrets shown only as "
                "has_* flags). Output: the config object.",
)
async def cap_config_get(trace_id=None) -> Dict:
    return _redact(await _state_raw())


@capability(
    "prov.config.save",
    http_method="POST", http_path="/provisioning/config/save",
    http_tags=["provisioning"], memory="off",
    description="Update the security-suite config. Secret inputs (openbao_token, "
                "stepca_provisioner_password) are SEALED (Fernet) and never "
                "returned; blank keeps the existing value. Inputs: openbao_addr, "
                "openbao_namespace, openbao_mount ('secret'), openbao_token, "
                "stepca_url, stepca_fingerprint, stepca_provisioner, "
                "stepca_provisioner_password, base_domain, default_ssh_host_id, "
                "verify_tls (bool). Output: redacted config.",
)
async def cap_config_save(
    openbao_addr: str = "", openbao_namespace: str = "", openbao_mount: str = "",
    openbao_token: str = "", stepca_url: str = "", stepca_fingerprint: str = "",
    stepca_provisioner: str = "", stepca_provisioner_password: str = "",
    base_domain: str = "", default_ssh_host_id: str = "",
    verify_tls: Optional[bool] = None, trace_id=None,
) -> Dict:
    patch = {
        "openbao_addr": openbao_addr or None,
        "openbao_namespace": openbao_namespace or None,
        "openbao_mount": openbao_mount or None,
        "openbao_token": openbao_token,            # sealed if truthy
        "stepca_url": stepca_url or None,
        "stepca_fingerprint": stepca_fingerprint or None,
        "stepca_provisioner": stepca_provisioner or None,
        "stepca_provisioner_password": stepca_provisioner_password,  # sealed if truthy
        "base_domain": base_domain or None,
        "default_ssh_host_id": default_ssh_host_id or None,
        "verify_tls": verify_tls,
    }
    saved = await _state_save({k: v for k, v in patch.items() if v is not None})
    if saved.get("error"):
        return saved
    return _redact(saved)


@capability(
    "prov.status",
    http_method="GET", http_path="/provisioning/status", http_tags=["provisioning"],
    memory="off", silent=True,
    description="Aggregate health of the suite. Output: {openbao:{configured,"
                "reachable,initialized,sealed,version}, stepca:{configured,"
                "reachable,has_root}}.",
)
async def cap_status(trace_id=None) -> Dict:
    st = await _state_opened()
    bao = {"configured": bool(st.get("openbao_addr")), "reachable": False,
           "initialized": None, "sealed": None, "version": ""}
    if bao["configured"]:
        h, err = await _bao_health(st)
        if h is not None:
            bao.update({"reachable": True, "initialized": h.get("initialized"),
                        "sealed": h.get("sealed"), "version": h.get("version", "")})
        else:
            bao["error"] = err
    pki = {"configured": bool(st.get("stepca_url")), "reachable": False,
           "has_root": bool(st.get("stepca_root_pem"))}
    if pki["configured"]:
        ok, err = await _stepca_health(st)
        pki["reachable"] = ok
        if not ok:
            pki["error"] = err
    return {"openbao": bao, "stepca": pki}


# ═════════════════════════════════════════════════════════════════════════════
#  CAPABILITIES — OpenBao secret store
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "secstore.bootstrap",
    http_method="POST", http_path="/provisioning/secstore/bootstrap",
    http_tags=["provisioning"], memory="off",
    description="Adopt or initialize OpenBao. If already initialized, just reports "
                "state. If uninitialized, runs sys/init (secret_shares=1), SEALS "
                "the unseal key + root token into Redis (Fernet), unseals, and "
                "enables a KV-v2 mount. Inputs: addr (str — sets/overrides "
                "openbao_addr), mount ('secret'). Output: {ok, initialized, sealed, "
                "stored_keys}. Unseal material is never returned.",
)
async def cap_secstore_bootstrap(addr: str = "", mount: str = "",
                                 trace_id=None) -> Dict:
    if addr or mount:
        await _state_save({k: v for k, v in
                           {"openbao_addr": addr, "openbao_mount": mount}.items() if v})
    st = await _state_opened()
    if not st.get("openbao_addr"):
        return {"error": "openbao_addr required"}
    health, err = await _bao_health(st)
    if health is None:
        return {"error": f"OpenBao unreachable: {err}"}

    if not health.get("initialized"):
        init, err = await _bao(st, "PUT", "/v1/sys/init",
                               json_body={"secret_shares": 1, "secret_threshold": 1})
        if init is None:
            return {"error": f"init failed: {err}"}
        keys = init.get("keys_base64") or init.get("keys") or []
        root = init.get("root_token", "")
        await _state_save({"openbao_unseal": json.dumps(keys),
                           "openbao_token": root})
        st = await _state_opened()

    # Unseal (idempotent) using the stored key(s).
    await _do_unseal(st)
    # Ensure the KV-v2 mount exists (ignore "already enabled").
    mnt = st.get("openbao_mount", "secret")
    await _bao(st, "POST", f"/v1/sys/mounts/{mnt}",
               json_body={"type": "kv", "options": {"version": "2"}})
    health, _ = await _bao_health(st)
    return {"ok": True, "initialized": health.get("initialized") if health else None,
            "sealed": health.get("sealed") if health else None,
            "stored_keys": True}


async def _do_unseal(st: Dict) -> Dict:
    raw = st.get("openbao_unseal", "")
    if not raw:
        return {"error": "no stored unseal keys"}
    try:
        keys = json.loads(raw)
    except Exception:
        keys = [raw]
    last = {}
    for k in keys:
        last, err = await _bao(st, "PUT", "/v1/sys/unseal", json_body={"key": k})
        if last is None:
            return {"error": err}
        if not last.get("sealed", True):
            break
    # Once unsealed, hand OpenBao to the shared secret helper so it becomes the
    # store of record for ALL new secrets (comms/accounts included) — see
    # vera/security/secrets.py backend selection. Export the address + token into
    # the process env, which is exactly what secrets._bao_cfg() reads.
    try:
        if last and not last.get("sealed", True):
            _export_bao_env(st)
    except Exception as e:
        log.debug("provisioning: could not export OpenBao env: %s", e)
    return last or {}


def _export_bao_env(st: Dict) -> None:
    """Publish the (opened) OpenBao address + token into os.environ so the
    leaf-level secrets helper routes new seals to the vault. `st` is a
    secret-OPENED state (token in plaintext)."""
    addr = (st.get("openbao_addr") or "").rstrip("/")
    token = st.get("openbao_token") or ""
    if not (addr and token):
        return
    os.environ["BAO_ADDR"] = addr
    os.environ["BAO_TOKEN"] = token
    if st.get("openbao_mount"):
        os.environ["BAO_KV_MOUNT"] = st["openbao_mount"]
    if st.get("openbao_namespace"):
        os.environ["BAO_NAMESPACE"] = st["openbao_namespace"]
    if st.get("verify_tls"):
        os.environ["BAO_VERIFY_TLS"] = "true"
    os.environ.setdefault("VERA_SECRET_BACKEND", "openbao")
    # Reset the secrets helper's cached probe so it re-detects immediately.
    try:
        vsecrets._bao_cache["active"] = None
    except Exception:
        pass
    log.info("provisioning: OpenBao is now the active secret backend (%s)", addr)


@capability(
    "secstore.unseal",
    http_method="POST", http_path="/provisioning/secstore/unseal",
    http_tags=["provisioning"], memory="off",
    description="Unseal OpenBao using the Fernet-sealed keys stored at bootstrap "
                "(safe to call on every Vera start). Output: {sealed}.",
)
async def cap_secstore_unseal(trace_id=None) -> Dict:
    st = await _state_opened()
    res = await _do_unseal(st)
    if res.get("error"):
        return res
    return {"ok": True, "sealed": res.get("sealed")}


@capability(
    "secstore.kv.put",
    http_method="POST", http_path="/provisioning/secstore/kv/put",
    http_tags=["provisioning"], memory="off",
    description="Write a secret to OpenBao KV v2. Inputs: path (str!), data "
                "(object! — key/value pairs). Output: {ok, version}.",
)
async def cap_kv_put(path: str = "", data: Optional[Dict] = None,
                     trace_id=None) -> Dict:
    if not path or not isinstance(data, dict):
        return {"error": "path and data{} required"}
    st = await _state_opened()
    mnt = st.get("openbao_mount", "secret")
    res, err = await _bao(st, "POST", f"/v1/{mnt}/data/{path}",
                          json_body={"data": data})
    if res is None:
        return {"error": err}
    return {"ok": True, "version": (res.get("data") or {}).get("version")}


@capability(
    "secstore.kv.get",
    http_method="POST", http_path="/provisioning/secstore/kv/get",
    http_tags=["provisioning"], memory="off",
    description="Read a secret from OpenBao KV v2. Input: path (str!). "
                "Output: {ok, data} or {ok:false} if missing.",
)
async def cap_kv_get(path: str = "", trace_id=None) -> Dict:
    if not path:
        return {"error": "path required"}
    st = await _state_opened()
    mnt = st.get("openbao_mount", "secret")
    res, err = await _bao(st, "GET", f"/v1/{mnt}/data/{path}")
    if res is None:
        return {"error": err}
    if res.get("_status") == 404:
        return {"ok": False}
    return {"ok": True, "data": (res.get("data") or {}).get("data", {})}


@capability(
    "secstore.kv.list",
    http_method="POST", http_path="/provisioning/secstore/kv/list",
    http_tags=["provisioning"], memory="off",
    description="List secret keys under a KV-v2 path. Input: path (str, default ''). "
                "Output: {keys:[...]}.",
)
async def cap_kv_list(path: str = "", trace_id=None) -> Dict:
    st = await _state_opened()
    mnt = st.get("openbao_mount", "secret")
    p = f"/v1/{mnt}/metadata/{path}".rstrip("/")
    res, err = await _bao(st, "GET", p, params={"list": "true"})
    if res is None:
        return {"error": err}
    if res.get("_status") == 404:
        return {"keys": []}
    return {"keys": (res.get("data") or {}).get("keys", [])}


@capability(
    "secstore.kv.delete",
    http_method="POST", http_path="/provisioning/secstore/kv/delete",
    http_tags=["provisioning"], memory="off",
    description="Delete a secret (all versions) from OpenBao KV v2. Input: path "
                "(str!). Output: {ok}.",
)
async def cap_kv_delete(path: str = "", trace_id=None) -> Dict:
    if not path:
        return {"error": "path required"}
    st = await _state_opened()
    mnt = st.get("openbao_mount", "secret")
    res, err = await _bao(st, "DELETE", f"/v1/{mnt}/metadata/{path}")
    if res is None:
        return {"error": err}
    return {"ok": True}


# ═════════════════════════════════════════════════════════════════════════════
#  STEP-CA CLIENT + PKI CAPABILITIES
# ═════════════════════════════════════════════════════════════════════════════
async def _stepca_health(st: Dict) -> Tuple[bool, str]:
    url = (st.get("stepca_url") or "").rstrip("/")
    if not url:
        return False, "stepca_url not configured"
    try:
        async with httpx.AsyncClient(timeout=10, verify=bool(st.get("verify_tls"))) as c:
            r = await c.get(url + "/health")
            return (r.status_code == 200 and "ok" in r.text.lower()), \
                   ("" if r.status_code == 200 else f"HTTP {r.status_code}")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


@capability(
    "pki.root",
    http_method="GET", http_path="/provisioning/pki/root", http_tags=["provisioning"],
    memory="off",
    description="Fetch step-ca's root CA certificate(s) (PEM) for trust "
                "distribution, and cache them in config. Output: {ok, roots_pem}.",
)
async def cap_pki_root(trace_id=None) -> Dict:
    st = await _state_opened()
    url = (st.get("stepca_url") or "").rstrip("/")
    if not url:
        return {"error": "stepca_url not configured"}
    try:
        async with httpx.AsyncClient(timeout=10, verify=bool(st.get("verify_tls"))) as c:
            r = await c.get(url + "/roots")
            if r.status_code >= 400:
                return {"error": f"HTTP {r.status_code}"}
            crts = (r.json() or {}).get("crts", [])
            pem = "\n".join(crts) if isinstance(crts, list) else str(crts)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if pem:
        await _state_save({"stepca_root_pem": pem})
    return {"ok": True, "roots_pem": pem}


@capability(
    "pki.bootstrap",
    http_method="POST", http_path="/provisioning/pki/bootstrap",
    http_tags=["provisioning"], memory="off",
    description="Adopt a step-ca instance: verify health, fetch + cache the root "
                "of trust. Inputs: url (str — sets stepca_url), fingerprint, "
                "provisioner. Output: {ok, reachable, has_root}.",
)
async def cap_pki_bootstrap(url: str = "", fingerprint: str = "",
                            provisioner: str = "", trace_id=None) -> Dict:
    patch = {"stepca_url": url, "stepca_fingerprint": fingerprint,
             "stepca_provisioner": provisioner}
    await _state_save({k: v for k, v in patch.items() if v})
    st = await _state_opened()
    ok, err = await _stepca_health(st)
    if not ok:
        return {"ok": False, "reachable": False, "error": err}
    root = await cap_pki_root()
    return {"ok": True, "reachable": True, "has_root": bool(root.get("roots_pem"))}


def _issue_script(st: Dict, fqdn: str, out_dir: str) -> str:
    """Commands to bootstrap trust + obtain a cert on a target host via the
    `step` CLI. Uses the configured provisioner; assumes `step` is installed."""
    url = st.get("stepca_url", "")
    fp = st.get("stepca_fingerprint", "")
    prov = st.get("stepca_provisioner", "")
    crt = f"{out_dir}/{fqdn}.crt"
    key = f"{out_dir}/{fqdn}.key"
    lines = [
        f"mkdir -p {out_dir}",
        f"step ca bootstrap --ca-url {url} --fingerprint {fp} --force --install || true",
    ]
    cert_cmd = f"step ca certificate {fqdn} {crt} {key} --force"
    if prov:
        cert_cmd += f" --provisioner {prov}"
    lines.append(cert_cmd)
    lines.append(f"echo '---CERT---'; cat {crt}")
    return " && ".join(lines)


@capability(
    "pki.cert.issue",
    http_method="POST", http_path="/provisioning/pki/cert/issue",
    http_tags=["provisioning"], memory="off",
    description="Issue a TLS cert for an FQDN from step-ca. Returns the ready-to-"
                "run bootstrap+issue commands; if an SSH target is given "
                "(ssh_host_id, or host/user/...), runs them on the target via "
                "exec.ssh.run, reads back the cert, and stores cert metadata in "
                "OpenBao under pki/<fqdn>. Inputs: fqdn (str!), ssh_host_id (str), "
                "out_dir (str='/etc/vera/certs'), install (bool=true when a target "
                "is given). Output: {ok, commands, ran, stdout, stored}.",
)
async def cap_cert_issue(fqdn: str = "", ssh_host_id: str = "",
                         out_dir: str = "/etc/vera/certs",
                         install: bool = True, trace_id=None) -> Dict:
    if not fqdn:
        return {"error": "fqdn required"}
    st = await _state_opened()
    if not st.get("stepca_url"):
        return {"error": "stepca_url not configured (run pki.bootstrap)"}
    script = _issue_script(st, fqdn, out_dir)
    out: Dict[str, Any] = {"ok": True, "commands": script, "ran": False}

    host_id = ssh_host_id or st.get("default_ssh_host_id", "")
    if install and host_id:
        ssh = _orch.CAPABILITY_REGISTRY.get("exec.ssh.run")
        if not ssh or not ssh.get("func"):
            out["error"] = "exec.ssh.run unavailable"
            return out
        res = await ssh["func"](command=script, host_id=host_id, timeout=120)
        out["ran"] = True
        out["stdout"] = (res or {}).get("stdout", "")
        out["stderr"] = (res or {}).get("stderr", "")
        cert_pem = ""
        if "---CERT---" in out["stdout"]:
            cert_pem = out["stdout"].split("---CERT---", 1)[1].strip()
        # Record an inventory entry (cert PEM is non-secret; the key stays on host).
        meta = {"fqdn": fqdn, "issued_at": now_iso(), "host_id": host_id,
                "cert_pem": cert_pem, "method": "step-cli"}
        kv = await cap_kv_put(path=f"pki/{fqdn}", data=meta)
        out["stored"] = bool(kv.get("ok"))
        await emit_event({"type": "provisioning.cert.issued", "fqdn": fqdn,
                          "host_id": host_id, "ok": bool(cert_pem)})
    return out


@capability(
    "pki.cert.list",
    http_method="GET", http_path="/provisioning/pki/cert/list",
    http_tags=["provisioning"], memory="off",
    description="List certs Vera has issued/recorded (from OpenBao pki/). "
                "Output: {certs:[{fqdn, issued_at, host_id}]}.",
)
async def cap_cert_list(trace_id=None) -> Dict:
    listing = await cap_kv_list(path="pki")
    certs = []
    for name in listing.get("keys", []):
        g = await cap_kv_get(path=f"pki/{name.rstrip('/')}")
        if g.get("ok"):
            d = g["data"]
            certs.append({"fqdn": d.get("fqdn", name), "issued_at": d.get("issued_at"),
                          "host_id": d.get("host_id", "")})
    return {"certs": certs}


# ═════════════════════════════════════════════════════════════════════════════
#  AUTO-CERT HOOK — called by provisioning flows (Iteration 2) or manually
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "provisioning.asset.online",
    http_method="POST", http_path="/provisioning/asset/online",
    http_tags=["provisioning"], memory="off",
    description="Register a freshly-online asset: issue a TLS cert (step-ca), "
                "store it in OpenBao, and install it on the asset when reachable. "
                "Designed to be called by the provisioning flow when a guest/LXC/"
                "VM/container comes up. Inputs: fqdn (str — or name+base_domain), "
                "name (str), ip (str), kind ('lxc'|'vm'|'docker'|'host'), "
                "ssh_host_id (str). Output: {ok, fqdn, cert}.",
)
async def cap_asset_online(fqdn: str = "", name: str = "", ip: str = "",
                           kind: str = "host", ssh_host_id: str = "",
                           trace_id=None) -> Dict:
    st = await _state_opened()
    if not fqdn:
        base = st.get("base_domain", "")
        fqdn = f"{name}.{base}" if (name and base) else (name or ip)
    if not fqdn:
        return {"error": "need fqdn, or name(+base_domain), or ip"}
    await emit_event({"type": "provisioning.online", "fqdn": fqdn, "ip": ip,
                      "kind": kind})
    # Best-effort identity registration (FreeIPA host + DNS A record) when the
    # Identity module (Iteration 4) is configured. Optional / decoupled.
    registered = None
    ident = _orch.CAPABILITY_REGISTRY.get("identity.host.register")
    if ident and ident.get("func"):
        try:
            registered = await ident["func"](fqdn=fqdn, ip=ip)
        except Exception as e:
            registered = {"error": str(e)}
    cert = await cap_cert_issue(fqdn=fqdn, ssh_host_id=ssh_host_id, install=True)
    return {"ok": not cert.get("error"), "fqdn": fqdn,
            "registered": registered, "cert": cert}


# ═════════════════════════════════════════════════════════════════════════════
#  PANEL
# ═════════════════════════════════════════════════════════════════════════════
@APP.get("/provisioning/panel", include_in_schema=False)
async def _provisioning_panel():
    p = _HERE / "provisioning_panel.html"
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<p style='color:red'>provisioning_panel.html not found</p>")


# Standalone top-level tab retired — embedded as the Security sub-tab of the
# workers/Ollama panel's Provision pane. The /provisioning/panel route is kept.
register_ui = (lambda *a, **k: None)
register_ui(
    "provisioning-panel",
    "Security",
    "🛡",
    """<div id="provisioning-panel-mount" style="height:100%;display:flex;flex-direction:column;">
  <iframe src="/provisioning/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          allow="clipboard-read; clipboard-write">
  </iframe>
</div>""",
    "",
    ui_caps=[
        "prov.config.get", "prov.config.save", "prov.status",
        "secstore.bootstrap", "secstore.unseal",
        "secstore.kv.put", "secstore.kv.get", "secstore.kv.list", "secstore.kv.delete",
        "pki.bootstrap", "pki.root", "pki.cert.issue", "pki.cert.list",
        "provisioning.asset.online",
        "exec.ssh.hosts.list",
    ],
    mode="tab",
    tab_order=56,
)


# Best-effort auto-unseal on startup so the vault is usable after a Vera restart.
async def _startup_unseal():
    try:
        st = await _state_opened()
        if st.get("openbao_addr") and st.get("openbao_unseal"):
            await _do_unseal(st)     # exports OpenBao env on success
            log.info("provisioning: OpenBao auto-unseal attempted")
        elif st.get("openbao_addr") and st.get("openbao_token"):
            # Already-unsealed external vault (no stored unseal keys) — still make
            # it the active secret backend if it's healthy.
            _export_bao_env(st)
    except Exception as e:
        log.debug("provisioning startup unseal: %s", e)


try:
    _loop = asyncio.get_event_loop()
    if _loop.is_running():
        _loop.create_task(_startup_unseal())
except Exception:
    pass


log.info("provisioning_capabilities ready")
