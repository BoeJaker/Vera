"""
test_runner_core.py — a pipeline gate's pytest run, split across runners

The critical tier runs one gate at a time on the Vera host (one `docker run`
of vera:latest, 335-901 s for ~6,000 tests). The node workers sit next to it
with 12 cores each. This splits a gate's run into shards: the host runs one in
its container, each node with room runs one in a pinned test venv whose
packages match the gate image (`pip freeze` of vera:latest), and the results
merge into the same summary and failure details as a single run.

Parity with the single run is the rule:
  * every shard runs `pytest <its files> -m <markers>` over the SAME code - the
    branch worktree's tracked and untracked files, shipped as one archive - so
    a test file that fails to import still fails the gate, wherever it lands;
  * a shard a node could not run (no room, lost, stale venv) reruns on the
    host: a missing shard is never a pass;
  * the node's output is the same marker format the host's runner prints, so
    one parser reads both.

Pure but for node_run_shard (which runs pytest in a subprocess on the node)
and the filesystem in testenv_state. Tested without a cluster.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: Where a node's test venv lives, and the file that says what it was built from.
TESTENV_DIR = os.environ.get("VERA_TESTENV_DIR", "/opt/vera/testenv")
TESTENV_MARKER = ".vera-testenv.json"
#: Packages the gate image has that a CPU node must not try to install.
_SKIP_PKGS = re.compile(r"^(nvidia-|triton\b|torch\b|torchvision\b|torchaudio\b)", re.I)

#: Per-test-file durations (hash file -> seconds, smoothed), from every run.
DURATIONS_KEY = "vera:evolve:test_durations"
#: A file with no recorded duration counts as this many seconds when sharding.
DEFAULT_FILE_S = 1.0
#: Pool config (json): {"enabled": bool, "max_nodes": int, "min_files": int}.
POOL_KEY = "vera:evolve:test_pool"
POOL_DEFAULTS = {"enabled": False, "max_nodes": 3, "min_files": 40, "chunks": 12,
                 "node_parallel": 4, "host_parallel": 3,
                 # Gates beside a census (compute-roles K, owner 11 Oct 2026): a gate's shards share
                 # the CPU the census's inference uses. idle_priority = shards run at SCHED_IDLE +
                 # nice 19 on the nodes and with --cpu-shares 2 on the host, so inference always
                 # preempts them; busy_avoid = node names/host ids a gate does not use while a loop
                 # or census is running (cpu-246/247 share their physical cores with the 35B).
                 # Both off until measured (K1).
                 "idle_priority": False, "busy_avoid": [],
                 # P0-4 (looplab-control §9.0, 18 Oct 2026): busy_avoid node -> the Ollama instance it hosts. With a
                 # mapping, a busy_avoid node is left alone only while running loop work has used ITS instance in the
                 # last BUSY_RECENT_S; without one, every busy_avoid node is left alone whenever anything runs.
                 "busy_map": {},
                 # P0-6 (owner 18 Oct 2026: "running less tests per gate is approved"): an ADOPT gate runs the test
                 # files that touch its change (impact_select); promote-to-main / nightly runs keep the full tier
                 "impact": True}
#: a node counts as in use by the running work if its instance served a loop call this recently
BUSY_RECENT_S = 600
#: The docker --cpu-shares of an idle-priority host shard (default 1024).
IDLE_CPU_SHARES = 2
#: Per-runner lease for the host's container runner (replaces the one-gate lease).
HOST_RUNNER_LEASE = "vera:evolve:runner:host"


def merge_pool_config(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cfg = dict(POOL_DEFAULTS)
    for k, v in (raw or {}).items():
        try:
            if k == "enabled":
                cfg[k] = bool(v)
            elif k == "max_nodes":
                cfg[k] = max(0, min(16, int(v)))
            elif k == "min_files":
                cfg[k] = max(1, int(v))
            elif k == "chunks":
                cfg[k] = max(1, min(64, int(v)))
            elif k in ("node_parallel", "host_parallel"):
                cfg[k] = max(1, min(8, int(v)))
            elif k in ("idle_priority", "impact"):
                cfg[k] = bool(v)
            elif k == "busy_avoid":
                vals = v.split(",") if isinstance(v, str) else list(v or [])
                cfg[k] = [str(x).strip() for x in vals if str(x).strip()][:16]
            elif k == "busy_map":
                if isinstance(v, str):
                    v = dict(p.split("=", 1) for p in v.split(",") if "=" in p)
                cfg[k] = {str(a).strip(): str(b).strip() for a, b in dict(v or {}).items() if str(a).strip() and str(b).strip()}
        except (TypeError, ValueError):
            pass
    return cfg


def idle_docker_argv(argv: Sequence[str], shares: int = IDLE_CPU_SHARES) -> List[str]:
    """`docker run ...` with --cpu-shares inserted after `run` (unchanged when there is no `run`
    or the argv already sets cpu shares)."""
    out = list(argv)
    if any(str(a).startswith("--cpu-shares") or a == "-c" for a in out) or "run" not in out:
        return out
    i = out.index("run")
    return out[:i + 1] + ["--cpu-shares", str(int(shares))] + out[i + 1:]


def avoided(pick: Dict[str, Any], avoid: Sequence[str]) -> bool:
    """Whether a node pick ({host, host_id, ...}) is one of `avoid` (names or host ids, any case)."""
    want = {str(a).strip().lower() for a in avoid or [] if str(a).strip()}
    if not want:
        return False
    names = {str(pick.get(k) or "").strip().lower() for k in ("host", "host_id", "name", "node")}
    return bool(want & (names - {""}))


# ── the node's test venv ──────────────────────────────────────────────────────
def lock_lines(freeze: str) -> List[str]:
    """The gate image's `pip freeze`, as the pins a CPU node installs: one
    `name==version` per line, sorted; editable/path installs and the GPU stack
    dropped."""
    out = set()
    for line in (freeze or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-") or " @ " in s or "==" not in s:
            continue
        if _SKIP_PKGS.match(s):
            continue
        out.add(s)
    return sorted(out, key=str.lower)


def lock_sha(lines: Sequence[str], python_minor: str = "") -> str:
    h = hashlib.sha256(("py" + python_minor + "\n" + "\n".join(lines)).encode("utf-8"))
    return h.hexdigest()[:16]


def testenv_build_cmd(lines: Sequence[str], sha: str, root: str = TESTENV_DIR) -> str:
    """Shell that (re)builds the test venv beside the old one and swaps it in.
    Installs the lock with --no-deps (it is a complete closure); when the batch
    fails, installs line by line so one unbuildable package (a system library
    the node lacks) costs that package, not the venv - and is reported."""
    req = "\n".join(lines) + "\n"
    new = f"{root}.new"
    return (
        "set -u; "
        # the image's command-line tools first, on every run (not only on a
        # rebuild): tests that drive them fail or skip without them
        + system_tools_cmd() +
        f"[ -f {root}/{TESTENV_MARKER} ] && grep -q '\"sha\": \"{sha}\"' {root}/{TESTENV_MARKER} "
        f"&& {{ echo VERA_TESTENV_CURRENT; cat {root}/{TESTENV_MARKER}; exit 0; }}; "
        f"rm -rf {new}; python3 -m venv {new} || {{ echo VERA_TESTENV_FAILED venv; exit 1; }}; "
        f"{new}/bin/pip install -q -U pip wheel >/dev/null 2>&1; "
        f"printf %s {_sq(req)} > {new}/lock.txt; "
        f"FAILED=''; "
        f"if ! {new}/bin/pip install -q --no-deps -r {new}/lock.txt >/dev/null 2>&1; then "
        f"while read -r L; do [ -z \"$L\" ] && continue; "
        f"{new}/bin/pip install -q --no-deps \"$L\" >/dev/null 2>&1 || FAILED=\"$FAILED $L\"; "
        f"done < {new}/lock.txt; fi; "
        f"{new}/bin/python -c 'import pytest' || {{ echo VERA_TESTENV_FAILED pytest; exit 1; }}; "
        f"printf '{{\"sha\": \"{sha}\", \"failed\": \"%s\", \"python\": \"%s\"}}\\n' \"$FAILED\" "
        f"\"$({new}/bin/python -c 'import sys;print(sys.version.split()[0])')\" > {new}/{TESTENV_MARKER}; "
        f"rm -rf {root}.old; [ -d {root} ] && mv {root} {root}.old; mv {new} {root}; rm -rf {root}.old; "
        f"echo VERA_TESTENV_BUILT; cat {root}/{TESTENV_MARKER}"
    )


#: Command-line tools the gate image has that the critical tier drives. A node
#: without git failed 4 worktree-repair tests and skipped 7 more in the first
#: parity run (2026-10-04).
SYSTEM_TOOLS = ("git",)


def system_tools_cmd(tools: Sequence[str] = SYSTEM_TOOLS) -> str:
    """Shell that installs each missing tool with apt (an apt update only when
    the install fails), never failing the venv build itself."""
    out = ""
    for t in tools:
        out += (f"command -v {t} >/dev/null 2>&1 || {{ export DEBIAN_FRONTEND=noninteractive; "
                f"apt-get install -y -q {t} >/dev/null 2>&1 || "
                f"{{ apt-get update -q >/dev/null 2>&1; apt-get install -y -q {t} >/dev/null 2>&1; }} || "
                f"echo VERA_TESTENV_TOOL_FAILED {t}; }}; ")
    return out


def junit_cases(junit_xml: str) -> Dict[str, Dict[str, str]]:
    """node_id -> {outcome: passed|failed|error|skipped, message} from a JUnit XML."""
    try:
        root = ET.fromstring(junit_xml)
    except ET.ParseError:
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for case in root.iter("testcase"):
        nid = f"{case.get('classname', '')}::{case.get('name', '')}"
        outcome, msg = "passed", ""
        for kind in ("failure", "error", "skipped"):
            el = case.find(kind)
            if el is not None:
                outcome = "failed" if kind == "failure" else kind
                msg = " ".join((el.get("message") or el.text or "").split())[:300]
                break
        out[nid] = {"outcome": outcome, "message": msg}
    return out


def parity_diff(host_outputs: Sequence[str], pool_outputs: Sequence[str]) -> Dict[str, Any]:
    """Where a pooled run's outcomes differ from the host's, test by test, with
    the reason the pool gave (a skip message, a failure message)."""
    def cases(outs):
        merged: Dict[str, Dict[str, str]] = {}
        for o in outs:
            merged.update(junit_cases(junit_of(o)))
        return merged
    h, p = cases(host_outputs), cases(pool_outputs)
    diff = []
    for nid in sorted(set(h) | set(p)):
        a, b = h.get(nid, {"outcome": "missing"}), p.get(nid, {"outcome": "missing"})
        if a["outcome"] != b["outcome"]:
            diff.append({"test": nid, "host": a["outcome"], "pool": b["outcome"],
                         "why": b.get("message") or a.get("message") or ""})
    return {"host_cases": len(h), "pool_cases": len(p), "differences": diff}


def _sq(v: str) -> str:
    return "'" + v.replace("'", "'\"'\"'") + "'"


def parse_testenv_output(out: str) -> Dict[str, Any]:
    """{ok, sha, failed: [...], python, state: current|built|failed}."""
    text = out or ""
    state = ("current" if "VERA_TESTENV_CURRENT" in text else
             "built" if "VERA_TESTENV_BUILT" in text else "failed")
    rec: Dict[str, Any] = {}
    m = re.search(r"\{[^{}]*\"sha\"[^{}]*\}", text)
    if m:
        try:
            rec = json.loads(m.group(0))
        except ValueError:
            rec = {}
    failed = [x for x in str(rec.get("failed") or "").split() if x]
    err = ""
    if state == "failed":
        fm = re.search(r"VERA_TESTENV_FAILED\s+(\S+)", text)
        err = (fm.group(1) if fm else text.strip()[-300:]) or "unknown"
    return {"ok": state != "failed", "state": state, "sha": rec.get("sha", ""),
            "failed": failed, "python": rec.get("python", ""), "error": err}


def testenv_state(root: str = TESTENV_DIR) -> Dict[str, Any]:
    try:
        with open(os.path.join(root, TESTENV_MARKER), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ── sharding ──────────────────────────────────────────────────────────────────
def test_files(names: Iterable[str]) -> List[str]:
    """`tests/test_*.py` paths, sorted, from a directory listing of tests/."""
    return sorted(f"tests/{n}" for n in names if n.startswith("test_") and n.endswith(".py"))


def shard(files: Sequence[str], durations: Dict[str, float], n: int) -> List[List[str]]:
    """Longest-first into the currently lightest shard (LPT): n shards whose
    recorded durations come out close. Every file lands in exactly one."""
    n = max(1, min(int(n), len(files) or 1))
    known = [durations.get(f) for f in files if durations.get(f) is not None]
    default = sorted(known)[len(known) // 2] if known else DEFAULT_FILE_S
    order = sorted(files, key=lambda f: (-(durations.get(f) or default), f))
    loads = [0.0] * n
    out: List[List[str]] = [[] for _ in range(n)]
    for f in order:
        i = min(range(n), key=lambda k: (loads[k], k))
        out[i].append(f)
        loads[i] += durations.get(f) or default
    return [sorted(s) for s in out if s]


def file_durations(junit_xml: str) -> Dict[str, float]:
    """Seconds per test file from a JUnit XML (testcase classname is the module
    path, e.g. `tests.test_x` or `tests.test_x.TestY`)."""
    try:
        root = ET.fromstring(junit_xml)
    except ET.ParseError:
        return {}
    out: Dict[str, float] = {}
    for case in root.iter("testcase"):
        cls = case.get("classname") or ""
        parts = cls.split(".")
        mod = next((p for p in parts if p.startswith("test_")), "")
        if not mod:
            continue
        f = f"tests/{mod}.py"
        try:
            out[f] = out.get(f, 0.0) + float(case.get("time") or 0)
        except ValueError:
            pass
    return out


def junit_of(output: str) -> str:
    start = output.find("__VERA_JUNIT__")
    end = output.find("__VERA_RC=", start + 1)
    return output[start + len("__VERA_JUNIT__"):end].strip() if start >= 0 and end > start else ""


def smooth(old: Optional[float], new: float, alpha: float = 0.5) -> float:
    return round(new if old is None else (alpha * new + (1 - alpha) * old), 3)


def merge(parsed: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """One result from the shards' parsed results (parse_pytest_output shape)."""
    keys = ("passed", "failed", "errors", "skipped", "total")
    out: Dict[str, Any] = {k: sum(int(p.get(k) or 0) for p in parsed) for k in keys}
    rcs = [int(p.get("rc", -1)) for p in parsed]
    # pytest rc 5 = "no tests collected" - normal for a shard whose files hold
    # no critical tests; any other non-zero rc is the run's
    real = [rc for rc in rcs if rc not in (0, 5)]
    out["rc"] = real[0] if real else 0
    details: List[Dict[str, str]] = []
    for p in parsed:
        details.extend(p.get("failure_details") or [])
    out["failure_details"] = details[:50]
    out["ok"] = (out["failed"] == 0 and out["errors"] == 0 and out["total"] > 0
                 and out["rc"] == 0)
    out["summary"] = (f"{out['passed']} passed, {out['failed']} failed, {out['errors']} errors, "
                      f"{out['skipped']} skipped (rc={out['rc']}, {len(parsed)} shards)")
    return out


