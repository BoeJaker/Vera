"""System 1 students trained by the ML Workshop (ml-workshop-compute MW-6,
four-minds FM-2.4; owner authorised 2026-10-11, "yes start" 2026-10-12).

Pins the guard rails (night window, the 90% disk rule - no busy-box wait), the
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
                     "state": {"step": "s", "step_goal": "g",
                               "result": f"r{i} " + ("finished" if i < n_pos else "unfinished")}})
    return rows


def test_guard_rails():
    assert core.guard(hour=2)[0]
    assert not core.guard(hour=14)[0] and "window" in core.guard(hour=14)[1]
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
    folds = core.stratified_folds([1.0] * 12 + [0.0] * 10)
    assert len(folds) == core.FOLDS and sorted(i for f in folds for i in f) == list(range(22))
    assert all(sum(1 for i in f if i >= 12) >= 2 for f in folds), "each fold holds both classes"
    th = core.thresholds(0.72)
    assert th["accuracy"] == (0.72, "maximize") and th["auc"] == (core.AUC_MIN, "maximize")
    assert core.thresholds(0.2)["accuracy"][0] == 0.8, "majority either way"
    mod = core.head_module("loop.step_done")
    assert mod["id"] == "student-loop-step-done" and mod["nodes"][0]["params"]["activation"] == "sigmoid"


def test_a_student_binding_says_what_it_answers(monkeypatch):
    from vera import model_binding_core as mb
    mo = _load("ml_onnx")
    t = {"student": True, "questions": ["loop.step_done"], "tier": 3, "auc": 0.81, "accuracy": 0.75,
         "task": "binary_classification"}
    uses = mo.model_uses("student-loop-step-done", t)
    assert "System 1 student for loop.step_done (shadow only)" in uses and "held-out AUC 0.81" in uses
    assert "5-fold CV AUC 0.81" in mo.model_uses("m", dict(t, eval="5-fold CV"))
    b = mb.binding("binary_classification", "onnx", uses=uses, io_in="vectors", io_out="labels",
                   questions=["loop.step_done"], tier=3)
    assert mb.validate(b) == []


@pytest.mark.parametrize("signal", [True, False], ids=["learns-is-bound", "chance-is-unbound"])
def test_the_whole_w2_path_with_faked_capabilities(tmp_path, monkeypatch, signal):
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
        # first feature says the answer (only the "unfinished" rows lack it)
        return {"embeddings": [[0.0 if "unfinished" in t else 1.0] + [0.01 * (i % 7)] * (core.DIM - 1)
                               for i, t in enumerate(texts)]}

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
        # ml_training reports early stopping as its own finished state (prod, 2026-10-12)
        return {"job_id": job_id, "module_id": trained_jobs[job_id], "status": "early_stopped"}

    async def ml_stop(job_id):
        return {"ok": True}

    async def ml_predict(module_id, X):
        rows = json.loads(X)
        calls.append(("predict", module_id))
        return {"ok": True, "predictions": [[(0.9 if r[0] > 0.5 else 0.1) if signal else 0.6] for r in rows]}

    async def ml_delete(id):
        calls.append(("delete", id))
        return {"ok": True}

    onnx_dir = tmp_path / "onnx"
    onnx_dir.mkdir()

    async def ml_export(module_id, register_cap=False):
        p = onnx_dir / f"{module_id}.onnx"
        p.write_bytes(b"onnx-" + module_id.encode())
        return {"ok": True, "module_id": module_id, "path": str(p), "opset": 17}

    async def hosts():
        return {"hosts": [{"id": "gpu-host", "host": "10.0.0.250"}, {"id": "cpu", "host": "10.0.0.246"}]}

    binds = []
    fake_onnx = types.SimpleNamespace(ONNX_DIR=str(onnx_dir), _safe=lambda s: s, trained_for=lambda mid: {},
                                      bind_student=lambda slug, mid, t, bound=True: binds.append((t, bound)))
    reg = {n: {"func": f} for n, f in (("decide.export", decide_export), ("nlp.embed", nlp_embed),
                                        ("ml.create", ml_create), ("ml.train", ml_train),
                                        ("ml.train.status", ml_status), ("ml.train.stop", ml_stop),
                                        ("ml.export.onnx", ml_export), ("exec.ssh.hosts.list", hosts),
                                        ("ml.train.predict", ml_predict), ("ml.delete", ml_delete))}

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
                "MLWorkshopTrainingRuntime": TA.MLWorkshopTrainingRuntime,
                "SQLiteModelPackageRegistry": PS.SQLiteModelPackageRegistry,
                "EvaluationReport": TC.EvaluationReport, "EvaluationRequest": TC.EvaluationRequest,
                "MetricResult": TC.MetricResult, "TrainingRequest": TC.TrainingRequest,
                "guard_out_of_tree": lambda p: p, "state_dir": lambda *p: (tmp_path / "state").joinpath(*p)}
    monkeypatch.setattr(st_mod, "_imports", imports)
    monkeypatch.setattr(st_mod, "_disk_pct", lambda: 50.0)
    monkeypatch.setattr(st_mod, "POLL_S", 0)

    dry = asyncio.run(st_mod.train_student("loop.step_done", dry_run=True))
    assert dry["ok"] and dry["dry_run"] and dry["features"]["rows"] == 60 and not any(c[0] == "train" for c in calls)
    assert dry["features"]["folds"] == core.FOLDS

    res = asyncio.run(st_mod.train_student("loop.step_done", dry_run=False, force=True))
    assert res["ok"], res
    assert res["state"] == ("trained" if signal else "trained_not_bound") and res["bound"] is signal
    assert res["package_id"] and res["report_id"].startswith("eval_")
    t, b = binds[-1]
    assert b is signal and t["questions"] == ["loop.step_done"] and t["tier"] == 3 and t["student"]
    assert t["package_id"] == res["package_id"] and t["eval"] == "5-fold CV"
    assert (res["metrics"]["auc"] == 1.0) if signal else (res["metrics"]["auc"] == 0.5)
    cv = [c[1] for c in calls if c[0] == "train" and "-cv" in c[1]]
    assert len(cv) == core.FOLDS and sorted(c[1] for c in calls if c[0] == "delete") == sorted(cv)
    pkg = PS.SQLiteModelPackageRegistry(tmp_path / "state" / "packages.sqlite3").get(res["package_id"])
    assert pkg is not None and pkg.format == "onnx"
    reports = list((tmp_path / "state" / "eval" / "students" / "student-loop-step-done").glob("eval_*.json"))
    body = json.loads(reports[0].read_text())
    assert len(reports) == 1 and body["report"]["status"] == "completed" and body["report"]["passed"] is signal
    assert body["report"]["provider_id"] == "vera.students.cv5" and body["report"]["evaluated_cases"] == 60
    assert ("train", "student-loop-step-done") in calls and ("event", "decide.student.trained") in calls
    # outside the window, not forced: skipped and recorded, nothing trained
    monkeypatch.setattr(st_mod.core, "in_window", lambda h, w=None: False)
    calls.clear()
    skip = asyncio.run(st_mod.train_student("loop.step_done", dry_run=False))
    assert skip.get("skipped") and not any(c[0] == "train" for c in calls)


def test_the_nightly_tick_retries_an_error_but_not_forever(monkeypatch):
    """17 Oct: a Redis drop restarted the node workers, the run found no node
    and failed, and the night was over. Errors now retry on later ticks, up to
    core.ATTEMPTS; a finished run or a not-trainable answer ends the night."""
    orch = types.ModuleType("Vera.vera.capability_orchestration")

    class R:
        def __init__(self): self.kv = {}
        async def get(self, k): return self.kv.get(k)
        async def set(self, k, v, ex=None): self.kv[k] = v
        async def incr(self, k):
            self.kv[k] = int(self.kv.get(k) or 0) + 1
            return self.kv[k]
        async def expire(self, k, s): pass

    orch.CAPABILITY_REGISTRY, orch.REDIS, orch.PG_POOL, orch.OLLAMA_INSTANCES = {}, R(), None, {}
    orch.capability = lambda *a, **k: (lambda f: f)
    orch.schedule = lambda *a, **k: None
    orch.emit_event = lambda e: asyncio.sleep(0)
    monkeypatch.setitem(sys.modules, "Vera.vera.capability_orchestration", orch)
    st = _load("student_training")
    monkeypatch.setattr(st, "_orch", orch)
    monkeypatch.setattr(st.core, "in_window", lambda h, w=None: True)
    results = [{"ok": False, "error": "no node had room"}] * 5
    seen = []

    async def train(question_id, dry_run):
        seen.append(question_id)
        return results[len(seen) - 1]
    monkeypatch.setattr(st, "cap_decide_student_train", train)
    monkeypatch.setattr(st, "_questions", lambda: ["loop.step_done"])
    for _ in range(5):
        asyncio.run(st._nightly())
    assert len(seen) == core.ATTEMPTS, "three errors, then the night is over"
    done = [k for k in orch.REDIS.kv if k.startswith("vera:students:done:")]
    assert len(done) == 1 and "error" in orch.REDIS.kv[done[0]]

    orch.REDIS.kv.clear(); seen.clear()
    results[:] = [{"ok": False, "error": "lost"}, {"ok": True, "state": "trained_not_bound"}, {"ok": True}]
    for _ in range(3):
        asyncio.run(st._nightly())
    assert len(seen) == 2, "an error, then a finished run ends the night"



def test_every_two_label_question_is_trained_and_the_box_is_never_waited_for(monkeypatch):
    """Owner 18 Oct: the box is never quiet - a pass does not skip for a census
    or a held GPU; and the candidate matrix's new two-label applications get
    students as soon as their gold allows. One question's error does not hold
    up the next."""
    assert core.guard(hour=2) == (True, "ok")
    assert "busy_reason" not in core.guard.__code__.co_varnames
    assert core.order_questions(["output.claim_supported", "loop.step_done", "intel.signal_relevant"]) == [
        "loop.step_done", "intel.signal_relevant", "output.claim_supported"]
    orch = types.ModuleType("Vera.vera.capability_orchestration")

    class R:
        def __init__(self): self.kv = {}
        async def get(self, k): return self.kv.get(k)
        async def set(self, k, v, ex=None): self.kv[k] = v
        async def incr(self, k):
            self.kv[k] = int(self.kv.get(k) or 0) + 1
            return self.kv[k]
        async def expire(self, k, s): pass

    orch.CAPABILITY_REGISTRY, orch.REDIS, orch.PG_POOL, orch.OLLAMA_INSTANCES = {}, R(), None, {}
    orch.capability = lambda *a, **k: (lambda f: f)
    orch.schedule = lambda *a, **k: None
    orch.emit_event = lambda e: asyncio.sleep(0)
    monkeypatch.setitem(sys.modules, "Vera.vera.capability_orchestration", orch)
    st = _load("student_training")
    monkeypatch.setattr(st, "_orch", orch)
    monkeypatch.setattr(st.core, "in_window", lambda h, w=None: True)
    monkeypatch.setattr(st, "_questions", lambda: ["loop.step_done", "intel.signal_relevant", "output.claim_supported"])
    answers = {"loop.step_done": {"ok": False, "error": "no node had room"},
               "intel.signal_relevant": {"ok": False, "not_trainable": True, "why": "0 gold examples (< 50)"},
               "output.claim_supported": {"ok": True, "state": "trained"}}
    seen = []

    async def train(question_id, dry_run):
        seen.append(question_id)
        return answers[question_id]
    monkeypatch.setattr(st, "cap_decide_student_train", train)
    out = asyncio.run(st.nightly_pass())
    assert seen == ["loop.step_done", "intel.signal_relevant", "output.claim_supported"], "an error holds nothing up"
    assert out["trained"] == ["intel.signal_relevant", "output.claim_supported"] and "loop.step_done" in out["error"]
    seen.clear()
    asyncio.run(st.nightly_pass())
    assert seen == ["loop.step_done"], "the rest are done tonight; the failed one is tried again"
