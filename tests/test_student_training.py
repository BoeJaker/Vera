"""System 1 students trained by the ML Workshop (ml-workshop-compute MW-6,
four-minds FM-2.4; owner authorised 2026-10-11, "yes start" 2026-10-12).

Pins the guard rails (night window, busy box, the 90% disk rule), the
trainability rules, the features / metrics, and the whole W2 path end to end:
decide.export -> state vectors -> MLWorkshopCapabilityBridge / Runtime ->
ml.train -> ONNX ModelPackage -> EvaluationReport -> a binding with
questions + tier 3 - against faked Vera capabilities but the real W2-07 code."""
import asyncio
import importlib.util
import json
import os
import sys
import types

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ML = os.path.join(ROOT, "vera", "machine learning")


def _load(name):
    spec = importlib.util.spec_from_file_location("t_" + name, os.path.join(ML, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


core = _load("student_training_core")


def _rows(n_pos=40, n_neg=20, n_test=8):
    rows = []
    for i in range(n_pos + n_neg):
        rows.append({"id": f"d{i}", "question": {"id": "loop.step_done"}, "gold": "end" if i < n_pos else "continue",
                     "split": "test" if i % ((n_pos + n_neg) // n_test) == 0 else "train",
                     "state": {"step": "s", "step_goal": "g", "result": f"r{i}"}})
    return rows


def test_guard_rails():
    assert core.guard(hour=2)[0]
    assert not core.guard(hour=14)[0] and "window" in core.guard(hour=14)[1]
    assert not core.guard(hour=2, busy_reason="census goal running")[0]
    ok, why = core.guard(hour=2, disk_pct=93.0)
    assert not ok and "93%" in why
    assert core.guard(hour=14, force=True)[0]
    assert not core.guard(hour=14, force=True, disk_pct=95)[0], "force never skips the disk rule"


def test_trainability():
    assert core.trainable(_rows(), "end")[0]
    ok, why, _ = core.trainable(_rows(n_pos=50, n_neg=5), "end")
    assert not ok and "(< 10)" in why
    ok, why, c = core.trainable(_rows(n_pos=20, n_neg=15), "end")
    assert not ok and c["gold"] == 35
    one_class = [dict(r, gold="not_met") for r in _rows()]
    assert not core.trainable(one_class, "met")[0]


def test_features_metrics_and_pruning():
    rows = _rows()
    vecs = {r["id"]: [0.1] * core.DIM for r in rows[:-3]}
    xy = core.build_xy(rows, vecs, "end")
    assert xy["skipped"] == 3 and len(xy["train"][0]) + len(xy["test"][0]) == len(rows) - 3
    assert xy["train"][1][0] == [1.0]
    m = core.metrics([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0])
    assert m["accuracy"] == 1.0 and m["auc"] == 1.0 and m["coverage_at_0_8"] == 1.0
    assert core.metrics([0.6, 0.6], [1, 0])["auc"] == 0.5
    assert core.prune([("a", 1), ("b", 3), ("c", 2), ("d", 4)], keep=2) == ["c", "a"]
    mod = core.head_module("loop.step_done")
    assert mod["id"] == "student-loop-step-done" and mod["nodes"][0]["params"]["activation"] == "sigmoid"


def test_a_student_binding_says_what_it_answers(monkeypatch):
    from vera import model_binding_core as mb
    mo = _load("ml_onnx")
    t = {"student": True, "questions": ["loop.step_done"], "tier": 3, "auc": 0.81, "accuracy": 0.75,
         "task": "binary_classification"}
    uses = mo.model_uses("student-loop-step-done", t)
    assert "System 1 student for loop.step_done (shadow only)" in uses and "AUC 0.81" in uses
    b = mb.binding("binary_classification", "onnx", uses=uses, io_in="vectors", io_out="labels",
                   questions=["loop.step_done"], tier=3)
    assert mb.validate(b) == []


def test_the_whole_w2_path_with_faked_capabilities(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    st_mod = None
    calls = []
    trained_jobs = {}

    async def decide_export(**a):
        snap = tmp_path / "snap_x"
        snap.mkdir(exist_ok=True)
        (snap / "examples.jsonl").write_text("\n".join(json.dumps(r) for r in _rows()))
        return {"ok": True, "snapshot_id": "snap_" + "a" * 64, "path": str(snap)}

    async def nlp_embed(texts):
        return {"embeddings": [[0.01 * (i % 7)] * core.DIM for i, _ in enumerate(texts)]}

    async def ml_create(**a):
        calls.append(("create", a["module_id"]))
        return {"ok": True, "id": a["module_id"]}

    async def ml_train(**a):
        cfg = json.loads(a["config"])
        assert cfg["loss"] == "bce" and cfg["avoid_nodes"] == ["gpu-host"] and cfg["epochs"] == core.EPOCHS
        trained_jobs["j1"] = a["module_id"]
        calls.append(("train", a["module_id"]))
        return {"ok": True, "job_id": "j1", "module_id": a["module_id"]}

    async def ml_status(job_id):
        return {"job_id": job_id, "module_id": trained_jobs[job_id], "status": "complete"}

    async def ml_stop(job_id):
        return {"ok": True}

    onnx_dir = tmp_path / "onnx"
    onnx_dir.mkdir()

    async def ml_export(module_id, register_cap=False):
        p = onnx_dir / f"{module_id}.onnx"
        p.write_bytes(b"onnx-" + module_id.encode())
        return {"ok": True, "module_id": module_id, "path": str(p), "opset": 17}

    async def hosts():
        return {"hosts": [{"id": "gpu-host", "host": "10.0.0.250"}, {"id": "cpu", "host": "10.0.0.246"}]}

    async def run_artifact(slug, X):
        n = len(json.loads(X))
        return {"ok": True, "predictions": [[0.9 if i % 2 else 0.2] for i in range(n)], "ran_on": "cpu-246"}

    bound = {}
    fake_onnx = types.SimpleNamespace(ONNX_DIR=str(onnx_dir), _safe=lambda s: s, _run_artifact=run_artifact,
                                      trained_for=lambda mid: {}, bind_student=lambda slug, mid, t: bound.update(t))
    reg = {n: {"func": f} for n, f in (("decide.export", decide_export), ("nlp.embed", nlp_embed),
                                        ("ml.create", ml_create), ("ml.train", ml_train),
                                        ("ml.train.status", ml_status), ("ml.train.stop", ml_stop),
                                        ("ml.export.onnx", ml_export), ("exec.ssh.hosts.list", hosts))}

    class R:
        def __init__(self): self.kv, self.lists = {}, {}
        async def lpush(self, k, v): self.lists.setdefault(k, []).insert(0, v)
        async def ltrim(self, *a): pass
        async def get(self, k): return self.kv.get(k)
        async def set(self, k, v, ex=None): self.kv[k] = v

    orch = types.ModuleType("Vera.vera.capability_orchestration")
    orch.CAPABILITY_REGISTRY = reg
    orch.REDIS = R()
    orch.PG_POOL = None
    orch.OLLAMA_INSTANCES = {"gpu": {"url": "http://10.0.0.250:11434", "has_gpu": True}}
    orch.capability = lambda *a, **k: (lambda f: f)
    orch.schedule = lambda *a, **k: None

    async def emit(e): calls.append(("event", e.get("type")))
    orch.emit_event = emit
    monkeypatch.setitem(sys.modules, "Vera.vera.capability_orchestration", orch)
    monkeypatch.setitem(sys.modules, "ml_onnx", fake_onnx)
    monkeypatch.setitem(sys.modules, "ml_training", types.SimpleNamespace(_WEIGHTS={}))
    monkeypatch.setitem(sys.modules, "evolve_capabilities",
                        types.SimpleNamespace(_box_busy_reason=lambda: asyncio.sleep(0, result="")))
    monkeypatch.setenv("VERA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VERA_MODEL_PACKAGE_DB", str(tmp_path / "state" / "packages.sqlite3"))
    st_mod = _load("student_training")
    monkeypatch.setattr(st_mod, "_orch", orch)    # bound at import; point it at the fakes
    # W2-07 and the contracts are imported from the worktree (vera.*), not main
    import vera.minds.judge_core as JC
    import vera.models.ml_workshop_runtime_bridge as BR
    import vera.models.ml_workshop_training_adapter as TA
    import vera.models.model_package_store as PS
    import vera.models.training_contracts as TC
    import vera.state_paths as SP

    def imports():
        return {"JC": JC, "MLWorkshopCapabilityBridge": BR.MLWorkshopCapabilityBridge,
                "MLWorkshopONNXOutputBinding": BR.MLWorkshopONNXOutputBinding,
                "MLWorkshopONNXPublisher": BR.MLWorkshopONNXPublisher,
                "MLWorkshopTrainingBinding": TA.MLWorkshopTrainingBinding,
                "SQLiteModelPackageRegistry": PS.SQLiteModelPackageRegistry,
                "EvaluationReport": TC.EvaluationReport, "EvaluationRequest": TC.EvaluationRequest,
                "MetricResult": TC.MetricResult, "TrainingRequest": TC.TrainingRequest,
                "guard_out_of_tree": lambda p: p, "state_dir": lambda *p: (tmp_path / "state").joinpath(*p)}
    monkeypatch.setattr(st_mod, "_imports", imports)
    monkeypatch.setattr(st_mod, "_disk_pct", lambda: 50.0)
    monkeypatch.setattr(st_mod, "POLL_S", 0)

    dry = asyncio.run(st_mod.train_student("loop.step_done", dry_run=True))
    assert dry["ok"] and dry["dry_run"] and dry["features"]["train"] > 0 and not any(c[0] == "train" for c in calls)

    res = asyncio.run(st_mod.train_student("loop.step_done", dry_run=False, force=True))
    assert res["ok"], res
    assert res["state"] == "trained" and res["package_id"] and res["report_id"].startswith("eval_")
    assert bound["questions"] == ["loop.step_done"] and bound["tier"] == 3 and bound["student"]
    pkg = PS.SQLiteModelPackageRegistry(tmp_path / "state" / "packages.sqlite3").get(res["package_id"])
    assert pkg is not None and pkg.format == "onnx"
    reports = list((tmp_path / "state" / "eval" / "students" / "student-loop-step-done").glob("eval_*.json"))
    assert len(reports) == 1 and json.loads(reports[0].read_text())["report"]["status"] == "completed"
    assert ("train", "student-loop-step-done") in calls and ("event", "decide.student.trained") in calls
    # outside the window, not forced: skipped and recorded, nothing trained
    monkeypatch.setattr(st_mod.core, "in_window", lambda h, w=None: False)
    calls.clear()
    skip = asyncio.run(st_mod.train_student("loop.step_done", dry_run=False))
    assert skip.get("skipped") and not any(c[0] == "train" for c in calls)
