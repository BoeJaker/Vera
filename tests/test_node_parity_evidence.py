"""Node parity by evidence: a capability may run on a node worker only when the
static audit of its code path AND the footprint observed on the host while it
ran both show nothing host-bound (owner, 2026-10-04: parity vetted in tiers).

These pin the classifier (what counts as host-bound), the verdict, the
cross-module static audit, and how a vetted capability is placed.
"""
import json
import os

import pytest

from vera.workers import cap_footprint_core as fc
from vera.workers import node_cap_audit as audit
from vera.workers import worker_placement_core as wp

pytestmark = pytest.mark.critical

SHARED = {("localhost", 6379), ("192.168.0.138", 5432)}
PORTS = {6379, 5432, 7687, 8000}
NODES = {"192.168.0.246", "192.168.0.250"}
HOST = {"192.168.0.138"}


def _sock(addr):
    return fc.classify("socket.connect", (None, addr), shared=SHARED, shared_ports=PORTS,
                       nodes=NODES, host_ips=HOST)


def test_files():
    roots = ("/home/u/Vera/",)
    assert fc.classify_open("/home/u/Vera/vera/ui/x.html", "r", 0, roots) is None
    # with the tracked files known: a tracked template is code, an untracked
    # file in the tree is runtime data the host wrote (2026-10-04:
    # vera/ide/.vera_remote_instances.json passed as "code")
    tracked = frozenset({"/home/u/Vera/vera/ui/x.html"})
    assert fc.classify_open("/home/u/Vera/vera/ui/x.html", "r", 0, roots, tracked) is None
    assert fc.classify_open("/home/u/Vera/vera/ide/.vera_remote_instances.json", "r", 0, roots,
                            tracked) == ("file_read", "/home/u/Vera/vera/ide/.vera_remote_instances.json")
    assert fc.classify_open("/home/u/Vera/vera/data/state.json", "w", 0, roots)[0] == "file_write"
    assert fc.classify_open("/usr/lib/python3/x.py", "r", 0) is None
    assert fc.classify_open("/tmp/x", "w", 0) is None
    assert fc.classify_open("/home/u/.vera/secret.key", "r", 0)[0] == "file_read"
    assert fc.classify_open("/srv/data/f.bin", None, os.O_WRONLY)[0] == "file_write"
    assert fc.classify_open(7, "r", 0) is None


def test_sockets():
    assert _sock(("localhost", 6379))[0] == "socket_shared"
    assert _sock(("127.0.0.1", 5432))[0] == "socket_shared"       # a shared port on loopback
    assert _sock(("127.0.0.1", 8999))[0] == "socket_local"        # the host's own API
    assert _sock("/var/run/docker.sock") == ("socket_local", "unix:/var/run/docker.sock")
    assert _sock(("192.168.0.246", 11434))[0] == "socket_node"
    assert _sock(("140.82.112.3", 443))[0] == "socket_external"
    assert _sock(("192.168.0.138", 8200))[0] == "socket_local"    # the host's LAN IP, not shared
    assert fc.classify("sqlite3.connect", ("/home/u/fabric.db",))[0] == "sqlite"
    assert fc.classify("subprocess.Popen", ("docker", ["docker", "ps"], None, None))[0] == "subprocess"


def test_merge_bounds_targets_and_counts_runs():
    fp = None
    for i in range(10):
        fp = fc.merge(fp, {"socket_external": 1}, {"socket_external": {f"h{i}:443"}}, 1, 100.0 + i)
    assert fp["runs"] == 10 and fp["runs_tracked"] == 10 and fp["kinds"]["socket_external"] == 10
    assert len(fp["targets"]["socket_external"]) == fc.MAX_TARGETS
    assert fp["first"] == 100.0 and fp["last"] == 109.0


def test_verdict_needs_both_halves():
    clean = {"runs": 5, "kinds": {"socket_shared": 9}}
    assert fc.verdict(clean, [])[0] == "vetted"
    assert fc.verdict(clean, ["sqlite"]) == ("host", "source: sqlite")
    assert fc.verdict({"runs": 5, "kinds": {"subprocess": 1}, "targets": {"subprocess": ["git"]}}, [])[0] == "host"
    assert fc.verdict({"runs": 1}, [])[0] == "unproven"
    assert fc.verdict(None, [])[0] == "unproven"
    assert fc.verdict(clean, [], static_known=False)[0] == "unproven"