def combine_rc(rcs: Sequence[int]) -> int:
    """One exit code for several pytest processes over one chunk: the first
    real failure; 5 (no tests collected) only when every process had none."""
    real = [rc for rc in rcs if rc not in (0, 5)]
    if real:
        return real[0]
    return 5 if rcs and all(rc == 5 for rc in rcs) else 0


def combine_outputs(outs: Sequence[str]) -> str:
    """Several runs' outputs (each text + __VERA_JUNIT__ xml + __VERA_RC=n__) as
    ONE in the same format: the texts' tails, the JUnit reports merged into one
    suite, and the combined exit code."""
    rcs, xmls, texts = [], [], []
    for out in outs:
        m = re.search(r"__VERA_RC=(-?\d+)__", out or "")
        rcs.append(int(m.group(1)) if m else -1)
        xmls.append(junit_of(out or ""))
        texts.append((out or "").split("__VERA_JUNIT__")[0][-6000:])
    return ("\n".join(texts)[-20000:] + "\n__VERA_JUNIT__\n" + merge_junit(xmls)
            + "\n__VERA_RC=" + str(combine_rc(rcs)) + "__")


def merge_junit(xmls: Sequence[str]) -> str:
    """Several JUnit XML files as ONE testsuite whose counts are the sums (the
    gate's parser reads a single suite's totals)."""
    cases, totals, secs = [], {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}, 0.0
    for x in xmls:
        if not x:
            continue
        try:
            root = ET.fromstring(x)
        except ET.ParseError:
            continue
        for suite in root.iter("testsuite"):
            for k in totals:
                try:
                    totals[k] += int(suite.get(k) or 0)
                except ValueError:
                    pass
            try:
                secs += float(suite.get("time") or 0)
            except ValueError:
                pass
            cases.extend(ET.tostring(c, encoding="unicode") for c in suite.findall("testcase"))
    attrs = " ".join(f'{k}="{v}"' for k, v in totals.items())
    return (f'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" {attrs} '
            f'time="{secs:.3f}">' + "".join(cases) + "</testsuite></testsuites>")


