"""
metrics_capabilities.py — Prometheus / cAdvisor / node-exporter (Phase 4)
=========================================================================

Adds a real metrics backend to Vera so the monitor/sysmon surface can show
container- and host-level time series, with the option to *provision* the
exporter stack on any registered Docker host when it isn't running yet.

  metrics.prom.save / list / delete    — Prometheus endpoint store (basic-auth
                                          password sealed via security/secrets)
  metrics.prom.query                   — instant PromQL query
  metrics.prom.query_range             — range PromQL query (for graphs)
  metrics.prom.targets                 — Prometheus scrape-target health

  metrics.stack.status                 — detect cadvisor / node-exporter /
                                          prometheus on a Docker host
  metrics.stack.provision              — docker-run the missing pieces (cAdvisor
                                          :8080, node-exporter :9100, Prometheus
                                          :9090 with a generated scrape config)

Provisioning goes through the existing sandbox-gated `docker.run` capability, so
the exec-sandbox policy governs it exactly like every other container Vera runs.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    capability, emit_event, now_iso,
)

try:
    from Vera.vera.security import secrets as vsecrets
    _HAS_SECRETS = True
except Exception:                       # pragma: no cover
    vsecrets = None                     # type: ignore
    _HAS_SECRETS = False

log = logging.getLogger("vera.remote.metrics")
KEY_PROM = "vera:remote:prometheus"


def _redis():
    return getattr(_orch, "REDIS", None)


def _cap_raw(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return c.get("raw") if c else None


def _seal(v: str) -> str:
    if v and _HAS_SECRETS:
        try:
            return vsecrets.seal(v)
        except Exception:
            return v
    return v


def _open(v: str) -> str:
    if v and _HAS_SECRETS:
        try:
            return vsecrets.open_secret(v)
        except Exception:
            return v
    return v


async def _tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        fut = asyncio.open_connection(host, port)
        _, w = await asyncio.wait_for(fut, timeout=timeout)
        w.close()
        return True
    except Exception:
        return False


async def _docker_host_addr(host_id: str) -> str:
    """Address of a registered Docker host reachable from the orchestrator."""
    d_hosts = _cap_raw("docker.hosts.list")
    if not d_hosts:
        return "127.0.0.1"
    try:
        for h in (await d_hosts()).get("hosts", []):
            if h.get("id") == (host_id or "local"):
                url = h.get("url", "")
                if url:
                    return urlparse(url.replace("tcp://", "http://")).hostname or "127.0.0.1"
                return "127.0.0.1"
    except Exception:
        pass
    return "127.0.0.1"


# ═════════════════════════════════════════════════════════════════════════════
#  PROMETHEUS ENDPOINT STORE + QUERY
# ═════════════════════════════════════════════════════════════════════════════
async def _all_prom() -> List[Dict]:
    r = _redis()
    if not r:
        return []
    try:
        items = await r.hgetall(KEY_PROM)
    except Exception:
        return []
    out = []
    for v in items.values():
        try:
            out.append(json.loads(v))
        except Exception:
            continue
    out.sort(key=lambda p: (p.get("label") or p.get("id") or ""))
    return out


async def _get_prom(prom_id: str, opened: bool = False) -> Optional[Dict]:
    r = _redis()
    if not r or not prom_id:
        return None
    raw = await r.hget(KEY_PROM, prom_id)
    if not raw:
        return None
    rec = json.loads(raw)
    if opened:
        rec = dict(rec)
        rec["password"] = _open(rec.get("password", ""))
    return rec


async def _default_prom() -> Optional[Dict]:
    all_p = await _all_prom()
    for p in all_p:
        if p.get("default"):
            return await _get_prom(p["id"], opened=True)
    return await _get_prom(all_p[0]["id"], opened=True) if all_p else None


@capability(
    "metrics.prom.save",
    http_method="POST", http_path="/remote/metrics/prom/save", http_tags=["remote", "metrics"],
    memory="off",
    description="Add/update a Prometheus endpoint. Basic-auth password is sealed. "
                "Inputs: label (str), url (str! — e.g. http://host:9090), user "
                "(str), password (str — blank keeps existing), verify_tls "
                "(bool=false), make_default (bool), id (str — update). "
                "Output: {ok, prometheus(redacted)}.",
)
async def cap_prom_save(label: str = "", url: str = "", user: str = "",
                        password: str = "", verify_tls: bool = False,
                        make_default: bool = False, id: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r:
        return {"ok": False, "error": "store unavailable"}
    if not url and not id:
        return {"ok": False, "error": "url required"}
    pid = id or uuid.uuid4().hex[:12]
    existing = await _get_prom(pid) or {}
    rec = dict(existing) if existing else {"id": pid, "created": now_iso()}
    rec["id"] = pid
    if url:
        rec["url"] = url.rstrip("/")
    rec["label"] = label or rec.get("label") or rec.get("url", pid)
    rec["user"] = user if user else rec.get("user", "")
    if password:
        rec["password"] = _seal(password)
    rec.setdefault("password", "")
    rec["verify_tls"] = bool(verify_tls)
    rec["default"] = bool(make_default) or rec.get("default", False)
    rec["updated"] = now_iso()
    if make_default:
        for other in await _all_prom():
            if other["id"] != pid and other.get("default"):
                other["default"] = False
                await r.hset(KEY_PROM, other["id"], json.dumps(other))
    await r.hset(KEY_PROM, pid, json.dumps(rec))
    red = {k: v for k, v in rec.items() if k != "password"}
    red["has_password"] = bool(rec.get("password"))
    return {"ok": True, "prometheus": red}


@capability(
    "metrics.prom.list",
    http_method="GET", http_path="/remote/metrics/prom/list", http_tags=["remote", "metrics"],
    memory="off", silent=True,
    description="List Prometheus endpoints (password redacted). Output: {prometheus:[...]}.",
)
async def cap_prom_list(trace_id=None) -> Dict:
    out = []
    for p in await _all_prom():
        red = {k: v for k, v in p.items() if k != "password"}
        red["has_password"] = bool(p.get("password"))
        out.append(red)
    return {"prometheus": out, "count": len(out)}


@capability(
    "metrics.prom.delete",
    http_method="POST", http_path="/remote/metrics/prom/delete", http_tags=["remote", "metrics"],
    description="Delete a Prometheus endpoint. Input: id (str!). Output: {ok}.",
)
async def cap_prom_delete(id: str = "", trace_id=None) -> Dict:
    r = _redis()
    if not r or not id:
        return {"ok": False, "error": "id required"}
    await r.hdel(KEY_PROM, id)
    return {"ok": True, "deleted": id}


async def _prom_get(rec: Dict, path: str, params: Dict) -> Dict:
    url = rec["url"].rstrip("/") + path
    auth = None
    if rec.get("user"):
        auth = (rec["user"], rec.get("password", ""))
    try:
        async with httpx.AsyncClient(verify=bool(rec.get("verify_tls")), timeout=20) as c:
            r = await c.get(url, params=params, auth=auth)
            if r.status_code >= 400:
                return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
            return r.json()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@capability(
    "metrics.prom.query",
    http_method="POST", http_path="/remote/metrics/prom/query", http_tags=["remote", "metrics"],
    memory="off",
    description="Run an instant PromQL query against a Prometheus endpoint. "
                "Inputs: query (str!), prom_id (str — blank uses the default "
                "endpoint), time (str — RFC3339/unix, optional). Output: {ok, "
                "resultType, result:[...]} or {error}.",
)
async def cap_prom_query(query: str = "", prom_id: str = "", time: str = "",
                         trace_id=None) -> Dict:
    if not query:
        return {"ok": False, "error": "query required"}
    rec = await _get_prom(prom_id, opened=True) if prom_id else await _default_prom()
    if not rec:
        return {"ok": False, "error": "no Prometheus endpoint configured"}
    params = {"query": query}
    if time:
        params["time"] = time
    data = await _prom_get(rec, "/api/v1/query", params)
    if data.get("error"):
        return {"ok": False, "error": data["error"]}
    d = data.get("data", {})
    return {"ok": True, "resultType": d.get("resultType"), "result": d.get("result", [])}


@capability(
    "metrics.prom.query_range",
    http_method="POST", http_path="/remote/metrics/prom/query_range", http_tags=["remote", "metrics"],
    memory="off",
    description="Run a range PromQL query (for time-series graphs). Inputs: query "
                "(str!), start (str! — RFC3339/unix), end (str! ), step (str='30s'), "
                "prom_id (str). Output: {ok, resultType, result:[...]} or {error}.",
)
async def cap_prom_query_range(query: str = "", start: str = "", end: str = "",
                               step: str = "30s", prom_id: str = "", trace_id=None) -> Dict:
    if not (query and start and end):
        return {"ok": False, "error": "query, start and end required"}
    rec = await _get_prom(prom_id, opened=True) if prom_id else await _default_prom()
    if not rec:
        return {"ok": False, "error": "no Prometheus endpoint configured"}
    data = await _prom_get(rec, "/api/v1/query_range",
                           {"query": query, "start": start, "end": end, "step": step})
    if data.get("error"):
        return {"ok": False, "error": data["error"]}
    d = data.get("data", {})
    return {"ok": True, "resultType": d.get("resultType"), "result": d.get("result", [])}


@capability(
    "metrics.prom.targets",
    http_method="POST", http_path="/remote/metrics/prom/targets", http_tags=["remote", "metrics"],
    memory="off", silent=True,
    description="List Prometheus scrape targets and their health. Input: prom_id "
                "(str). Output: {ok, active:[{job,instance,health,lastError}]}.",
)
async def cap_prom_targets(prom_id: str = "", trace_id=None) -> Dict:
    rec = await _get_prom(prom_id, opened=True) if prom_id else await _default_prom()
    if not rec:
        return {"ok": False, "error": "no Prometheus endpoint configured"}
    data = await _prom_get(rec, "/api/v1/targets", {})
    if data.get("error"):
        return {"ok": False, "error": data["error"]}
    active = [{"job": t.get("labels", {}).get("job", ""),
              "instance": t.get("labels", {}).get("instance", ""),
              "health": t.get("health", ""), "lastError": t.get("lastError", "")}
              for t in data.get("data", {}).get("activeTargets", [])]
    return {"ok": True, "active": active}


# ═════════════════════════════════════════════════════════════════════════════
#  EXPORTER STACK — status + provision
# ═════════════════════════════════════════════════════════════════════════════
_STACK = {
    "cadvisor":      {"port": 8080, "image": "gcr.io/cadvisor/cadvisor:latest",
                      "name": "vera-cadvisor"},
    "node_exporter": {"port": 9100, "image": "prom/node-exporter:latest",
                      "name": "vera-node-exporter"},
    "prometheus":    {"port": 9090, "image": "prom/prometheus:latest",
                      "name": "vera-prometheus"},
}


@capability(
    "metrics.stack.status",
    http_method="POST", http_path="/remote/metrics/stack/status", http_tags=["remote", "metrics"],
    memory="off",
    description="Detect whether cAdvisor / node-exporter / Prometheus are running "
                "on a Docker host (by probing their ports). Inputs: host_id (str — "
                "docker host, default local), host (str — override address). "
                "Output: {host, components:{cadvisor:bool, node_exporter:bool, "
                "prometheus:bool}, urls:{...}}.",
)
async def cap_stack_status(host_id: str = "", host: str = "", trace_id=None) -> Dict:
    addr = host or await _docker_host_addr(host_id)
    comps, urls = {}, {}
    for key, spec in _STACK.items():
        up = await _tcp_open(addr, spec["port"], 1.0)
        comps[key] = up
        if up:
            urls[key] = f"http://{addr}:{spec['port']}"
    return {"host": addr, "components": comps, "urls": urls}


def _prom_config(addr: str) -> str:
    """Minimal Prometheus scrape config for the provisioned exporters. Uses
    host.docker.internal (mapped to host-gateway) so Prometheus can reach the
    sibling exporters via the host's published ports on any Docker host."""
    return (
        "global:\n"
        "  scrape_interval: 15s\n"
        "scrape_configs:\n"
        "  - job_name: prometheus\n"
        "    static_configs:\n"
        "      - targets: ['localhost:9090']\n"
        "  - job_name: cadvisor\n"
        "    static_configs:\n"
        "      - targets: ['host.docker.internal:8080']\n"
        "  - job_name: node-exporter\n"
        "    static_configs:\n"
        "      - targets: ['host.docker.internal:9100']\n"
    )


