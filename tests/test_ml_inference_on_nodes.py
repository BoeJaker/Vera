"""ML Workshop inference on the node compute pool (ml-workshop-compute MW-1,
owner 2026-10-11): only training epochs ran on the nodes; predict, evaluate,
ml.run and every exported ONNX model ran in the host process. They are now
compute.run functions (ml.forward, ml.module_forward, onnx.run), with the host
only as the degraded fallback when no node has room within INFER_WAIT_S."""
import asyncio
import importlib.util
import os
import sys
import types

import pytest

from vera.provisioning import components_core as cc
from vera.workers import node_compute_core as nc

pytestmark = pytest.mark.critical

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ML = os.path.join(ROOT, "vera", "machine learning")


def _load(name):
    spec = importlib.util.spec_from_file_location("t_" + name, os.path.join(ML, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_inference_functions_are_registered_and_sized():
    for fn, mod in (("ml.forward", "ml_training"), ("ml.module_forward", "ml_workshop"),
                    ("onnx.run", "ml_onnx")):
        assert nc.FUNCTIONS[fn][0] == mod and fn in nc.NEEDS
    assert "onnxruntime" in nc.FUNCTIONS["onnx.run"][2] and "onnxruntime" in nc.PROBED_LIBS
    # a node without onnxruntime is never sent an ONNX job
    assert nc.node_missing("onnx.run", {"numpy": True}) == ["onnxruntime"]
    assert nc.ml_pins({"onnxruntime": "1.22.1"}) == {"onnxruntime": "1.22.1"}
    assert ("onnxruntime",) in cc.WORKER_ML_PIP
    assert "onnxruntime==1.22.1" in cc.worker_ml_pip_cmd("/v", {"onnxruntime": "1.22.1"})


def test_blob_refs_resolve_for_the_worker_and_the_host_fallback():
    store = {"k1": b"bytes"}

    async def get(k):
        return store.get(k)
    inp = {"_blob_refs": {"artifact": "k1"}, "X": [1]}
    assert asyncio.run(nc.resolve_blob_refs(inp, get)) is None
    assert inp == {"artifact": b"bytes", "X": [1]}
    assert "expired" in asyncio.run(nc.resolve_blob_refs({"_blob_refs": {"a": "gone"}}, get))
    src = open(os.path.join(ROOT, "vera", "workers", "node_compute_capabilities.py"), encoding="utf-8").read()
    assert src.count("core.resolve_blob_refs(") == 2          # worker path AND _run_here


def test_node_forward_matches_the_host_forward_pass():
    np = pytest.importorskip("numpy")
    mt = _load("ml_training")
    module = {"nodes": [{"id": "d", "type": "dense", "params": {"in_features": 3, "out_features": 2}}],
              "edges": []}
    w = mt._init_weights(module)
    X = np.random.default_rng(0).standard_normal((4, 3)).astype(np.float32)
    ref, _ = mt._forward_with_weights(module, w, X)
    out = mt.node_forward({"module": module, "weights": [[k, v] for k, v in w.items()], "X": X}, None, None)
    assert np.allclose(out["y"], ref)


def test_node_onnx_run_serves_an_artifact_from_its_bytes():
    np = pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper
    W = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
    g = helper.make_graph([helper.make_node("MatMul", ["X", "W"], ["Y"])], "t",
                          [helper.make_tensor_value_info("X", TensorProto.FLOAT, [None, 2])],
                          [helper.make_tensor_value_info("Y", TensorProto.FLOAT, [None, 2])],
                          [helper.make_tensor("W", TensorProto.FLOAT, [2, 2], W.ravel().tolist())])
    model = helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 10              # what onnxruntime 1.22 loads (ml_onnx pins the same)
    art = model.SerializeToString()
    m = _load("ml_onnx")
    out = m.node_onnx_run({"artifact": art, "X": [[1.0, 1.0]]}, None, None)
    assert np.allclose(out["y"], [[4.0, 6.0]])


def test_exported_models_and_predictions_go_through_the_pool(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    calls = []

    async def run(fn, inputs, **kw):
        calls.append((fn, inputs, kw))
        if fn == "onnx.run":
            return {"y": np.asarray(inputs["X"]) * 2, "provider": "CPU", "_node": "cpu-246"}
        return {"y": np.zeros((len(inputs["X"]), 1)), "_node": "cpu-247"}

    class R:
        def __init__(self): self.kv = {}
        async def set(self, k, v, ex=None, nx=False):
            if not (nx and k in self.kv): self.kv[k] = v
            return True
    orch = types.SimpleNamespace(NODE_COMPUTE=run, REDIS=R())
    monkeypatch.setitem(sys.modules, "Vera.vera.capability_orchestration", orch)

    m = _load("ml_onnx")
    art = tmp_path / "x.onnx"
    art.write_bytes(b"onnx-bytes")
    m.HAS_ORT, m.HAS_NP = True, True
    m._paths = lambda slug: (str(art), str(tmp_path / "x.json"))
    res = asyncio.run(m._run_artifact("x", "[[1, 2]]"))
    assert res["ok"] and res["ran_on"] == "cpu-246" and res["predictions"] == [[2.0, 4.0]]
    fn, inputs, kw = calls[-1]
    assert fn == "onnx.run" and kw["host_fallback"] is True and kw["max_wait_s"] == m.INFER_WAIT_S
    assert list(inputs["_blob_refs"]) == ["artifact"] and "artifact" not in inputs
    assert orch.REDIS.kv[inputs["_blob_refs"]["artifact"]] == b"onnx-bytes"     # stored once, by hash

    mt = _load("ml_training")
    y, where = asyncio.run(mt._forward_on_pool({"nodes": []}, {"d": {"W": np.ones(1)}}, np.ones((3, 2)), "t"))
    assert where == "cpu-247" and y.shape == (3, 1)
    fn, inputs, kw = calls[-1]
    assert fn == "ml.forward" and kw["host_fallback"] is True and inputs["weights"][0][0] == "d"