def test_a_file_reading_source_is_settled_by_its_runs():
    # static analysis cannot tell a template read from a data file; the tracer
    # can (it ignores the code tree), so `localfs` needs more runs, not a veto
    assert fc.verdict({"runs": 3, "runs_tracked": 3, "kinds": {}}, ["localfs"])[0] == "unproven"
    # runs from before the tracked-files check do not count for a file-reading source
    assert fc.verdict({"runs": 50, "kinds": {}}, ["localfs"])[0] == "unproven"
    v = fc.verdict({"runs": 9, "runs_tracked": fc.MIN_RUNS_SOFT, "kinds": {"socket_shared": 2}}, ["localfs"])
    assert v[0] == "vetted" and "code tree" in v[1]
    bad = fc.verdict({"runs": 9, "kinds": {"file_read": 1}, "targets": {"file_read": ["/srv/x.json"]}},
                     ["localfs"])
    assert bad[0] == "host" and "/srv/x.json" in bad[1]
    # a hard marker beside it still decides
    assert fc.verdict({"runs": 9, "kinds": {}}, ["localfs", "sqlite"])[0] == "host"


def test_a_caller_of_a_host_bound_cap_is_host_bound():
    rows = {"a.pure": {"markers": [], "calls": ["b.mid"]},
            "b.mid": {"markers": [], "calls": ["c.db"]},
            "c.db": {"markers": ["sqlite"], "calls": []},
            "d.ok": {"markers": [], "calls": ["llm.generate"]},
            "e.st": {"markers": [], "state": True, "calls": []}}
    eff = fc.static_markers(rows, safe={"llm.generate"})
    assert eff["c.db"] == ["sqlite"] and eff["b.mid"] == ["calls_hostbound"]
    assert eff["a.pure"] == ["calls_hostbound"] and eff["d.ok"] == [] and eff["e.st"] == ["state"]


def test_vetted_caps_are_placed_in_the_extended_class():
    try:
        assert wp.set_vetted({"canvas.render": {"cls": "extended", "why": "evidence"}})
        assert not wp.set_vetted({"canvas.render": {"cls": "extended", "why": "evidence"}})
        assert wp.placement("canvas.render")[0] == "any"
        assert wp.class_of("canvas.render", env={}) == "extended"
        assert wp.may_run_here("canvas.render", is_worker=True, classes=["general"])[0] is False
        assert wp.may_run_here("canvas.render", is_worker=True, classes=["extended"])[0] is True
        # evidence beats the setter-by-name guess (the audit checks the state
        # write the name only suggests) ...
        assert wp.placement("canvas.config.set")[0] == "host"     # not vetted: the name rule
        wp.set_vetted({"canvas.config.set": ("extended", "x")})
        assert wp.placement("canvas.config.set")[0] == "any"
        # ... and the operator's brake still beats everything
        assert wp.placement_from_env("canvas.config.set",
                                     {"VERA_WORKER_HOST_ONLY": "canvas"})[0] == "host"
        # no node takes it by default
        assert "extended" not in wp.DEFAULT_CLASSES_CPU
    finally:
        wp.set_vetted({})


