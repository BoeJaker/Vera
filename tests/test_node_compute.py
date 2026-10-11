"""Model training on the node workers (node_compute_core + the three trainers).

The host gathers, a node computes, the host keeps the result. These pin the
contract between the two: the wire format carries arrays and bytes without
pickle, a job goes to the nodes only when every compute worker can run it, a
job the host gave up on is never left running, and each trainer's node entry
point does the same work the host used to do in-process.
"""
import ast
import json
import os
import subprocess
import sys

import pytest

np = pytest.importorskip("numpy")

from vera.workers import node_compute_core as core
from vera.workers import worker_placement_core as wp
from vera.provisioning import components_core as cc

pytestmark = pytest.mark.critical

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── the wire format ───────────────────────────────────────────────────────────
def test_pack_round_trips_arrays_bytes_and_json():
    x = np.arange(12, dtype=np.float32).reshape(3, 4)
    obj = {"X": x, "state": b"\x00\x01blob", "n": 3, "cfg": {"lr": 0.1, "on": True},
           "weights": [[7, {"W": np.ones((2, 2)), "b": np.zeros(2)}], ["a", {"E": np.eye(2)}]],
           "rids": ["r1", "r2"]}
    back = core.unpack(core.pack(obj))
    assert back["X"].dtype == np.float32 and np.array_equal(back["X"], x)
    assert back["state"] == b"\x00\x01blob"
    assert back["n"] == 3 and back["cfg"] == {"lr": 0.1, "on": True}
    # a numeric node id survives because weights travel as pairs, not dict keys
    assert back["weights"][0][0] == 7 and np.array_equal(back["weights"][0][1]["W"], np.ones((2, 2)))
    assert back["rids"] == ["r1", "r2"]


def test_pack_never_carries_a_pickle():
    with pytest.raises(TypeError):
        core.pack({"x": np.array([object()], dtype=object)})


# ── where a job runs ──────────────────────────────────────────────────────────
def _w(host, libs, classes=("general", "nlp", "cpu_compute"), status="idle"):
    return {"role": "node-worker", "host": host, "status": status,
            "classes": json.dumps(list(classes)), "libs": json.dumps(libs)}


FULL = {"numpy": True, "torch": True, "sklearn": True, "scipy": True}


def test_decide_goes_to_the_nodes_when_every_compute_worker_can_run_it():
    ws = [_w("cpu-246", FULL), _w("cpu-247", FULL, status="running:compute.run")]
    assert core.decide("worldview.train", ws, host_fallback=False)[0] == "node"
    # a worker without the class is not counted, nor does it block
    ws.append(_w("ollama", {"numpy": True}, classes=("general",)))
    assert core.decide("worldview.train", ws, host_fallback=False)[0] == "node"
    # a worker on older code (no libs reported) never reads the compute stream
    old = {"role": "node-worker", "host": "cpu-247", "status": "idle",
           "classes": json.dumps(["cpu_compute"])}
    assert core.decide("worldview.train", ws + [old], host_fallback=False)[0] == "node"
    assert core.decide("worldview.train", [old], host_fallback=False)[0] == "refuse"


def test_decide_refuses_rather_than_running_on_the_host_by_default():
    ws = [_w("cpu-246", FULL), _w("cpu-247", {"numpy": True, "torch": False})]
    # a job is planned onto ONE node, so one capable node is enough
    assert core.decide("worldview.train", ws, host_fallback=False)[0] == "node"
    assert [w["host"] for w in core.capable("worldview.train", ws)] == ["cpu-246"]
    no_torch = [_w("cpu-247", {"numpy": True, "torch": False})]
    where, why = core.decide("worldview.train", no_torch, host_fallback=False)
    assert where == "refuse" and "torch" in why and "host fallback" in why
    # numpy alone is enough for the ML Lab trainer
    assert core.decide("ml.train", ws, host_fallback=False)[0] == "node"
    assert core.decide("worldview.train", [], host_fallback=False)[0] == "refuse"
    assert core.decide("worldview.train", [], host_fallback=True)[0] == "host"
    assert core.decide("os.system", ws, host_fallback=True)[0] == "refuse"
    # a dev sandbox has no node workers on its Redis
    assert core.decide("ml.train", ws, host_fallback=False, is_sandbox=True)[0] == "host"


