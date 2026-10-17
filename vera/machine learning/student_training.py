"""decide.student.* - System 1 student heads trained by the ML Workshop.

ml-workshop-compute MW-6 / four-minds FM-2.4 (owner, 2026-10-11: authorised;
2026-10-12: "yes start"). One training path, on the W2 contracts:

  decide.export (W2-03 DatasetSnapshot) -> MiniLM-384 state vectors
  -> MLWorkshopCapabilityBridge / MLWorkshopTrainingRuntime (W2-07)
     -> ml.train (a node job, never gpu-250)
  -> MLWorkshopONNXPublisher -> ONNX ModelPackage (SQLiteModelPackageRegistry)
  -> W5-03 EvaluationReport on the held-out split (stored under state_dir)
  -> the model's ml.onnx.model.<slug> binding: questions=[q], tier=3.

SHADOW ONLY: nothing here answers a live decision; four-minds decides any
promotion (FM-2.5). Guard rails (MONITOR-STUDENT-TRAINING.md): 01:00-06:00,
not while a census goal or agent loop runs, one run per question per night,
>= 50 gold with >= 10 per class, nothing written above 90% data disk.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability, emit_event, schedule

log = logging.getLogger("vera.ml.students")

_spec = importlib.util.spec_from_file_location(
    "student_training_core", os.path.join(os.path.dirname(os.path.abspath(__file__)), "student_training_core.py"))
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
sys.modules.setdefault("student_training_core", core)

RUNS_KEY = "vera:students:runs"            # newest first, the last 200 runs
DONE_KEY = "vera:students:done:{q}:{day}"  # one run per question per night
TRIES_KEY = "vera:students:tries:{q}:{day}"  # errors so far tonight (core.ATTEMPTS ends the night)
ENABLED_KEY = "vera:students:enabled"      # "0" pauses the nightly tick
POLL_S = 5
TRAIN_TIMEOUT_S = 1800


def _imports():
    from Vera.vera.minds import judge_core as JC
    from Vera.vera.models.ml_workshop_runtime_bridge import (
        MLWorkshopCapabilityBridge, MLWorkshopONNXOutputBinding, MLWorkshopONNXPublisher)
    from Vera.vera.models.ml_workshop_training_adapter import MLWorkshopTrainingBinding, MLWorkshopTrainingRuntime
    from Vera.vera.models.model_package_store import SQLiteModelPackageRegistry
    from Vera.vera.models.training_contracts import (
        EvaluationReport, EvaluationRequest, MetricResult, TrainingRequest)
    from Vera.vera.state_paths import guard_out_of_tree, state_dir
    return locals()


def _cap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    if not c:
        raise RuntimeError(f"{name} is not loaded")
    return c["func"]


def _mod(*names):
    for n in names:
        m = sys.modules.get(n)
        if m is not None:
            return m
    return None


async def _busy_reason() -> str:
    ev = _mod("evolve_capabilities", "Vera.vera.evolve.evolve_capabilities")
    try:
        return str(await ev._box_busy_reason()) if ev is not None else ""
    except Exception as e:
        return f"busy check failed: {type(e).__name__}"


def _disk_pct() -> Optional[float]:
    """The fullest of the data disk (Postgres / Docker) and the state dir."""
    I = _imports()
    worst = None
    for p in ("/var/lib/docker", str(I["state_dir"]())):
        try:
            if os.path.exists(p):
                u = shutil.disk_usage(p)
                pct = 100.0 * u.used / u.total
                worst = pct if worst is None else max(worst, pct)
        except Exception:
            pass
    return worst


async def _gpu_avoid() -> List[str]:
    """Host ids and names of the GPU node's workers: student training never
    runs there (MONITOR-STUDENT-TRAINING.md)."""
    from urllib.parse import urlparse
    ips = {urlparse(str(i.get("url") or "")).hostname for i in (getattr(_orch, "OLLAMA_INSTANCES", {}) or {}).values()
           if isinstance(i, dict) and i.get("has_gpu")}
    out: List[str] = []
    try:
        hosts = ((await _cap("exec.ssh.hosts.list")()) or {}).get("hosts", [])
        out += [h["id"] for h in hosts if h.get("host") in ips and h.get("id")]
    except Exception as e:
        log.debug("students: ssh hosts: %s", e)
    return out


async def _export(q: str) -> Dict[str, Any]:
    r = await _cap("decide.export")(question_id=q, days=90, min_strength="medium")
    if not (r or {}).get("ok"):
        raise RuntimeError(f"decide.export failed: {(r or {}).get('error')}")
    path = Path(r["path"])
    f = path / "examples.jsonl" if path.is_dir() else path
    rows = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [x for x in rows if (x.get("question") or {}).get("id") == q]
    return {"snapshot_id": r["snapshot_id"], "rows": rows}


async def _vectors(q: str, rows: List[Dict[str, Any]]) -> Dict[str, List[float]]:
    """MiniLM-384 per decision: the kNN judge's cached decision_vectors first,
    nlp.embed (on the NLP nodes) for the rest. Nothing is written back."""
    I = _imports()
    JC = I["JC"]
    ids = [str(x["id"]) for x in rows]
    vecs: Dict[str, List[float]] = {}
    pool = getattr(_orch, "PG_POOL", None)
    if pool is not None and ids:
        try:
            async with pool.acquire() as conn:
                recs = await conn.fetch("SELECT decision_id, vec FROM decision_vectors "
                                        "WHERE decision_id = ANY($1::text[]) AND dim = $2", ids, core.DIM)
            for rr in recs:
                v = rr["vec"]
                v = json.loads(v) if isinstance(v, str) else v
                if isinstance(v, list) and len(v) == core.DIM:
                    vecs[str(rr["decision_id"])] = [float(x) for x in v]
        except Exception as e:
            log.info("students: decision_vectors unavailable (%s) - embedding all", e)
    todo = [(str(x["id"]), (JC.user_message(q, x.get("state") or {}) or "")[:2000]) for x in rows
            if str(x["id"]) not in vecs]
    todo = [(i, t) for i, t in todo if t]
    embed = _cap("nlp.embed")
    for k in range(0, len(todo), 64):
        part = todo[k:k + 64]
        res = await embed(texts=[t for _, t in part])
        embs = (res or {}).get("embeddings") or []
        if len(embs) != len(part):
            raise RuntimeError(f"nlp.embed returned {len(embs)} of {len(part)} vectors: {(res or {}).get('error')}")
        for (i, _), v in zip(part, embs):
            vecs[i] = [float(x) for x in v]
    return vecs


def _package_db():
    I = _imports()
    p = os.getenv("VERA_MODEL_PACKAGE_DB", "").strip()
    path = Path(p).expanduser() if p else I["state_dir"]("models") / "packages.sqlite3"
    return I["guard_out_of_tree"](path)


async def _record(rec: Dict[str, Any]) -> None:
    r = getattr(_orch, "REDIS", None)
    if r is None:
        return
    try:
        await r.lpush(RUNS_KEY, json.dumps(rec, default=str))
        await r.ltrim(RUNS_KEY, 0, 199)
    except Exception:
        pass


async def _cross_validate(q: str, mod: Dict[str, Any], X: List[List[float]], y: List[List[float]],
                          folds: List[List[int]], avoid: List[str]) -> Tuple[List[float], List[str]]:
    """Out-of-fold p(positive) for every row: one temporary head per fold,
    trained with ml.train (on a node) and scored with ml.train.predict, then
    deleted. Sequential - five small jobs, kind to a shared box."""
    oof: List[float] = [0.0] * len(X)
    ran_on: List[str] = []
    mt = _mod("ml_training", "Vera.vera.machine learning.ml_training")
    # the same job-status reading as the W2-07 runtime (early_stopped is a success)
    native = _imports()["MLWorkshopTrainingRuntime"]._NATIVE_STATUS
    for k, held in enumerate(folds):
        fid = f"{core.module_id(q)}-cv{k}"
        hs = set(held)
        Xk = [X[i] for i in range(len(X)) if i not in hs]
        yk = [y[i] for i in range(len(y)) if i not in hs]
        Xa, ya, Xv, yv = core.split_val(Xk, yk)
        created = await _cap("ml.create")(name=f"{mod['name']} cv{k}", nodes=json.dumps(mod["nodes"]),
                                          edges="[]", description=f"CV fold {k} (temporary)",
                                          tags="student,cv", module_id=fid)
        if (created or {}).get("error"):
            raise RuntimeError(f"ml.create {fid}: {created['error']}")
        try:
            if mt is not None:
                getattr(mt, "_WEIGHTS", {}).pop(fid, None)
            cfg = {"epochs": core.EPOCHS, "lr": core.LR, "batch_size": 16, "loss": "bce",
                   "avoid_nodes": avoid}
            job = await _cap("ml.train")(module_id=fid, X_train=json.dumps(Xa), y_train=json.dumps(ya),
                                         X_val=json.dumps(Xv), y_val=json.dumps(yv), config=json.dumps(cfg),
                                         dataset_id="vera.decide", task=f"student {q} cv{k}")
            if not (job or {}).get("job_id"):
                raise RuntimeError(f"ml.train {fid}: {(job or {}).get('error') or job}")
            t0 = time.time()
            while True:
                st = await _cap("ml.train.status")(job_id=job["job_id"]) or {}
                raw = str(st.get("status") or "").strip().lower()
                s = native.get(raw)
                if s == "succeeded":
                    ran_on.append(str(st.get("trained_on") or ""))
                    break
                if s not in ("queued", "running"):       # failed, cancelled or unknown: stop now
                    raise RuntimeError(f"CV fold {k}: {st.get('error') or raw or 'no status'}")
                if time.time() - t0 > TRAIN_TIMEOUT_S:
                    await _cap("ml.train.stop")(job_id=job["job_id"])
                    raise RuntimeError(f"CV fold {k} did not finish within the timeout")
                await asyncio.sleep(POLL_S)
            pr = await _cap("ml.train.predict")(module_id=fid, X=json.dumps([X[i] for i in held]))
            if (pr or {}).get("error"):
                raise RuntimeError(f"ml.train.predict {fid}: {pr['error']}")
            for i, v in zip(held, pr["predictions"]):
                oof[i] = float(v[0] if isinstance(v, list) else v)
        finally:
            try:
                await _cap("ml.delete")(id=fid)
            except Exception as e:
                log.info("students: deleting %s: %s", fid, e)
            if mt is not None:
                getattr(mt, "_WEIGHTS", {}).pop(fid, None)
    return oof, ran_on


async def train_student(question_id: str, *, dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    """One student training run for one question (see the module doc)."""
    I = _imports()
    JC = I["JC"]
    q = question_id
    rec: Dict[str, Any] = {"question": q, "started": datetime.utcnow().isoformat() + "Z", "dry_run": dry_run}
    if q not in JC.QUESTIONS:
        return {"ok": False, "error": f"unknown question {q!r}"}
    positive = JC.positive_label(q)
    if positive is None:
        return {"ok": False, "error": f"{q} is not a binary question (a logistic head needs two labels)"}
    ok, why = core.guard(hour=datetime.now().hour, busy_reason=await _busy_reason(),
                         disk_pct=_disk_pct(), force=force)
    rec["guard"] = why
    if not ok and not dry_run:
        rec.update(state="skipped", why=why)
        await _record(rec)
        return {"ok": False, "skipped": True, "why": why}
    ex = await _export(q)
    rows = ex["rows"]
    tr_ok, tr_why, counts = core.trainable(rows, positive)
    rec.update(snapshot_id=ex["snapshot_id"], counts=counts)
    if not tr_ok:
        rec.update(state="not_trainable", why=tr_why)
        if not dry_run:
            await _record(rec)
        return {"ok": False, "not_trainable": True, "why": tr_why, "counts": counts}
    vecs = await _vectors(q, rows)
    xy = core.build_xy(rows, vecs, positive)
    # every gold row: evaluated by stratified k-fold CV, then the final head is
    # trained on all of them (the export's test split is too small to score on)
    Xall = xy["train"][0] + xy["test"][0]
    yall = xy["train"][1] + xy["test"][1]
    folds = core.stratified_folds([v[0] for v in yall])
    rec.update(features={"rows": len(Xall), "skipped": xy["skipped"], "folds": len(folds),
                         "fold_sizes": [len(f) for f in folds]})
    if dry_run:
        return {"ok": True, "dry_run": True, "guard": why, "would_train": ok, **rec}

    mid = core.module_id(q)
    mod = core.head_module(q)
    created = await _cap("ml.create")(name=mod["name"], nodes=json.dumps(mod["nodes"]), edges="[]",
                                      description=mod["description"], tags="student,four-minds",
                                      module_id=mid)
    if (created or {}).get("error"):
        raise RuntimeError(f"ml.create: {created['error']}")
    mt = _mod("ml_training", "Vera.vera.machine learning.ml_training")
    if mt is not None:
        getattr(mt, "_WEIGHTS", {}).pop(mid, None)       # every night from fresh weights
    avoid = await _gpu_avoid()
    oof, cv_on = await _cross_validate(q, mod, Xall, yall, folds, avoid)
    Xa, ya, Xv, yv = core.split_val(Xall, yall)

    async def resolve(revision):
        return {"dataset_revision_id": revision, "X_train": Xa, "y_train": ya, "X_val": Xv, "y_val": yv}

    async def train_call(**a):
        cfg = json.loads(a.get("config") or "{}")
        cfg.update({"loss": "bce", "avoid_nodes": avoid})
        return await _cap("ml.train")(module_id=a["module_id"], X_train=a["X_train"], y_train=a["y_train"],
                                      X_val=a["X_val"], y_val=a["y_val"], config=json.dumps(cfg),
                                      dataset_id="vera.decide", task=f"student {q}")

    async def status_call(**a):
        return await _cap("ml.train.status")(job_id=a["job_id"])

    async def stop_call(**a):
        return await _cap("ml.train.stop")(job_id=a["job_id"])

    async def export_call(**a):
        return await _cap("ml.export.onnx")(module_id=a["module_id"], register_cap=False)

    mo = _mod("ml_onnx", "Vera.vera.machine learning.ml_onnx")
    store = I["state_dir"]("models", "students", mid)
    store.mkdir(parents=True, exist_ok=True)
    registry = I["SQLiteModelPackageRegistry"](_package_db())
    publisher = I["MLWorkshopONNXPublisher"](
        export_call=export_call, artifact_store=store,
        allowed_export_roots=(Path(getattr(mo, "ONNX_DIR", "")),),
        outputs=(I["MLWorkshopONNXOutputBinding"](
            module_id=mid, name=mid.replace("-", "_"), architecture="logistic_head",
            task="binary_classification", input_contract="minilm384.v1",
            output_contract="probability.v1"),),
        registry=registry)
    bridge = I["MLWorkshopCapabilityBridge"](dataset_resolver=resolve, train_call=train_call,
                                             status_call=status_call, stop_call=stop_call,
                                             publisher=publisher)
    adapter = bridge.runtime(runtime_id="vera.students", bindings=(I["MLWorkshopTrainingBinding"](
        base_model_package_id=f"mpkg_{mid.replace('-', '_')}_base", module_id=mid,
        objectives=("binary_classification",)),))
    request = I["TrainingRequest"](dataset_revision_id=ex["snapshot_id"], objective="binary_classification",
                                   base_model_package_id=f"mpkg_{mid.replace('-', '_')}_base",
                                   hyperparameters=(("epochs", core.EPOCHS), ("learning_rate", core.LR),
                                                    ("batch_size", 16)))
    t0 = time.time()
    run = await adapter.submit(request)
    while run.status not in ("succeeded", "failed", "cancelled"):
        if time.time() - t0 > TRAIN_TIMEOUT_S:
            await adapter.cancel(run.training_run_id)
            raise RuntimeError("training did not finish within the timeout")
        await asyncio.sleep(POLL_S)
        run = await adapter.observe(run.training_run_id)
    rec.update(training_run_id=run.training_run_id, train_status=run.status,
               train_seconds=round(time.time() - t0, 1))
    if run.status != "succeeded":
        rec.update(state="failed", why=run.error_code or run.status)
        await _record(rec)
        return {"ok": False, **rec}
    pkg_id = run.output_model_package_id
    rec["package_id"] = pkg_id

    # the report: out-of-fold predictions from the k CV heads (same recipe as
    # the packaged head, which saw every row)
    slug = mo._safe(mid)
    y = [v[0] for v in yall]
    m = core.metrics(oof, y)
    th = core.thresholds(sum(y) / len(y) if y else 0.5)
    report = I["EvaluationReport"](
        request=I["EvaluationRequest"](subject_kind="model_package", subject_id=pkg_id,
                                       dataset_revision_id=ex["snapshot_id"], metric_ids=core.METRICS),
        provider_id=f"vera.students.cv{len(folds)}", status="completed",
        metrics=tuple(I["MetricResult"](k, m[k], th[k][0], th[k][1]) for k in core.METRICS),
        evaluated_cases=len(y))
    rdir = I["state_dir"]("eval", "students", mid)
    rdir.mkdir(parents=True, exist_ok=True)
    body = {"report": report.to_dict(), "question": q, "package_id": pkg_id, "snapshot_id": ex["snapshot_id"],
            "training_run_id": run.training_run_id, "counts": counts, "cv_trained_on": cv_on,
            "thresholds": {k: v[0] for k, v in th.items()},
            "created": datetime.utcnow().isoformat() + "Z"}
    (rdir / f"{report.evaluation_report_id}.json").write_text(json.dumps(body, indent=1, default=str))
    rec.update(report_id=report.evaluation_report_id, metrics=m, passed=report.passed)

    # bind only a student whose report passes: questions + tier 3, so
    # model.callable question=<q> lists it (shadow only). A failing one is
    # unbound (and stays unbound across restarts) - its package and report stay.
    trained = dict(mo.trained_for(mid)) if hasattr(mo, "trained_for") else {}
    trained.update({"task": "binary_classification", "questions": [q], "tier": 3,
                    "dataset_id": ex["snapshot_id"], "student": True, "package_id": pkg_id,
                    "auc": m["auc"], "accuracy": m["accuracy"], "eval": f"{len(folds)}-fold CV",
                    "report_id": report.evaluation_report_id})
    mo.bind_student(slug, mid, trained, bound=report.passed)
    rec.update(cap=f"ml.onnx.model.{slug}" if report.passed else "", bound=report.passed)

    # keep the last KEEP artifacts per question
    old = core.prune([(str(f), f.stat().st_mtime) for f in store.glob("*.onnx")])
    for f in old:
        try:
            os.remove(f)
        except OSError:
            pass
    rec.update(state="trained" if report.passed else "trained_not_bound", pruned=len(old),
               finished=datetime.utcnow().isoformat() + "Z")
    await _record(rec)
    await emit_event({"type": "decide.student.trained", "question": q, "package_id": pkg_id,
                      "metrics": m, "report_id": report.evaluation_report_id, "bound": report.passed})
    return {"ok": True, **rec}


@capability(
    "decide.student.train",
    http_method="POST", http_path="/decide/student/train", http_tags=["minds", "ml"],
    memory="off",
    description=(
        "Train a System 1 student head for one decision question with the ML Workshop "
        "(four-minds FM-2.4 / MW-6): decide.export gold -> MiniLM-384 state vectors -> a "
        "logistic head trained on a CPU node (never the GPU node) -> ONNX ModelPackage -> an "
        "EvaluationReport from stratified 5-fold cross-validation (accuracy must match the "
        "majority-label rate, AUC >= 0.7) -> bound with questions + tier 3 ONLY if it passes "
        "(a failing student is unbound; package and report stay). SHADOW ONLY. "
        "Runs only 01:00-06:00 and not while a census or loop runs, unless force=true (the "
        "disk rule always applies). Inputs: question_id (str='loop.step_done'), dry_run "
        "(bool=true: check the guard, data and features, train nothing), force (bool). Output: "
        "{ok, state, counts, metrics, package_id, report_id, cap} or {skipped / not_trainable, why}."
    ),
)
async def cap_decide_student_train(question_id: str = "loop.step_done", dry_run: bool = True,
                                   force: bool = False, trace_id=None) -> Dict[str, Any]:
    try:
        return await train_student(question_id, dry_run=dry_run, force=force)
    except Exception as e:
        log.warning("decide.student.train %s: %s", question_id, e)
        await _record({"question": question_id, "state": "error", "error": str(e)[:300],
                       "at": datetime.utcnow().isoformat() + "Z", "dry_run": dry_run})
        return {"ok": False, "error": str(e)[:300]}


@capability(
    "decide.student.status",
    http_method="GET", http_path="/decide/student/status", http_tags=["minds", "ml"],
    memory="off", silent=True,
    description="Student training: the nightly questions, the guard right now, the last runs "
                "(state, counts, metrics, package, report) and whether the nightly tick is on. "
                "Inputs: limit (int=20). Output: {enabled, questions, guard, runs}.",
)
async def cap_decide_student_status(limit: int = 20, trace_id=None) -> Dict[str, Any]:
    r = getattr(_orch, "REDIS", None)
    runs, enabled = [], True
    if r is not None:
        try:
            runs = [json.loads(x) for x in await r.lrange(RUNS_KEY, 0, max(1, int(limit)) - 1)]
            enabled = (await r.get(ENABLED_KEY)) not in (b"0", "0")
        except Exception:
            pass
    ok, why = core.guard(hour=datetime.now().hour, busy_reason=await _busy_reason(), disk_pct=_disk_pct())
    return {"enabled": enabled, "questions": list(core.QUESTIONS), "window": core.WINDOW,
            "guard": {"ok": ok, "why": why, "disk_pct": _disk_pct()}, "runs": runs}


@capability(
    "decide.student.enable",
    http_method="POST", http_path="/decide/student/enable", http_tags=["minds", "ml"],
    memory="off",
    description="Turn the nightly student training on or off. Inputs: enabled (bool). Output: {ok, enabled}.",
)
async def cap_decide_student_enable(enabled: bool = True, trace_id=None) -> Dict[str, Any]:
    r = getattr(_orch, "REDIS", None)
    if r is None:
        return {"ok": False, "error": "no Redis"}
    await r.set(ENABLED_KEY, "1" if enabled else "0")
    return {"ok": True, "enabled": bool(enabled)}


async def nightly_pass() -> Dict[str, Any]:
    """One pass of the nightly training: in the window, train each question once per night when the
    box is not busy; a busy box retries on a later pass, and so does an error (no node with room, a
    lost job - e.g. workers restarting after a Redis drop, 17 Oct) up to core.ATTEMPTS times a night."""
    if not core.in_window(datetime.now().hour):
        return {"ok": True, "skipped": True, "why": "outside the %s window" % (core.WINDOW,)}
    r = getattr(_orch, "REDIS", None)
    if r is None or (await r.get(ENABLED_KEY)) in (b"0", "0"):
        return {"ok": True, "skipped": True, "why": "student training is switched off"}
    day = datetime.now().strftime("%Y-%m-%d")
    done, trained = [], []
    for q in core.QUESTIONS:
        key = DONE_KEY.format(q=q, day=day)
        if await r.get(key):
            done.append(q)
            continue
        res = await cap_decide_student_train(question_id=q, dry_run=False)
        if res.get("skipped"):
            return {"ok": True, "skipped": True, "why": res.get("why") or "busy", "trained": trained}
        if res.get("error"):
            tries = int(await r.incr(TRIES_KEY.format(q=q, day=day)) or 0)
            await r.expire(TRIES_KEY.format(q=q, day=day), 86400)
            if tries < core.ATTEMPTS:
                log.info("students: %s failed (%d of %d tonight), retrying next tick: %s",
                         q, tries, core.ATTEMPTS, res["error"])
                return {"ok": False, "error": "%s: %s (try %d of %d)" % (q, res["error"], tries, core.ATTEMPTS),
                        "trained": trained}
        await r.set(key, json.dumps({"state": res.get("state") or ("error" if res.get("error") else "done")}),
                    ex=86400)
        trained.append(q)
    return {"ok": True, "trained": trained, "already": done,
            "summary": "trained %d, %d already done tonight" % (len(trained), len(done))}


@capability(
    "decide.student.nightly",
    http_method="POST", http_path="/decide/student/nightly", http_tags=["minds", "ml"],
    memory="off",
    description="One pass of the nightly student training (looplab-control A5: a Loop Lab cap schedule runs "
                "this; the built-in 10-minute timer stands down while one is enabled). Trains each question "
                "once per night, inside its own window and guard. Output: {ok, trained, already, summary} or "
                "{skipped, why}.",
)
async def cap_decide_student_nightly(trace_id=None) -> Dict[str, Any]:
    return await nightly_pass()


async def _nightly():
    """The built-in timer (every 10 min). Stands down while a Loop Lab schedule runs the pass."""
    cov = CAPABILITY_REGISTRY.get("evolve.schedule.covers") if "CAPABILITY_REGISTRY" in globals() else None
    if cov is None:
        cov = getattr(_orch, "CAPABILITY_REGISTRY", {}).get("evolve.schedule.covers")
    if cov and cov.get("func"):
        try:
            if (await cov["func"](name="decide.student.nightly")).get("covered"):
                return
        except Exception:
            pass
    await nightly_pass()


schedule(_nightly, interval=600, name="decide.student.nightly", skip_in_sandbox=True, singleton=True)
