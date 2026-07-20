"""
security_provision_capabilities.py — provision Security / Identity / Net-Policy /
Enroll backing services (Phase 5)
=================================================================================

The Proxmox dashboard exposes four control systems — **Security, Identity, Net
Policy, Enroll** — but they only become *usable* once their backing services are
running. This module provisions those services onto any registered Docker host
(docker.hosts.*), exactly like stores_capabilities does for the data stores, so
the four systems light up:

  • Security  → OpenBao (secrets/KV/transit) + step-ca (private PKI)
  • Identity  → lldap  (lightweight LDAP directory)
  • Net Policy→ OPA    (Open Policy Agent — network/authz policy engine)
  • Enroll    → step-ca ACME (device/cert enrolment; shared with Security)

Capabilities (group `secprov.*`)
────────────────────────────────
  secprov.services   — catalog of provisionable backing services
  secprov.deploy     — run one (or 'all') on a docker host; generated secrets are
                       sealed (Fernet) into Redis and returned ONCE
  secprov.status     — container state + reachability probe
  secprov.remove     — stop + remove a service container (named volumes kept)

Provisioning goes through the sandbox-gated docker.run (now with a `command`
param), and configs that need a file are written into a named volume via a
throwaway helper container (works on remote/ssh daemons too — no bind mounts).
"""

from __future__ import annotations

import base64
import json
import logging
import secrets as _pysecrets
import sys
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, emit_event, now_iso

try:
    from Vera.vera.security import secrets as vsecrets
    _HAS_SECRETS = True
except Exception:                       # pragma: no cover
    vsecrets = None                     # type: ignore
    _HAS_SECRETS = False

log = logging.getLogger("vera.provision.security")
KEY_SEC = "vera:provisioning:security"     # redis hash: "<host_id>:<svc>" → JSON
_LABEL = "vera.secsvc"


def _redis():
    return getattr(_orch, "REDIS", None)


def _dk():
    m = sys.modules.get("docker_capabilities")
    if m is not None:
        return m
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.endswith("docker_capabilities"):
            return mod
    return None


def _seal(v: str) -> str:
    if v and _HAS_SECRETS:
        try:
            return vsecrets.seal(v)
        except Exception:
            return v
    return v


async def _host_addr(dk, rec: Dict) -> str:
    kind = rec.get("kind", "local")
    if kind == "tcp":
        u = urlparse(dk._normalize_tcp(rec.get("url", "")))
        return u.hostname or "localhost"
    if kind == "ssh":
        uh = await dk._ssh_user_host(rec.get("ssh_host_id", ""))
        return uh.split("@")[-1] if uh else ""
    return "localhost"


# ─────────────────────────────────────────────────────────────────────────────
# Catalog. `secrets` = generated values sealed into Redis + returned once.
# `env`/`command` are templated with {addr} and the generated secrets.
# ─────────────────────────────────────────────────────────────────────────────
_SERVICES: Dict[str, Dict[str, Any]] = {
    "openbao": {
        "system": "Security", "label": "OpenBao (secrets)",
        "image": "openbao/openbao:latest", "ports": {"8200": 8200},
        "gen": {"root_token": lambda: _pysecrets.token_urlsafe(24)},
        "env": {"BAO_DEV_ROOT_TOKEN_ID": "{root_token}",
                "BAO_DEV_LISTEN_ADDRESS": "0.0.0.0:8200"},
        "command": "server -dev",
        "extra_args": "--cap-add=IPC_LOCK",
        "probe": 8200,
        "desc": "HashiCorp-Vault-compatible secrets engine (dev mode — data is "
                "in-memory; use for wiring/testing, then move to a sealed server).",
    },
    "step-ca": {
        "system": "Security / Enroll", "label": "step-ca (private PKI + ACME)",
        "image": "smallstep/step-ca:latest", "ports": {"9000": 9000},
        "volumes": {"vera-stepca": "/home/step"},
        "gen": {"ca_password": lambda: _pysecrets.token_urlsafe(20)},
        "env": {"DOCKER_STEPCA_INIT_NAME": "Vera CA",
                "DOCKER_STEPCA_INIT_DNS_NAMES": "localhost,{addr}",
                "DOCKER_STEPCA_INIT_ACME": "true",
                "DOCKER_STEPCA_INIT_PASSWORD": "{ca_password}"},
        "probe": 9000,
        "desc": "Online private CA with an ACME endpoint — backs certificate "
                "issuance and device Enrolment. Auto-initialises on first run.",
    },
    "lldap": {
        "system": "Identity", "label": "lldap (LDAP directory)",
        "image": "lldap/lldap:latest", "ports": {"17170": 17170, "3890": 3890},
        "volumes": {"vera-lldap": "/data"},
        "gen": {"jwt_secret": lambda: _pysecrets.token_hex(32),
                "admin_password": lambda: _pysecrets.token_urlsafe(16)},
        "env": {"LLDAP_JWT_SECRET": "{jwt_secret}",
                "LLDAP_LDAP_USER_PASS": "{admin_password}",
                "LLDAP_LDAP_BASE_DN": "dc=vera,dc=local"},
        "probe": 17170,
        "desc": "Lightweight LDAP directory with a web admin (:17170) and LDAP "
                "(:3890) — backs the Identity system.",
    },
    "opa": {
        "system": "Net Policy", "label": "OPA (policy engine)",
        "image": "openpolicyagent/opa:latest", "ports": {"8181": 8181},
        "command": "run --server --addr=0.0.0.0:8181 --log-level info",
        "probe": 8181,
        "desc": "Open Policy Agent — evaluates network/authz policy (Rego). Backs "
                "the Net Policy system.",
    },
}


