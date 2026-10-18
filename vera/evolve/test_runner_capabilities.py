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
import collections
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
    contract={"presentation": {"kind": "table", "form": "table", "map": {"rows": "nodes"}, "draw": {"columns": ["host", "state", "current", "sha", "python", "failed", "error"]}}},
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


async def _host_shard(wt: str, files: List[str], markers: str, timeout: int,
                      parallel: int = 1, durations: Optional[Dict[str, float]] = None
                      ) -> Dict[str, Any]:
    """A chunk on the host: `parallel` test containers side by side (split by
    the recorded durations, each file in exactly one), one combined result. One
    process per chunk made the host the slowest runner (2026-10-05 parity: nodes
    88-99 s, host 133 s)."""
    ev = _ev()
    groups = core.shard(files, durations or {}, max(1, min(int(parallel or 1), len(files))))

    async def one(g):
        tokens, err = ev._ut_sanitize(" ".join(g), markers, "")
        if err:
            raise ValueError(err)
        argv = ev._ut_argv(ev.DEV_IMAGE, os.path.realpath(wt), ev._ut_inner(tokens))
        res = await ev._sh(argv, timeout=timeout)
        return ((res.get("out") or "") + "\n" + (res.get("err") or "")).strip()
    try:
        outs = await asyncio.gather(*(one(g) for g in groups))
    except ValueError as e:
        return {"error": str(e)}
    bad = [o for o in outs if "__VERA_RC=" not in o]
    if bad:
        return {"error": "the host's test container did not run pytest",
                "detail": bad[0][-800:]}
    combined = outs[0] if len(outs) == 1 else core.combine_outputs(outs)
    return {"parsed": ev._ut_parse(combined), "out": combined}


