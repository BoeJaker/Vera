"""
stores_capabilities.py — provision Vera's backing stores onto Docker hosts
===========================================================================

The docker-compose stack ships redis/postgres/chromadb/neo4j/garage for the
local deployment. This module provisions those SAME stores onto any REGISTERED
Docker host (the docker.hosts.* registry — local socket, tcp daemon, or a host
reached over SSH), so blob storage and the other backends can be stood up
anywhere Vera can reach — the "provision the stores on docker" path.

Capabilities (group `provision.store*`)
───────────────────────────────────────
  provision.stores                  — catalog of provisionable stores
  provision.store.deploy           — run one store (or 'all') on a docker host
  provision.store.status           — container state + reachability probe
  provision.store.remove           — stop + remove a store container (volumes kept)
  provision.store.garage.bootstrap — layout/key/bucket via the garage ADMIN API
                                     (also fixes the local compose stack when the
                                     garage-init sidecar never completed)

Garage is special-cased: its config file is generated and written into a named
volume through a one-shot helper container (no bind mounts — works on remote
daemons too), the container is started from that volume, then the admin API
bootstraps layout/key/bucket so the S3 endpoint is immediately usable by the
data fabric (fabric.objects.*). Generated secrets are sealed (Fernet) into
Redis `vera:provisioning:stores` and returned ONCE by the deploy call.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets as _pysecrets
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, emit_event, now_iso
from Vera.vera.security import secrets as vsecrets

log = logging.getLogger("vera.provision.stores")

KEY_STORES = "vera:provisioning:stores"   # redis hash: "<host_id>:<store>" → JSON


# ─────────────────────────────────────────────────────────────────────────────
# Bridges (docker module loads after this one — resolve lazily at call time)
# ─────────────────────────────────────────────────────────────────────────────
def _dk():
    """The loaded docker_capabilities module, or None."""
    m = sys.modules.get("docker_capabilities")
    if m is not None:
        return m
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.endswith("docker_capabilities"):
            return mod
    return None


def _redis():
    return getattr(_orch, "REDIS", None)


async def _host_addr(dk, rec: Dict) -> str:
    """Best-effort address of a docker host as reachable FROM VERA (for probes
    and published-port endpoints)."""
    kind = rec.get("kind", "local")
    if kind == "tcp":
        u = urlparse(dk._normalize_tcp(rec.get("url", "")))
        return u.hostname or "localhost"
    if kind == "ssh":
        uh = await dk._ssh_user_host(rec.get("ssh_host_id", ""))
        if uh:
            return uh.split("@")[-1]
        return ""
    return "localhost"


# ─────────────────────────────────────────────────────────────────────────────
# Catalog — mirrors docker-compose.yml (same images, ports, volumes, env)
# ─────────────────────────────────────────────────────────────────────────────
_STORE_LABEL = "vera.store"

_STORES: Dict[str, Dict[str, Any]] = {
    "redis": {
        "label": "Redis", "image": "redis:7-alpine",
        "ports": {"6379": 6379},
        "volumes": {"vera-redis-data": "/data"},
        "cmd": "redis-server --appendonly yes --maxmemory 512mb "
               "--maxmemory-policy allkeys-lru",
        "probe": ("tcp", 6379),
        "desc": "Event streams, task queues, caching.",
    },
    "postgres": {
        "label": "PostgreSQL", "image": "postgres:16-alpine",
        "ports": {"5433": 5432},
        "volumes": {"vera-pg-data": "/var/lib/postgresql/data"},
        "env": {"POSTGRES_USER": "admin", "POSTGRES_PASSWORD": "admin",
                "POSTGRES_DB": "postgres"},
        "probe": ("tcp", 5433),
        "desc": "Persistent memory + data-fabric archive (source of truth).",
    },
    "chromadb": {
        "label": "ChromaDB", "image": "chromadb/chroma:0.5.23",
        "ports": {"8008": 8000},
        "volumes": {"vera-chroma-data": "/chroma/chroma"},
        "env": {"IS_PERSISTENT": "TRUE", "ANONYMIZED_TELEMETRY": "FALSE"},
        "probe": ("tcp", 8008),
        "desc": "Vector embeddings store.",
    },
    "neo4j": {
        "label": "Neo4j", "image": "neo4j:5-community",
        "ports": {"7474": 7474, "7687": 7687},
        "volumes": {"vera-neo4j-data": "/data"},
        "env": {"NEO4J_AUTH": "neo4j/veraneo4j", "NEO4J_PLUGINS": '["apoc"]',
                "NEO4J_server_memory_heap_max__size": "512m"},
        "probe": ("tcp", 7687),
        "desc": "Memory graph database.",
    },
    "garage": {
        "label": "Garage (S3 blob store)", "image": "dxflrs/garage:v1.0.1",
        "ports": {"3900": 3900, "3903": 3903},
        "volumes": {"vera-garage-meta": "/var/lib/garage/meta",
                    "vera-garage-data": "/var/lib/garage/data",
                    "vera-garage-etc": "/etc/garage"},
        "cmd": "/garage -c /etc/garage/garage.toml server",
        "probe": ("tcp", 3900),
        "special": "garage",
        "desc": "S3-compatible blob storage for the data fabric "
                "(fabric.objects.*). Deploy generates config + bootstraps "
                "layout/key/bucket via the admin API.",
    },
    "ollama": {
        "label": "Ollama", "image": "ollama/ollama:latest",
        "ports": {"11434": 11434},
        "volumes": {"vera-ollama": "/root/.ollama"},
        "probe": ("tcp", 11434),
        "gpus_hint": True,
        "desc": "LLM inference server. Pass gpus='all' on GPU hosts; CPU-only "
                "otherwise. This is the same container the Docker pane's Vera "
                "Stack widget deploys — one backend, two views.",
    },
}


def _garage_toml(rpc_secret: str, admin_token: str, region: str = "garage") -> str:
    return f"""# Generated by provision.store.deploy — Vera data-fabric object store