def test_static_audit_follows_calls_across_modules(tmp_path):
    v = tmp_path / "vera"
    (v / "store").mkdir(parents=True)
    (v / "store" / "db.py").write_text(
        "import sqlite3\n\ndef put(x):\n    c = sqlite3.connect('/var/x.db')\n    c.execute('x')\n")
    (v / "caps.py").write_text(
        "import os\n"
        "from Vera.vera.store.db import put\n"
        "from Vera.vera.capability_orchestration import capability, emit_event\n"
        "_CACHE = {}\n\n"
        "@capability('demo.save')\nasync def save(x=''):\n    put(x)\n\n"
        "@capability('demo.pure')\nasync def pure(x=''):\n    return {'n': len(x)}\n\n"
        "@capability('demo.key')\nasync def key():\n    return os.getenv('SOME_API_KEY')\n\n"
        "@capability('demo.remember')\nasync def remember(x=''):\n    _CACHE[x] = 1\n\n"
        "@capability('demo.recall')\nasync def recall(x=''):\n    return _CACHE.get(x)\n\n"
        "@capability('demo.chain')\nasync def chain():\n    return await _call('demo.save')\n")
    # a stand-in orchestrator: health the host updates at runtime vs. a
    # registry filled while modules load
    (v / "capability_orchestration.py").write_text(
        "OLLAMA_INSTANCES = {}\nCAPABILITY_REGISTRY = {}\nREDIS = None\n\n"
        "def _health_tick():\n    OLLAMA_INSTANCES['a'] = {'ok': True}\n\n"
        "def _connect():\n    global REDIS\n    REDIS = object()\n\n"
        "def capability(name):\n    def d(f):\n        CAPABILITY_REGISTRY[name] = f\n        return f\n    return d\n")
    (v / "orchcaps.py").write_text(
        "import Vera.vera.capability_orchestration as _orch\n"
        "from Vera.vera.capability_orchestration import OLLAMA_INSTANCES, capability\n\n"
        "@capability('demo.health')\nasync def health():\n    return dict(_orch.OLLAMA_INSTANCES)\n\n"
        "@capability('demo.health2')\nasync def health2():\n    return list(OLLAMA_INSTANCES)\n\n"
        "@capability('demo.health3')\nasync def health3():\n    return getattr(_orch, 'OLLAMA_INSTANCES', {})\n\n"
        "@capability('demo.caps')\nasync def caps():\n    return sorted(_orch.CAPABILITY_REGISTRY)\n\n"
        "@capability('demo.kv')\nasync def kv():\n    return await _orch.REDIS.get('k')\n")
    # state the earlier rules missed (2026-10-04 review of the first vet list):
    # a float setting rebound via `global`, an object built at load time, an
    # event a node would emit to no UI, a measurement of the local machine
    (v / "misc.py").write_text(
        "import psutil\n"
        "from Vera.vera.capability_orchestration import capability, emit_event\n"
        "_SCALE = 1.0\n\n"
        "class Model:\n    def stats(self):\n        return {}\n\n"
        "MODEL = Model()\n\n"
        "@capability('demo.scale.set')\nasync def scale_set(v=1.0):\n    global _SCALE\n    _SCALE = v\n\n"
        "@capability('demo.scale.get')\nasync def scale_get():\n    return _SCALE\n\n"
        "@capability('demo.model.stats')\nasync def model_stats():\n    return MODEL.stats()\n\n"
        "@capability('demo.notify')\nasync def notify():\n    await emit_event({'type': 'ui.x'})\n\n"
        "@capability('demo.cpu')\nasync def cpu():\n    return psutil.cpu_percent()\n\n"
        "async def _check_cpu():\n    return psutil.cpu_percent()\n\n"
        "@capability('demo.scan')\nasync def scan():\n"
        "    return [await c() for c in (_check_cpu,)]\n\n"
        "import sys\n"
        "@capability('demo.skills')\nasync def skills():\n"
        "    return list(getattr(sys.modules.get('skills'), 'SKILLS', {}))\n\n"
        "if True:\n    _SWEEPS = {}\n\n"
        "    @capability('demo.sweep.start')\n    async def sweep_start(i=''):\n        _SWEEPS[i] = {'done': 0}\n\n"
        "    @capability('demo.sweep.status')\n    async def sweep_status(i=''):\n        return _SWEEPS.get(i)\n")
    # gaps found reviewing vet batch 3 (2026-10-09): a Response object cannot
    # cross the worker wire; a path in the host's home directory; state built
    # behind `getattr(...) or {...}`
    (v / "gaps.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "from Vera.vera.capability_orchestration import capability\n"
        "from fastapi.responses import HTMLResponse\n"
        "_POLICY = Path(os.path.join(os.path.expanduser('~'), '.x.json'))\n"
        "_STATE = getattr(object, '_X', None) or {'fired': 0}\n\n"
        "def _load():\n    return _POLICY.exists()\n\n"
        "async def _tick():\n    _STATE['fired'] += 1\n\n"
        "@capability('demo.panel')\nasync def panel():\n    return HTMLResponse('<h1>x</h1>')\n\n"
        "@capability('demo.policy')\nasync def policy():\n    return {'p': _load(), 'path': str(_POLICY)}\n\n"
        "@capability('demo.fired')\nasync def fired():\n    return _STATE['fired']\n")
    # another module's runtime state, read through its alias (2026-10-11:
    # web.engines.health -> search_engines.SEARXNG_STATE); its functions and
    # load-time constants are fine
    (v / "web2").mkdir()
    (v / "web2" / "eng.py").write_text(
        "STATE = {}\nLIMIT = 5\n\n"
        "def note(x):\n    STATE['last'] = x\n\n"
        "def status(s):\n    return dict(s)\n")
    (v / "webcaps.py").write_text(
        "from Vera.vera.web2 import eng as _eng\n"
        "from Vera.vera.capability_orchestration import capability\n\n"
        "@capability('demo.engines.health')\nasync def health():\n    return _eng.status(_eng.STATE)\n\n"
        "@capability('demo.engines.limit')\nasync def limit():\n    return _eng.LIMIT\n")
    rows = audit.audit(str(tmp_path))
    assert "http_response" in rows["demo.panel"]["markers"], rows["demo.panel"]
    assert "reads_state" in rows["demo.engines.health"]["markers"], rows["demo.engines.health"]
    assert "reads_state" not in rows["demo.engines.limit"]["markers"], rows["demo.engines.limit"]
    assert "home_fs" in rows["demo.policy"]["markers"], rows["demo.policy"]
    assert "reads_state" in rows["demo.fired"]["markers"], rows["demo.fired"]
    assert fc.verdict({"runs": 50, "runs_tracked": 50, "kinds": {}}, rows["demo.policy"]["markers"])[0] == "host"
    assert "reads_state" in rows["demo.scale.get"]["markers"], rows["demo.scale.get"]
    assert "reads_state" in rows["demo.model.stats"]["markers"], rows["demo.model.stats"]
    # emitting an event is fine on a node: the host relays workers' events to
    # its UI (worker_event_relay)
    assert rows["demo.notify"]["markers"] == [], rows["demo.notify"]
    assert "local_metrics" in rows["demo.cpu"]["markers"]
    # a helper run by reference (from a tuple), not by a direct call
    assert "local_metrics" in rows["demo.scan"]["markers"], rows["demo.scan"]
    # another module's state through sys.modules; state defined in a top-level if
    assert "foreign_state" in rows["demo.skills"]["markers"]
    assert "reads_state" in rows["demo.sweep.status"]["markers"], rows["demo.sweep.status"]
    # host runtime state read through the orchestrator: a worker's copy is empty
    for c in ("demo.health", "demo.health2", "demo.health3"):
        assert "orch_state" in rows[c]["markers"], (c, rows[c])
    # state filled at load time is the same in every process, and a shared
    # backend's connection handle (rebound via `global` when it connects) is
    # every process's own way to the same store
    assert rows["demo.caps"]["markers"] == []
    assert rows["demo.kv"]["markers"] == [], rows["demo.kv"]
    assert rows["demo.save"]["markers"] == ["sqlite"] and "store.db" in rows["demo.save"]["via"]
    assert rows["demo.pure"]["markers"] == [] and not rows["demo.pure"]["state"]
    assert "env" in rows["demo.key"]["markers"]
    assert rows["demo.remember"]["state"] is True
    assert "reads_state" in rows["demo.recall"]["markers"]
    assert rows["demo.chain"]["calls"] == ["demo.save"]
    eff = fc.static_markers(rows, safe=set())
    assert eff["demo.chain"] == ["calls_hostbound"] and eff["demo.pure"] == []



