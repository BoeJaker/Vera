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


def selector_values(serves: Any, key: str = "task") -> List[str]:
    """The values of one selector argument across a capability's bindings, in
    declared order - what the capability accepts for it (nlp.ner checks `task`
    against this, so what it declares and what it accepts cannot drift)."""
    raw = [serves] if isinstance(serves, Mapping) else list(serves or [])
    out = []
    for b in raw:
        v = (b.get("selector") or {}).get(key) if isinstance(b, Mapping) else None
        if v is not None and str(v) not in out:
            out.append(str(v))
    return out


def _first_sentence(text: str, limit: int = 90) -> str:
    t = str(text or "").strip()
    cut = re.split(r"(?<!e\.g)(?<!i\.e)(?<!etc)\.\s", t)[0].rstrip(".")
    return cut if len(cut) <= limit else cut[:limit - 1].rstrip() + "…"


def selector_schema(bindings: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Schema fragments for the selector arguments: an enum of the values and a
    description of what each picks - so the tool form, the LLM-facing
    signature and the check come from the bindings."""
    keys: List[str] = []
    for b in bindings or ():
        for k in (b.get("selector") or {}):
            if k not in keys:
                keys.append(k)
    out = {}
    for k in keys:
        vals, parts = [], []
        for b in bindings:
            v = (b.get("selector") or {}).get(k)
            if v is None or v in vals:
                continue
            vals.append(v)
            parts.append(f"{v} - {_first_sentence(b.get('uses', ''))}")
        out[k] = {"enum": vals, "description": "Picks the model: " + "; ".join(parts) + "."}
    return out


# ── LLMs: uses and limits from facts (the operator's tags refine them) ───────
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*([bm])\b", re.I)
_ACTIVE = re.compile(r"\ba(\d+(?:\.\d+)?)b\b", re.I)          # 35b-a3b: 3B active (MoE)


def _billions(text: str) -> Optional[float]:
    m = _SIZE.search(str(text or ""))
    if not m:
        return None
    n = float(m.group(1))
    return n / 1000 if m.group(2).lower() == "m" else n


def show_facts(show: Mapping[str, Any]) -> Dict[str, Any]:
    """The facts an Ollama /api/show reply gives about a model."""
    det = show.get("details") or {}
    info = show.get("model_info") or {}
    ctx = None
    arch = info.get("general.architecture", "")
    if arch and isinstance(info.get(f"{arch}.context_length"), (int, float)):
        ctx = int(info[f"{arch}.context_length"])
    else:
        for k, v in info.items():
            if str(k).endswith(".context_length") and isinstance(v, (int, float)):
                ctx = int(v)
                break
    return {"family": str(det.get("family") or arch or ""),
            "params": str(det.get("parameter_size") or ""),
            "quant": str(det.get("quantization_level") or ""),
            "ctx": ctx, "abilities": sorted(str(c) for c in (show.get("capabilities") or []))}


def llm_describe(model: str, facts: Optional[Mapping[str, Any]] = None,
                 tags: Sequence[str] = (), speeds: Optional[Mapping[str, float]] = None) -> Tuple[str, str]:
    """(uses, limits) for one LLM, in sentences a planner can act on.

    facts  - show_facts(...) (may be empty: then only the name and speed speak)
    tags   - the operator's purpose tags (planning, code, chat, fast, vision ...)
    speeds - {node: measured tokens/s} from the router's rolling stats
    """
    f = dict(facts or {})
    ab = set(f.get("abilities") or [])
    name = str(model or "")
    low = name.lower()
    embed_only = ("embedding" in ab and "completion" not in ab) or (not ab and "embed" in low)
    size = _billions(f.get("params")) or _billions(name)
    active = _ACTIVE.search(name)
    uses, limits = [], []
    if tags:
        uses.append("Tagged for: " + ", ".join(tags) + ".")
    if embed_only:
        uses.append("Text embeddings.")
        limits.append("cannot generate text - use the embedding path, not llm.generate.")
    else:
        what = []
        if "vision" in ab or "-vl" in low or "vision" in low:
            what.append("reads images")
        if "tools" in ab:
            what.append("native tool calling")
        if "thinking" in ab:
            what.append("a thinking mode")
        if "coder" in low or "code" in low:
            what.append("code")
        kind = (f"{f.get('family')} " if f.get("family") else "") + \
               (f.get("params") or (f"{size:g}B" if size else "")) + \
               (f" ({f.get('quant')})" if f.get("quant") else "")
        desc = (kind.strip() or "General") + " LLM"
        if active:
            desc += f", mixture-of-experts with {active.group(1)}B active (fast for its size)"
        uses.append(desc + (": " + ", ".join(what) if what else "") + ".")
        if f.get("ctx"):
            uses.append(f"Context window {int(f['ctx']) // 1024}k tokens.")
        if size is not None and size < 4 and not active:
            limits.append("small - weak at multi-step reasoning and long instructions.")
        if ab and "tools" not in ab:
            limits.append("no native tool calling.")
        if f.get("ctx") and int(f["ctx"]) < 16384:
            limits.append("short context.")
    sp = {k: float(v) for k, v in (speeds or {}).items() if v}
    if sp:
        best = max(sp, key=sp.get)
        uses.append(f"Measured {sp[best]:.0f} tok/s on {best}" +
                    (f" ({', '.join(f'{k} {v:.0f}' for k, v in sorted(sp.items()) if k != best)})"
                     if len(sp) > 1 else "") + ".")
        if sp[best] < 5:
            limits.append(f"slow here (best {sp[best]:.1f} tok/s) - not for interactive turns.")
    elif not tags and not f:
        uses.append("No facts or tags yet - describe it with ollama.model_tags.set.")
    return " ".join(uses), " ".join(limits)


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
             cap: str = "llm.generate", search: str = "",
             facts: Optional[Mapping[str, Mapping[str, Any]]] = None,
             stats: Optional[Iterable[Mapping[str, Any]]] = None) -> List[Dict[str, Any]]:
    """The LLMs the routing system can pick, in the view's row shape. `model`
    is the routing argument (llm.generate model=...). `uses` / `limits` are
    written from the model's facts (/api/show: size, quantisation, context,
    abilities), its measured speed per node (the router's rolling stats) and
    the operator's purpose tags (ollama.model_tags.*), which lead when set.

    facts - {model: show_facts(...)}; stats - router stats rows
            ({model, instance, ema_tps, n})"""
    speeds: Dict[str, Dict[str, float]] = {}
    for st in stats or ():
        m, iid, t = st.get("model"), st.get("instance"), st.get("ema_tps")
        if m and iid and t:
            cur = speeds.setdefault(str(m), {})
            cur[str(iid)] = max(cur.get(str(iid), 0.0), float(t))
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
        f = dict((facts or {}).get(m) or {})
        uses, limits = llm_describe(m, f, t, speeds.get(m))
        embed_only = "cannot generate text" in limits
        rows.append({
            "id": f"{cap}:{m}", "cap": cap, "selector": {"model": m},
            "task": "embed" if embed_only else "generate",
            "family": "ollama", "uses": uses, "limits": limits,
            "io": {"in": "text", "out": "vector" if embed_only else "answer"},
            "labels": [], "questions": [],
            "tier": 0, "placement": "any", "package_id": "", "note": "routed by the LLM routing system",
            "model": m, "nodes": nodes,
            "loaded_on": sorted(k for k, v in nodes.items() if v["loaded"]),
            "present_on": sorted(nodes), "node_models": [m], "installable": "",
            "mcp_expose": True, "tags": t, "facts": f, "tps": speeds.get(m, {}),
        })
    return rows