@capability(
    "metrics.stack.provision",
    http_method="POST", http_path="/remote/metrics/stack/provision", http_tags=["remote", "metrics"],
    description="Provision the exporter stack on a Docker host via docker.run "
                "(sandbox-gated): cAdvisor (:8080), node-exporter (:9100) and "
                "Prometheus (:9090) with a generated scrape config. Skips any piece "
                "already running. Also registers the new Prometheus as a Vera "
                "endpoint. Inputs: host_id (str — docker host, default local), "
                "components (list — subset of ['cadvisor','node_exporter',"
                "'prometheus']; default all), register_prom (bool=true). "
                "Output: {ok, provisioned:[...], skipped:[...], prometheus_url, errors:[...]}.",
    schema={"properties": {"components": {"items": {"enum": list(_STACK.keys())}}}},
)
async def cap_stack_provision(host_id: str = "", components: Optional[List[str]] = None,
                              register_prom: bool = True, trace_id=None) -> Dict:
    run = _cap_raw("docker.run")
    if not run:
        return {"ok": False, "error": "docker.run unavailable (docker module not loaded)"}
    want = components or list(_STACK.keys())
    addr = await _docker_host_addr(host_id)
    status = await cap_stack_status(host_id=host_id)
    running = status.get("components", {})
    provisioned, skipped, errors = [], [], []

    async def _run(name, image, ports, **kw):
        res = await run(host_id=host_id or "local", image=image, name=name,
                        ports=ports, pull=True, **kw)
        if res.get("ok"):
            provisioned.append(name)
        else:
            errors.append({"name": name, "error": res.get("error", "run failed")})
        return res

    if "cadvisor" in want:
        if running.get("cadvisor"):
            skipped.append("cadvisor")
        else:
            await _run(_STACK["cadvisor"]["name"], _STACK["cadvisor"]["image"], "8080:8080",
                       volumes="/:/rootfs:ro,/var/run:/var/run:ro,/sys:/sys:ro,"
                               "/var/lib/docker/:/var/lib/docker:ro,/dev/disk/:/dev/disk:ro",
                       extra_args="--privileged --device=/dev/kmsg")

    if "node_exporter" in want:
        if running.get("node_exporter"):
            skipped.append("node_exporter")
        else:
            await _run(_STACK["node_exporter"]["name"], _STACK["node_exporter"]["image"],
                       "9100:9100",
                       extra_args="--pid=host -v /:/host:ro,rslave",
                       command="--path.rootfs=/host")

    prom_url = f"http://{addr}:9090"
    if "prometheus" in want:
        if running.get("prometheus"):
            skipped.append("prometheus")
        else:
            # 1) write the scrape config into a named volume via a throwaway container
            cfg_b64 = base64.b64encode(_prom_config(addr).encode()).decode()
            writer = await run(
                host_id=host_id or "local", image="busybox:latest",
                name=f"vera-prom-cfg-{uuid.uuid4().hex[:6]}", restart="no",
                volumes="vera-prom-config:/cfg",
                command=f"sh -c \"echo {cfg_b64} | base64 -d > /cfg/prometheus.yml\"")
            if not writer.get("ok"):
                errors.append({"name": "prometheus-config", "error": writer.get("error", "")})
            else:
                await asyncio.sleep(1.5)
                await _run(_STACK["prometheus"]["name"], _STACK["prometheus"]["image"],
                           "9090:9090", volumes="vera-prom-config:/etc/prometheus",
                           extra_args="--add-host=host.docker.internal:host-gateway",
                           command="--config.file=/etc/prometheus/prometheus.yml "
                                   "--web.enable-lifecycle")
                if register_prom:
                    save = _cap_raw("metrics.prom.save")
                    if save:
                        await save(label=f"vera-prometheus@{addr}", url=prom_url,
                                   make_default=True)

    await emit_event({"type": "remote.metrics.provision", "host": addr,
                      "provisioned": provisioned, "skipped": skipped})
    return {"ok": not errors, "provisioned": provisioned, "skipped": skipped,
            "prometheus_url": prom_url if "prometheus" in want else "", "errors": errors}


log.info("metrics_capabilities loaded — prometheus query + exporter provisioning")