def test_a_reviewed_list_scopes_the_drops_too():
    # 2026-10-10: applying a reviewed batch also dropped every applied cap the
    # evidence no longer cleared (ui.room) - `only` limited only the adds
    v = {"a.one": ("vetted", ""), "a.two": ("vetted", ""), "b.room": ("host", "calls_hostbound"),
         "a.gone": ("host", "x"), "a.unproven": ("unproven", "")}
    p = fc.plan_vet(v, current={"b.room", "a.gone"}, only=["a.one", "a.unproven"])
    assert p == {"add": ["a.one"], "drop": [], "not_cleared": ["a.unproven"]}, p
    # no list: everything the evidence clears is added, everything it no longer clears is dropped
    p = fc.plan_vet(v, current={"b.room", "a.gone", "a.one"})
    assert p["add"] == ["a.two"] and p["drop"] == ["a.gone", "b.room"], p
    # a namespace scopes both sides; a reviewed list naming a cap to drop drops it
    assert fc.plan_vet(v, current={"b.room", "a.gone"}, namespace="a")["drop"] == ["a.gone"]
    assert fc.plan_vet(v, current={"b.room"}, only=["b.room"])["drop"] == ["b.room"]



def test_decrypting_a_secret_keeps_a_cap_on_the_host(tmp_path):
    """2026-10-17: ha.summary, n8n.* and proxmox.status passed every rule but
    unseal their tokens through platform_access / vsecrets (the in-process
    platforms copy, the host's master key)."""
    v = tmp_path / "vera"
    v.mkdir()
    (v / "capability_orchestration.py").write_text(
        "CAPABILITY_REGISTRY = {}\n\ndef capability(name):\n    def d(f):\n        return f\n    return d\n")
    (v / "svc.py").write_text(
        "import Vera.vera.capability_orchestration as _orch\n"
        "from Vera.vera.capability_orchestration import capability\n\n"
        "def _pa():\n    import platform_access\n    return platform_access\n\n"
        "async def _load_config():\n"
        "    return {'token': await _pa().open_any(_orch.CAPABILITY_REGISTRY, 'ref')}\n\n"
        "@capability('demo.summary')\nasync def summary():\n    return await _load_config()\n\n"
        "@capability('demo.cluster')\nasync def cluster():\n"
        "    from vsecrets import open_secret\n    return open_secret('x')\n\n"
        "@capability('demo.list')\nasync def listing():\n    return await _orch.REDIS.hgetall('k')\n\n"
        "@capability('demo.reopen')\nasync def reopen():\n    return reopen_any('x')\n")
    rows = audit.audit(str(tmp_path))
    assert "opens_secret" in rows["demo.summary"]["markers"], "followed into the helper"
    assert "opens_secret" in rows["demo.cluster"]["markers"]
    assert "opens_secret" not in rows["demo.list"]["markers"]
    assert "opens_secret" not in rows["demo.reopen"]["markers"], "whole names only"
