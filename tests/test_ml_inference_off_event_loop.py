"""ML inference must not run on the event loop (ML Workshop on the node pool,
MW-0, 2026-10-11): ml.onnx.run / ml.onnx.model.<slug> ran onnxruntime inline
and ml.run its NumPy forward inline, so one large input stalled every request
on the 2-core host."""
import asyncio
import importlib.util
import os
import time

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ML = os.path.join(ROOT, "vera", "machine learning")


def _load(name):
    spec = importlib.util.spec_from_file_location("t_" + name, os.path.join(ML, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_onnx_inference_leaves_the_event_loop_free(tmp_path):
    np = pytest.importorskip("numpy")
    m = _load("ml_onnx")
    art = tmp_path / "x.onnx"
    art.write_bytes(b"onnx")
    m.HAS_ORT, m.HAS_NP = True, True
    m._paths = lambda slug: (str(art), str(tmp_path / "x.json"))

    def slow(slug, X):
        time.sleep(0.4)                        # a big batch
        return np.asarray(X) * 2, "CPUExecutionProvider"
    m._run_session_array = slow

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1
        t = asyncio.ensure_future(ticker())
        res = await m._run_artifact("x", "[[1, 2]]")
        t.cancel()
        return res, ticks
    res, ticks = asyncio.run(main())
    assert res["ok"] and res["predictions"] == [[2.0, 4.0]]
    assert ticks >= 10, f"the event loop stalled during inference ({ticks} ticks in 0.4 s)"


def test_the_forward_pass_and_export_run_in_the_executor():
    src = open(os.path.join(ML, "ml_workshop.py"), encoding="utf-8").read()
    assert "run_in_executor(None, _numpy_forward, m, inp)" in src
    onnx = open(os.path.join(ML, "ml_onnx.py"), encoding="utf-8").read()
    assert "run_in_executor(None, _build_and_save)" in onnx and "run_in_executor(None, _both)" in onnx