async def run_pooled(rec: Dict[str, Any], branch: str, *, markers: str = "critical",
                     timeout: int = 900, force: bool = False,
                     keep_outputs: bool = False, pipeline_id: str = "") -> Optional[Dict[str, Any]]:
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

    # The tier is split into CHUNKS that every free runner pulls from, one at a
    # time, until none are left: a gate that starts while others hold the
    # runners takes more as they free up, instead of running everything on the
    # one runner free at its start (2026-10-05: 7 of 25 gates ran as 1 shard).
    durations = await _durations()
    chunks = core.shard(files, durations, min(cfg["chunks"], len(files)))
    pending = collections.deque({"files": c, "tries": 0, "host_only": False} for c in chunks)
    run = getattr(_orch, "NODE_COMPUTE", None)
    plan = getattr(_orch, "NODE_PLAN", None)
    want = _s(await _orch.REDIS.get(_nc.TESTENV_WANT_KEY)) if _orch.REDIS is not None else ""
    blob_key = ""
    report: List[Dict[str, Any]] = []
    parsed: List[Dict[str, Any]] = []
    outputs: List[str] = []
    running: Dict[Any, Dict[str, Any]] = {}
    host_held = False
    waited_note = False

    async def put_archive() -> str:
        """The worktree archive, once per gate, for every node chunk to fetch."""
        nonlocal blob_key
        if not blob_key:
            data = await _archive(wt)
            blob_key = f"vera:evolve:test_pool:archive:{rid}:{int(t0)}"
            await _orch.REDIS.set(blob_key, data, ex=3 * 3600)
        return blob_key

    async def on_node(c):
        t = time.time()
        key = await put_archive()
        res = await run("pytest.shard", {
            "_blob_refs": {"archive": key}, "files": c["files"], "markers": markers,
            "testenv_sha": want, "timeout": timeout,
            # several pytest processes per node chunk (each file still once)
            "parallel": cfg["node_parallel"],
            "durations": {f: durations.get(f) for f in c["files"] if durations.get(f) is not None}},
            label=f"gate chunk {rid} ({len(c['files'])} files)", max_wait_s=30,
            host_fallback=False)
        out = res["out"]
        if "__VERA_RC=" not in out:
            raise RuntimeError("no result from pytest")
        p = ev._ut_parse(out)
        # pytest could not start: the runner's failure, not the code's
        if p.get("rc") in (3, 4) or (p.get("total", 0) == 0 and p.get("rc") not in (0, 5)):
            raise RuntimeError(f"pytest rc={p.get('rc')} on {res.get('_node')}")
        return {"parsed": p, "out": out, "runner": res.get("_node", "node"),
                "seconds": round(time.time() - t, 1)}

    async def on_host(c):
        t = time.time()
        res = await _host_shard(wt, c["files"], markers, timeout,
                                parallel=cfg["host_parallel"], durations=durations)
        if res.get("error"):
            raise RuntimeError(res["error"] + " " + res.get("detail", ""))
        return {"parsed": res["parsed"], "out": res["out"], "runner": "host",
                "seconds": round(time.time() - t, 1)}

    await note(f"{len(files)} test files in {len(chunks)} chunks over the host and the nodes "
               f"as they free up")
    try:
        while pending or running:
            # the host's container, when this gate can take its lease
            idx = next((i for i, c in enumerate(pending)), None)
            if idx is not None and not host_held and await _host_lease(token):
                host_held = True
                c = pending[idx]
                del pending[idx]
                running[asyncio.ensure_future(on_host(c))] = {"chunk": c, "where": "host"}
            # every node with room now: one chunk each (run_on_node reserves
            # its node in the same step as its pick, so they spread)
            if pending and run is not None and plan is not None:
                ready = [c for c in pending if not c["host_only"]]
                slots = await _node_slots(min(cfg["max_nodes"], len(ready))) if ready else []
                for _ in slots:
                    c = next((x for x in pending if not x["host_only"]), None)
                    if c is None:
                        break
                    pending.remove(c)
                    running[asyncio.ensure_future(on_node(c))] = {"chunk": c, "where": "node"}
            if not running:
                if not waited_note:
                    waited_note = True
                    await note("test runners all busy - waiting for one")
                await asyncio.sleep(5)
                continue
            done, _ = await asyncio.wait(list(running), timeout=5,
                                         return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                meta = running.pop(task)
                c = meta["chunk"]
                if meta["where"] == "host":
                    await _host_release(token)
                    host_held = False
                    res = task.result()        # a host-container failure fails the gate
                else:
                    try:
                        res = task.result()
                    except Exception as e:
                        msg = str(e)
                        if "no node had room" not in msg:
                            c["tries"] += 1
                            report.append({"runner": "node", "files": len(c["files"]),
                                           "error": msg[:300], "requeued": True})
                        # a chunk a node failed twice runs on the host; one
                        # that found no room just waits for the next runner
                        c["host_only"] = c["tries"] >= 2
                        pending.appendleft(c)
                        continue
                parsed.append(res["parsed"])
                outputs.append(res["out"])
                report.append({"runner": res["runner"], "files": len(c["files"]),
                               "seconds": res["seconds"], "summary": res["parsed"]["summary"]})
    finally:
        for task in running:
            task.cancel()
        if host_held:
            await _host_release(token)
        if blob_key:
            try:
                await _orch.REDIS.delete(blob_key)
            except Exception:
                pass

    merged = core.merge(parsed)
    for out in outputs:
        await record_durations(out)
    label = branch or "primary"
    await ev._audit("unittest.run", f"[{label}] {merged['summary']} (pool)", ok=merged["ok"])
    await ev._record_unittest_run(merged, branch=branch, markers=markers, paths="tests",
                                  label=label, pipeline_id=rec.get("id", "") or pipeline_id)
    await emit_event({"type": "evolve.unittest.done", "branch": label, "ok": merged["ok"],
                      "passed": merged["passed"], "failed": merged["failed"],
                      "errors": merged["errors"], "pool": True})
    text = "\n".join(f"[{x['runner']}] {x.get('summary') or x.get('error')}" for x in report)
    res = {**merged, "code": merged["rc"], "image": ev.DEV_IMAGE, "branch": label,
           "out": text, "repo": "vera", "pool": {"shards": report,
                                                 "seconds": round(time.time() - t0, 1)}}
    if keep_outputs:
        res["_outputs"] = outputs
    return res


async def run_for_unittest(branch: str, *, markers: str, timeout: int,
                           pipeline_id: str = "") -> Optional[Dict[str, Any]]:
    """evolve.unittest.run's whole-suite runs through the pool too: they were a
    third of all runs and went to the host alone, beside the gates' host chunks
    and without its lease (2026-10-09). No pipeline record is touched (an empty
    rec); the run is still recorded under pipeline_id. None: run it as before."""
    try:
        return await run_pooled({}, branch, markers=markers, timeout=timeout,
                                pipeline_id=pipeline_id)
    except Exception as e:
        log.warning("test pool failed for unittest.run, host run instead: %s", e)
        return None


_orch.TEST_POOL = run_pooled
_orch.TEST_POOL_UNITTEST = run_for_unittest
_orch.TEST_DURATIONS = record_durations


# ═════════════════════════════════════════════════════════════════════════════
#  status, config, parity
# ═════════════════════════════════════════════════════════════════════════════
@capability(
    "evolve.test_pool.status",
    contract={"presentation": {"kind": "composite", "parts": [{"kind": "table", "map": {"rows": "nodes_now"}, "title": "Nodes now"}, {"kind": "kv", "map": {"fields": "config"}, "title": "Config"}]}},
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
                "default 3), min_files (int - below this a run is not split, default 40), "
                "chunks (int - chunks a run is split into, default 12), node_parallel / "
                "host_parallel (int 1-8 - pytest processes per chunk on a node / on the "
                "host, defaults 4 / 3). Output: {ok, config}.",
)
async def cap_test_pool_config_set(enabled: Optional[bool] = None, max_nodes: Optional[int] = None,
                                   min_files: Optional[int] = None, chunks: Optional[int] = None,
                                   node_parallel: Optional[int] = None,
                                   host_parallel: Optional[int] = None, trace_id=None) -> Dict:
    r = _orch.REDIS
    if r is None:
        return {"ok": False, "error": "no Redis"}
    cfg = await _pool_config()
    for k, v in (("enabled", enabled), ("max_nodes", max_nodes), ("min_files", min_files),
                 ("chunks", chunks), ("node_parallel", node_parallel),
                 ("host_parallel", host_parallel)):
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
                "the pool - and compare test by test: every test's outcome (passed, failed, "
                "error, skipped) must be the same in both. Inputs: branch (str - a branch "
                "with a worktree), markers (str='critical'). Output: {ok, identical, host, "
                "pool, pool_shards, differences: [{test, host, pool, why}]}.",
)
async def cap_test_pool_parity(branch: str = "bleeding-edge", markers: str = "critical",
                               trace_id=None) -> Dict:
    ev = _ev()
    wt = await ev._branch_worktree(branch)
    if not wt or not os.path.isdir(os.path.join(wt, "tests")):
        return {"ok": False, "error": f"no worktree with tests/ for {branch!r}"}
    files = core.test_files(os.listdir(os.path.join(wt, "tests")))
    t0 = time.time()
    host = await _host_shard(wt, files, markers, ev._CRITICAL_TIER_TIMEOUT_S)
    if host.get("error"):
        return {"ok": False, "error": "host run: " + host["error"]}
    host_s = round(time.time() - t0, 1)
    pool = await run_pooled({"id": ""}, branch, markers=markers, force=True, keep_outputs=True)
    if pool is None:
        return {"ok": False, "error": "the pool did not apply (too few test files)"}
    d = core.parity_diff([host["out"]], pool.pop("_outputs", []))
    hp = host["parsed"]
    same = (not d["differences"]) and all(hp.get(k) == pool.get(k)
                                          for k in ("passed", "failed", "errors", "skipped"))
    return {"ok": True, "identical": same,
            "host": {k: hp.get(k) for k in ("summary", "passed", "failed", "errors", "skipped")},
            "host_seconds": host_s,
            "pool": {k: pool.get(k) for k in ("summary", "passed", "failed", "errors", "skipped")},
            "pool_shards": pool.get("pool"),
            "cases": {"host": d["host_cases"], "pool": d["pool_cases"]},
            "differences": d["differences"][:60]}