def test_liveness():
    kw = dict(now=1000.0, dispatched_at=990.0, workers_alive=2)
    assert core.liveness(hb_seen_at=0, hb_alive=False, **kw)[0] == "wait"        # queued
    assert core.liveness(hb_seen_at=995, hb_alive=True, **kw)[0] == "wait"
    assert core.liveness(hb_seen_at=1000 - core.HEARTBEAT_LOST - 1, hb_alive=False, **kw)[0] == "lost"
    assert core.liveness(now=1000.0, dispatched_at=1000.0 - core.QUEUE_ORPHAN - 1,
                         workers_alive=0, hb_seen_at=0, hb_alive=False)[0] == "orphan"


def test_merge_config_defaults_to_nodes_only():
    cfg = core.merge_config({})
    assert cfg["host_fallback"] is False and cfg["threads"] == 4
    assert cfg["reserve_cpu"] == 2.0 and cfg["reserve_mem_gb"] == 8.0
    assert core.merge_config({"threads": 999, "host_fallback": 1})["threads"] == 32


# ── the stream only node workers read ─────────────────────────────────────────
def test_compute_run_rides_a_stream_the_host_does_not_read():
    assert wp.node_only_stream("cpu_compute") == core.NODE_COMPUTE_STREAM
    assert wp.class_of("compute.run", env={}) == "cpu_compute"
    node = wp.streams_to_read(is_worker=True, classes=["nlp", "cpu_compute"])
    assert core.NODE_COMPUTE_STREAM in node
    assert core.NODE_COMPUTE_STREAM not in wp.streams_to_read(is_worker=True, classes=["nlp"])
    assert core.NODE_COMPUTE_STREAM not in wp.streams_to_read(is_worker=False)
    ok, _ = wp.may_run_here("compute.run", is_worker=True, classes=["cpu_compute"])
    assert ok
    # the automatic offload never queues it on the class stream the host reads
    ok, why = wp.offload_decision("compute.run", stage=5, free={"cpu_compute": 3},
                                  is_worker=False, is_sandbox=False, args_ok=True)
    assert not ok and "node-only" in why


def test_every_function_is_a_module_level_def_in_its_module():
    files = {"worldview_jepa": "vera/worldview/worldview_jepa.py",
             "ml_training": "vera/machine learning/ml_training.py",
             "markets_lab_capabilities": "vera/markets/markets_lab_capabilities.py",
             "test_runner_core": "vera/evolve/test_runner_core.py",
             "ml_workshop": "vera/machine learning/ml_workshop.py",
             "ml_onnx": "vera/machine learning/ml_onnx.py"}
    for name, (base, attr, _libs) in core.FUNCTIONS.items():
        tree = ast.parse(open(os.path.join(ROOT, files[base]), encoding="utf-8").read())
        top = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        assert attr in top, f"{name}: {base}.{attr} must be a module-level function"


# ── pickles a node writes, read on the host ───────────────────────────────────
def test_numpy2_module_names_map_on_a_numpy1_host_only():
    assert core.compat_module_name("numpy._core.multiarray", 1) == "numpy.core.multiarray"
    assert core.compat_module_name("numpy._core", 1) == "numpy.core"
    assert core.compat_module_name("numpy._core.multiarray", 2) == "numpy._core.multiarray"
    assert core.compat_module_name("numpy._corex", 1) == "numpy._corex"
    import pickle
    blob = pickle.dumps({"a": np.arange(3)})
    assert np.array_equal(core.compat_pickle_module().loads(blob)["a"], np.arange(3))


