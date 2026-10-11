"""The ML Workshop on the node pool in full (ml-workshop-compute MW-2..5, owner
2026-10-11): prepared data stored once and fetched by reference, parallel
trials (sweeps, k-fold) spread over the nodes, a job's threads equal to the
cores it was admitted for, and an exported model that says what it was
trained for."""
import asyncio
import importlib.util
import os
import sys
import types

import pytest

from vera import model_binding_core as mb
from vera.workers import node_compute_core as nc

pytestmark = pytest.mark.critical
np = pytest.importorskip("numpy")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ML = os.path.join(ROOT, "vera", "machine learning")
MODULE = {"id": "m1", "name": "demo", "nodes": [{"id": "d", "type": "dense",
                                                 "params": {"in_features": 3, "out_features": 1}}], "edges": []}


def _load(name):
    spec = importlib.util.spec_from_file_location("t_" + name, os.path.join(ML, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeRedis:
    def __init__(self): self.kv = {}
    async def set(self, k, v, ex=None, nx=False):
        if not (nx and k in self.kv): self.kv[k] = v
        return True
    async def get(self, k): return self.kv.get(k)


def _data(n=40):
    rng = np.random.default_rng(1)
    X = rng.standard_normal((n, 3)).astype(np.float32)
    y = (X @ np.array([[1.0], [-2.0], [0.5]], dtype=np.float32))
    return {"X_train": X[:30], "y_train": y[:30], "X_val": X[30:], "y_val": y[30:]}


def test_folds_are_disjoint_and_cover_the_data():
    mt = _load("ml_training")
    d = _data()
    seen = []
    for f in range(4):
        Xtr, ytr, Xva, yva = mt.fold_split(d["X_train"], d["y_train"], d["X_val"], d["y_val"], f, 4)
        assert len(Xtr) + len(Xva) == 40 and len(Xva) == 10
        seen.extend(map(tuple, Xva.tolist()))
    assert len(set(seen)) == 40


def test_a_node_trains_from_data_by_reference_and_one_fold():
    mt = _load("ml_training")
    w0 = mt._init_weights(MODULE)
    blob = nc.pack({k: v for k, v in _data().items()})
    out = mt.node_train_epochs({"module": MODULE, "weights": [[k, v] for k, v in w0.items()],
                                "data": blob, "fold": 1, "folds": 4,
                                "config": {"epochs": 2, "batch_size": 8}}, lambda d: None, lambda: False)
    assert out["epoch"] >= 1 and out.get("best_val_loss") is not None


def test_sweep_configs_and_ranking():
    mt = _load("ml_training")
    cs = mt.sweep_configs({"epochs": 5}, {"lr": [0.1, 0.01], "batch_size": [16, 32, 64]})
    assert len(cs) == 6 and all(c["epochs"] == 5 for c in cs)
    assert len(mt.sweep_configs({}, {"lr": [1, 2, 3, 4]}, n_random=2)) == 2
    assert mt.sweep_configs({}, {}) == [{}]
    r = mt.rank_trials([{"config": {"lr": 1}, "best_val_loss": 0.4}, {"config": {"lr": 1}, "best_val_loss": 0.2},
                        {"config": {"lr": 2}, "best_val_loss": 0.1}, {"config": {"lr": 3}, "error": "x"}])
    assert [x["config"]["lr"] for x in r] == [2, 1] and r[1]["mean_val_loss"] == 0.3 and r[1]["folds_done"] == 2


def test_a_sweep_runs_its_trials_side_by_side_on_the_pool(monkeypatch):
    mt = _load("ml_training")
    redis = FakeRedis()
    live, peak, calls = [0], [0], []

    async def run(fn, inputs, **kw):
        calls.append((fn, inputs, kw))
        live[0] += 1
        peak[0] = max(peak[0], live[0])
        await asyncio.sleep(0.05)
        live[0] -= 1
        assert "data" in inputs["_blob_refs"] and "X_train" not in inputs     # by reference
        return {"best_val_loss": inputs["config"]["lr"] + 0.01 * inputs.get("fold", 0), "epoch": 3,
                "status": "complete", "_node": f"n{len(calls) % 3}"}
    monkeypatch.setitem(sys.modules, "Vera.vera.capability_orchestration",
                        types.SimpleNamespace(NODE_COMPUTE=run, REDIS=redis))
    monkeypatch.setitem(sys.modules, "ml_workshop", types.SimpleNamespace(_MODULES={"m1": MODULE}))
    started = []

    async def fake_loop(job_id, job):
        started.append(job)
    monkeypatch.setattr(mt, "_train_loop", fake_loop)

    async def main():
        ref = await mt._store_data(_data())
        assert (await mt._load_data(ref))["X_train"].shape == (30, 3)
        sw = {"sweep_id": "sw-1", "module_id": "m1", "data_ref": ref, "dataset_id": "ds", "task": "regression",
              "configs": mt.sweep_configs({"epochs": 3}, {"lr": [0.3, 0.1, 0.2]}), "folds": 2, "status": "running",
              "trials": [], "started": "", "apply_best": True, "max_parallel": 4}
        await mt._run_sweep(sw)
        await asyncio.sleep(0)
        return sw
    sw = asyncio.run(main())
    assert len(calls) == 6 and all(c[0] == "ml.train" for c in calls)
    assert peak[0] > 1, "trials ran one at a time"
    v = mt._sweep_view(sw)
    assert v["done"] == 6 and v["best"]["config"]["lr"] == 0.1 and sw["status"] == "done"
    assert started and started[0]["config"]["lr"] == 0.1 and started[0]["data_ref"] == sw["data_ref"]


def test_threads_follow_what_a_job_was_admitted_for():
    assert nc.threads_for("ml.train", 4) == 1
    assert nc.threads_for("worldview.train", 4) == 4
    assert nc.threads_for("no.such.fn", 3) == 3


def test_an_exported_model_says_what_it_was_trained_for(monkeypatch):
    mo = _load("ml_onnx")
    monkeypatch.setitem(sys.modules, "ml_training", types.SimpleNamespace(_TRAINING_JOBS={
        "j1": {"module_id": "m1", "status": "complete", "dataset_id": "btc_ohlcv", "task": "ohlcv_direction",
               "n_features": 7, "best_val_loss": 0.41234, "config": {"loss": "bce"}}}))
    monkeypatch.setitem(sys.modules, "ml_workshop", types.SimpleNamespace(_MODULES={"m1": {"name": "Direction net"}}))
    t = mo.trained_for("m1")
    assert t == {"name": "Direction net", "dataset_id": "btc_ohlcv", "task": "ohlcv_direction",
                 "n_features": 7, "best_val_loss": 0.41234, "loss": "bce"}
    u = mo.model_uses("m1", t)
    assert "'Direction net' trained for ohlcv_direction on dataset btc_ohlcv" in u and "7 input features" in u
    b = mb.binding(t["task"], "onnx", uses=u, io_in="vectors", io_out="scores")
    assert mb.validate(b) == []
    assert "Predictions from the ML Workshop model 'm9'" in mo.model_uses("m9", {})
