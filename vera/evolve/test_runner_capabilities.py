"""
test_runner_capabilities.py — pipeline gates' pytest across the host and the nodes

The runtime half of test_runner_core (read that first).

  nodes.testenv.sync     build / refresh each node's pinned test venv from the
                         gate image's `pip freeze` (a no-op where it is current)
  nodes.testenv.status   each node's venv against what the gate needs
  evolve.test_pool.*     the pool's config, its state, and a parity check
  run_pooled()           the gate's sharded run - published as _orch.TEST_POOL
                         so evolve_capabilities reaches it without an import
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, emit_event

try:
    from Vera.vera.evolve import test_runner_core as core
    from Vera.vera.workers import node_compute_core as _nc
except ImportError:                                          # pragma: no cover
    from vera.evolve import test_runner_core as core         # type: ignore
    from vera.workers import node_compute_core as _nc        # type: ignore

log = logging.getLogger("vera.test_pool")


def _ev():
    """evolve_capabilities, as loaded (cap modules load under bare names)."""
    for name in ("evolve_capabilities", "Vera.vera.evolve.evolve_capabilities"):
        m = sys.modules.get(name)
        if m is not None and hasattr(m, "_branch_worktree"):
            return m
    raise RuntimeError("evolve_capabilities is not loaded")


def _s(v) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v or "")


async def _pool_config() -> Dict[str, Any]:
    r = _orch.REDIS
    try:
        raw = await r.get(core.POOL_KEY) if r is not None else None
        return core.merge_pool_config(json.loads(raw) if raw else {})
    except Exception:
        return core.merge_pool_config({})


def _run_bytes(argv: List[str], cwd: Optional[str] = None, timeout: int = 300) -> Dict[str, Any]:
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout)
        return {"ok": p.returncode == 0, "out": p.stdout, "err": p.stderr.decode("utf-8", "replace"),
                "code": p.returncode}
    except Exception as e:
        return {"ok": False, "out": b"", "err": str(e), "code": -1}


# ═════════════════════════════════════════════════════════════════════════════
#  the nodes' test venvs
# ═════════════════════════════════════════════════════════════════════════════
_FREEZE_CACHE: Dict[str, Any] = {}


async def image_lock() -> Dict[str, Any]:
    """{ok, image_id, python, lines, sha} for the gate image (cached per image id)."""
    ev = _ev()
    loop = asyncio.get_running_loop()
    ins = await loop.run_in_executor(None, _run_bytes,
                                     ["docker", "image", "inspect", ev.DEV_IMAGE, "--format", "{{.Id}}"])
    iid = (ins.get("out") or b"").decode().strip()
    if not ins.get("ok") or not iid:
        return {"ok": False, "error": f"cannot inspect {ev.DEV_IMAGE}: {ins.get('err', '')[:200]}"}
    if _FREEZE_CACHE.get("image_id") == iid:
        return _FREEZE_CACHE
    res = await loop.run_in_executor(None, lambda: _run_bytes(
        ["docker", "run", "--rm", ev.DEV_IMAGE, "sh", "-c",
         "python -c 'import sys;print(\"PY\", sys.version.split()[0])'; pip freeze"], timeout=180))
    text = (res.get("out") or b"").decode("utf-8", "replace")
    if not res.get("ok"):
        return {"ok": False, "error": f"pip freeze in {ev.DEV_IMAGE} failed: {res.get('err', '')[:200]}"}
    py = next((ln.split()[1] for ln in text.splitlines() if ln.startswith("PY ")), "")
    lines = core.lock_lines("\n".join(ln for ln in text.splitlines() if not ln.startswith("PY ")))
    minor = ".".join(py.split(".")[:2])
    out = {"ok": True, "image_id": iid, "python": py, "lines": lines,
           "sha": core.lock_sha(lines, minor)}
    _FREEZE_CACHE.clear()
    _FREEZE_CACHE.update(out)
    return out


async def _compute_nodes() -> List[Dict[str, str]]:
    r = _orch.REDIS
    out, seen = [], set()
    if r is None:
        return out
    async for k in r.scan_iter("vera:workers:*"):
        try:
            vals = await r.hmget(k, "role", "host_id", "host", "classes")
        except Exception:
            continue
        role, hid, host, classes = (_s(v) for v in vals)
        if role == "node-worker" and hid and "cpu_compute" in classes and hid not in seen:
            seen.add(hid)
            out.append({"host_id": hid, "host": host})
    # provisioned nodes whose worker is restarting right now (a release's sync
    # refreshes the workers just before this step) still get their venv
    try:
        roles = {_s(k): _s(v) for k, v in (await r.hgetall("vera:node_workers:roles")).items()}
        for k, v in (await r.hgetall("vera:node_workers")).items():
            hid = _s(k)
            if hid in seen or "cpu_compute" not in roles.get(hid, ""):
                continue
            rec = json.loads(v)
            seen.add(hid)
            out.append({"host_id": hid, "host": rec.get("nodename") or rec.get("host") or hid})
    except Exception as e:
        log.debug("testenv node registry: %s", e)
    return out


@capability(
    "nodes.testenv.sync",
    http_method="POST", http_path="/nodes/testenv/sync", http_tags=["nodes", "evolve"],
    memory="off",
    description="Build or refresh the pinned pytest venv (/opt/vera/testenv) on every node "
                "worker that takes CPU compute, from the gate image's `pip freeze` (GPU "
                "packages dropped), so a node can run gate shards with the gate's packages. "
                "A node whose venv already matches is left alone; a package that will not "
                "install is reported, not fatal. Inputs: host_id (str - one node; default "
                "all), dry_run (bool). Output: {ok, sha, python, nodes: {host: {state, ok, "
                "failed, python, error}}}.",
)
async def cap_nodes_testenv_sync(host_id: str = "", dry_run: bool = False, trace_id=None) -> Dict:
    lock = await image_lock()
    if not lock.get("ok"):
        return {"ok": False, "error": lock.get("error")}
    r = _orch.REDIS
    nodes = [n for n in await _compute_nodes() if not host_id or n["host_id"] == host_id]
    if dry_run:
        return {"ok": True, "dry_run": True, "sha": lock["sha"], "python": lock["python"],
                "packages": len(lock["lines"]), "nodes": [n["host"] for n in nodes]}
    if r is not None:
        await r.set(_nc.TESTENV_WANT_KEY, lock["sha"])
    ssh = _orch.CAPABILITY_REGISTRY.get("exec.ssh.run")
    if not ssh:
        return {"ok": False, "error": "exec.ssh.run is not loaded"}
    cmd = core.testenv_build_cmd(lock["lines"], lock["sha"])

    async def one(n):
        res = await ssh["raw"](host_id=n["host_id"], command=cmd, timeout=1800)
        res = res if isinstance(res, dict) else {}
        st = core.parse_testenv_output((res.get("stdout") or "") + "\n" + (res.get("stderr") or ""))
        rec = {**st, "at": time.time(), "host": n["host"]}
        if r is not None:
            await r.hset(_nc.TESTENV_KEY, n["host_id"], json.dumps(rec))
        return n["host"], rec

    results = dict(await asyncio.gather(*(one(n) for n in nodes)))
    await emit_event({"type": "nodes.testenv.sync", "sha": lock["sha"],
                      "ok": all(v["ok"] for v in results.values()),
                      "nodes": {k: v["state"] for k, v in results.items()}})
    return {"ok": all(v["ok"] for v in results.values()), "sha": lock["sha"],
            "python": lock["python"], "packages": len(lock["lines"]), "nodes": results}


@capability(
    "nodes.testenv.status",
    http_method="GET", http_path="/nodes/testenv/status", http_tags=["nodes", "evolve"],
    memory="off", silent=True,
    description="Each node's pinned pytest venv against the one the gate image needs: "
                "{want, nodes: {host: {sha, current, failed, python, at}}}.",
)
async def cap_nodes_testenv_status(trace_id=None) -> Dict:
    r = _orch.REDIS
    if r is None:
        return {"ok": False, "error": "no Redis"}
    want = _s(await r.get(_nc.TESTENV_WANT_KEY))
    nodes = {}
    for k, v in (await r.hgetall(_nc.TESTENV_KEY)).items():
        try:
            rec = json.loads(v)
        except ValueError:
            continue
        rec["current"] = bool(want) and rec.get("ok") and rec.get("sha") == want
        nodes[rec.get("host") or _s(k)] = rec
    return {"ok": True, "want": want, "nodes": nodes}


# ═════════════════════════════════════════════════════════════════════════════
#  the pooled gate run
# ═════════════════════════════════════════════════════════════════════════════
async def _archive(wt: str) -> bytes:
    """The worktree as the gate container sees it - tracked and untracked
    files, not ignored ones - as tar.gz."""
    loop = asyncio.get_running_loop()
    sh = ("git -c safe.directory='*' ls-files -z --cached --others --exclude-standard | "
          "tar --null --ignore-failed-read -czf - -T - 2>/dev/null")
    res = await loop.run_in_executor(None, lambda: _run_bytes(["sh", "-c", sh], cwd=wt, timeout=300))
    if not res.get("out"):
        raise RuntimeError(f"could not archive {wt}: {res.get('err', '')[:200]}")
    return res["out"]


async def _durations() -> Dict[str, float]:
    r = _orch.REDIS
    try:
        raw = await r.hgetall(core.DURATIONS_KEY) if r is not None else {}
        return {_s(k): float(_s(v)) for k, v in raw.items()}
    except Exception:
        return {}


async def record_durations(output: str) -> None:
    """Fold one run's per-file times into the recorded durations."""
    r = _orch.REDIS
    if r is None:
        return
    d = core.file_durations(core.junit_of(output or ""))
    if not d:
        return
    try:
        old = await _durations()
        await r.hset(core.DURATIONS_KEY, mapping={f: str(core.smooth(old.get(f), v))
                                                  for f, v in d.items()})
    except Exception as e:
        log.debug("test durations: %s", e)