def _fmt(v: str, ctx: Dict[str, str]) -> str:
    try:
        return v.format(**ctx)
    except Exception:
        return v


@capability(
    "secprov.services",
    http_method="GET", http_path="/provision/security/services", http_tags=["provision"],
    memory="off", silent=True,
    description="Catalog of Security/Identity/Net-Policy/Enroll backing services "
                "provisionable onto a Docker host. Output: {services:[{key,system,"
                "label,image,ports,desc}]}.",
)
async def cap_sec_services(trace_id=None) -> Dict:
    return {"services": [
        {"key": k, "system": s["system"], "label": s["label"], "image": s["image"],
         "ports": s.get("ports", {}), "desc": s["desc"]}
        for k, s in _SERVICES.items()]}


async def _container_state(dk, rec: Dict, cname: str) -> Optional[Dict]:
    import urllib.parse
    filt = urllib.parse.quote(json.dumps({"name": [cname]}))
    try:
        status, body, _ = await dk._engine_request(
            rec, "GET", f"/containers/json?all=true&filters={filt}")
        rows = json.loads(body or b"[]") if status == 200 else []
        for r in rows:
            if any(n.lstrip("/") == cname for n in (r.get("Names") or [])):
                return r
    except Exception:
        pass
    return None


async def _write_volume_file(dk, rec: Dict, volume: str, path_in_vol: str,
                             content: str) -> Dict:
    """Write a file into a named volume via a throwaway busybox container."""
    b64 = base64.b64encode(content.encode()).decode()
    run = await dk.cap_docker_run(
        host_id=rec["id"], image="busybox:latest",
        name=f"vera-secw-{uuid.uuid4().hex[:6]}", restart="no",
        volumes=f"{volume}:/mnt",
        command=f"sh -c \"echo {b64} | base64 -d > /mnt/{path_in_vol}\"")
    return {"ok": bool(run.get("ok")), "error": run.get("error", "")}