metadata_dir = "/var/lib/garage/meta"
data_dir     = "/var/lib/garage/data"
db_engine    = "sqlite"

replication_factor = 1

rpc_bind_addr   = "[::]:3901"
rpc_public_addr = "127.0.0.1:3901"
rpc_secret      = "{rpc_secret}"

[s3_api]
s3_region     = "{region}"
api_bind_addr = "[::]:3900"
root_domain   = ".s3.garage.localhost"

[admin]
api_bind_addr = "[::]:3903"
admin_token   = "{admin_token}"
"""


async def _write_volume_file(dk, rec: Dict, volume: str, path_in_vol: str,
                             content: str, timeout: int = 120) -> Dict:
    """Write a file into a named volume on the target daemon via a one-shot
    alpine container (portable — no bind mounts, works on remote daemons)."""
    b64 = base64.b64encode(content.encode("utf-8")).decode()
    args = ["run", "--rm", "-v", f"{volume}:/vol", "alpine:3.20", "sh", "-c",
            f"echo '{b64}' | base64 -d > /vol/{path_in_vol}"]
    argv = await dk._docker_argv(rec, args)
    ok, reason = dk._sandbox_gate(" ".join(argv))
    if not ok:
        return {"ok": False, "error": f"sandbox: {reason}"}
    res = await dk._run_local(argv, timeout=timeout)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or res.get("error")
                or "config write failed"}
    return {"ok": True}


async def _container_state(dk, rec: Dict, name: str) -> Optional[Dict]:
    """Inspect a container by exact name on a host. None if absent."""
    import urllib.parse
    filt = urllib.parse.quote(json.dumps({"name": [f"^/{name}$"]}))
    status, body, _ = await dk._engine_request(
        rec, "GET", f"/containers/json?all=true&filters={filt}")
    if status != 200:
        return None
    try:
        rows = json.loads(body or b"[]")
    except Exception:
        rows = []
    return rows[0] if rows else None


async def _probe(addr: str, kind: str, port: int, timeout: float = 4.0) -> bool:
    if not addr:
        return False
    try:
        if kind == "tcp":
            _r, w = await asyncio.wait_for(
                asyncio.open_connection(addr, int(port)), timeout=timeout)
            w.close()
            try:
                await w.wait_closed()
            except Exception:
                pass
            return True
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"http://{addr}:{port}/")
            return r.status_code < 500
    except Exception:
        return False


async def _record_save(host_id: str, store: str, rec: Dict) -> None:
    r = _redis()
    if not r:
        return
    try:
        await r.hset(KEY_STORES, f"{host_id}:{store}", json.dumps(rec))
    except Exception as e:
        log.debug("store record save: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────
@capability(
    "provision.stores",
    http_method="GET", http_path="/provision/stores", http_tags=["provision"],
    memory="off", silent=True,
    description="Catalog of Vera backing stores provisionable onto a Docker host "
                "(redis, postgres, chromadb, neo4j, garage blob store). Output: "
                "{stores:[{key,label,image,ports,desc,special}]}.",
)
async def cap_stores(trace_id=None) -> Dict:
    return {"stores": [
        {"key": k, "label": s["label"], "image": s["image"],
         "ports": s["ports"], "desc": s["desc"],
         "special": s.get("special", ""), "gpus_hint": s.get("gpus_hint", False)}
        for k, s in _STORES.items()
    ]}


@capability(
    "provision.store.deploy",
    http_method="POST", http_path="/provision/store/deploy", http_tags=["provision"],
    memory="off",
    description="Deploy a backing store (or 'all' of them) onto a registered "
                "Docker host as a named container (vera-<store>, label "
                "vera.store). Data lives in named volumes so redeploys keep it. "
                "Garage additionally gets a generated config (rpc secret + admin "
                "token, sealed into Redis and returned ONCE) and an automatic "
                "layout/key/bucket bootstrap via its admin API. Inputs: host_id "
                "(str — docker host, default 'local'), store (str! — "
                "redis|postgres|chromadb|neo4j|garage|all), env (dict — extra/"
                "override container env), ports (dict — host:container port "
                "overrides), s3_access/s3_secret/s3_bucket (str — garage key/"
                "bucket; default FABRIC_S3_* env), admin_token (str — garage "
                "admin token; default generated), bootstrap (bool=true — run the "
                "garage bootstrap after start). Output: per-store {ok, container_"
                "id|already, endpoint, bootstrap?, secrets?}.",
)
async def cap_store_deploy(host_id: str = "local", store: str = "",
                           env: Optional[Dict[str, str]] = None,
                           ports: Optional[Dict[str, Any]] = None,
                           s3_access: str = "", s3_secret: str = "",
                           s3_bucket: str = "", admin_token: str = "",
                           gpus: str = "", bootstrap: bool = True,
                           trace_id=None) -> Dict:
    dk = _dk()
    if dk is None:
        return {"error": "docker module not loaded"}
    if not store:
        return {"error": "store required (one of %s or 'all')" % "|".join(_STORES)}
    names = list(_STORES) if store.strip().lower() == "all" else [store.strip().lower()]
    unknown = [n for n in names if n not in _STORES]
    if unknown:
        return {"error": f"unknown store(s): {unknown}", "stores": list(_STORES)}
    rec = dk._get_host(host_id)
    if not rec:
        return {"error": f"unknown docker host: {host_id}"}
    addr = await _host_addr(dk, rec)

    results: Dict[str, Any] = {}
    for name in names:
        spec = _STORES[name]
        cname = f"vera-{name}"
        out: Dict[str, Any] = {"image": spec["image"], "container": cname}

        existing = await _container_state(dk, rec, cname)
        if existing:
            out.update({"ok": True, "already": True,
                        "state": existing.get("State", "")})
            # A stopped previous deployment is restarted rather than recreated.
            if existing.get("State") != "running":
                argv = await dk._docker_argv(rec, ["start", cname])
                res = await dk._run_local(argv, timeout=60)
                out["restarted"] = bool(res.get("ok"))
            results[name] = out
            continue

        port_map = {**{str(k): v for k, v in spec["ports"].items()},
                    **{str(k): v for k, v in (ports or {}).items()}}
        env_map = {**(spec.get("env") or {}), **(env or {})}
        secrets_out: Dict[str, str] = {}

        # ── garage: generate config into its config volume first ─────────────
        if spec.get("special") == "garage":
            rpc_secret = _pysecrets.token_hex(32)
            adm = admin_token or os.getenv("GARAGE_ADMIN_TOKEN", "") \
                or _pysecrets.token_urlsafe(24)
            region = os.getenv("FABRIC_S3_REGION", "garage")
            w = await _write_volume_file(
                dk, rec, "vera-garage-etc", "garage.toml",
                _garage_toml(rpc_secret, adm, region))
            if not w.get("ok"):
                out.update({"ok": False, "error": w.get("error")})
                results[name] = out
                continue
            secrets_out = {"admin_token": adm, "rpc_secret": rpc_secret}

        vols = ",".join(f"{v}:{p}" for v, p in spec["volumes"].items())
        prts = ",".join(f"{h}:{c}" for h, c in port_map.items())
        if spec.get("cmd"):
            # stores with a custom container command (redis flags, garage -c) —
            # cap_docker_run has no cmd parameter, so use the gated CLI directly.
            await dk._run_local(await dk._docker_argv(rec, ["pull", spec["image"]]),
                                timeout=600)
            run = await _run_with_cmd(dk, rec, name, spec, cname, prts,
                                      env_map, vols)
        else:
            extra = f"--label {_STORE_LABEL}={name}"
            if gpus and spec.get("gpus_hint"):
                extra += f" --gpus {gpus}"
            run = await dk.cap_docker_run(
                host_id=rec["id"], image=spec["image"], name=cname,
                ports=prts, env=env_map, volumes=vols,
                restart="unless-stopped",
                extra_args=extra,
                pull=True,
            )

        out["ok"] = bool(run.get("ok"))
        out["container_id"] = (run.get("container_id") or "")[:12]
        if not out["ok"]:
            out["error"] = run.get("error", "docker run failed")
            results[name] = out
            continue

        out["endpoint"] = f"{addr}:{list(port_map.keys())[0]}" if port_map else addr
        record = {"host_id": rec["id"], "store": name, "image": spec["image"],
                  "container": cname, "ports": port_map, "addr": addr,
                  "created": now_iso()}
        if secrets_out:
            record["sealed"] = {k: vsecrets.seal(v) for k, v in secrets_out.items()}
            out["secrets"] = secrets_out   # returned ONCE — sealed in Redis after
        await _record_save(rec["id"], name, record)

        # ── garage: bootstrap layout/key/bucket over the admin API ───────────
        if spec.get("special") == "garage" and bootstrap:
            adm_port = next((h for h, c in port_map.items() if int(c) == 3903), "3903")
            bs = await cap_garage_bootstrap(
                admin_url=f"http://{addr}:{adm_port}",
                admin_token=secrets_out.get("admin_token", ""),
                s3_access=s3_access, s3_secret=s3_secret, s3_bucket=s3_bucket,
                wait_secs=60)
            out["bootstrap"] = bs

        await emit_event({"type": "provision.store.deployed", "store": name,
                          "host_id": rec["id"], "ok": out["ok"]})
        results[name] = out

    return {"ok": all(v.get("ok") for v in results.values()), "host_id": rec["id"],
            "addr": addr, "stores": results}


async def _run_with_cmd(dk, rec: Dict, store: str, spec: Dict, cname: str,
                        ports: str, env_map: Dict[str, str], vols: str) -> Dict:
    """docker run -d with a custom container command (cap_docker_run has no cmd
    parameter). Same sandbox gate + labels as the capability path."""
    import shlex
    args = ["run", "-d", "--name", cname, "--restart", "unless-stopped",
            "--label", f"{_STORE_LABEL}={store}"]
    for p in [x for x in ports.split(",") if x]:
        args += ["-p", p]
    for v in [x for x in vols.split(",") if x]:
        args += ["-v", v]
    for k, val in env_map.items():
        args += ["-e", f"{k}={val}"]
    args += [spec["image"]] + shlex.split(spec["cmd"])
    argv = await dk._docker_argv(rec, args)
    ok, reason = dk._sandbox_gate(" ".join(argv))
    if not ok:
        return {"ok": False, "error": f"sandbox: {reason}"}
    res = await dk._run_local(argv, timeout=300)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("stderr") or res.get("error")
                or "docker run failed"}
    cid = (res.get("stdout", "") or "").strip().splitlines()[-1] \
        if res.get("stdout") else ""
    return {"ok": True, "container_id": cid}


@capability(
    "provision.store.status",
    http_method="POST", http_path="/provision/store/status", http_tags=["provision"],
    memory="off", silent=True,
    description="State of provisioned stores on a Docker host: container state "
                "(label vera.store, falling back to the canonical vera-<store> "
                "container name so compose-stack deployments are recognised too) "
                "+ a TCP probe of each store's primary port from Vera. Inputs: "
                "host_id (str='local'). Output: {stores:[{store, container, "
                "state, status, image, reachable}], addr}.",
)
async def cap_store_status(host_id: str = "local", trace_id=None) -> Dict:
    dk = _dk()
    if dk is None:
        return {"error": "docker module not loaded"}
    rec = dk._get_host(host_id)
    if not rec:
        return {"error": f"unknown docker host: {host_id}"}
    addr = await _host_addr(dk, rec)
    import urllib.parse
    filt = urllib.parse.quote(json.dumps({"label": [_STORE_LABEL]}))
    status, body, _ = await dk._engine_request(
        rec, "GET", f"/containers/json?all=true&filters={filt}")
    rows = []
    if status == 200:
        try:
            rows = json.loads(body or b"[]")
        except Exception:
            rows = []
    by_store: Dict[str, Dict] = {}
    for r in rows:
        name = (r.get("Labels") or {}).get(_STORE_LABEL, "")
        if name in _STORES:
            by_store[name] = r
    # docker-compose containers (container_name: vera-<store>) carry no
    # vera.store label — fall back to the canonical name so a running compose
    # stack doesn't report every backend as absent.
    for name in _STORES:
        if name not in by_store:
            row = await _container_state(dk, rec, f"vera-{name}")
            if row:
                by_store[name] = row
    out = []
    for name, spec in _STORES.items():
        r = by_store.get(name)
        probe_kind, probe_port = (spec.get("probe") or ("tcp", 0))
        reachable = await _probe(addr, probe_kind, probe_port) if probe_port else None
        out.append({
            "store": name,
            "container": (r.get("Names") or ["?"])[0].lstrip("/") if r else "",
            "state": (r.get("State", "") if r else "absent"),
            "status": (r.get("Status", "") if r else ""),
            "image": (r.get("Image", "") if r else spec["image"]),
            "reachable": reachable,
        })
    return {"host_id": rec["id"], "addr": addr, "stores": out,
            "count": sum(1 for s in out if s["state"] != "absent")}


@capability(
    "provision.store.remove",
    http_method="POST", http_path="/provision/store/remove", http_tags=["provision"],
    memory="off",
    description="Stop and remove a provisioned store container. Named data "
                "volumes are KEPT unless purge_volumes=true (DESTROYS the "
                "store's data). Inputs: host_id (str='local'), store (str!), "
                "purge_volumes (bool=false). Output: {ok, removed, purged}.",
)
async def cap_store_remove(host_id: str = "local", store: str = "",
                           purge_volumes: bool = False, trace_id=None) -> Dict:
    dk = _dk()
    if dk is None:
        return {"error": "docker module not loaded"}
    spec = _STORES.get((store or "").strip().lower())
    if not spec:
        return {"error": f"unknown store: {store}", "stores": list(_STORES)}
    rec = dk._get_host(host_id)
    if not rec:
        return {"error": f"unknown docker host: {host_id}"}
    cname = f"vera-{store.strip().lower()}"
    argv = await dk._docker_argv(rec, ["rm", "-f", cname])
    res = await dk._run_local(argv, timeout=60)
    purged: List[str] = []
    if purge_volumes:
        for vol in spec["volumes"]:
            argv = await dk._docker_argv(rec, ["volume", "rm", "-f", vol])
            vres = await dk._run_local(argv, timeout=40)
            if vres.get("ok"):
                purged.append(vol)
    r = _redis()
    if r:
        try:
            await r.hdel(KEY_STORES, f"{rec['id']}:{store.strip().lower()}")
        except Exception:
            pass
    await emit_event({"type": "provision.store.removed", "store": store,
                      "host_id": rec["id"], "purged": purged})
    return {"ok": res.get("ok", False), "removed": cname, "purged": purged,
            "detail": (res.get("stderr") or "").strip()}


# ─────────────────────────────────────────────────────────────────────────────
# GARAGE ADMIN-API BOOTSTRAP (shared by deploy + standalone repair)
# ─────────────────────────────────────────────────────────────────────────────
@capability(
    "provision.store.garage.bootstrap",
    http_method="POST", http_path="/provision/store/garage/bootstrap",
    http_tags=["provision", "fabric"], memory="off",
    description="Bootstrap (or repair) a Garage node over its ADMIN API: assign+"
                "apply a single-node layout, import the S3 access key, create "
                "the data-fabric bucket and grant read/write/owner. Idempotent — "
                "safe to re-run; use it when fabric.objects.status shows "
                "AccessDenied/'No such key' (the compose garage-init sidecar "
                "never completed). Afterwards the fabric ObjectStore reconnects "
                "automatically. Inputs: admin_url (str — default "
                "GARAGE_ADMIN_URL or http://localhost:3903), admin_token (str — "
                "default GARAGE_ADMIN_TOKEN env or the dev token), s3_access/"
                "s3_secret/s3_bucket (str — default FABRIC_S3_* env), capacity "
                "(int bytes=1000000000), wait_secs (int=20). Output: {ok, node, "
                "layout, key, bucket, object_store}.",
)
async def cap_garage_bootstrap(admin_url: str = "", admin_token: str = "",
                               s3_access: str = "", s3_secret: str = "",
                               s3_bucket: str = "", capacity: int = 1000000000,
                               wait_secs: int = 20, trace_id=None) -> Dict:
    admin_url = (admin_url or os.getenv("GARAGE_ADMIN_URL", "")
                 or "http://localhost:3903").rstrip("/")
    admin_token = admin_token or os.getenv("GARAGE_ADMIN_TOKEN",
                                           "vera-garage-admin-change-me")
    access = s3_access or os.getenv("FABRIC_S3_ACCESS", "")
    secret = s3_secret or os.getenv("FABRIC_S3_SECRET", "")
    bucket = s3_bucket or os.getenv("FABRIC_S3_BUCKET", "vera-data-fabric")
    if not access or not secret:
        return {"error": "s3_access/s3_secret required (or set FABRIC_S3_ACCESS/"
                         "FABRIC_S3_SECRET)"}

    hdr = {"Authorization": f"Bearer {admin_token}"}
    out: Dict[str, Any] = {"admin_url": admin_url, "bucket": bucket}

    async with httpx.AsyncClient(timeout=20) as c:
        # ── wait for the admin API ────────────────────────────────────────────
        node = ""
        deadline = asyncio.get_event_loop().time() + max(1, int(wait_secs))
        last_err = ""
        while asyncio.get_event_loop().time() < deadline:
            try:
                r = await c.get(admin_url + "/v1/status", headers=hdr)
                if r.status_code == 200:
                    node = (r.json() or {}).get("node", "")
                    break
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            await asyncio.sleep(2)
        if not node:
            return {"error": f"garage admin API unreachable at {admin_url}: "
                             f"{last_err}"}
        out["node"] = node

        # ── 1. layout ─────────────────────────────────────────────────────────
        r = await c.get(admin_url + "/v1/layout", headers=hdr)
        layout = r.json() if r.status_code == 200 else {}
        assigned = any(role.get("id") == node
                       for role in (layout.get("roles") or []))
        if assigned:
            out["layout"] = "already-assigned"
        else:
            r = await c.post(admin_url + "/v1/layout", headers=hdr,
                             json=[{"id": node, "zone": "dc1",
                                    "capacity": int(capacity), "tags": ["vera"]}])
            if r.status_code >= 400:
                return {**out, "error": f"layout stage failed: HTTP "
                                        f"{r.status_code}: {r.text[:300]}"}
            ver = int(layout.get("version") or 0) + 1
            r = await c.post(admin_url + "/v1/layout/apply", headers=hdr,
                             json={"version": ver})
            if r.status_code >= 400:
                return {**out, "error": f"layout apply failed: HTTP "
                                        f"{r.status_code}: {r.text[:300]}"}
            out["layout"] = f"applied-v{ver}"

        # ── 2. access key (idempotent import) ─────────────────────────────────
        r = await c.get(admin_url + f"/v1/key?id={access}", headers=hdr)
        if r.status_code == 200:
            out["key"] = "already-present"
        else:
            r = await c.post(admin_url + "/v1/key/import", headers=hdr,
                             json={"accessKeyId": access,
                                   "secretAccessKey": secret,
                                   "name": "vera-fabric"})
            if r.status_code >= 400:
                return {**out, "error": f"key import failed: HTTP "
                                        f"{r.status_code}: {r.text[:300]}"}
            out["key"] = "imported"

        # ── 3. bucket + grant ─────────────────────────────────────────────────
        r = await c.get(admin_url + f"/v1/bucket?globalAlias={bucket}", headers=hdr)
        if r.status_code == 200:
            bid = (r.json() or {}).get("id", "")
            out["bucket_state"] = "already-present"
        else:
            r = await c.post(admin_url + "/v1/bucket", headers=hdr,
                             json={"globalAlias": bucket})
            if r.status_code >= 400:
                return {**out, "error": f"bucket create failed: HTTP "
                                        f"{r.status_code}: {r.text[:300]}"}
            bid = (r.json() or {}).get("id", "")
            out["bucket_state"] = "created"
        if not bid:
            return {**out, "error": "could not resolve bucket id"}
        r = await c.post(admin_url + "/v1/bucket/allow", headers=hdr,
                         json={"bucketId": bid, "accessKeyId": access,
                               "permissions": {"read": True, "write": True,
                                               "owner": True}})
        if r.status_code >= 400:
            return {**out, "error": f"bucket grant failed: HTTP "
                                    f"{r.status_code}: {r.text[:300]}"}

    # ── reconnect the fabric ObjectStore so blobs work immediately ───────────
    obj = None
    fab = sys.modules.get("data_fabric")
    if fab is not None and hasattr(fab, "OBJECT_STORE"):
        try:
            fab.OBJECT_STORE._client = None
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(None, fab.OBJECT_STORE.ensure)
            obj = {"available": bool(ok),
                   "last_error": getattr(fab.OBJECT_STORE, "last_error", "")}
        except Exception as e:
            obj = {"available": False, "last_error": str(e)}

    await emit_event({"type": "provision.store.garage.bootstrapped",
                      "admin_url": admin_url, "bucket": bucket})
    return {"ok": True, **out, "object_store": obj}


log.info("stores_capabilities ready — %d provisionable stores", len(_STORES))
