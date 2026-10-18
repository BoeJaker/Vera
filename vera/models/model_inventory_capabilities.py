"""Read-only capability exposing Vera's portable model inventory."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any

from Vera.vera.capability_orchestration import capability
from Vera.vera.models.inference_deployment import SQLiteInferenceDeploymentRegistry
from Vera.vera.models.model_inventory import (
    project_model_inventory,
    project_persisted_inference_providers,
)
from Vera.vera.models.model_package_store import SQLiteModelPackageRegistry
from Vera.vera.models.nlp_inventory import project_nlp_inventory
from Vera.vera.state_paths import guard_out_of_tree, state_dir


def _state_path(env_name: str, filename: str) -> Path:
    configured = os.getenv(env_name, "").strip()
    path = Path(configured).expanduser() if configured else state_dir("models") / filename
    return guard_out_of_tree(path)


def _read_persisted_records() -> dict[str, Any]:
    package_path = _state_path("VERA_MODEL_PACKAGE_DB", "packages.sqlite3")
    deployment_path = _state_path(
        "VERA_INFERENCE_DEPLOYMENT_DB", "deployments.sqlite3")
    result: dict[str, Any] = {
        "packages": (), "aliases": (), "admissions": (), "activations": (),
        "legacy_bindings": (), "deployments": (), "observations": (),
        "sources": {},
    }
    if package_path.is_file():
        try:
            store = SQLiteModelPackageRegistry(package_path)
            result.update({
                "packages": store.list(),
                "aliases": store.aliases(),
                "admissions": store.admission_history(),
                "activations": store.activation_history(),
                "legacy_bindings": store.legacy_capability_bindings(),
            })
            result["sources"]["registry"] = {"status": "available"}
        except Exception as exc:
            result["sources"]["registry"] = {
                "status": "error", "reason": type(exc).__name__}
    else:
        result["sources"]["registry"] = {"status": "not_configured"}

    if deployment_path.is_file():
        try:
            store = SQLiteInferenceDeploymentRegistry(deployment_path)
            deployments = store.list()
            observations = tuple(
                observation for deployment in deployments
                if (observation := store.current(deployment.deployment_id)) is not None
            )
            result["deployments"] = deployments
            result["observations"] = observations
            result["providers"] = project_persisted_inference_providers(
                deployments, observations)
            result["sources"]["deployments"] = {"status": "available"}
        except Exception as exc:
            result["sources"]["deployments"] = {
                "status": "error", "reason": type(exc).__name__}
    else:
        result["sources"]["deployments"] = {"status": "not_configured"}
    return result


async def _read_nlp_inventory() -> tuple[dict[str, Any] | None, dict[str, str]]:
    dispatch = sys.modules.get("nlp_dispatch")
    if dispatch is None:
        return None, {"status": "unavailable", "reason": "dispatch_not_loaded"}
    try:
        nodes = await dispatch.discover()
        return project_nlp_inventory(nodes), {"status": "available"}
    except Exception as exc:  # source outage is inventory data, not a 500
        return None, {
            "status": "unavailable",
            "reason": type(exc).__name__,
        }


@capability(
    "model.inventory",
    contract={"presentation": {"kind": "composite", "parts": [{"kind": "kv", "map": {"fields": "counts"}, "title": "Counts"}, {"kind": "table", "form": "table", "map": {"rows": "candidates"}, "draw": {"columns": ["task", "model", "format", "status", "blockers"]}, "title": "Unresolved candidates", "empty": "No unresolved candidates"}]}},
    http_method="GET",
    http_path="/models/inventory",
    http_tags=["models", "inventory"],
    memory="off",
    silent=True,
    description=(
        "Return a deterministic read-only inventory of portable ModelPackages, "
        "aliases, admission receipts, deployments, provider evidence, and "
        "verified or unresolved source-owned candidates. This capability does "
        "not download, hash, load, execute, activate, or route a model."
    ),
)
async def model_inventory(trace_id=None) -> dict[str, Any]:
    records = _read_persisted_records()
    nlp_inventory, nlp_status = await _read_nlp_inventory()
    sources = dict(records.pop("sources"))
    sources["nlp"] = nlp_status
    external = (("nlp", nlp_inventory),) if nlp_inventory is not None else ()
    return project_model_inventory(
        **records,
        external_inventories=external,
        source_status=sources,
    )


# ── callable models: the capability registry's model bindings, joined ──────────
#: {model: json show_facts + at} - an LLM's size, quantisation, context and
#: abilities from Ollama /api/show (cheap: reads metadata, loads nothing).
LLM_FACTS_KEY = "vera:models:llm_facts"
_FACTS_PER_CALL = 12          # a first call stays quick; the rest fill in on later calls


async def _llm_facts(instances: dict, refresh: bool = False) -> tuple[dict, dict]:
    """Cached /api/show facts per LLM, fetching the missing ones (bounded)."""
    import asyncio
    import json
    import time

    import httpx

    import Vera.vera.capability_orchestration as _orch
    from Vera.vera import model_binding_core as _mb

    r = getattr(_orch, "REDIS", None)
    cached: dict = {}
    if r is not None:
        try:
            for k, v in (await r.hgetall(LLM_FACTS_KEY)).items():
                k = k.decode() if isinstance(k, bytes) else k
                cached[k] = json.loads(v)
        except Exception:
            cached = {}
    where: dict = {}
    for inst in (instances or {}).values():
        if not isinstance(inst, dict) or not inst.get("url") or inst.get("enabled") is False \
                or inst.get("status") in ("offline", "error", "down"):
            continue
        for m in _mb._names(inst.get("models")):
            where.setdefault(m, inst["url"])
    todo = [m for m in where if refresh or m not in cached]
    batch = todo[:_FACTS_PER_CALL]
    sem = asyncio.Semaphore(4)

    async def one(m):
        async with sem:
            try:
                async with httpx.AsyncClient(timeout=8) as c:
                    resp = await c.post(f"{where[m]}/api/show", json={"model": m})
                if resp.status_code == 200:
                    return m, _mb.show_facts(resp.json())
            except Exception:
                pass
            return m, None
    got = {m: f for m, f in await asyncio.gather(*(one(m) for m in batch)) if f}
    if got and r is not None:
        try:
            await r.hset(LLM_FACTS_KEY, mapping={m: json.dumps({**f, "at": time.time()})
                                                 for m, f in got.items()})
        except Exception:
            pass
    cached.update(got)
    return cached, {"cached": len(cached), "fetched": len(got),
                    "still_to_fetch": max(0, len(todo) - len(batch))}


@capability(
    "model.callable",
    http_method="GET",
    http_path="/models/callable",
    http_tags=["models", "inventory"],
    memory="off",
    silent=True,
    description=(
        "Every model Vera can call, and what each is FOR - pick a model for a task "
        "here. Specialist models (NLP, NER, GLiNER, spaCy, zero-shot, QA, embeddings, "
        "reranker, decision models) come from the capabilities that serve them "
        "(@capability serves=); LLMs come from the routing system (call llm.generate "
        "with model=<name>; purposes are the operator's model tags). Each row: id, cap "
        "and selector (the call that reaches it), task, family, uses, limits, io, "
        "labels, questions (decision ids), tier, placement, model, loaded_on / "
        "present_on (nodes), installable (specialist catalog id); LLM rows add facts "
        "(size, quantisation, context, abilities) and measured tok/s per node, from "
        "which their uses/limits are written. Inputs: task, family, question (str "
        "filters), search (str - in id, uses, model), include_llms (bool=true), "
        "refresh_facts (bool - re-read every LLM's facts), measure (bool=true - add "
        "`measured`: calls, p50_s / p95_s, error_rate and nodes over the last 24 h "
        "from node activity). Output: {ok, count, models:[...], sources, errors}. "
        "To find the right model for a task described in words, use model.pick."
    ),
)
async def model_callable(task: str = "", family: str = "", question: str = "",
                         search: str = "", include_llms: bool = True,
                         refresh_facts: bool = False, measure: bool = True,
                         trace_id: str | None = None) -> dict[str, Any]:
    import Vera.vera.capability_orchestration as _orch
    from Vera.vera import model_binding_core as _mb
    from Vera.vera.catalog.specialist_catalog import CURATED
    from Vera.vera.research.nlp_dispatch_core import DEFAULT_MODELS

    nodes, sources = [], {}
    # cap modules load under bare names in prod; the package name elsewhere
    dispatch = (sys.modules.get("nlp_dispatch")
                or sys.modules.get("Vera.vera.research.nlp_dispatch"))
    if dispatch is None:
        sources["nlp_nodes"] = "unavailable: dispatch not loaded"
    else:
        try:
            nodes = await dispatch.discover()
            sources["nlp_nodes"] = f"{len(nodes)} node(s)"
        except Exception as exc:          # an outage is data, not a 500
            sources["nlp_nodes"] = f"unavailable: {type(exc).__name__}"
    rows = _mb.callable_view(_orch.CAPABILITY_REGISTRY, defaults=DEFAULT_MODELS,
                             nodes=nodes, catalog=CURATED,
                             task=task, family=family, question=question)
    if include_llms and not (family and family != "ollama") and not question \
            and (not task or task in ("generate", "embed")):
        try:
            await _orch._model_tags_hydrate()
        except Exception:
            pass
        instances = getattr(_orch, "OLLAMA_INSTANCES", {}) or {}
        facts, sources["llm_facts"] = await _llm_facts(instances, refresh=bool(refresh_facts))
        llms = _mb.llm_rows(instances, getattr(_orch, "MODEL_TAGS", {}) or {}, facts=facts,
                            stats=list((getattr(_orch, "_ROUTE_STATS", {}) or {}).values()))
        rows += [r for r in llms if not task or r["task"] == task]
    if search:
        q = search.lower()
        rows = [r for r in rows if q in " ".join(
            str(r.get(k) or "") for k in ("id", "uses", "model", "task")).lower()]
    if measure and rows:
        recs = []
        na = (sys.modules.get("node_activity_capabilities")
              or sys.modules.get("Vera.vera.workers.node_activity_capabilities"))
        try:
            recs = await na._records(1500) if na is not None else []
            sources["measured"] = f"{len(recs)} recent node calls"
        except Exception as exc:
            sources["measured"] = f"unavailable: {type(exc).__name__}"
        _mb.measure(rows, recs)
    return {"ok": True, "count": len(rows), "models": rows, "sources": sources,
            "errors": list(getattr(_orch, "MODEL_BINDING_ERRORS", []) or [])}


#: uses text -> MiniLM vector (the rows' texts change only with a release)
_PICK_VECS: dict[str, list] = {}


@capability(
    "model.pick",
    http_method="POST",
    http_path="/models/pick",
    http_tags=["models", "inventory"],
    memory="off",
    description=(
        "Which model fits a task? Describe the task in words and get the best few "
        "callable models - specialist models (NER, GLiNER, spaCy, zero-shot, QA, "
        "sentiment, language id, embeddings, reranker, speech, images, vision ...) "
        "and LLMs - ranked by how well their stated uses match it (meaning, via "
        "nlp.embed, plus shared words), minus what their limits rule out, preferring "
        "models loaded now. Each result says how to call it (cap + selector) and why "
        "it ranked. Inputs: need (str! - e.g. 'find dates and money in an English "
        "article'), top_k (int=5), include_llms (bool=true), family / task (str "
        "filters). Output: {ok, need, picks:[{id, cap, selector, uses, limits, "
        "score, why, loaded_on, measured}], ranked_by}."
    ),
)
async def model_pick(need: str = "", top_k: int = 5, include_llms: bool = True,
                     family: str = "", task: str = "",
                     trace_id: str | None = None) -> dict[str, Any]:
    import Vera.vera.capability_orchestration as _orch
    from Vera.vera import model_binding_core as _mb

    if not str(need or "").strip():
        return {"ok": False, "error": "need is required: describe the task in words"}
    view = await model_callable(task=task, family=family, include_llms=include_llms)
    rows = view.get("models") or []
    need_vec, row_vecs, ranked_by = None, {}, "words"
    embed = (_orch.CAPABILITY_REGISTRY.get("nlp.embed") or {}).get("func")
    if embed is not None and rows:
        texts = {r["id"]: f"{r.get('uses', '')} {r.get('task', '')}" for r in rows}
        todo = [t for t in dict.fromkeys(texts.values()) if t not in _PICK_VECS]
        try:
            res = await embed(texts=[need] + todo)
            vecs = (res or {}).get("embeddings") or []
            if len(vecs) == 1 + len(todo):
                need_vec = vecs[0]
                for t, v in zip(todo, vecs[1:]):
                    _PICK_VECS[t] = v
                row_vecs = {i: _PICK_VECS[t] for i, t in texts.items() if t in _PICK_VECS}
                ranked_by = "meaning (nlp.embed) + words"
        except Exception:
            need_vec = None
    picks = _mb.pick(rows, need, top_k=top_k, need_vec=need_vec, row_vecs=row_vecs)
    keep = ("id", "cap", "selector", "task", "family", "model", "uses", "limits", "io",
            "placement", "loaded_on", "measured", "score", "why")
    return {"ok": True, "need": need, "ranked_by": ranked_by,
            "picks": [{k: p.get(k) for k in keep} for p in picks]}