async def _host_lease(token: str) -> bool:
    r = _orch.REDIS
    if r is None:
        return True
    ttl = _ev()._CRITICAL_TIER_LEASE_S
    return bool(await r.set(core.HOST_RUNNER_LEASE, token, nx=True, ex=ttl))


async def _host_release(token: str) -> None:
    r = _orch.REDIS
    if r is None:
        return
    try:
        await r.eval(_ev()._CRITICAL_TIER_RELEASE_LUA, 1, core.HOST_RUNNER_LEASE, token)
    except Exception:
        pass


async def _node_slots(max_nodes: int) -> List[str]:
    """Nodes that could take a gate shard right now (one shard per node)."""
    plan = getattr(_orch, "NODE_PLAN", None)
    if plan is None or max_nodes <= 0:
        return []
    picked: List[str] = []
    for _ in range(max_nodes):
        p = await plan("pytest.shard", exclude=picked)
        if p["pick"] is None:
            break
        picked.append(str(p["pick"].get("host_id")))
    return picked


async def _host_shard(wt: str, files: List[str], markers: str, timeout: int) -> Dict[str, Any]:
    ev = _ev()
    tokens, err = ev._ut_sanitize(" ".join(files), markers, "")
    if err:
        return {"error": err}
    argv = ev._ut_argv(ev.DEV_IMAGE, os.path.realpath(wt), ev._ut_inner(tokens))
    res = await ev._sh(argv, timeout=timeout)
    combined = ((res.get("out") or "") + "\n" + (res.get("err") or "")).strip()
    if "__VERA_RC=" not in combined:
        return {"error": "the host's test container did not run pytest",
                "detail": combined[-800:]}
    return {"parsed": ev._ut_parse(combined), "out": combined}