def test_worker_training_libraries_are_pinned_to_the_hosts():
    pins = core.ml_pins({"torch": "2.7.1+cu126", "scikit-learn": "1.7.1", "numpy": "1.26.4"})
    assert pins == {"torch": "2.7.1", "scikit-learn": "1.7.1"}
    assert core.ml_pins({"torch": "nightly"}) == {}
    cmd = cc.worker_ml_pip_cmd("/v", pins)
    assert "torch==2.7.1" in cmd and "https://download.pytorch.org/whl/cpu" in cmd
    assert "scikit-learn==1.7.1" in cmd
    # the worker's metrics need psutil: without it no node publishes free
    # CPU/memory and node scheduling admits nothing (2026-10-04)
    assert "install -q psutil" in cmd
    # each group may fail without stopping the worker's setup (set -e)
    assert cmd.count("|| echo") == len(cc.WORKER_ML_PIP)
    full = cc.native_worker_cmd(root="/opt/vera/worker", repo="", redis_url="redis://h:6379",
                                backend_kv={}, bundle=True, ml_pins=pins)
    assert "scikit-learn==1.7.1" in full
    assert "torch" not in cc.native_worker_cmd(root="/r", repo="u", redis_url="redis://x:6379",
                                               backend_kv={}, ml_pip=False)


# ── the trainers' node entry points ───────────────────────────────────────────
_NO_ORCH = r'''
import builtins, runpy, sys
real = builtins.__import__
def guarded(name, *a, **k):
    if name.startswith("Vera.vera.capability_orchestration") or name.startswith("Vera.vera.config"):
        raise ImportError("no orchestrator in this test")
    return real(name, *a, **k)
builtins.__import__ = guarded
'''


def _run(script: str, timeout=180):
    r = subprocess.run([sys.executable, "-c", _NO_ORCH + script], cwd=ROOT, text=True,
                       capture_output=True, timeout=timeout,
                       env={**os.environ, "PYTHONPATH": ROOT})
    assert r.returncode == 0, (r.stdout + r.stderr)[-4000:]
    return r.stdout


def test_ml_lab_epochs_train_and_travel():
    out = _run(r'''
import numpy as np
from vera.workers import node_compute_core as core
ns = runpy.run_path("vera/machine learning/ml_training.py", run_name="ml_training")
module = {"nodes": [{"id": 1, "type": "input"},
                    {"id": 2, "type": "dense", "params": {"in_features": 3, "out_features": 1}}],
          "edges": [{"from": 1, "to": 2}]}
rng = np.random.default_rng(0)
X = rng.standard_normal((200, 3)).astype("float32")
y = (X @ np.array([[1.0], [-2.0], [0.5]])).astype("float32")
w = ns["_init_weights"](module)
inputs = core.unpack(core.pack({"module": module, "weights": [[k, v] for k, v in w.items()],
    "X_train": X[:160], "y_train": y[:160], "X_val": X[160:], "y_val": y[160:],
    "config": {"epochs": 25, "lr": 0.05, "batch_size": 32, "loss": "mse",
               "early_stopping_patience": 50}}))
seen = []
res = core.unpack(core.pack(ns["node_train_epochs"](inputs, seen.append, lambda: False)))
h = res["history"]["val_loss"]
assert res["status"] == "complete" and res["epoch"] == 25 and len(seen) == 25, res["status"]
assert h[-1] < h[0] * 0.5, h
assert res["weights"][0][0] == 2, res["weights"][0][0]       # int node id survives
stopped = ns["node_train_epochs"](inputs, lambda d: None, lambda: True)
assert stopped["status"] == "stopped" and stopped["epoch"] == 0
print("ok", round(h[0], 4), round(h[-1], 4))
''')
    assert out.strip().startswith("ok")


def test_markets_model_trains_from_arrays_alone():
    pytest.importorskip("sklearn")
    out = _run(r'''
import types, numpy as np, pickle
ma = types.ModuleType("markets_analysis_capabilities")
ma.__dict__.update(runpy.run_path("vera/markets/markets_analysis_capabilities.py",
                                  run_name="markets_analysis_capabilities"))
sys.modules["markets_analysis_capabilities"] = ma
ns = runpy.run_path("vera/markets/markets_lab_capabilities.py", run_name="markets_lab_capabilities")
n = 1200
rng = np.random.default_rng(1)
c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
arr = {"t": np.arange(n, dtype=np.int64) * 3600 + 1_700_000_000, "o": c, "h": c * 1.01,
       "l": c * 0.99, "c": c, "v": rng.uniform(1, 2, n)}
row = {"features": None, "hyperparams": {"n_estimators": 20}, "task": "classify",
       "model_kind": "rf", "horizon": 1}
folds = []
res = ns["node_train_model"]({"row": row, "arr": arr, "_threads": 2}, folds.append, lambda: False)
assert res["metrics"]["samples"] > 200 and folds and folds[0]["stage"] == "fold"
m = pickle.loads(res["blob"])
assert m["task"] == "classify" and hasattr(m["model"], "predict")
print("ok", res["metrics"]["cv_folds"])
''')
    assert out.strip().startswith("ok")


