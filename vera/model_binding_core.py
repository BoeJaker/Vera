"""Model bindings: which model(s) a capability runs.

A model Vera can USE is a model reachable through a capability - the capability
already gives the call its schema, policy, tracing, MCP exposure, node placement
and ledger attribution. What it lacked is a declaration of the model behind it
(owner, 2026-10-10: "a registry of callable NLP, NER, spaCy, GLiNER specialist
models, decision models ... perhaps the capability is the best place").

So `@capability(..., serves=binding(...))` - or `serves=[...]` for a capability
that serves several models picked by an argument (nlp.ner's `task`) - and the
"registry" is a VIEW: the registry entries that carry bindings, joined with what
the nodes report loaded and what the specialist catalog can install
(`model.callable`). There is no second store to keep in sync.

Each binding says what the model is FOR (`uses`, required) and where it falls
short (`limits`), so an LLM can pick the right one for a task (owner, same day);
the decorator appends them to the capability's description, which is what the
MCP tool list, caps.search and caps.shortlist already show a model.

The decorator keyword is `serves`, not `model`: `model` is already the LLM
routing system's argument (per-cap routing rules pin `model`; llm.generate and
vision.describe take `model=`). The view lists those routable LLMs too, from
what the routing system already knows, so every model shows in one place.

Pure (no Vera imports): worktree-testable.
Plan: shared-planning/four-minds/PROPOSAL-model-bindings-2026-10-10.md.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: How the model is run. A family names the loader, not the vendor.
FAMILIES = frozenset({
    "nlp",           # an ONNX/optimum export served by the node NLP server
    "fastembed",     # fastembed (the reranker)
    "gliner",        # a GLiNER torch checkpoint
    "spacy",         # a spaCy pipeline package
    "onnx",          # a raw ONNX artifact (ml.onnx.*)
    "ollama", "llama-server", "vllm",   # LLM servers
    "sklearn",       # a tabular student
    "knn",           # nearest neighbours over stored decisions
    "rules",         # patterns / rules (System 1 tier 1)
})

#: What goes in and what comes out, coarsely - enough for a planner to know
#: which models fit a step.
IO_KINDS = frozenset({
    "text", "texts", "pair", "query+texts", "text+labels", "question+context",
    "state",                                   # a decision state
    "spans", "label", "labels", "scores", "vector", "vectors", "answer",
})

#: Where the model runs (worker_placement_core classes + host / any).
PLACEMENTS = frozenset({"nlp", "cpu_compute", "gpu", "media", "host", "any"})

_ID_PART = re.compile(r"[^A-Za-z0-9._-]+")


def binding(task: str, family: str, *, uses: str = "", limits: str = "",
            model: str = "", selector: Optional[Mapping[str, Any]] = None,
            io_in: str = "text", io_out: str = "labels", labels: Sequence[str] = (),
            questions: Sequence[str] = (), tier: int = 0, placement: str = "any",
            package_id: str = "", served_as: str = "", note: str = "") -> Dict[str, Any]:
    """One model behind a capability, as a plain (JSON-safe) dict.

    task       - what it does (the NLP server's task key for nlp.*: ner, gliner, ...)
    uses       - REQUIRED: what it is for / when to pick it, in a sentence an LLM can act on
    limits     - when NOT to pick it (language, length, label set, latency ...)
    family     - how it is run (FAMILIES)
    model      - the model id; "" when it is resolved at view time (nlp.*: DEFAULT_MODELS,
                 which the node may override - the view reports what the node loaded)
    selector   - the call arguments that pick THIS model on a multi-model capability
    labels     - a classifier's / decision question's label set
    questions  - decision question ids (judge_core.QUESTIONS) this model can answer
    tier       - System 1 cascade tier: 1 rules, 2 kNN, 3 encoder, 4 small LLM, 5 big LLM
    placement  - PLACEMENTS
    package_id - a ModelPackage id when the model is packaged
    served_as  - the key a server reports it under (default: selector['task'] or task)
    """
    return {"task": str(task or ""), "family": str(family or ""), "model": str(model or ""),
            "uses": " ".join(str(uses or "").split()), "limits": " ".join(str(limits or "").split()),
            "selector": dict(selector or {}), "io": {"in": str(io_in), "out": str(io_out)},
            "labels": [str(x) for x in labels or ()], "questions": [str(x) for x in questions or ()],
            "tier": int(tier or 0), "placement": str(placement or "any"),
            "package_id": str(package_id or ""),
            "served_as": str(served_as or (selector or {}).get("task") or task or ""),
            "note": str(note or "")}


def validate(b: Mapping[str, Any]) -> List[str]:
    """Problems with one binding ([] = fine)."""
    errs = []
    if not isinstance(b, Mapping):
        return ["a binding must be a dict (use model_binding_core.binding(...))"]
    if not str(b.get("task") or "").strip():
        errs.append("task is required")
    if len(str(b.get("uses") or "").strip()) < 12:
        errs.append("uses is required: say what the model is for, so an LLM can pick it")
    if b.get("family") not in FAMILIES:
        errs.append(f"family {b.get('family')!r} is not one of {sorted(FAMILIES)}")
    io = b.get("io") or {}
    for side in ("in", "out"):
        if io.get(side) not in IO_KINDS:
            errs.append(f"io.{side} {io.get(side)!r} is not one of {sorted(IO_KINDS)}")
    if b.get("placement") not in PLACEMENTS:
        errs.append(f"placement {b.get('placement')!r} is not one of {sorted(PLACEMENTS)}")
    if not isinstance(b.get("selector") or {}, Mapping):
        errs.append("selector must be a dict of call arguments")
    t = b.get("tier") or 0
    if not isinstance(t, int) or not 0 <= t <= 5:
        errs.append("tier must be 0 (none) or 1-5")
    return errs


def binding_id(cap: str, b: Mapping[str, Any]) -> str:
    """`<cap>` for a single-model capability, `<cap>:<selector values>` otherwise.
    Stable, so it can be the ledger's implementation id."""
    sel = b.get("selector") or {}
    if not sel:
        return cap
    key = "-".join(_ID_PART.sub("_", str(sel[k])) for k in sorted(sel))
    return f"{cap}:{key}"


def bind(cap: str, serves: Any = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The decorator's `serves=` (one binding or a list) as validated bindings
    with ids, and the problems found. An invalid binding is dropped (and
    reported) rather than failing the module's import."""
    if serves is None:
        raw = []
    elif isinstance(serves, Mapping):
        raw = [serves]
    else:
        raw = list(serves)
    out, errs, seen = [], [], set()
    if len(raw) > 1:
        for b in raw:
            if isinstance(b, Mapping) and not (b.get("selector") or {}):
                errs.append(f"{cap}: several models need a selector each (which call arguments pick it)")
                return [], errs
    for b in raw:
        problems = validate(b)
        if problems:
            errs.extend(f"{cap}: {p}" for p in problems)
            continue
        row = dict(b)
        row["id"] = binding_id(cap, row)
        row["cap"] = cap
        if row["id"] in seen:
            errs.append(f"{cap}: two bindings share the id {row['id']}")
            continue
        seen.add(row["id"])
        out.append(row)
    return out, errs


def describe(bindings: Sequence[Mapping[str, Any]]) -> str:
    """The models section appended to a capability's description: what each
    model is for, and which call arguments pick it."""
    if not bindings:
        return ""
    lines = []
    for b in bindings:
        sel = b.get("selector") or {}
        pick = ", ".join(f"{k}={v!r}" for k, v in sorted(sel.items()))
        head = f"{pick}: " if pick else ""
        line = f"- {head}{b.get('uses', '')}"
        if b.get("limits"):
            line += f" Not for: {b['limits']}"
        lines.append(line)
    return "Models:\n" + "\n".join(lines)


def with_models(description: str, bindings: Sequence[Mapping[str, Any]]) -> str:
    """A capability's description with its models section (once)."""
    d = str(description or "").rstrip()
    sec = describe(bindings)
    if not sec or "\nModels:\n" in "\n" + d:
        return d
    return (d + "\n" + sec) if d else sec


# ── the view ─────────────────────────────────────────────────────────────────
def _node_rows(nodes: Sequence[Mapping[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    """Per node: what it reports for one served task key ({model, present, loaded})."""
    out = {}
    for n in nodes or ():
        row = (n.get("tasks") or {}).get(key)
        if isinstance(row, Mapping):
            out[str(n.get("node_id") or n.get("node") or "?")] = {
                "model": row.get("model") or "", "present": bool(row.get("present")),
                "loaded": bool(row.get("loaded"))}
    return out


def callable_view(registry: Mapping[str, Mapping[str, Any]], *,
                  defaults: Optional[Mapping[str, str]] = None,
                  nodes: Sequence[Mapping[str, Any]] = (),
                  catalog: Sequence[Mapping[str, Any]] = (),
                  task: str = "", family: str = "", question: str = "") -> List[Dict[str, Any]]:
    """Every binding in the capability registry, joined with what serves it.

    registry - CAPABILITY_REGISTRY (only entries' "models" and "mcp_expose" are read)
    defaults - task -> default model (nlp_dispatch_core.DEFAULT_MODELS) for bindings with model ""
    nodes    - nlp_dispatch.discover() rows ({node_id, tasks: {task: {model, present, loaded}}})
    catalog  - specialist_catalog.CURATED entries ({family, task, model})
    """
    defaults = defaults or {}
    by_model: Dict[str, str] = {}
    for e in catalog or ():
        if e.get("model"):
            by_model.setdefault(str(e["model"]), ":".join(
                str(x) for x in (e.get("family"), e.get("task"), e.get("model")) if x))
    rows = []
    for cap, entry in sorted((registry or {}).items()):
        for b in (entry.get("models") or []) if isinstance(entry, Mapping) else []:
            if task and b.get("task") != task:
                continue
            if family and b.get("family") != family:
                continue
            if question and question not in (b.get("questions") or []):
                continue
            served = _node_rows(nodes, b.get("served_as") or b.get("task") or "")
            model = b.get("model") or defaults.get(b.get("served_as") or "", "") or ""
            rows.append({
                **{k: b.get(k) for k in ("id", "cap", "selector", "task", "family", "uses", "limits",
                                         "io", "labels", "questions", "tier", "placement",
                                         "package_id", "note")},
                "model": model,
                "nodes": served,
                "loaded_on": sorted(k for k, v in served.items() if v["loaded"]),
                "present_on": sorted(k for k, v in served.items() if v["present"]),
                # a node may serve a different model than the default (VERA_NLP_MODEL_<TASK>)
                "node_models": sorted({v["model"] for v in served.values() if v["model"]}),
                "installable": by_model.get(model, ""),
                "mcp_expose": bool(entry.get("mcp_expose", True)),
            })
    return rows


def _names(xs: Any) -> List[str]:
    out = []
    for x in xs or ():
        n = x.get("name") or x.get("model") if isinstance(x, Mapping) else x
        if n:
            out.append(str(n))
    return out


def llm_rows(instances: Mapping[str, Mapping[str, Any]], tags: Mapping[str, Sequence[str]], *,
             cap: str = "llm.generate", search: str = "") -> List[Dict[str, Any]]:
    """The LLMs the routing system can pick, in the view's row shape. `model`
    is the routing argument (llm.generate model=...); `uses` comes from the
    operator's purpose tags (ollama.model_tags.*) - an untagged model says so,
    because an LLM cannot pick what nothing describes."""
    per: Dict[str, Dict[str, Dict[str, bool]]] = {}
    for iid, inst in (instances or {}).items():
        if not isinstance(inst, Mapping):
            continue
        running = set(_names(inst.get("running")))
        for m in _names(inst.get("models")):
            per.setdefault(m, {})[str(iid)] = {"present": True, "loaded": m in running}
    rows = []
    for m in sorted(set(per) | set(tags or {})):
        if search and search.lower() not in m.lower():
            continue
        t = list((tags or {}).get(m) or [])
        nodes = {k: {"model": m, **v} for k, v in per.get(m, {}).items()}
        rows.append({
            "id": f"{cap}:{m}", "cap": cap, "selector": {"model": m}, "task": "generate",
            "family": "ollama",
            "uses": ("Purpose tags: " + ", ".join(t)) if t else
                    "untagged - set its purposes with ollama.model_tags.set so it can be picked",
            "limits": "", "io": {"in": "text", "out": "answer"}, "labels": [], "questions": [],
            "tier": 0, "placement": "any", "package_id": "", "note": "routed by the LLM routing system",
            "model": m, "nodes": nodes,
            "loaded_on": sorted(k for k, v in nodes.items() if v["loaded"]),
            "present_on": sorted(nodes), "node_models": [m], "installable": "",
            "mcp_expose": True, "tags": t,
        })
    return rows