# ── the node's side ───────────────────────────────────────────────────────────
#: Directories of the worktree a shard never needs (and that can be huge).
ARCHIVE_EXCLUDE = (".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache")


def yield_cpu(pid: int) -> bool:
    """Put a shard's pytest process at SCHED_IDLE + nice 19 (pool idle_priority): it then runs
    only on cycles the node's inference leaves. Set right after spawn; anything it starts later
    inherits it. Lowering priority needs no privilege. False when the platform has no SCHED_IDLE."""
    ok = False
    try:
        os.setpriority(os.PRIO_PROCESS, pid, 19)
        ok = True
    except (AttributeError, OSError):
        pass
    try:
        os.sched_setscheduler(pid, os.SCHED_IDLE, os.sched_param(0))
    except (AttributeError, OSError):
        return False
    return ok


def node_run_shard(inputs: Dict[str, Any], progress, should_stop) -> Dict[str, Any]:
    """compute.run entry (node_compute_core.FUNCTIONS["pytest.shard"]).

    inputs: archive (tar.gz bytes of the worktree), files ([tests/...]),
    markers (str), testenv_sha (str - the venv the host expects), timeout (s).
    Runs pytest in this node's test venv with the gate container's layout and
    environment, and returns its output in the gate runner's marker format."""
    st = testenv_state()
    want = str(inputs.get("testenv_sha") or "")
    if not st or (want and st.get("sha") != want):
        raise RuntimeError(f"test venv is {st.get('sha') or 'missing'}, the gate needs {want}")
    files = [f for f in inputs.get("files") or [] if re.match(r"^tests/[A-Za-z0-9_./-]+\.py$", f)
             and ".." not in f]
    if not files:
        raise RuntimeError("no test files")
    markers = str(inputs.get("markers") or "")
    if markers and not re.match(r"^[A-Za-z0-9_ ()-]+$", markers):
        raise RuntimeError("unsafe marker expression")
    timeout = max(30, min(3600, int(inputs.get("timeout") or 900)))
    run = tempfile.mkdtemp(prefix="vera-shard-")
    try:
        app = os.path.join(run, "Vera")
        os.makedirs(app)
        with tarfile.open(fileobj=io.BytesIO(inputs["archive"]), mode="r:gz") as tf:
            for m in tf.getmembers():
                if m.name.startswith("/") or ".." in m.name.split("/") or m.issym() or m.islnk():
                    continue
                tf.extract(m, app)
        # Split the chunk over `parallel` pytest processes (balanced by the
        # recorded durations): a node has ~10 free cores, one process used one
        # of them (2026-10-05). Each file still runs exactly once, in its own
        # process group, as with chunks.
        k = max(1, min(int(inputs.get("parallel") or 1), len(files)))
        groups = shard(files, {f: float(v) for f, v in (inputs.get("durations") or {}).items()}, k)
        base_env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                    "PYTHONPATH": f"{run}:{app}", "PYTHONDONTWRITEBYTECODE": "1",
                    "TLS_ENABLED": "0", "VERA_IS_DEV_SANDBOX": "1",
                    "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
        procs = []
        progress({"stage": "running", "files": len(files), "processes": len(groups)})
        t0 = time.time()
        for i, g in enumerate(groups):
            home = os.path.join(run, f"home{i}")
            os.makedirs(home)
            junit = os.path.join(run, f"junit{i}.xml")
            cmd = [os.path.join(TESTENV_DIR, "bin", "python"), "-m", "pytest", *g,
                   "--junitxml", junit, "-p", "no:cacheprovider", "-q"]
            if markers:
                cmd += ["-m", markers]
            pr = subprocess.Popen(cmd, cwd=app, env=dict(base_env, HOME=home),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  start_new_session=True)
            if inputs.get("idle"):
                yield_cpu(pr.pid)
            procs.append((pr, junit))
        outs = [b""] * len(procs)
        import threading

        def drain(i, proc):
            outs[i] = proc.stdout.read() or b""
        readers = [threading.Thread(target=drain, args=(i, pr), daemon=True)
                   for i, (pr, _j) in enumerate(procs)]
        for t in readers:
            t.start()
        while any(pr.poll() is None for pr, _j in procs):
            if should_stop() or time.time() - t0 > timeout:
                import signal
                for pr, _j in procs:
                    if pr.poll() is None:
                        try:
                            os.killpg(pr.pid, signal.SIGKILL)
                        except OSError:
                            pass
                break
            time.sleep(1)
        for pr, _j in procs:
            try:
                pr.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        for t in readers:
            t.join(timeout=5)
        rcs = [pr.returncode if pr.returncode is not None else -9 for pr, _j in procs]
        xmls = []
        for _pr, junit in procs:
            try:
                with open(junit, encoding="utf-8") as f:
                    xmls.append(f.read())
            except OSError:
                xmls.append("")
        rc = combine_rc(rcs)
        xml = merge_junit(xmls)
        text = "\n".join(o.decode("utf-8", "replace")[-6000:] for o in outs)
        combined = (text[-20000:] + "\n__VERA_JUNIT__\n" + xml + "\n__VERA_RC=" + str(rc) + "__")
        return {"out": combined, "seconds": round(time.time() - t0, 1), "files": len(files),
                "processes": len(groups), "python": st.get("python", "")}
    finally:
        shutil.rmtree(run, ignore_errors=True)