async def run_pooled(rec: Dict[str, Any], branch: str, *, markers: str = "critical",
                     timeout: int = 900, force: bool = False) -> Optional[Dict[str, Any]]:
    """The gate's run, sharded over the host's container and the nodes with
    room. Returns evolve.unittest.run's result shape, or None when the pool is
    off or does not apply (the caller then runs the tier as before)."""
    ev = _ev()
    cfg = await _pool_config()
    if not (cfg["enabled"] or force):
        return None
    wt = await ev._branch_worktree(branch)
    if not wt or not os.path.isdir(os.path.join(wt, "tests")):
        return None
    files = core.test_files(os.listdir(os.path.join(wt, "tests")))
    if len(files) < cfg["min_files"]:
        return None
    rid = rec.get("id", "") or "manual"
    token = f"{rid}:{branch}:pool"
    t0 = time.time()

    async def note(msg):
        if rec.get("id"):
            rec["current"] = msg
            ev._pstep(rec, "test-pool", True, msg)
            await ev._save_pipeline(rec)

    # runners: the host's container (one lease) and each node with room now
    queued = False
    while True:
        host_ok = await _host_lease(token)
        nodes = await _node_slots(cfg["max_nodes"])
        if host_ok or nodes:
            break
        if not queued:
            queued = True
            await note("test runners all busy - waiting for one")
        await asyncio.sleep(10)
    try:
        n = len(nodes) + (1 if host_ok else 0)
        durations = await _durations()
        shards = core.shard(files, durations, n)
        node_shards = shards[:len(nodes)]
        host_shards = shards[len(nodes):]
        await note(f"{len(files)} test files in {len(shards)} shards: "
                   f"{len(node_shards)} on nodes, {len(host_shards)} on the host"
                   + (f" (queued {int(time.time() - t0)} s)" if queued else ""))
        archive = await _archive(wt) if node_shards else b""
        want = _s(await _orch.REDIS.get(_nc.TESTENV_WANT_KEY)) if _orch.REDIS is not None else ""
        run = getattr(_orch, "NODE_COMPUTE", None)
        report: List[Dict[str, Any]] = []
        parsed: List[Dict[str, Any]] = []
        outputs: List[str] = []
        redo: List[List[str]] = []

        async def on_node(s: List[str]):
            t = time.time()
            try:
                res = await run("pytest.shard", {
                    "archive": archive, "files": s, "markers": markers,
                    "testenv_sha": want, "timeout": timeout},
                    label=f"gate shard {rid} ({len(s)} files)", max_wait_s=120,
                    host_fallback=False)
                out = res["out"]
                if "__VERA_RC=" not in out:
                    raise RuntimeError("no result from pytest")
                p = ev._ut_parse(out)
                # a shard that could not even start pytest is the runner's
                # failure, not the code's: run it again on the host
                if p.get("rc") in (3, 4) or (p.get("total", 0) == 0 and p.get("rc") not in (0, 5)):
                    raise RuntimeError(f"pytest rc={p.get('rc')} on {res.get('_node')}")
                parsed.append(p)
                outputs.append(out)
                report.append({"runner": res.get("_node", "node"), "files": len(s),
                               "seconds": round(time.time() - t, 1), "summary": p["summary"]})
            except Exception as e:
                redo.append(s)
                report.append({"runner": "node", "files": len(s), "error": str(e)[:300],
                               "seconds": round(time.time() - t, 1), "rerun_on_host": True})

        async def on_host(s: List[str], label="host"):
            t = time.time()
            res = await _host_shard(wt, s, markers, timeout)
            if res.get("error"):
                raise RuntimeError(res["error"] + " " + res.get("detail", ""))
            parsed.append(res["parsed"])
            outputs.append(res["out"])
            report.append({"runner": label, "files": len(s), "seconds": round(time.time() - t, 1),
                           "summary": res["parsed"]["summary"]})

        jobs = [on_node(s) for s in node_shards] + [on_host(s) for s in host_shards]
        await asyncio.gather(*jobs)
        if redo:
            # a missing shard is never a pass: the host runs what a node could not
            if not host_ok:
                while not await _host_lease(token):
                    await asyncio.sleep(10)
                host_ok = True
            await note(f"{len(redo)} shard(s) a node could not run - running them on the host")
            for s in redo:
                await on_host(s, label="host (rerun)")
    finally:
        if host_ok:
            await _host_release(token)

    merged = core.merge(parsed)
    for out in outputs:
        await record_durations(out)
    label = branch or "primary"
    await ev._audit("unittest.run", f"[{label}] {merged['summary']} (pool)", ok=merged["ok"])
    await ev._record_unittest_run(merged, branch=branch, markers=markers, paths="tests",
                                  label=label, pipeline_id=rec.get("id", ""))
    await emit_event({"type": "evolve.unittest.done", "branch": label, "ok": merged["ok"],
                      "passed": merged["passed"], "failed": merged["failed"],
                      "errors": merged["errors"], "pool": True})
    text = "\n".join(f"[{x['runner']}] {x.get('summary') or x.get('error')}" for x in report)
    return {**merged, "code": merged["rc"], "image": ev.DEV_IMAGE, "branch": label,
            "out": text, "repo": "vera", "pool": {"shards": report,
                                                  "seconds": round(time.time() - t0, 1)}}