def test_worldview_stages_train_a_shipped_checkpoint():
    pytest.importorskip("torch")
    env = {"WORLDVIEW_EMBED_DIM": "16", "WORLDVIEW_LATENT_DIM": "8", "WORLDVIEW_HIDDEN_DIM": "16",
           "WORLDVIEW_NUM_CONCEPTS": "8", "WORLDVIEW_DYN_DIM": "16", "WORLDVIEW_DYN_HEADS": "2",
           "WORLDVIEW_DYN_LAYERS": "1", "WORLDVIEW_STREAM_AUTOSTART": "0"}
    script = r'''
import numpy as np
from vera.workers import node_compute_core as core
ns = runpy.run_path("vera/worldview/worldview_jepa.py", run_name="worldview_jepa")
WV = ns["WorldView"]
m = WV()
N = 24
rng = np.random.default_rng(2)
rids = [f"r{i}" for i in range(N)]
edges = [(rids[i], rids[(i + 1) % N], "RELATED_TO") for i in range(N)]
edges += [(r, r, "RELATED_TO") for r in rids]
idx = {r: i for i, r in enumerate(rids)}
graph = {"rids": rids, "idx_of": idx, "edges": edges,
         "records": {r: {"dataset_id": "d", "text": "t " + r, "created_at": ""} for r in rids},
         "X": rng.standard_normal((N, 16)).astype("float32"),
         "src": np.array([idx[e[0]] for e in edges]), "dst": np.array([idx[e[1]] for e in edges]),
         "edge_type": np.zeros(len(edges), dtype="int64")}
inputs = {"state": m.serialize_bytes(), "graph": ns["_graph_for_job"](graph),
          "dims": {"embed_dim": 16, "latent_dim": 8, "hidden_dim": 16, "num_concepts": 8},
          "arch": {k: ns[k] for k in ns["_ARCH_CONSTANTS"]}, "config": {"max_walks": 50},
          "epochs": {"gnn": 2, "codebook": 2, "dynamics": 1}}
inputs = core.unpack(core.pack(inputs))
events = []
out = core.unpack(core.pack(ns["node_train_stages"](inputs, events.append, lambda: False)))
m2 = WV()
assert m2.load_bytes(out["state"], pickle_module=core.compat_pickle_module())
assert m2.train_steps["gnn"] == 2 and m2.train_steps["codebook"] == 2, m2.train_steps
assert len(m2.record_concepts) == N
stages = [e["stage"] for e in events]
assert "gnn_epoch" in stages and "codebook_epoch" in stages and "dynamics_epoch" in stages, stages
# FM-8.2: the held-out evaluation ran on the node and travels back in the checkpoint
assert "dynamics_held_out" in stages, stages
assert m2.held_out.get("test_walks", 0) > 0 and "beats_baseline" in m2.held_out, m2.held_out
assert any(e.get("_hist") for e in events)
print("ok", out["walks"])
'''
    r = subprocess.run([sys.executable, "-c", "import runpy, sys\n" + script],
                       cwd=ROOT, text=True, capture_output=True, timeout=300,
                       env={**os.environ, **env, "PYTHONPATH": os.pathsep.join(
                           p for p in (ROOT, os.environ.get("PYTHONPATH", "")) if p)})
    if r.returncode != 0 and "No module named 'Vera'" in r.stderr:
        pytest.skip("worldview_jepa imports Vera.vera.* - not importable here")
    assert r.returncode == 0, (r.stdout + r.stderr)[-4000:]
    assert r.stdout.strip().startswith("ok")