def placement(shards) -> str:
    """Where a gate's chunks ran, one line: "host 4, Ollama 5, Ollama-C 3 (1 requeued)".
    17 Oct 2026: after the Vera VM ran out of memory under a gate, nothing in vera.log said
    whether its chunks ran on the VM or on the nodes."""
    counts = {}
    requeued = 0
    for s in shards or ():
        if s.get("requeued"):
            requeued += 1
            continue
        r = str(s.get("runner") or "?")
        counts[r] = counts.get(r, 0) + 1
    out = ", ".join("%s %d" % (k, counts[k]) for k in sorted(counts, key=lambda k: (k != "host", k)))
    return (out or "no chunks") + (" (%d requeued)" % requeued if requeued else "")



def live_avoid(avoid: Sequence[str], busy_map: Dict[str, str], route_stats: Iterable[Dict[str, Any]],
               now: float, recent_s: float = BUSY_RECENT_S) -> List[str]:
    """P0-4: of `avoid`, the nodes the running loop work is actually using. A node with no entry in `busy_map`
    stays avoided (nothing says it is free); a mapped node is avoided only if its instance served a loop_* call in
    the last `recent_s` seconds (route stats' last_ts). Before this, both CPU nodes were skipped whenever anything
    ran, leaving gates the GPU node alone (18 Oct 2026)."""
    from datetime import datetime
    used = set()
    for s in route_stats or []:
        if not str(s.get("job_type") or "").startswith("loop_"):
            continue
        try:
            t = datetime.fromisoformat(str(s.get("last_ts") or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if now - t <= recent_s:
            used.add(str(s.get("instance") or ""))
    return [n for n in avoid or [] if n not in (busy_map or {}) or busy_map[n] in used]



# ── P0-5: one fair queue for every gate (looplab-control §9.0, 18 Oct 2026) ──────────────────────────────────────
# ~15 gates polled for runners at once, each grabbing whatever was free that second: no order, and a gate adopted an
# hour earlier could keep losing to newer ones. Now every gate joins one queue (joined time = its place), heartbeats
# while it has chunks waiting, and takes a runner only in its turn: the host's lease goes to the oldest waiting gate;
# free node slots go to the oldest first, then one each to the next ones while slots remain.
GATE_QUEUE_KEY = "vera:evolve:test_pool:queue"                 # zset  token -> joined (epoch s)
GATE_QUEUE_PENDING = "vera:evolve:test_pool:queue:pending"     # hash  token -> chunks waiting
GATE_QUEUE_ALIVE = "vera:evolve:test_pool:queue:alive:"        # + token, expires: a gate that died drops out
GATE_QUEUE_ALIVE_S = 90


def queue_order(joined: Dict[str, float], pending: Dict[str, int], alive: Iterable[str]) -> List[Tuple[str, float, int]]:
    """The gates waiting for a runner, oldest first: alive, and with at least one chunk not yet running."""
    live = set(alive or ())
    rows = [(t, float(ts), int(pending.get(t, 0) or 0)) for t, ts in (joined or {}).items() if t in live]
    return sorted([r for r in rows if r[2] > 0], key=lambda r: (r[1], r[0]))


def queue_position(order: Sequence[Tuple[str, float, int]], token: str, exclude: Iterable[str] = ()) -> Tuple[int, int]:
    """(this gate's 0-based place, how many wait), leaving out `exclude` (the host lease's holder for the host)."""
    ex = set(exclude or ())
    toks = [t for t, _, _ in order if t not in ex]
    return (toks.index(token) if token in toks else len(toks)), len(toks)


def node_share(position: int, free_slots: int) -> int:
    """How many of the free node slots this gate takes now: the oldest takes all it can use; each next gate one, while
    slots remain - so a gate never starves behind the head, and the head is never starved by the rest."""
    if free_slots <= 0:
        return 0
    if position == 0:
        return free_slots
    return 1 if position < free_slots else 0



# ── P0-6: test-impact selection for adopt gates (owner approved 18 Oct 2026) ─────────────────────────────────────
# Every gate ran all 1,079 test files whatever it changed; a one-file panel fix waited as long as a core rewrite.
# An adopt gate now runs the files that touch the change; anything that could touch everything runs everything.
#: a change to any of these runs the full tier
IMPACT_FULL = ("vera/capability_orchestration.py", "tests/conftest.py", "conftest.py", "requirements", "pytest.ini",
               "setup.cfg", "pyproject.toml", "vera/__init__.py", "tests/__init__.py")
#: above this share of all files, the selection is not worth it: run the full tier
IMPACT_MAX_SHARE = 0.6
#: how many steps out from a changed module its importers count
IMPACT_DEPTH = 3
#: modules nearly everything imports (for the decorator, the registry): reaching one does not spread further - a
#: change TO one runs the full tier (IMPACT_FULL); a change that a hub merely looks up stays local
IMPACT_HUBS = {"capability_orchestration"}
_SYSMOD_RE = re.compile(r"sys\.modules(?:\.get\(|\[)\s*[\"']([\w.]+)[\"']")
_IMPORT_RE = re.compile(r"^[ \t]*(?:from[ \t]+([\w.]+)[ \t]+import[ \t]+(\([^)]*\)|[\w*, \t]+)|import[ \t]+([\w., \t]+))", re.M)


def module_name(path: str) -> str:
    """vera/evolve/x_core.py -> x_core (the loader registers modules under bare names; tests use vera.X or the bare
    name or the file path - all contain it)."""
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    return base[:-3] if base.endswith(".py") else base


def imports_of(src: str) -> List[str]:
    """Bare module names a source imports: `from a.b import c, d` gives b, c, d; `import a.b` gives b."""
    out: List[str] = []
    for m in _IMPORT_RE.finditer(src or ""):
        if m.group(1):
            out.append(m.group(1).rsplit(".", 1)[-1])
            out += [x.strip().split(" as ")[0].strip() for x in m.group(2).replace("(", "").replace(")", "").split(",")]
        elif m.group(3):
            out += [x.strip().split(" as ")[0].strip().rsplit(".", 1)[-1] for x in m.group(3).split(",")]
    # modules loaded by bare name are reached through sys.modules, not imports (the loader registers them so):
    # sys.modules.get("evolve_capabilities") or ...get("Vera.vera.evolve.evolve_capabilities") is a dependency too
    out += [m.rsplit(".", 1)[-1] for m in _SYSMOD_RE.findall(src or "")]
    return [x for x in out if x and x != "*"]


def importers_index(sources: Dict[str, str]) -> Dict[str, set]:
    """bare module name -> the bare names of the vera modules that import it."""
    idx: Dict[str, set] = {}
    for path, src in (sources or {}).items():
        me = module_name(path)
        for dep in set(imports_of(src)):
            idx.setdefault(dep, set()).add(me)
    return idx


def affected_names(changed: Iterable[str], importers: Dict[str, set], depth: int = IMPACT_DEPTH) -> set:
    """The changed modules and everything that imports them, up to `depth` steps out; plus every changed file's name
    (an .html / .js / .json is matched by its file name)."""
    names: set = set()
    frontier: set = set()
    for c in changed or []:
        base = c.replace("\\", "/").rsplit("/", 1)[-1]
        names.add(base)
        if c.endswith(".py"):
            frontier.add(module_name(c))
    seen = set(frontier)
    for _ in range(max(0, depth)):
        nxt = set()
        for n in frontier:
            nxt |= importers.get(n, set()) - seen - IMPACT_HUBS
        seen |= nxt
        frontier = nxt
        if not frontier:
            break
    return names | seen


def impact_select(test_files: Sequence[str], changed: Sequence[str], test_text: Dict[str, str],
                  importers: Dict[str, set], max_share: float = IMPACT_MAX_SHARE) -> Tuple[List[str], str]:
    """(the test files to run, why). Full list when the change could touch everything, when nothing was selected,
    or when the selection is most of the suite anyway."""
    files = list(test_files or [])
    ch = [c.replace("\\", "/") for c in (changed or [])]
    full = [c for c in ch if any(c == f or c.startswith(f) for f in IMPACT_FULL)]
    if full:
        return files, "full tier: the change touches %s" % ", ".join(sorted(set(full))[:3])
    names = {n for n in affected_names(ch, importers) if len(n) >= 4}
    # one alternation for every name (one pass per test file, not one per name)
    rx = re.compile(r"(?<![\w])(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?![\w])")         if names else None
    picked = []
    for f in files:
        base = f.rsplit("/", 1)[-1]
        if any(c == f or c == "tests/" + base for c in ch):
            picked.append(f)
            continue
        txt = test_text.get(f) or ""
        if rx is not None and rx.search(txt):
            picked.append(f)
    if not picked:
        return files, "full tier: no test names the change (safe default)"
    if len(picked) > max_share * len(files):
        return files, "full tier: %d of %d files touch the change" % (len(picked), len(files))
    return picked, "%d of %d test files touch the change (%d module(s) affected)" % (
        len(picked), len(files), sum(1 for n in names if not n.endswith((".html", ".js", ".json", ".css"))))