_orch.TEST_POOL = run_pooled
_orch.TEST_DURATIONS = record_durations


# ═════════════════════════════════════════════════════════════════════════════
#  status, config, parity
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "evolve.test_pool.status",
    http_method="GET", http_path="/evolve/test_pool/status", http_tags=["evolve"],
    memory="off", silent=True,
    description="The gate test pool: config (enabled, max_nodes, min_files), the host "
                "runner's lease holder, which nodes could take a shard now and why not, "
                "each node's test venv, and how many test files have recorded durations.",
)
async def cap_test_pool_status(trace_id=None) -> Dict:
    r = _orch.REDIS
    cfg = await _pool_config()
    plan = getattr(_orch, "NODE_PLAN", None)
    p = await plan("pytest.shard") if plan else {"nodes": [], "why": "compute module not loaded"}
    holder = _s(await r.get(core.HOST_RUNNER_LEASE)) if r is not None else ""
    return {"ok": True, "config": cfg, "host_runner": holder or "free",
            "nodes_now": p.get("nodes", []), "why": p.get("why", ""),
            "testenv": await cap_nodes_testenv_status(),
            "durations_recorded": len(await _durations())}


@capability(
    "evolve.test_pool.config.set",
    http_method="POST", http_path="/evolve/test_pool/config", http_tags=["evolve"],
    memory="off",
    description="Configure the gate test pool. Inputs: enabled (bool - shard gate test "
                "runs over the host's container and the nodes with room), max_nodes (int, "
                "default 3), min_files (int - below this a run is not split, default 40). "
                "Output: {ok, config}.",
)
async def cap_test_pool_config_set(enabled: Optional[bool] = None, max_nodes: Optional[int] = None,
                                   min_files: Optional[int] = None, trace_id=None) -> Dict:
    r = _orch.REDIS
    if r is None:
        return {"ok": False, "error": "no Redis"}
    cfg = await _pool_config()
    for k, v in (("enabled", enabled), ("max_nodes", max_nodes), ("min_files", min_files)):
        if v is not None:
            cfg[k] = v
    cfg = core.merge_pool_config(cfg)
    await r.set(core.POOL_KEY, json.dumps(cfg))
    return {"ok": True, "config": cfg}


