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
POOL_DEFAULTS = {"enabled": False, "max_nodes": 3, "min_files": 40}
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
        except (TypeError, ValueError):
            pass
    return cfg


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


# ── the node's side ───────────────────────────────────────────────────────────
#: Directories of the worktree a shard never needs (and that can be huge).
ARCHIVE_EXCLUDE = (".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache")


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
        junit = os.path.join(run, "junit.xml")
        cmd = [os.path.join(TESTENV_DIR, "bin", "python"), "-m", "pytest", *files,
               "--junitxml", junit, "-p", "no:cacheprovider", "-q"]
        if markers:
            cmd += ["-m", markers]
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": run, "LANG": "C.UTF-8",
               "PYTHONPATH": f"{run}:{app}", "PYTHONDONTWRITEBYTECODE": "1",
               "TLS_ENABLED": "0", "VERA_IS_DEV_SANDBOX": "1",
               "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
        progress({"stage": "running", "files": len(files)})
        t0 = time.time()
        proc = subprocess.Popen(cmd, cwd=app, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
        out = b""
        while True:
            try:
                out, _ = proc.communicate(timeout=2)
                break
            except subprocess.TimeoutExpired:
                if should_stop() or time.time() - t0 > timeout:
                    import signal
                    os.killpg(proc.pid, signal.SIGKILL)
                    out, _ = proc.communicate()
                    break
        rc = proc.returncode if proc.returncode is not None else -9
        try:
            with open(junit, encoding="utf-8") as f:
                xml = f.read()
        except OSError:
            xml = ""
        text = out.decode("utf-8", "replace")
        combined = (text[-20000:] + "\n__VERA_JUNIT__\n" + xml + "\n__VERA_RC=" + str(rc) + "__")
        return {"out": combined, "seconds": round(time.time() - t0, 1), "files": len(files),
                "python": st.get("python", "")}
    finally:
        shutil.rmtree(run, ignore_errors=True)
