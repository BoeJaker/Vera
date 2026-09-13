"""exec.ssh.hosts.cleanup - fold repeated, superseded and stale SSH logins.

Reads every exec-store login, finds which ids other stores refer to (Redis values
and Vera's ~/.vera_*.json registries), probes each login's SSH port once, and
matches addresses and names against every Proxmox guest; ssh_cleanup_core decides
what happens to each. A dry run unless apply=true. Before anything changes, the
full records (sealed secrets included) are copied into the secrets service, so a
removed login can be restored; without it, the cleanup refuses to apply.
"""
from __future__ import annotations

import asyncio
import glob
import inspect
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, emit_event
from Vera.vera.execution import ssh_cleanup_core as core

log = logging.getLogger("vera.execution.ssh_cleanup")

PROBE_TIMEOUT_S = 2.0
PROBE_CONCURRENCY = 16
# Not stores of record: capability result caches, and running logs that mention
# a login in passing.
SKIP_PREFIXES = ("vera:cap:result:", "vera:activity", "vera:loop:", "vera:dream:", "vera:events")


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


def _redis():
    return getattr(_orch, "REDIS", None)


async def _logins() -> Dict[str, Dict[str, Any]]:
    fn = (_module_of("exec.ssh.hosts.list") or {}).get("_load_hosts")
    return {k: dict(v) for k, v in (await fn()).items()} if fn else {}


async def _referenced(ids: Set[str]) -> Set[str]:
    """The ids that appear anywhere outside the exec store itself."""
    found: Set[str] = set()
    r = _redis()
    if r is not None:
        async for raw in r.scan_iter(count=500):
            key = _text(raw)
            if key.startswith(SKIP_PREFIXES):
                continue
            try:
                kind = _text(await r.type(key))
                if kind == "string":
                    blob = _text(await r.get(key))
                elif kind == "hash":
                    blob = "\n".join(_text(k) + "\n" + _text(v) for k, v in (await r.hgetall(key)).items())
                elif kind == "set":
                    blob = "\n".join(_text(v) for v in await r.smembers(key))
                else:
                    continue
            except Exception:
                continue
            found |= {i for i in ids - found if i in blob}
    for path in glob.glob(os.path.join(os.path.expanduser("~"), ".vera_*.json")):
        if os.path.basename(path) == ".vera_ssh_hosts.json":
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                blob = fh.read()
        except Exception:
            continue
        found |= {i for i in ids - found if i in blob}
    return found


async def _probe(host: str, port: int) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), PROBE_TIMEOUT_S)
        writer.close()
        return True
    except Exception:
        return False


async def _reachability(logins: Dict[str, Dict[str, Any]]) -> Dict[str, Optional[bool]]:
    sem = asyncio.Semaphore(PROBE_CONCURRENCY)
    out: Dict[str, Optional[bool]] = {}

    async def one(rid: str, rec: Dict[str, Any]) -> None:
        async with sem:
            try:
                port = int(rec.get("port") or 22)
            except (TypeError, ValueError):
                port = 22
            out[rid] = await _probe(str(rec.get("host") or ""), port)

    await asyncio.gather(*[one(rid, rec) for rid, rec in logins.items()])
    return out


async def _guests() -> Optional[List[Dict[str, Any]]]:
    res = await _call("estate.machines")
    if res.get("error"):
        return None
    return [{"name": m.get("label"), "ips": m.get("ips") or [], "addr": m.get("addr")}
            for m in res.get("machines") or [] if m.get("kind") in ("guest", "proxmox-node")]


@capability(
    "exec.ssh.hosts.cleanup",
    http_method="POST", http_path="/exec/ssh/hosts/cleanup", http_tags=["exec"],
    memory="on",
    description="Clean up the SSH login store: fold exact repeats (same host, port, user, "
                "auth and key) into one login that keeps all their tags, drop a password "
                "login where the same host and user also have a key login, and drop logins "
                "whose port does not answer and whose address and name match no Proxmox "
                "guest. A login referenced by any other store (Redis, ~/.vera_*.json) is "
                "always kept. A dry run unless apply=true; before applying, every login is "
                "backed up into the secrets service (OpenBao) and nothing changes if that "
                "fails. Inputs: apply (bool), probe (bool=true; probe SSH ports). Output: "
                "{dry_run, counts, steps, referenced, notes, applied?}.",
)
async def cap_ssh_cleanup(apply: bool = False, probe: bool = True, trace_id=None) -> Dict[str, Any]:
    logins = await _logins()
    if not logins:
        return {"error": "the exec SSH store is not loaded or holds no logins"}
    referenced = await _referenced(set(logins))
    notes: List[str] = []
    guests = await _guests()
    if guests is None:
        notes.append("the machine list could not be read, so no login is judged stale")
        guests, reach = [], {}
    else:
        reach = await _reachability(logins) if _flag(probe) else {}
    plan = core.plan(logins, referenced, reach, guests)
    out: Dict[str, Any] = {"dry_run": not _flag(apply), "counts": plan["counts"], "steps": plan["steps"],
                           "tag_updates": plan["tag_updates"], "referenced": sorted(referenced), "notes": notes}
    if not _flag(apply):
        return out
    removals = [s for s in plan["steps"] if s["action"] != "keep"]
    if not removals and not plan["tag_updates"]:
        return {**out, "dry_run": False, "applied": {"removed": 0, "tagged": 0, "errors": []}}
    put = (_module_of("secrets.status") or {}).get("put_named")
    if put is None:
        return {**out, "error": "the secrets service is not loaded, so the logins cannot be backed up; "
                                "nothing changed"}
    backup = await put(f"backup/ssh-logins/{time.strftime('%Y%m%d-%H%M%S')}",
                       {"value": json.dumps(logins), "notes": "exec-store logins before exec.ssh.hosts.cleanup"})
    if backup.get("error"):
        return {**out, "error": f"the backup failed, so nothing changed: {backup['error']}"}
    tagged, removed, errors = 0, 0, []
    for keeper, extra in plan["tag_updates"].items():
        rec = logins[keeper]
        tags = core._tags(rec) + [t for t in extra if t not in core._tags(rec)]
        res = await _call("exec.ssh.hosts.save", id=keeper, host=rec.get("host", ""), user=rec.get("user", ""),
                          port=int(rec.get("port") or 22), label=rec.get("label", ""),
                          auth=rec.get("auth", "password"), key_path=rec.get("key_path", ""),
                          tags=",".join(tags))
        if res.get("ok"):
            tagged += 1
        else:
            errors.append(f"{rec.get('label') or keeper}: tags not saved ({res.get('error')})")
    for step in removals:
        res = await _call("exec.ssh.hosts.delete", id=step["id"])
        if res.get("ok"):
            removed += 1
        else:
            errors.append(f"{step['label'] or step['id']}: not removed ({res.get('error')})")
    await emit_event({"type": "exec.ssh.hosts.cleanup", "removed": removed, "tagged": tagged})
    return {**out, "dry_run": False,
            "applied": {"removed": removed, "tagged": tagged, "backup": backup.get("path"), "errors": errors}}


log.info("ssh_cleanup_capabilities ready - exec.ssh.hosts.cleanup")