@capability(
    "evolve.test_pool.parity",
    http_method="POST", http_path="/evolve/test_pool/parity", http_tags=["evolve"],
    memory="off",
    description="Run a branch's critical tier twice - once on the host alone, once through "
                "the pool - and compare: totals and the exact sets of failed and errored "
                "tests must match. Takes the host runner for the whole check. Inputs: branch "
                "(str - default bleeding-edge), markers (str='critical'). Output: {ok, "
                "identical, host, pool, only_host, only_pool}.",
)
async def cap_test_pool_parity(branch: str = "bleeding-edge", markers: str = "critical",
                               trace_id=None) -> Dict:
    ev = _ev()
    host = await ev.evolve_unittest_run(branch=branch, paths="tests", markers=markers,
                                        timeout=ev._CRITICAL_TIER_TIMEOUT_S)
    pool = await run_pooled({"id": ""}, branch, markers=markers, force=True)
    if pool is None:
        return {"ok": False, "error": "the pool did not apply (no worktree or too few files)",
                "host": host.get("summary")}

    def bad(res):
        return {d.get("node_id") for d in (res.get("failure_details") or [])}
    only_host, only_pool = sorted(bad(host) - bad(pool)), sorted(bad(pool) - bad(host))
    same_counts = all(host.get(k) == pool.get(k) for k in ("passed", "failed", "errors", "skipped"))
    return {"ok": True, "identical": same_counts and not only_host and not only_pool,
            "host": {k: host.get(k) for k in ("summary", "passed", "failed", "errors", "skipped")},
            "pool": {k: pool.get(k) for k in ("summary", "passed", "failed", "errors", "skipped")},
            "pool_shards": pool.get("pool"), "only_host": only_host[:30], "only_pool": only_pool[:30]}