@capability(
    "secprov.deploy",
    http_method="POST", http_path="/provision/security/deploy", http_tags=["provision"],
    memory="off",
    description="Deploy a Security/Identity/Net-Policy/Enroll backing service (or "
                "'all') onto a registered Docker host as vera-<svc> (label "
                "vera.secsvc). Generated secrets (root tokens, CA/admin passwords) "
                "are sealed into Redis and returned ONCE in the response. Inputs: "
                "host_id (str — docker host, default 'local'), service (str! — "
                "openbao|step-ca|lldap|opa|all), env (dict — extra/override env), "
                "ports (dict — host:container overrides). Output: per-service "
                "{ok, container_id|already, endpoint, secrets?}.",
)
async def cap_sec_deploy(host_id: str = "local", service: str = "",
                         env: Optional[Dict[str, str]] = None,
                         ports: Optional[Dict[str, Any]] = None, trace_id=None) -> Dict:
    dk = _dk()
    if dk is None:
        return {"error": "docker module not loaded"}
    if not service:
        return {"error": "service required (%s or 'all')" % "|".join(_SERVICES)}
    names = list(_SERVICES) if service.strip().lower() == "all" else [service.strip().lower()]
    unknown = [n for n in names if n not in _SERVICES]
    if unknown:
        return {"error": f"unknown service(s): {unknown}", "services": list(_SERVICES)}
    rec = dk._get_host(host_id)
    if not rec:
        return {"error": f"unknown docker host: {host_id}"}
    addr = await _host_addr(dk, rec)

    results: Dict[str, Any] = {}
    for name in names:
        spec = _SERVICES[name]
        cname = f"vera-{name}"
        out: Dict[str, Any] = {"image": spec["image"], "container": cname,
                               "system": spec["system"]}

        existing = await _container_state(dk, rec, cname)
        if existing:
            out.update({"ok": True, "already": True, "state": existing.get("State", "")})
            if existing.get("State") != "running":
                res = await dk._run_local(await dk._docker_argv(rec, ["start", cname]),
                                          timeout=60)
                out["restarted"] = bool(res.get("ok"))
            results[name] = out
            continue

        # Generate secrets + build the templating context.
        gen = {k: fn() for k, fn in (spec.get("gen") or {}).items()}
        ctx = {"addr": addr or "localhost", **gen}
        env_map = {k: _fmt(v, ctx) for k, v in (spec.get("env") or {}).items()}
        env_map.update(env or {})
        port_map = {**{str(k): v for k, v in spec.get("ports", {}).items()},
                    **{str(k): v for k, v in (ports or {}).items()}}
        vols = ",".join(f"{v}:{p}" for v, p in (spec.get("volumes") or {}).items())
        prts = ",".join(f"{h}:{c}" for h, c in port_map.items())
        extra = f"--label {_LABEL}={name}"
        if spec.get("extra_args"):
            extra += " " + spec["extra_args"]

        run = await dk.cap_docker_run(
            host_id=rec["id"], image=spec["image"], name=cname, ports=prts,
            env=env_map, volumes=vols, restart="unless-stopped",
            extra_args=extra, command=_fmt(spec.get("command", ""), ctx), pull=True)

        out["ok"] = bool(run.get("ok"))
        out["container_id"] = (run.get("container_id") or "")[:12]
        if not out["ok"]:
            out["error"] = run.get("error", "docker run failed")
            results[name] = out
            continue
        out["endpoint"] = f"{addr}:{list(port_map.keys())[0]}" if port_map else addr

        record = {"host_id": rec["id"], "service": name, "system": spec["system"],
                  "image": spec["image"], "container": cname, "ports": port_map,
                  "addr": addr, "created": now_iso()}
        if gen:
            record["sealed"] = {k: _seal(v) for k, v in gen.items()}
            out["secrets"] = gen        # returned ONCE; sealed in Redis
        r = _redis()
        if r:
            await r.hset(KEY_SEC, f"{rec['id']}:{name}", json.dumps(record))
        await emit_event({"type": "provision.security.deployed", "service": name,
                          "system": spec["system"], "host_id": rec["id"], "ok": out["ok"]})
        results[name] = out

    return {"ok": all(v.get("ok") for v in results.values()), "host_id": rec["id"],
            "addr": addr, "services": results}


@capability(
    "secprov.status",
    http_method="POST", http_path="/provision/security/status", http_tags=["provision"],
    memory="off",
    description="State + reachability of the security backing services on a Docker "
                "host. Inputs: host_id (str='local'). Output: {host, services:"
                "[{key,system,container,state,reachable,endpoint}]}.",
)
async def cap_sec_status(host_id: str = "local", trace_id=None) -> Dict:
    dk = _dk()
    if dk is None:
        return {"error": "docker module not loaded"}
    rec = dk._get_host(host_id)
    if not rec:
        return {"error": f"unknown docker host: {host_id}"}
    addr = await _host_addr(dk, rec)
    import asyncio
    out = []
    for name, spec in _SERVICES.items():
        cname = f"vera-{name}"
        state = await _container_state(dk, rec, cname)
        reachable = False
        port = list(spec.get("ports", {}).keys())[0] if spec.get("ports") else spec.get("probe")
        if port:
            try:
                fut = asyncio.open_connection(addr or "localhost", int(port))
                _, w = await asyncio.wait_for(fut, timeout=1.0)
                w.close()
                reachable = True
            except Exception:
                reachable = False
        out.append({"key": name, "system": spec["system"], "container": cname,
                    "state": (state or {}).get("State", "absent"),
                    "reachable": reachable,
                    "endpoint": f"{addr}:{port}" if port else addr})
    return {"host": addr, "services": out}


@capability(
    "secprov.remove",
    http_method="POST", http_path="/provision/security/remove", http_tags=["provision"],
    description="Stop and remove a security backing-service container (named "
                "volumes are kept). Inputs: host_id (str='local'), service (str!). "
                "Output: {ok}.",
)
async def cap_sec_remove(host_id: str = "local", service: str = "", trace_id=None) -> Dict:
    dk = _dk()
    if dk is None:
        return {"error": "docker module not loaded"}
    if service not in _SERVICES:
        return {"error": f"unknown service: {service}"}
    rec = dk._get_host(host_id)
    if not rec:
        return {"error": f"unknown docker host: {host_id}"}
    cname = f"vera-{service}"
    res = await dk._run_local(await dk._docker_argv(rec, ["rm", "-f", cname]), timeout=40)
    r = _redis()
    if r:
        await r.hdel(KEY_SEC, f"{rec['id']}:{service}")
    await emit_event({"type": "provision.security.removed", "service": service,
                      "host_id": rec["id"]})
    return {"ok": res.get("ok", False), "stderr": res.get("stderr", "")}


log.info("security_provision_capabilities loaded — openbao/step-ca/lldap/opa")
