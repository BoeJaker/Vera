"""
bench_matrix_core.py — pure planning and scoring for a context × quantisation sweep
==================================================================================

bench.run answers "how good is this model in this role". It cannot answer the
questions that decide how a node should actually be configured:

  * how large a context window fits before a GPU node starts spilling part of
    the model to the CPU, and what that costs; and
  * which quantisation of a model earns its memory on this hardware.

Both are properties of (model, window, node), so they need a sweep. This module
plans the grid, reduces each cell's raw calls to comparable numbers, judges the
cell, and picks a recommendation. It never talks to a node — the capability does
the I/O — so the rules that produce a recommendation are unit-tested.

Two measurements shaped the rules (gpu-250, Tesla V100-PCIE-12GB):
  * changing num_ctx forces Ollama to reload the runner — ~9.9s against a ~0.85s
    warm call on 2026-09-09 — so a cell records that load separately from its
    warm speed instead of averaging it in;
  * the window sweep recorded in Vera's .env went from 105.8 tok/s at 28672 to
    33.4 tok/s at 32768 because 17% of the model moved onto the CPU — so a
    spilled cell can never be recommended, however it happens to look.

Nothing here imports anything beyond the stdlib.
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_CTXS: Tuple[int, ...] = (2048, 4096, 8192, 16384, 32768)
MIN_CTX = 256
MAX_CELLS = 40
MAX_REPEATS = 5
# A window is "as good as the best" when its generation speed is within this
# fraction of the model's fastest clean cell. Warm repeats on a live node vary by
# several percent, so a tighter bound would chase noise.
DEFAULT_TOLERANCE = 0.10
# Ollama reports size_vram a few bytes under size for fully offloaded models.
SPILL_THRESHOLD_PCT = 99.5
RECOMMENDABLE = frozenset({"ok", "throttled", "cpu_bound"})
# Encoder-only families cannot generate text; a generation sweep over them only
# produces errors, so they are left out of the variant picker.
_EMBEDDING_FAMILY_MARKERS = ("bert",)

BASE_PROMPT = ("You are a planning assistant. Enumerate, in a numbered list, the concrete "
               "steps required to migrate a monolithic web service to a modular "
               "capability-based architecture. Be specific about ordering and risk.")
_FILLER = ("Reference material follows. The service exposes capabilities over HTTP, "
           "records every call with its arguments and outcome, and routes model work "
           "to the least busy node that can hold the model. ")
# Deliberately low, matching Vera's router, so a filled prompt errs short of the
# window rather than overflowing it and being truncated by Ollama.
CHARS_PER_TOKEN = 3.4
MAX_FILL = 0.9
# Room left for the chat template and the nonce line on top of the output budget.
_TEMPLATE_TOKENS = 64


def cell_prompt(num_ctx: int, fill: float, num_predict: int, nonce: str,
                chars_per_token: float = CHARS_PER_TOKEN) -> str:
    """The prompt for one call.

    A unique nonce LEADS the prompt: Ollama reuses its KV cache for a repeated
    prompt prefix, so identical warm calls would report almost no prefill time
    and the prefill figure would measure the cache, not the node. With fill > 0
    the prompt is padded to roughly that share of the window, which is how long-
    context prefill and generation-at-depth get measured; room for the output is
    always left."""
    head = "[bench %s]\n" % nonce
    fill = min(max(float(fill or 0.0), 0.0), MAX_FILL)
    if fill <= 0:
        return head + BASE_PROMPT
    budget_tokens = int(int(num_ctx) * fill) - int(num_predict) - _TEMPLATE_TOKENS
    want_chars = int(budget_tokens * chars_per_token) - len(head) - len(BASE_PROMPT) - 2
    if want_chars <= 0:
        return head + BASE_PROMPT
    padding = (_FILLER * (want_chars // len(_FILLER) + 1))[:want_chars]
    return head + padding + "\n\n" + BASE_PROMPT


def normalise_models(models: Any) -> List[str]:
    """List or comma-separated string -> ordered, de-duplicated model tags."""
    if models is None:
        return []
    items = models.split(",") if isinstance(models, str) else list(models)
    out: List[str] = []
    seen = set()
    for m in items:
        s = str(m or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def normalise_ctxs(ctxs: Any = None, ceiling: Optional[int] = None) -> List[int]:
    """Context windows to sweep, ascending and unique. Values below MIN_CTX, above
    `ceiling`, or not integers are dropped. Nothing given -> DEFAULT_CTXS."""
    if ctxs is None or ctxs == "" or ctxs == []:
        items: Sequence[Any] = DEFAULT_CTXS
    else:
        items = ctxs.split(",") if isinstance(ctxs, str) else list(ctxs)
    vals = set()
    for v in items:
        try:
            n = int(str(v).strip())
        except Exception:
            continue
        if n < MIN_CTX or (ceiling and n > int(ceiling)):
            continue
        vals.add(n)
    return sorted(vals)


def build_grid(models: Sequence[str], ctxs: Sequence[int],
               max_cells: int = MAX_CELLS) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Cells ordered model-then-ascending-window, so each model is loaded in one
    run of consecutive cells. Over `max_cells`, whole trailing models are dropped
    rather than leaving any model half-swept; they are returned so the caller can
    say so. (If even one model's windows exceed the cap, that model keeps its
    smallest `max_cells` windows.)"""
    ms, cs = list(models), list(ctxs)
    if not ms or not cs:
        return [], []
    max_cells = max(1, int(max_cells))
    per_model = len(cs)
    if len(ms) * per_model <= max_cells:
        keep_m, keep_c = ms, cs
    elif per_model <= max_cells:
        keep_m, keep_c = ms[: max_cells // per_model], cs
    else:
        keep_m, keep_c = ms[:1], cs[:max_cells]
    cells = [{"model": m, "num_ctx": c} for m in keep_m for c in keep_c]
    return cells, ms[len(keep_m):]


def _median(xs: List[Optional[float]]) -> Optional[float]:
    ys = [x for x in xs if x is not None]
    return round(statistics.median(ys), 1) if ys else None


def summarise_runs(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One cell's calls -> comparable numbers. The first call carries the load for
    this window (a cold load, or the reload a window change forces); speed and
    time-to-first-token come from the warm calls after it."""
    rs = [r for r in (runs or []) if isinstance(r, dict)]
    if not rs:
        return {}
    warm = rs[1:] or rs
    gen = [r.get("gen_tps") for r in warm if r.get("gen_tps")]
    pre = [r.get("prompt_tps") for r in warm if r.get("prompt_tps")]
    ttft = [r.get("ttft_ms") for r in warm if r.get("ttft_ms") is not None]
    med = _median(gen)
    return {
        "calls": len(rs),
        "load_ms": rs[0].get("load_ms"),
        "gen_tps": med,
        "gen_tps_min": min(gen) if gen else None,
        "gen_tps_max": max(gen) if gen else None,
        "gen_spread_pct": (round(100.0 * (max(gen) - min(gen)) / med, 1)
                           if gen and med else None),
        "prompt_tps": _median(pre),
        "ttft_ms": _median(ttft),
        "gen_tokens": warm[-1].get("gen_tokens"),
    }


def _tag(name: Any) -> str:
    n = str(name or "").strip()
    return n if ":" in n.rsplit("/", 1)[-1] else n + ":latest"


def residency(ps_models: List[Dict[str, Any]], model: str) -> Dict[str, Any]:
    """Where `model` lives right now, from a node's /api/ps list. {} when it is
    not loaded."""
    want = _tag(model)
    for m in ps_models or []:
        if _tag(m.get("name") or m.get("model")) != want:
            continue
        size = m.get("size") or 0
        vram = m.get("size_vram") or 0
        return {"size_gb": round(size / 1e9, 2), "vram_gb": round(vram / 1e9, 2),
                "resident_pct": round(100.0 * vram / size, 1) if size else None,
                "context_length": m.get("context_length")}
    return {}


def cell_verdict(cell: Dict[str, Any], has_gpu: bool) -> Tuple[str, str]:
    """(status, note) for one finished cell."""
    if cell.get("error"):
        return "error", str(cell["error"])[:200]
    res = cell.get("resident_pct")
    if has_gpu and res is not None and res < SPILL_THRESHOLD_PCT:
        return "spill", ("%.0f%% of the model is on the GPU; the rest runs on the CPU"
                         % res)
    if not cell.get("gen_tps"):
        return "error", "no tokens were generated"
    trace = cell.get("trace") or {}
    reasons = trace.get("throttle_reasons") or []
    if reasons:
        return "throttled", "clock throttled during this cell (%s)" % ", ".join(reasons)
    peak = (trace.get("cpu_pct") or {}).get("peak")
    if not has_gpu and peak is not None and peak >= 95.0:
        return "cpu_bound", "the node's CPUs were saturated (%.0f%% peak)" % peak
    return "ok", ""


def recommend(cells: List[Dict[str, Any]],
              tolerance: float = DEFAULT_TOLERANCE) -> List[Dict[str, Any]]:
    """Per model: the LARGEST window whose generation speed stays within
    `tolerance` of that model's fastest clean cell. A bigger window is free
    capacity as long as it costs no speed; a spilled or failed cell never counts."""
    by_model: Dict[str, List[Dict[str, Any]]] = {}
    for c in cells or []:
        by_model.setdefault(c.get("model", ""), []).append(c)
    out: List[Dict[str, Any]] = []
    for model, cs in by_model.items():
        valid = [c for c in cs if c.get("status") in RECOMMENDABLE and c.get("gen_tps")]
        spilled = [c["num_ctx"] for c in cs if c.get("status") == "spill"]
        first_spill = min(spilled) if spilled else None
        if not valid:
            out.append({"model": model, "num_ctx": None, "first_spill_ctx": first_spill,
                        "reason": "No tested window ran cleanly on this node."})
            continue
        best = max(c["gen_tps"] for c in valid)
        floor = best * (1.0 - float(tolerance))
        pick = max((c for c in valid if c["gen_tps"] >= floor),
                   key=lambda c: (c.get("effective_ctx") or c["num_ctx"], c["gen_tps"]))
        eff = pick.get("effective_ctx") or pick["num_ctx"]
        reason = ("Largest window within %d%% of this model's best speed (%.1f tok/s)."
                  % (round(float(tolerance) * 100), best))
        if first_spill and first_spill > pick["num_ctx"]:
            reason += " The next tested window (%d) spills onto the CPU." % first_spill
        out.append({"model": model, "num_ctx": eff, "requested_ctx": pick["num_ctx"],
                    "gen_tps": pick["gen_tps"], "prompt_tps": pick.get("prompt_tps"),
                    "best_gen_tps": best, "first_spill_ctx": first_spill,
                    "quant": pick.get("quant", ""), "size_gb": pick.get("size_gb"),
                    "reason": reason})
    return out


def group_variants(tag_models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Installed models from /api/tags grouped by (family, parameter size), so the
    quantisations of one model can be swept side by side. Tags that point at the
    same blob (`mistral:7b` and `mistral:latest`) are ONE variant with aliases —
    listed separately they invite sweeping identical weights twice. Groups with
    the most variants come first; variants ascend by size."""
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    by_digest: Dict[str, Dict[str, Any]] = {}
    for m in tag_models or []:
        name = m.get("name") or m.get("model")
        if not name:
            continue
        det = m.get("details") or {}
        family = str(det.get("family") or "")
        if any(k in family.lower() for k in _EMBEDDING_FAMILY_MARKERS):
            continue
        digest = str(m.get("digest") or "")
        if digest and digest in by_digest:
            by_digest[digest]["aliases"].append(name)
            continue
        params = str(det.get("parameter_size") or "")
        g = groups.setdefault((family, params), {"family": family, "params": params,
                                                 "variants": []})
        variant = {"name": name, "aliases": [],
                   "quant": det.get("quantization_level", ""),
                   "size_gb": round((m.get("size") or 0) / 1e9, 2)}
        g["variants"].append(variant)
        if digest:
            by_digest[digest] = variant
    for g in groups.values():
        g["variants"].sort(key=lambda v: (v["size_gb"], v["name"]))
    return sorted(groups.values(),
                  key=lambda g: (-len(g["variants"]), g["family"], g["params"]))


def apply_plan(recommendations: List[Dict[str, Any]], instance_id: str,
               learned: Optional[Dict[str, int]] = None,
               only: Any = None) -> List[Dict[str, Any]]:
    """What adopting a sweep's recommended windows would change.

    Vera keeps a learned-safe window per (node, model) and prefers it over its
    own pre-load estimate; the estimate is only consulted when no learned value
    exists. So adopting a measured window does two things: it replaces a guess
    with a measurement, and it takes the estimator out of the path for that pair.
    Pure: the caller performs the write."""
    learned = learned or {}
    wanted = {str(m) for m in (only or [])} or None
    out: List[Dict[str, Any]] = []
    for rec in recommendations or []:
        model = rec.get("model")
        if not model or (wanted and model not in wanted):
            continue
        ctx = rec.get("num_ctx")
        previous = learned.get("%s::%s" % (instance_id, model))
        row = {"instance_id": instance_id, "model": model, "num_ctx": ctx,
               "previous": previous, "gen_tps": rec.get("gen_tps")}
        if not ctx:
            row.update(action="skip",
                       reason="the sweep found no window that ran cleanly on this node")
        elif previous == ctx:
            row.update(action="skip", reason="already the learned window")
        else:
            row.update(action="set",
                       reason="measured on this node at %s tok/s" % (rec.get("gen_tps") or "?"))
        out.append(row)
    return out
